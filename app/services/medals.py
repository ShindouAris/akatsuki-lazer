"""Medals, awarded while a score's statistics are processed.

The medal set and its rules follow osuAkatsuki/score-service-rs (``src/achievements``); the ids follow
osu-web / ppy/osu-queue-score-statistics where the medal exists there:

* **Skill** (per ruleset): pass a map of N to N+1 stars (1-8, osu! also 9 and 10) without NoFail, or full
  combo it. The star rating is the one of the play *with its mods* (stored in the score data when it was
  submitted; maps without it fall back to the beatmap's own rating when no mod changes it).
* **Combo** (osu! only): 500, 750, 1000, 2000.
* **Plays** (osu! only): 5,000, 15,000, 25,000, 50,000.
* **Hits** (taiko 30k, catch 20k, mania 40k, each x1, x10, x100, x1000).
* **Rank** (per ruleset): top 50,000, 10,000, 5,000, 1,000 of the board.
* **Mod introductions**: pass a map with SD, PF, HR, DT, NC, HD, FL, EZ, NF, HT or SO.
* **Hush-hush**: only the ones Akatsuki implements (Consolation Prize, Stumbler, Jackpot).

Every board (nomod / Relax / Autopilot) earns them; the statistics based ones read the statistics of the play's
own board, and a medal is owned once, whichever board earned it. The catalogue is copied into the ``medals``
table on first use. Ids from 1000 up are not osu-web ids (they were made up for this server).
"""

import json
import logging
from datetime import UTC
from datetime import datetime
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

logger = logging.getLogger(__name__)

# Mods that change a map's star rating: without a stored star rating such a play cannot earn a skill medal.
_STAR_CHANGING_MODS = frozenset({"EZ", "HR", "DT", "NC", "HT", "DC", "DA", "TP", "CL", "MR"})
_CHEATING_MODS = frozenset({"AT", "CN"})
_LEADERBOARD_STATUSES = frozenset(
    {BeatmapStatus.RANKED, BeatmapStatus.APPROVED, BeatmapStatus.LOVED, BeatmapStatus.QUALIFIED},
)

OSU, TAIKO, CATCH, MANIA = 0, 1, 2, 3
_MODE_NAMES = {OSU: "osu!", TAIKO: "taiko", CATCH: "catch", MANIA: "mania"}


@dataclass(frozen=True, slots=True)
class MedalContext:
    """Everything a rule may look at."""

    score: Score
    beatmap: Beatmap
    stats: UserStatistics
    mods: frozenset[str]
    stars: float | None

    @property
    def mode(self) -> int:
        return self.score.ruleset_id

    @property
    def full_combo(self) -> bool:
        return bool(self.beatmap.max_combo) and self.score.max_combo >= self.beatmap.max_combo


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
    # osu-web's medal slug: the client loads the icon from ``s.ppy.sh/images/medals-client/<slug>@2x.png``.
    icon: str = ""

    @property
    def slug(self) -> str:
        """Unique per medal (stored in the database)."""
        return "-".join(self.name.lower().replace("!", "").replace("★", "star").split())

    @property
    def icon_slug(self) -> str:
        """The slug of the icon to show; several medals may share one."""
        return self.icon or self.slug


_ICON_PREFIX = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}
_INTRO_ICONS = {
    "SD": "suddendeath", "PF": "perfect", "HR": "hardrock", "DT": "doubletime", "NC": "nightcore", "HD": "hidden",
    "FL": "flashlight", "EZ": "easy", "NF": "nofail", "HT": "halftime", "SO": "spunout",
}


# --- conditions -----------------------------------------------------------------------------------------


def _skill(stars: int, full_combo: bool) -> Callable[[MedalContext], bool]:
    def check(c: MedalContext) -> bool:
        if c.beatmap.status not in _LEADERBOARD_STATUSES or c.stars is None:
            return False
        if not full_combo and "NF" in c.mods:
            return False
        if not stars <= c.stars < stars + 1:
            return False
        return c.full_combo if full_combo else True

    return check


def _combo(threshold: int) -> Callable[[MedalContext], bool]:
    return lambda c: not (c.mods & _CHEATING_MODS) and c.score.max_combo >= threshold


def _play_count(threshold: int) -> Callable[[MedalContext], bool]:
    return lambda c: c.stats.play_count >= threshold


def _total_hits(threshold: int) -> Callable[[MedalContext], bool]:
    return lambda c: c.stats.total_hits >= threshold


def _global_rank(threshold: int) -> Callable[[MedalContext], bool]:
    return lambda c: c.stats.global_rank is not None and 0 < c.stats.global_rank <= threshold


def _uses_mod(*acronyms: str) -> Callable[[MedalContext], bool]:
    wanted = frozenset(acronyms)
    return lambda c: bool(c.mods & wanted)


def _consolation_prize(c: MedalContext) -> bool:
    return c.score.total_score >= 100_000 and c.score.rank.strip().upper() == "D"


def _stumbler(c: MedalContext) -> bool:
    accuracy = c.score.accuracy * 100.0 if c.score.accuracy <= 1.0 else c.score.accuracy
    return c.full_combo and accuracy < 85.0


def _jackpot(c: MedalContext) -> bool:
    digits = str(c.score.total_score)
    return len(digits) >= 2 and len(set(digits)) == 1


# --- catalogue -------------------------------------------------------------------------------------------


def _build_catalogue() -> list[MedalRule]:
    rules: list[MedalRule] = []

    # Skill: osu-web ids, 8 per ruleset and kind (osu! has 9 and 10 stars on top).
    pass_start = {OSU: 55, TAIKO: 71, CATCH: 79, MANIA: 87}
    fc_start = {OSU: 63, TAIKO: 95, CATCH: 103, MANIA: 111}
    for mode in (OSU, TAIKO, CATCH, MANIA):
        name = _MODE_NAMES[mode]
        for stars in range(1, 9):
            rules.append(MedalRule(pass_start[mode] + stars - 1, f"{name} Pass {stars}★",
                                   f"Pass a {stars}★ beatmap in {name}.", "Skill", mode, _skill(stars, False),
                                   icon=f"{_ICON_PREFIX[mode]}-skill-pass-{stars}"))
        for stars in range(1, 9):
            rules.append(MedalRule(fc_start[mode] + stars - 1, f"{name} FC {stars}★",
                                   f"Full combo a {stars}★ beatmap in {name}.", "Skill", mode, _skill(stars, True),
                                   icon=f"{_ICON_PREFIX[mode]}-skill-fc-{stars}"))
    for medal_id, stars, fc in ((242, 9, False), (243, 9, True), (244, 10, False), (245, 10, True)):
        rules.append(MedalRule(medal_id, f"osu! {'FC' if fc else 'Pass'} {stars}★",
                               f"{'Full combo' if fc else 'Pass'} a {stars}★ beatmap in osu!.", "Skill", OSU,
                               _skill(stars, fc), icon=f"osu-skill-{'fc' if fc else 'pass'}-{stars}"))

    for medal_id, threshold in ((1, 500), (3, 750), (4, 1000), (5, 2000)):
        rules.append(MedalRule(medal_id, f"{threshold} Combo", f"Reach a {threshold} combo in osu!.", "Skill", OSU,
                               _combo(threshold), icon=f"osu-combo-{threshold}"))

    for medal_id, threshold in ((20, 5_000), (21, 15_000), (22, 25_000), (28, 50_000)):
        rules.append(MedalRule(medal_id, f"{threshold:,} Plays", f"Play osu! {threshold:,} times.", "Dedication",
                               OSU, _play_count(threshold), needs_pass=False, icon=f"osu-plays-{threshold}"))

    hit_ids = {
        TAIKO: ((31, 30_000), (32, 300_000), (33, 3_000_000), (291, 30_000_000)),
        CATCH: ((13, 20_000), (23, 200_000), (24, 2_000_000), (292, 20_000_000)),
        MANIA: ((46, 40_000), (47, 400_000), (48, 4_000_000), (293, 40_000_000)),
    }
    for mode, medals in hit_ids.items():
        for medal_id, threshold in medals:
            rules.append(MedalRule(medal_id, f"{_MODE_NAMES[mode]} {threshold:,} Hits",
                                   f"Hit {threshold:,} notes in {_MODE_NAMES[mode]}.", "Dedication", mode,
                                   _total_hits(threshold), icon=f"{_ICON_PREFIX[mode]}-hits-{threshold}"))

    # Rank: osu-web ids for osu!, made up ones (1000+) for the other rulesets.
    rank_ids = {
        OSU: (50, 51, 52, 53),
        TAIKO: (1101, 1102, 1103, 1104),
        CATCH: (1111, 1112, 1113, 1114),
        MANIA: (1121, 1122, 1123, 1124),
    }
    for mode, ids in rank_ids.items():
        # osu-web has no icons for these; borrow the skill ones so the unlock still shows a medal.
        for (medal_id, threshold), icon_stars in zip(zip(ids, (50_000, 10_000, 5_000, 1_000)), (5, 6, 7, 8)):
            rules.append(MedalRule(medal_id, f"{_MODE_NAMES[mode]} Top {threshold:,}",
                                   f"Reach the top {threshold:,} in {_MODE_NAMES[mode]}.", "Rank", mode,
                                   _global_rank(threshold), icon=f"{_ICON_PREFIX[mode]}-skill-pass-{icon_stars}"))

    intros = (
        (119, "Sudden Death", ("SD",)), (120, "Perfect", ("PF",)), (121, "Hard Rock", ("HR",)),
        (122, "Double Time", ("DT",)), (123, "Nightcore", ("NC",)), (124, "Hidden", ("HD",)),
        (125, "Flashlight", ("FL",)), (126, "Easy", ("EZ",)), (127, "No Fail", ("NF",)),
        (128, "Half Time", ("HT",)), (131, "Spun Out", ("SO",)),
    )
    for medal_id, mod_name, acronyms in intros:
        rules.append(MedalRule(medal_id, f"{mod_name} Introduction", f"Pass a beatmap with {mod_name}.",
                               "Mod Introduction", None, _uses_mod(*acronyms),
                               icon=f"all-intro-{_INTRO_ICONS[acronyms[0]]}"))

    rules.append(MedalRule(1201, "Consolation Prize", "Pass a beatmap with a D rank and 100,000 score.",
                           "Hush-Hush", None, _consolation_prize, icon="all-secret-consolation_prize"))
    rules.append(MedalRule(1202, "Stumbler", "Full combo a beatmap with less than 85% accuracy.",
                           "Hush-Hush", None, _stumbler, icon="all-secret-stumbler"))
    rules.append(MedalRule(1203, "Jackpot", "Pass a beatmap with a score of one repeated digit.",
                           "Hush-Hush", None, _jackpot, icon="all-secret-jackpot"))
    return rules


CATALOGUE: list[MedalRule] = _build_catalogue()
_RULES_BY_ID = {rule.id: rule for rule in CATALOGUE}
assert len(_RULES_BY_ID) == len(CATALOGUE), "medal ids must be unique"


# --- database ----------------------------------------------------------------------------------------------


async def ensure_catalogue(db: AsyncSession) -> int:
    """Insert the medals that are missing from the ``medals`` table. Returns how many were added. No commit."""
    existing = {medal.id: medal for medal in (await db.execute(select(Medal))).scalars().all()}
    added = 0
    for order, rule in enumerate(CATALOGUE):
        if rule.id in existing:
            continue
        db.add(
            Medal(id=rule.id, name=rule.name, description=rule.description, slug=rule.slug,
                  grouping=rule.grouping, ordering=order, mode=rule.mode, enabled=True),
        )
        added += 1
    await db.flush()
    return added


def _score_data(score: Score) -> dict[str, Any]:
    try:
        data = json.loads(score.data or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stars_of(data: dict[str, Any], beatmap: Beatmap, mods: frozenset[str]) -> float | None:
    """Star rating of the play with its mods; the beatmap's own when no mod changes it; otherwise unknown."""
    stored = data.get("star_rating")
    if isinstance(stored, int | float) and stored > 0:
        return float(stored)
    if mods & _STAR_CHANGING_MODS:
        return None
    return float(beatmap.difficulty_rating) if beatmap.difficulty_rating else None


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
    if beatmap.status <= BeatmapStatus.PENDING:
        return []

    await ensure_catalogue(db)

    owned = set((await db.execute(select(UserMedal.medal_id).where(UserMedal.user_id == score.user_id))).scalars())
    enabled = set((await db.execute(select(Medal.id).where(Medal.enabled.is_(True)))).scalars())

    data = _score_data(score)
    mods = frozenset(
        str(m.get("acronym", "")).upper() for m in data.get("mods", []) if isinstance(m, dict)
    )
    context = MedalContext(
        score=score, beatmap=beatmap, stats=stats, mods=mods, stars=_stars_of(data, beatmap, mods),
    )

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


_MODE_NAMES = ("osu", "taiko", "fruits", "mania")


def unlock_event_data(rule: MedalRule, user_id: int) -> dict[str, Any]:
    """The payload of the client's ``user_achievement_unlock`` notification (what ``MedalOverlay`` reads)."""
    return {
        "id": 0,
        "name": "user_achievement_unlock",
        "created_at": datetime.now(UTC).isoformat(),
        "object_type": "user_achievement",
        "object_id": rule.id,
        "source_user_id": user_id,
        "is_read": False,
        "details": {
            "achievement_id": rule.id,
            "achievement_mode": _MODE_NAMES[rule.mode] if rule.mode is not None else None,
            "cover_url": "",
            "slug": rule.icon_slug,
            "title": rule.name,
            "description": rule.description,
            "user_id": user_id,
        },
    }


async def notify_medals(user_id: int, medal_ids: tuple[int, ...] | list[int]) -> None:
    """Tell the player's connected client(s) about medals just earned so it shows the unlock overlay."""
    from app.services.chat import connections

    by_id = {rule.id: rule for rule in CATALOGUE}
    for medal_id in medal_ids:
        rule = by_id.get(medal_id)
        if rule is not None:
            await connections.send([user_id], "new", unlock_event_data(rule, user_id), chat_only=False)


__all__ = [
    "notify_medals",
    "unlock_event_data",
    "CATALOGUE",
    "MedalContext",
    "MedalRule",
    "award_medals",
    "ensure_catalogue",
    "get_user_medals",
]
