"""Score statistics processing.

Python port of the processors in ppy/osu-queue-score-statistics. Two kinds of statistics exist:

* **Additive counters** (play count, play time, total score, total hits, max combo) are applied exactly
  once per score. ``ScoreProcessHistory`` is the guard: a score with a history row is never applied again.
* **Derived values** (ranked score, grade counts, total pp, accuracy, level, ranks) are always recomputed
  from the ``scores`` table using each user's *best score per beatmap*. Because they are recomputed, they
  cannot drift and no "revert previous best" bookkeeping is needed.

``rebuild_user_statistics`` recomputes both kinds from scratch.
"""

import json
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapStatus
from app.models.legacy import ScoreProcessHistory
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics
from app.services.leaderboard import Leaderboard
from app.services.leaderboard import LeaderboardEntry
from app.services.rank_history import record_rank

logger = logging.getLogger(__name__)

# Beatmap statuses whose scores count towards ranked score and grade (SS/S/A) counts.
RANKED_COUNT_STATUSES = frozenset({BeatmapStatus.RANKED, BeatmapStatus.APPROVED, BeatmapStatus.LOVED})

# Beatmap statuses that award performance points.
PERFORMANCE_STATUSES = frozenset({BeatmapStatus.RANKED, BeatmapStatus.APPROVED})

# Automation mods never count towards the max combo statistic (autoplay, cinema, relax, autopilot, spun out).
_AUTOMATION_MODS = frozenset({"AT", "CN", "RX", "AP", "SO"})

# Default speed changes for rate adjusting mods that carry no explicit `speed_change` setting.
_DEFAULT_CLOCK_RATES = {"DT": 1.5, "NC": 1.5, "HT": 0.75, "DC": 0.75}

# Judgements that are not part of the scorable object count (used to decide if a failed play was meaningful).
_NON_SCORABLE_RESULTS = frozenset({"combo_break", "small_bonus", "large_bonus", "legacy_combo_increase"})

_GRADE_FIELDS = {
    "X": "grade_ss",
    "SS": "grade_ss",
    "XH": "grade_ssh",
    "SSH": "grade_ssh",
    "S": "grade_s",
    "SH": "grade_sh",
    "A": "grade_a",
}

_PP_WEIGHT = 0.95
# Legacy-compatible bonus pp: 416.667 * (1 - 0.995^n) where n is the number of (deduplicated) scores.
_BONUS_PP_MAX = 417.0 - 1.0 / 3.0
_BONUS_PP_DECAY = 0.995


@dataclass(frozen=True, slots=True)
class ScoreRow:
    """The columns of a score needed to derive statistics."""

    id: int
    beatmap_id: int
    total_score: int
    rank: str
    pp: float | None
    accuracy: float
    status: BeatmapStatus


@dataclass(frozen=True, slots=True)
class ProcessedScore:
    """Result of successfully processing a score."""

    score_id: int
    user_id: int
    mode: GameMode
    # To be applied to the leaderboard index *after* the transaction committed.
    leaderboard_entry: LeaderboardEntry | None = None


def normalize_accuracy(value: float) -> float:
    """Return accuracy as a fraction in [0, 1]; clients may send either a fraction or a percentage."""
    return value / 100.0 if value > 1.0 else value


def calculate_total_performance(rows: Iterable[ScoreRow]) -> tuple[float, float]:
    """Calculate (total pp, weighted accuracy percentage) from a user's scores.

    Only the best pp score of each beatmap is used. Mirrors ``UserTotalPerformanceAggregateHelper``.
    """
    best: dict[int, ScoreRow] = {}
    for row in rows:
        if row.pp is None:
            continue
        current = best.get(row.beatmap_id)
        if current is None or row.pp > (current.pp or 0.0):
            best[row.beatmap_id] = row

    ordered = sorted(best.values(), key=lambda r: r.pp or 0.0, reverse=True)

    total_pp = 0.0
    total_accuracy = 0.0
    factor = 1.0
    for row in ordered:
        total_pp += (row.pp or 0.0) * factor
        total_accuracy += normalize_accuracy(row.accuracy) * factor
        factor *= _PP_WEIGHT

    if not ordered:
        return 0.0, 100.0

    total_pp += _BONUS_PP_MAX * (1.0 - _BONUS_PP_DECAY ** len(ordered))
    total_accuracy *= 100.0 / (20.0 * (1.0 - _PP_WEIGHT ** len(ordered)))

    if not math.isfinite(total_pp) or total_pp < 0:
        raise ValueError(f"Invalid total pp calculated: {total_pp}")

    return total_pp, min(max(total_accuracy, 0.0), 100.0)


def pick_best_by_score(rows: Iterable[ScoreRow]) -> list[ScoreRow]:
    """Return each beatmap's highest total score (newest wins ties), like ``GetUserBestScoreFor``."""
    best: dict[int, ScoreRow] = {}
    for row in rows:
        current = best.get(row.beatmap_id)
        if current is None or (row.total_score, row.id) > (current.total_score, current.id):
            best[row.beatmap_id] = row
    return list(best.values())


def score_required_for_level(level: int) -> int:
    """Total score needed to reach ``level`` (osu!stable formula)."""
    if level <= 1:
        return 0
    if level <= 100:
        return math.floor(
            5000 / 3 * (4 * level**3 - 3 * level**2 - level) + math.floor(1.25 * 1.8 ** (level - 60)),
        )
    return 26931190829 + 100000000000 * (level - 100)


def level_from_total_score(total_score: int) -> tuple[int, int]:
    """Return (level, progress percentage towards the next level)."""
    level = 1
    while score_required_for_level(level + 1) <= total_score:
        level += 1

    floor = score_required_for_level(level)
    span = score_required_for_level(level + 1) - floor
    progress = int(100 * (total_score - floor) / span) if span > 0 else 0
    return level, min(max(progress, 0), 99)


def clock_rate_from_mods(mods: list[dict[str, Any]]) -> float:
    """Return the playback rate implied by rate adjusting mods (1.0 if none)."""
    for mod in mods:
        acronym = str(mod.get("acronym", "")).upper()
        if acronym not in _DEFAULT_CLOCK_RATES:
            continue

        settings = mod.get("settings")
        speed_change = settings.get("speed_change") if isinstance(settings, dict) else None
        if isinstance(speed_change, int | float) and math.isfinite(speed_change) and speed_change > 0:
            return float(speed_change)
        return _DEFAULT_CLOCK_RATES[acronym]

    return 1.0


def play_length_seconds(
    beatmap_length: int,
    mods: list[dict[str, Any]],
    started_at: datetime | None,
    ended_at: datetime | None,
) -> int:
    """Seconds the play lasted, bounded by the (rate adjusted) beatmap length."""
    real_time: float | None = None
    if started_at is not None and ended_at is not None:
        try:
            real_time = max((ended_at - started_at).total_seconds(), 0.0)
        except TypeError:  # naive/aware mix; fall back to the beatmap length
            real_time = None

    if beatmap_length <= 0:
        return int(real_time or 0)

    length = beatmap_length / clock_rate_from_mods(mods)
    return int(length if real_time is None else min(length, real_time))


def count_scorable_hits(statistics: dict[str, int]) -> int:
    """Number of judged objects in a statistics dict (used for the failed-play validity check)."""
    return sum(
        count
        for key, count in statistics.items()
        if isinstance(count, int) and not key.startswith("ignore") and key not in _NON_SCORABLE_RESULTS
    )


def count_hits(statistics: dict[str, int], ruleset_id: int) -> int:
    """Hits that count towards the profile's total hits (300/100/50), not misses."""
    catch = ruleset_id == 2
    hit_results = {"great", "perfect", "ok", "good", "meh"}
    if catch:
        hit_results |= {"large_tick_hit", "small_tick_hit"}

    return sum(
        max(count, 0) for key, count in statistics.items() if key in hit_results and isinstance(count, int)
    )


def is_valid_for_play_tracking(
    passed: bool,
    length: int,
    total_score: int,
    statistics: dict[str, int],
    maximum_statistics: dict[str, int],
) -> bool:
    """Whether a play counts towards play count / play time. Failed plays must be substantial."""
    if passed:
        return True

    judged = count_scorable_hits(statistics)
    total_objects = count_scorable_hits(maximum_statistics)
    return length >= 8 and total_score >= 5000 and judged >= min(0.1 * total_objects, 20)


def _load_score_data(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def apply_score_counters(stats: UserStatistics, score: Score, beatmap: Beatmap) -> None:
    """Apply the additive counters of a single score. Must be called at most once per score."""
    data = _load_score_data(score.data)
    mods = [m for m in data.get("mods", []) if isinstance(m, dict)]
    statistics = data.get("statistics") if isinstance(data.get("statistics"), dict) else {}
    maximum_statistics = data.get("maximum_statistics")
    if not isinstance(maximum_statistics, dict):
        maximum_statistics = {}

    length = play_length_seconds(beatmap.total_length, mods, score.started_at, score.ended_at)

    if is_valid_for_play_tracking(score.passed, length, score.total_score, statistics, maximum_statistics):
        stats.play_count += 1
        stats.play_time += length

    # Total score: always for ranked/approved maps, otherwise only if plausible for the play's length.
    if beatmap.status in PERFORMANCE_STATUSES or score.total_score <= length * 500_000:
        stats.total_score += score.total_score
        stats.level, stats.level_progress = level_from_total_score(stats.total_score)

    stats.total_hits += count_hits(statistics, score.ruleset_id)

    acronyms = {str(m.get("acronym", "")).upper() for m in mods}
    if (
        score.passed
        and beatmap.status >= BeatmapStatus.RANKED
        and not (acronyms & _AUTOMATION_MODS)
        and score.max_combo > stats.maximum_combo
    ):
        stats.maximum_combo = score.max_combo


async def _get_statistics_for_update(db: AsyncSession, user_id: int, mode: GameMode) -> UserStatistics:
    """Fetch (creating if needed) the statistics row, locked so workers serialise per user/mode."""
    result = await db.execute(
        select(UserStatistics)
        .where(UserStatistics.user_id == user_id, UserStatistics.mode == mode)
        .with_for_update(),
    )
    stats = result.scalars().first()
    if stats is None:
        stats = UserStatistics(user_id=user_id, mode=mode)
        db.add(stats)
        await db.flush()
    return stats


async def recompute_derived_statistics(
    db: AsyncSession,
    stats: UserStatistics,
    user: User,
    leaderboard: Leaderboard | None = None,
) -> LeaderboardEntry:
    """Recompute ranked score, grade counts, pp, accuracy and ranks from the user's best scores.

    Ranks come from ``leaderboard`` when given and usable, otherwise from the database. Returns the entry
    to apply to the leaderboard once the caller's transaction has committed.
    """
    result = await db.execute(
        select(
            Score.id,
            Score.beatmap_id,
            Score.total_score,
            Score.rank,
            Score.pp,
            Score.accuracy,
            Beatmap.status,
        )
        .join(Beatmap, Beatmap.id == Score.beatmap_id)
        .where(
            Score.user_id == stats.user_id,
            Score.ruleset_id == int(stats.mode),
            Score.passed.is_(True),
            Score.preserve.is_(True),
            Score.ranked.is_(True),
        ),
    )
    rows = [ScoreRow(*row) for row in result.all()]

    counted = pick_best_by_score(r for r in rows if r.status in RANKED_COUNT_STATUSES)
    stats.ranked_score = sum(r.total_score for r in counted)

    for field in set(_GRADE_FIELDS.values()):
        setattr(stats, field, 0)
    for row in counted:
        field = _GRADE_FIELDS.get(row.rank.strip().upper())
        if field is not None:
            setattr(stats, field, getattr(stats, field) + 1)

    # Restricted users keep their last known pp and are not ranked.
    if user.is_restricted:
        return LeaderboardEntry(stats.mode, user.id, user.country_acronym, stats.pp, ranked=False)

    total_pp, accuracy = calculate_total_performance(r for r in rows if r.status in PERFORMANCE_STATUSES)
    stats.pp = round(total_pp, 5)
    stats.accuracy = round(accuracy, 5)
    stats.hit_accuracy = stats.accuracy

    await _refresh_ranks(db, stats, user, leaderboard)
    if not user.is_bot and stats.pp > 0 and stats.global_rank is not None:
        await record_rank(db, user.id, stats.mode, stats.global_rank)
    return LeaderboardEntry(stats.mode, user.id, user.country_acronym, stats.pp, ranked=not user.is_bot)


async def _refresh_ranks(
    db: AsyncSession,
    stats: UserStatistics,
    user: User,
    leaderboard: Leaderboard | None,
) -> None:
    if leaderboard is not None:
        try:
            stats.global_rank, stats.country_rank = await leaderboard.ranks(
                stats.mode,
                stats.pp,
                user.country_acronym,
            )
            return
        except Exception as exc:  # Redis down or index not built yet: the database is always correct.
            logger.warning("Leaderboard unavailable (%s); counting ranks in the database", exc)

    visible = (UserStatistics.mode == stats.mode, UserStatistics.pp > stats.pp)

    global_count = await db.execute(
        select(func.count(UserStatistics.id))
        .join(User, User.id == UserStatistics.user_id)
        .where(*visible, User.is_restricted.is_(False), User.is_bot.is_(False)),
    )
    stats.global_rank = int(global_count.scalar_one()) + 1

    country_count = await db.execute(
        select(func.count(UserStatistics.id))
        .join(User, User.id == UserStatistics.user_id)
        .where(
            *visible,
            User.country_acronym == user.country_acronym,
            User.is_restricted.is_(False),
            User.is_bot.is_(False),
        ),
    )
    stats.country_rank = int(country_count.scalar_one()) + 1


async def process_score(
    db: AsyncSession,
    score_id: int,
    leaderboard: Leaderboard | None = None,
) -> ProcessedScore | None:
    """Process one score. Returns ``None`` if it was missing or already processed.

    Does not commit; the caller owns the transaction. A concurrent worker processing the same score
    makes the history insert fail with ``IntegrityError``, which the caller should treat as "already done".
    """
    row = (
        await db.execute(
            select(Score, Beatmap).join(Beatmap, Beatmap.id == Score.beatmap_id).where(Score.id == score_id),
        )
    ).first()
    if row is None:
        logger.warning("Score %s not found for statistics processing", score_id)
        return None
    score, beatmap = row

    try:
        mode = GameMode(score.ruleset_id)
    except ValueError:
        logger.warning("Score %s has unsupported ruleset %s; marking processed", score.id, score.ruleset_id)
        db.add(ScoreProcessHistory(score_id=score.id))
        await db.flush()
        return None

    stats = await _get_statistics_for_update(db, score.user_id, mode)

    already_processed = await db.execute(
        select(ScoreProcessHistory.id).where(ScoreProcessHistory.score_id == score.id),
    )
    if already_processed.first() is not None:
        return None

    user = await db.get(User, score.user_id)
    if user is None:
        logger.warning("User %s of score %s no longer exists", score.user_id, score.id)
        return None

    db.add(ScoreProcessHistory(score_id=score.id))
    apply_score_counters(stats, score, beatmap)
    await db.flush()
    entry = await recompute_derived_statistics(db, stats, user, leaderboard)
    await db.flush()

    return ProcessedScore(score_id=score.id, user_id=score.user_id, mode=mode, leaderboard_entry=entry)


async def rebuild_user_statistics(db: AsyncSession, user_id: int, mode: GameMode) -> int:
    """Recompute every statistic of a user/mode from its scores and mark all those scores processed.

    Returns the number of scores that were replayed. Does not commit.
    """
    user = await db.get(User, user_id)
    if user is None:
        return 0

    stats = await _get_statistics_for_update(db, user_id, mode)
    stats.play_count = 0
    stats.play_time = 0
    stats.total_score = 0
    stats.total_hits = 0
    stats.maximum_combo = 0
    stats.level = 1
    stats.level_progress = 0

    result = await db.execute(
        select(Score, Beatmap)
        .join(Beatmap, Beatmap.id == Score.beatmap_id)
        .where(Score.user_id == user_id, Score.ruleset_id == int(mode))
        .order_by(Score.id),
    )
    pairs = result.all()
    for score, beatmap in pairs:
        apply_score_counters(stats, score, beatmap)

    score_ids = [score.id for score, _ in pairs]
    if score_ids:
        known = await db.execute(
            select(ScoreProcessHistory.score_id).where(ScoreProcessHistory.score_id.in_(score_ids)),
        )
        known_ids = {row[0] for row in known.all()}
        db.add_all(ScoreProcessHistory(score_id=sid) for sid in score_ids if sid not in known_ids)

    await db.flush()
    await recompute_derived_statistics(db, stats, user)
    await db.flush()
    return len(pairs)
