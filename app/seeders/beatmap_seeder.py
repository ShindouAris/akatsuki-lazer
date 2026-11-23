from __future__ import annotations

import os
import random
from datetime import UTC
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapDifficulty
from app.models.beatmap import BeatmapFailtimes
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus
from app.models.user import GameMode
from app.models.user import User
from app.seeders.base import Seeder

OSU_API_V1_URL = "https://osu.ppy.sh/api/get_beatmaps"

# Mirror of ``Beatmap::MODES`` in the PHP codebase.
MODE_VALUES: tuple[int, ...] = tuple(int(m) for m in GameMode)


def _parse_approved(value: Any) -> BeatmapStatus:
    """Convert the API's integer ``approved`` field to our enum."""
    try:
        return BeatmapStatus(int(value))
    except (TypeError, ValueError):
        return BeatmapStatus.PENDING


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an API datetime string (``YYYY-MM-DD HH:MM:SS``, UTC)."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _failtime_row(beatmap_id: int, ftype: str) -> BeatmapFailtimes:
    """Build a BeatmapFailtimes row with p1..p100 each random in [1, 10000]."""
    row = BeatmapFailtimes(beatmap_id=beatmap_id, type=ftype)
    for i in range(1, 101):
        setattr(row, f"p{i}", random.randint(1, 10000))
    return row


class BeatmapSeeder(Seeder):
    """Fetch a page of maps from osu! API v1 and upsert them locally."""

    name = "BeatmapSeeder"

    def __init__(self) -> None:
        # PHP seeder caches these across iterations to cut down queries.
        self._beatmapsets: dict[int, BeatmapSet] = {}
        self._beatmaps: dict[int, Beatmap] = {}
        self._random_user: User | None = None
        self._users_checked = False

    async def run(self, session: AsyncSession) -> None:
        self.info("Seeding Beatmaps, this may take a while...")

        api_key = os.getenv("OSU_API_KEY")
        if not api_key:
            self.error(
                "Error: No OSU_API_KEY value set in .env file. "
                "Can't seed beatmap data!",
            )
            return

        try:
            response = httpx.get(
                OSU_API_V1_URL,
                params={"since": "2016-01-01 00:00:00", "k": api_key},
                timeout=30.0,
            )
            response.raise_for_status()
            items: list[dict[str, Any]] = response.json()
        except Exception as ex:
            self.error(f"Unable to fetch Beatmap data: {ex}")
            return

        if not isinstance(items, list) or not items:
            self.warn("osu! API returned no beatmaps")
            return

        sets = await self._populate_existing(session, items)

        new_sets = 0
        new_beatmaps = 0

        for item in items:
            set_id = int(item["beatmapset_id"])
            bm_id = int(item["beatmap_id"])

            beatmapset = self._beatmapsets.get(set_id)
            beatmap = self._beatmaps.get(bm_id)

            if beatmapset is None:
                beatmapset = await self._create_beatmapset(session, item)
                sets.setdefault(beatmapset.id, [])
                new_sets += 1
            self._beatmapsets[beatmapset.id] = beatmapset

            if beatmap is None:
                beatmap = await self._create_beatmap(session, item, beatmapset)
                sets.setdefault(beatmapset.id, []).append(beatmap.id)
                new_beatmaps += 1
            self._beatmaps[beatmap.id] = beatmap

            await self._create_failtimes(session, beatmap)
            await self._create_difficulty(session, beatmap)

        # Aggregate set-level stats, mirroring the final loop in the PHP seeder.
        for set_id, map_ids in sets.items():
            unique_ids = list(set(map_ids))
            if not unique_ids:
                continue
            set_playcount = 0
            names = []
            for mid in unique_ids:
                bm = self._beatmaps.get(mid)
                if bm is None:
                    continue
                set_playcount += bm.play_count
                names.append(f"{bm.version}@{int(bm.mode)}")
            bs = self._beatmapsets[set_id]
            bs.versions_available = len(unique_ids)
            bs.play_count = set_playcount
            bs.difficulty_names = ",".join(names)

        updated_sets = len(self._beatmapsets) - new_sets
        updated_maps = len(self._beatmaps) - new_beatmaps
        self.info(
            f"Beatmap Sets: {updated_sets} updated, {new_sets} new. "
            f"Beatmaps: {updated_maps} updated, {new_beatmaps} new.",
        )

    # ------------------------------------------------------------------
    # Helpers (private)
    # ------------------------------------------------------------------

    async def _populate_existing(
        self,
        session: AsyncSession,
        items: list[dict[str, Any]],
    ) -> dict[int, list[int]]:
        """Preload existing sets/beatmaps from the payload to avoid duplicates."""
        sets: dict[int, list[int]] = {}
        for bm in items:
            set_id = int(bm["beatmapset_id"])
            sets.setdefault(set_id, [])

        set_ids = list(sets.keys())
        map_ids = [int(bm["beatmap_id"]) for bm in items]

        if set_ids:
            existing_sets = (
                await session.scalars(
                    select(BeatmapSet).where(BeatmapSet.id.in_(set_ids)),
                )
            ).all()
            for s in existing_sets:
                self._beatmapsets[s.id] = s

        if map_ids:
            existing_maps = (
                await session.scalars(
                    select(Beatmap).where(Beatmap.id.in_(map_ids)),
                )
            ).all()
            for m in existing_maps:
                self._beatmaps[m.id] = m
                sets.setdefault(m.beatmapset_id, []).append(m.id)

        return sets

    async def _random_user_pick(self, session: AsyncSession) -> User:
        """Return (and cache) a random user; create one if the DB has none."""
        if self._random_user is not None:
            return self._random_user

        if not self._users_checked:
            users = (await session.scalars(select(User))).all()
            self._users_checked = True
            if users:
                self._random_user = random.choice(users)
                return self._random_user

            # PHP calls ``User::factory()->create()`` as a last resort. We
            # surface this as a hard error: UserSeeder must run first.
            raise RuntimeError(
                "BeatmapSeeder requires at least one user in the database "
                "(run UserSeeder first).",
            )

        assert self._random_user is not None
        return self._random_user

    async def _create_beatmapset(
        self,
        session: AsyncSession,
        item: dict[str, Any],
    ) -> BeatmapSet:
        user = await self._random_user_pick(session)
        set_id = int(item["beatmapset_id"])
        bs = BeatmapSet(
            id=set_id,
            user_id=user.id,
            artist=item.get("artist") or "",
            title=item.get("title") or "",
            creator=item.get("creator") or user.username,
            source=item.get("source") or None,
            tags=item.get("tags") or None,
            status=_parse_approved(item.get("approved")),
            ranked_date=_parse_datetime(item.get("approved_date")),
            submitted_date=datetime.now(UTC),
            last_updated=datetime.now(UTC),
            play_count=0,
            favourite_count=int(item.get("favourite_count") or 0),
            bpm=float(item.get("bpm") or 0),
            genre_id=int(item["genre_id"]) if item.get("genre_id") else None,
            language_id=int(item["language_id"]) if item.get("language_id") else None,
            versions_available=1,
            difficulty_names="",
        )
        session.add(bs)
        await session.flush()
        return bs

    async def _create_beatmap(
        self,
        session: AsyncSession,
        item: dict[str, Any],
        beatmapset: BeatmapSet,
    ) -> Beatmap:
        user = await self._random_user_pick(session)
        bm_id = int(item["beatmap_id"])
        max_combo = int(item.get("max_combo") or 0)
        playmode = int(item.get("mode") or 0)
        approved = _parse_approved(item.get("approved"))
        bm = Beatmap(
            id=bm_id,
            beatmapset_id=beatmapset.id,
            user_id=user.id,
            version=item.get("version") or "",
            checksum=item.get("file_md5") or None,
            mode=GameMode(playmode),
            status=approved,
            cs=float(item.get("diff_size") or 5),
            ar=float(item.get("diff_approach") or 5),
            od=float(item.get("diff_overall") or 5),
            hp=float(item.get("diff_drain") or 5),
            difficulty_rating=float(item.get("difficultyrating") or 0),
            total_length=int(item.get("total_length") or 0),
            hit_length=int(item.get("hit_length") or 0),
            bpm=float(item.get("bpm") or 0),
            # PHP seeder computes these as ratios of ``max_combo``.
            count_circles=round(max_combo - (0.2 * max_combo)),
            count_sliders=round(max_combo - (0.8 * max_combo)),
            count_spinners=1,
            play_count=int(item.get("playcount") or 0),
            pass_count=int(item.get("passcount") or 0),
            max_combo=max_combo or None,
        )
        session.add(bm)
        await session.flush()
        return bm

    async def _create_failtimes(self, session: AsyncSession, beatmap: Beatmap) -> None:
        # Delete any existing rows for this beatmap then re-create both types,
        # matching the PHP seeder which does ``delete()`` + ``saveMany``.
        await session.execute(
            delete(BeatmapFailtimes).where(BeatmapFailtimes.beatmap_id == beatmap.id),
        )
        session.add(_failtime_row(beatmap.id, "fail"))
        session.add(_failtime_row(beatmap.id, "exit"))

    async def _create_difficulty(self, session: AsyncSession, beatmap: Beatmap) -> None:
        await session.execute(
            delete(BeatmapDifficulty).where(BeatmapDifficulty.beatmap_id == beatmap.id),
        )

        # Non-osu! maps only have their own mode; osu! maps have all four
        # (mode converts), with a small random fuzz on the star rating.
        if int(beatmap.mode) != int(GameMode.OSU):
            modes: tuple[int, ...] = (int(beatmap.mode),)
        else:
            modes = MODE_VALUES

        for mode in modes:
            if mode == int(beatmap.mode):
                diff_unified = beatmap.difficulty_rating
            else:
                diff_unified = beatmap.difficulty_rating + random.randint(-1000, 1000) / 10000
                if diff_unified < 0:
                    diff_unified = random.randint(1, 10000) / 10000
            session.add(BeatmapDifficulty(
                beatmap_id=beatmap.id,
                mode=mode,
                mods=0,
                diff_unified=diff_unified,
            ))
