"""Seeds the 1v1 Duel modes (Nomod, RX, AP) of ranked play.

A mode is a ``ranked_play`` pool. Its cards (``matchmaking_pool_beatmaps``) are every *ranked* osu!standard map
the database has, and the required mods of a card come from the mode (none / RX / AP). The sets listed in
``matchmaking_sets.txt`` are only imported into the database first, so listing a set adds its ranked maps.

Beatmap metadata comes from a mirror that serves the osu! API v2 shape (default: mirror.nekoha.moe) and is
cached through :class:`app.services.beatmaps.BeatmapService`, so the checksums stored here are those of the
files the download mirrors serve.

Environment overrides:

* ``MATCHMAKING_SEED_SETS``: path of the set list (default: ``matchmaking_sets.txt`` next to this module).
* ``MATCHMAKING_SEED_METADATA_URL``: base URL answering ``GET /beatmapset/{id}`` (default: nekoha's ``/api``).

Safe to run repeatedly: cached sets are not fetched again, pools are created once, and cards that already
exist (together with any rating learned for them) are left untouched.

.. code-block:: bash

    python -m app.seeders matchmaking
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import RANKED_STATUSES
from app.models.beatmap import BeatmapStatus
from app.models.legacy import MatchmakingPool
from app.models.legacy import MatchmakingPoolBeatmap
from app.models.user import GameMode
from app.seeders.base import Seeder
from app.services.beatmaps import BeatmapService

DEFAULT_SETS_FILE = Path(__file__).with_name("matchmaking_sets.txt")
DEFAULT_METADATA_URL = "https://mirror.nekoha.moe/api"

POOL_TYPE = "ranked_play"
OSU_RULESET_ID = 0

# Ranked and loved maps are cards (loved maps award no pp, but are fine to duel on).
CARD_STATUSES = (*RANKED_STATUSES, BeatmapStatus.LOVED)
LOBBY_SIZE = 2

# Ranked queue matching: the search radius starts at 200 rating points and doubles every 30 seconds.
# ``rating_search_radius_exp`` is a divisor, so it must never be 0.
RATING_SEARCH_RADIUS = 200
RATING_SEARCH_RADIUS_EXP = 30
RATING_SEARCH_RADIUS_MAX = 9999

# The server deals 5 cards to every player, so fewer cards than this cannot fill two hands.
MIN_CARDS_FOR_TWO_HANDS = 10


def _mods(*acronyms: str) -> str:
    # Must be exactly what the mods are serialised to again at the end of a round (the "settings" key is
    # omitted while empty), because that string is used to look the card's rating row up.
    if not acronyms:
        return ""
    return json.dumps([{"acronym": acronym} for acronym in acronyms], separators=(",", ":"))


# (pool name, required mods of every card in the pool)
DUEL_MODES: tuple[tuple[str, str], ...] = (
    ("1v1 HD", _mods("HD")),
    ("1v1 RX HD", _mods("RX", "HD")),
    ("1v1 RX NC", _mods("RX", "NC")),
    ("1v1 AP HD", _mods("AP", "HD")),
    ("1v1 AP DT", _mods("AP", "DT")),
)

# Roughly how much a mod raises a map's star rating. The cards of a modded pool start rated by the stars they play
# at; the ratings then follow the results of the matches like any other (the server's own ratings adjust too).
STAR_FACTORS = {"DT": 1.4, "NC": 1.4, "HR": 1.08}


def star_factor(mods: str) -> float:
    factor = 1.0
    for mod in json.loads(mods) if mods else []:
        factor *= STAR_FACTORS.get(mod["acronym"], 1.0)
    return factor


def rating_for(star_rating: float) -> int:
    """Initial rating of a card that has none yet (the same formula osu-server-spectator uses)."""
    return round(800 + 500 * (math.exp(0.16 * star_rating) - 1))


class MatchmakingSeeder(Seeder):
    """Create the Duel pools and fill them with the maps of ``matchmaking_sets.txt``."""

    name = "MatchmakingSeeder"

    def __init__(
        self,
        sets_file: Path | None = None,
        metadata_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        fetch_delay: float = 0.5,
    ) -> None:
        self._sets_file = sets_file or Path(os.getenv("MATCHMAKING_SEED_SETS") or DEFAULT_SETS_FILE)
        self._metadata_url = (
            metadata_url or os.getenv("MATCHMAKING_SEED_METADATA_URL") or DEFAULT_METADATA_URL
        ).rstrip("/")
        self._http_client = http_client
        self._fetch_delay = fetch_delay

    async def run(self, session: AsyncSession) -> None:
        # The listed sets are only imported into the database; the cards are then every ranked osu!standard map the
        # database has, whether it came from the list or from anywhere else.
        set_ids = self._read_set_ids()
        if set_ids:
            await self._cache_beatmapsets(session, set_ids)
        else:
            self.warn(f"{self._sets_file} lists no beatmapset ids, using the ranked maps already in the database")

        beatmaps = await self._osu_beatmaps(session)
        if not beatmaps:
            self.warn("the database has no ranked osu!standard beatmap, no cards to add")
            return

        for pool_name, mods in DUEL_MODES:
            pool = await self._ensure_pool(session, pool_name)
            removed = await self._remove_ineligible_cards(session, pool)
            added = await self._ensure_cards(session, pool, mods, beatmaps)
            self.info(
                f"pool '{pool_name}' (id {pool.id}): {added} new card(s), {removed} unranked card(s) removed, "
                f"{len(beatmaps)} map(s) in total",
            )

        await self._deactivate_retired_pools(session)

        if len(beatmaps) < MIN_CARDS_FOR_TWO_HANDS:
            self.warn(
                f"only {len(beatmaps)} card(s) per pool; every player is dealt 5 cards, "
                "so real duels need many more maps in the set list",
            )

    def _read_set_ids(self) -> list[int]:
        ids: list[int] = []
        for line in self._sets_file.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                ids.append(int(line))
        return list(dict.fromkeys(ids))

    async def _cache_beatmapsets(self, session: AsyncSession, set_ids: list[int]) -> None:
        service = BeatmapService(session)
        client = self._http_client or httpx.AsyncClient(timeout=30.0)
        fetched_any = False

        try:
            for set_id in set_ids:
                already_cached = await session.scalar(
                    select(BeatmapSet.id).where(BeatmapSet.id == set_id)
                )
                if already_cached is not None:
                    self.info(f"beatmapset {set_id} is already cached")
                    continue

                if fetched_any:
                    await asyncio.sleep(self._fetch_delay)
                fetched_any = True

                data = await self._fetch_beatmapset(client, set_id)
                if data is None:
                    continue

                # Commits by itself, so the sets cached so far survive a failure further down the pipeline.
                await service._cache_beatmapset(data)
        finally:
            if self._http_client is None:
                await client.aclose()

    async def _fetch_beatmapset(
        self, client: httpx.AsyncClient, set_id: int
    ) -> dict[str, Any] | None:
        url = f"{self._metadata_url}/beatmapset/{set_id}"
        try:
            response = await client.get(url)
            if response.status_code == 404:
                self.warn(f"beatmapset {set_id} was not found at {self._metadata_url}, skipped")
                return None
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            self.warn(f"could not fetch beatmapset {set_id} from {url}: {e}")
            return None

        if not isinstance(data, dict) or "id" not in data:
            self.warn(f"{url} did not return a beatmapset, skipped")
            return None
        return data

    async def _osu_beatmaps(self, session: AsyncSession) -> list[Beatmap]:
        # Relax and Autopilot only exist for osu!standard.
        result = await session.execute(
            select(Beatmap)
            .where(
                Beatmap.mode == GameMode.OSU,
                Beatmap.status.in_(CARD_STATUSES),
                # A map without a star rating (a mirror has no difficulty for it) cannot be rated as a card: it would
                # be dealt as a "0 star" card, far below everything else.
                Beatmap.difficulty_rating > 0,
            )
            .order_by(Beatmap.id),
        )
        return list(result.scalars().all())

    async def _remove_ineligible_cards(self, session: AsyncSession, pool: MatchmakingPool) -> int:
        """Drop the cards of maps that are not ranked (or have no rating), left over from earlier runs."""
        result = await session.execute(
            select(MatchmakingPoolBeatmap)
            .join(Beatmap, Beatmap.id == MatchmakingPoolBeatmap.beatmap_id)
            .where(
                MatchmakingPoolBeatmap.pool_id == pool.id,
                or_(Beatmap.status.not_in(CARD_STATUSES), Beatmap.difficulty_rating <= 0),
            ),
        )
        stale = list(result.scalars().all())
        for card in stale:
            await session.delete(card)
        await session.flush()
        return len(stale)

    async def _deactivate_retired_pools(self, session: AsyncSession) -> None:
        """Take the "1v1 ..." pools that are no longer in DUEL_MODES out of the queue.

        They are deactivated, not deleted, so their cards, ratings and match history stay. Pools with other names
        (made by hand) are not touched.
        """
        wanted = {name for name, _ in DUEL_MODES}
        result = await session.execute(
            select(MatchmakingPool).where(
                MatchmakingPool.type == POOL_TYPE,
                MatchmakingPool.ruleset_id == OSU_RULESET_ID,
                MatchmakingPool.variant_id == 0,
                MatchmakingPool.active.is_(True),
                MatchmakingPool.name.like("1v1 %"),
            ),
        )
        for pool in result.scalars().all():
            if pool.name not in wanted:
                pool.active = False
                self.info(f"pool '{pool.name}' (id {pool.id}) is no longer a mode: deactivated")
        await session.flush()

    async def _ensure_pool(self, session: AsyncSession, name: str) -> MatchmakingPool:
        result = await session.execute(
            select(MatchmakingPool)
            .where(
                MatchmakingPool.name == name,
                MatchmakingPool.type == POOL_TYPE,
                MatchmakingPool.ruleset_id == OSU_RULESET_ID,
                MatchmakingPool.variant_id == 0,
            )
            .order_by(MatchmakingPool.id)
            .limit(1),
        )
        pool = result.scalars().first()

        if pool is None:
            pool = MatchmakingPool(
                ruleset_id=OSU_RULESET_ID,
                variant_id=0,
                name=name,
                type=POOL_TYPE,
                active=True,
                lobby_size=LOBBY_SIZE,
                rating_search_radius=RATING_SEARCH_RADIUS,
                rating_search_radius_max=RATING_SEARCH_RADIUS_MAX,
                rating_search_radius_exp=RATING_SEARCH_RADIUS_EXP,
            )
            session.add(pool)
            await session.flush()
        elif not pool.active:
            self.warn(f"pool '{name}' (id {pool.id}) exists but is inactive; left as it is")

        return pool

    async def _ensure_cards(
        self,
        session: AsyncSession,
        pool: MatchmakingPool,
        mods: str,
        beatmaps: list[Beatmap],
    ) -> int:
        column = MatchmakingPoolBeatmap.mods
        # A pool without required mods stores either an empty string or NULL, depending on who wrote the row.
        same_mods = or_(column == "", column.is_(None)) if mods == "" else column == mods

        result = await session.execute(
            select(MatchmakingPoolBeatmap.beatmap_id).where(
                MatchmakingPoolBeatmap.pool_id == pool.id,
                same_mods,
            ),
        )
        existing = set(result.scalars().all())

        factor = star_factor(mods)
        added = 0
        for beatmap in beatmaps:
            if beatmap.id in existing:
                continue
            session.add(
                MatchmakingPoolBeatmap(
                    pool_id=pool.id,
                    beatmap_id=beatmap.id,
                    mods=mods,
                    rating=rating_for(beatmap.difficulty_rating * factor),
                    selection_count=0,
                ),
            )
            added += 1

        await session.flush()
        return added
