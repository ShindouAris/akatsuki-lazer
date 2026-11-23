"""Medals, awarded while a score's statistics are processed.

Port of the medal awarders of ppy/osu-queue-score-statistics (``MedalProcessor`` and its awarders): after a
score updated the player's statistics, every medal the player does not own yet and that applies to the play's
ruleset is checked against the play and the fresh statistics.

Differences from the reference, on purpose:

* Only plays on the **vanilla** board earn medals. Relax and Autopilot plays never do, so a medal always means
  the same thing (the automation mods already disqualify the combo medals there).
* Star rating medals use the beatmap's own star rating and only count plays without a mod that changes it.
* The catalogue lives in this module (``CATALOGUE``) and is copied into the ``medals`` table on first use, so no
  separate data import is needed. Medal ids match osu-web's where the medal exists there.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapStatus
from app.models.medal import Medal
from app.models.medal import UserMedal
from app.models.score import Score
from app.models.user import User
from app.models.user import UserStatistics
from app.services.variant import Variant

logger = logging.getLogger(__name__)

# Mods that change a play's star rating or make it easier: such plays do not earn star rating medals.
_STAR_CHANGING_MODS = frozenset({"EZ", "HR", "DT", "NC", "HT", "DC", "NF", "FL", "DA", "TP", "CL", "MR"})
_AUTOMATION_MODS = frozenset({"AT", "CN", "RX", "AP", "SO"})


@dataclass(frozen=True, slots=True)
class MedalContext:
    """Everything a rule may look at."""

    score: Score
    beatmap: Beatmap
    stats: UserStatistics
    mods: frozenset[str]

    @property
    def mode(self) -> int:
        return self.score.ruleset_id


@dataclass(frozen=True, slots=True)
class MedalRule:
    """A medal and the condition that earns it."""

    id: int
    name: str
    description: str
    grouping: str
    mode: int | None
    check: Callable[[MedalContext], bool]
    needs_pass: bool = True

    @property
    def slug(self) -> str:
        return "-".join(self.name.lower().replace("!", "").replace("★", "star").split())


# --- conditions -----------------------------------------------------------------------------------------


def _combo(threshold: int) -> Callable[[MedalContext], bool]:
    return lambda c: not (c.mods & _AUTOMATION_MODS) and c.score.max_combo >= threshold


def _play_count(threshold: int) -> Callable[[MedalContext], bool]:
    return lambda c: c.stats.play_count >= threshold


def _total_hits(threshold: int) -> Callable[[MedalContext], bool]:
    return lambda c: c.stats.total_hits >= threshold


def _global_rank(threshold: int) -> Callable[[MedalContext], bool]:
    return lambda c: c.stats.global_rank is not None and 0 < c.stats.global_rank <= threshold


def _star_rating(stars: int, full_combo: bool) -> Callable[[MedalContext], bool]:
    def check(c: MedalContext) -> bool:
        if c.mods & (_STAR_CHANGING_MODS | _AUTOMATION_MODS):
            return False
        if c.beatmap.status not in (BeatmapStatus.RANKED, BeatmapStatus.APPROVED):
            return False
        if not stars <= c.beatmap.difficulty_rating < stars + 1:
            return False
        if full_combo:
            return bool(c.beatmap.max_combo) and c.score.max_combo >= c.beatmap.max_combo
        return True

    return check


def _build_catalogue() -> list[MedalRule]:
    rules: list[MedalRule] = []

    for medal_id, threshold in ((1, 500), (3, 750), (4, 1000), (5, 2000)):
        rules.append(
            MedalRule(medal_id, f"{threshold} Combo", f"Reach a {threshold} combo in a ranked play.",
                      "Skill", None, _combo(threshold)),
        )

    for medal_id, threshold in ((20, 5_000), (21, 15_000), (22, 25_000), (28, 50_000)):
        rules.append(
            MedalRule(medal_id, f"{threshold:,} Plays", f"Play {threshold:,} times.", "Dedication", None,
                      _play_count(threshold), needs_pass=False),
        )

    for medal_id, threshold in ((31, 30_000), (32, 300_000), (33, 3_000_000), (291, 30_000_000)):
        rules.append(
            MedalRule(medal_id, f"{threshold:,} Hits", f"Hit {threshold:,} notes.", "Dedication", None,
                      _total_hits(threshold)),
        )

    for medal_id, threshold in ((50, 50_000), (51, 10_000), (52, 5_000), (53, 1_000)):
        rules.append(
            MedalRule(medal_id, f"Top {threshold:,}", f"Reach the top {threshold:,} of a ruleset.",
                      "Rank", None, _global_rank(threshold)),
        )

    # Star rating medals: 1 to 8 stars per ruleset for passing and for a full combo (osu-web ids).
    pass_start = {0: 55, 1: 71, 2: 79, 3: 87}
    fc_start = {0: 63, 1: 95, 2: 103, 3: 111}
    mode_names = {0: "osu!", 1: "taiko", 2: "catch", 3: "mania"}
    for mode, start in pass_start.items():
        for stars in range(1, 9):
            rules.append(
                MedalRule(start + stars - 1, f"{mode_names[mode]} Pass {stars}★",
                          f"Pass a {stars}★ ranked beatmap in {mode_names[mode]}.", "Skill", mode,
                          _star_rating(stars, False)),
            )
    for mode, start in fc_start.items():
        for stars in range(1, 9):
            rules.append(
                MedalRule(start + stars - 1, f"{mode_names[mode]} FC {stars}★",
                          f"Full combo a {stars}★ ranked beatmap in {mode_names[mode]}.", "Skill", mode,
                          _star_rating(stars, True)),
            )
    for medal_id, stars, fc in ((242, 9, False), (243, 9, True), (244, 10, False), (245, 10, True)):
        rules.append(
            MedalRule(medal_id, f"osu! {'FC' if fc else 'Pass'} {stars}★",
                      f"{'Full combo' if fc else 'Pass'} a {stars}★ ranked beatmap in osu!.", "Skill", 0,
                      _star_rating(stars, fc)),
        )
    return rules


CATALOGUE: list[MedalRule] = _build_catalogue()
_RULES_BY_ID = {rule.id: rule for rule in CATALOGUE}
assert len(_RULES_BY_ID) == len(CATALOGUE), "medal ids must be unique"


# --- database ----------------------------------------------------------------------------------------------


async def ensure_catalogue(db: AsyncSession) -> int:
    """Insert the medals that are missing from the ``medals`` table. Returns how many were added. No commit."""
    existing = set((await db.execute(select(Medal.id))).scalars().all())
    added = 0
    for order, rule in enumerate(CATALOGUE):
        if rule.id in existing:
            continue
        db.add(
            Medal(id=rule.id, name=rule.name, description=rule.description, slug=rule.slug,
                  grouping=rule.grouping, ordering=order, mode=rule.mode, enabled=True),
        )
        added += 1
    if added:
        await db.flush()
    return added


def _mods_of(score: Score) -> frozenset[str]:
    try:
        data: Any = json.loads(score.data or "{}")
    except (TypeError, ValueError):
        return frozenset()
    mods = data.get("mods", []) if isinstance(data, dict) else []
    return frozenset(str(m.get("acronym", "")).upper() for m in mods if isinstance(m, dict))


async def award_medals(
    db: AsyncSession,
    score: Score,
    beatmap: Beatmap,
    stats: UserStatistics,
    user: User,
) -> list[int]:
    """Award the medals this play earned. Returns the ids of the medals that are new. Does not commit."""
    if user.is_restricted or user.is_bot:
        return []
    if int(stats.variant or 0) != int(Variant.VANILLA) or int(score.variant or 0) != int(Variant.VANILLA):
        return []
    if beatmap.status <= BeatmapStatus.PENDING:
        return []

    await ensure_catalogue(db)

    owned = set((await db.execute(select(UserMedal.medal_id).where(UserMedal.user_id == score.user_id))).scalars())
    enabled = set((await db.execute(select(Medal.id).where(Medal.enabled.is_(True)))).scalars())
    context = MedalContext(score=score, beatmap=beatmap, stats=stats, mods=_mods_of(score))

    awarded: list[int] = []
    for rule in CATALOGUE:
        if rule.id in owned or rule.id not in enabled:
            continue
        if rule.mode is not None and rule.mode != score.ruleset_id:
            continue
        if rule.needs_pass and not score.passed:
            continue
        try:
            earned = rule.check(context)
        except Exception:  # a broken rule must never fail the statistics of the play
            logger.exception("Medal rule %s failed for score %s", rule.id, score.id)
            continue
        if earned:
            db.add(UserMedal(user_id=score.user_id, medal_id=rule.id, beatmap_id=beatmap.id))
            awarded.append(rule.id)

    if awarded:
        await db.flush()
        logger.info("Awarded medals %s to user %s (score %s)", awarded, score.user_id, score.id)
    return awarded


async def get_user_medals(db: AsyncSession, user_id: int) -> list[tuple[int, Any]]:
    """(medal id, awarded at) of everything the user earned, oldest first."""
    rows = await db.execute(
        select(UserMedal.medal_id, UserMedal.awarded_at)
        .where(UserMedal.user_id == user_id)
        .order_by(UserMedal.awarded_at, UserMedal.medal_id),
    )
    return [(medal_id, awarded_at) for medal_id, awarded_at in rows.all()]


__all__ = [
    "CATALOGUE",
    "MedalContext",
    "MedalRule",
    "award_medals",
    "ensure_catalogue",
    "get_user_medals",
]
