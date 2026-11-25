"""Imports the metadata of ranked and loved osu!standard maps into the database, for the matchmaking pools.

Only metadata is stored (the search results of the mirror already carry every set with its beatmaps); the map
files are fetched later, when a client asks for them. Run it before ``matchmaking``, which turns the maps in the
database into cards:

.. code-block:: bash

    docker compose run --rm seed python -m app.seeders matchmaking-discover matchmaking

Environment:

* ``MATCHMAKING_DISCOVER_MAPS``: how many osu!standard maps to import (default 10000).
* ``MATCHMAKING_SEED_METADATA_URL``: mirror base URL serving ``GET /search`` (default: nekoha's ``/api``).

The maps are spread over the whole id range (each status takes pages spread evenly over its result list), so the
pool covers old and new maps and every difficulty. Sets already in the database are skipped, so it is safe to run
again; a second run adds more maps.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beatmap import BeatmapSet
from app.seeders.base import Seeder
from app.seeders.matchmaking_seeder import DEFAULT_METADATA_URL
from app.services.beatmaps import BeatmapService

DEFAULT_TARGET = 10_000
STATUSES = ("ranked", "loved")
MAX_PAGES_VISITED = 200  # per status
REQUEST_DELAY = 0.5


class MatchmakingDiscoverSeeder(Seeder):
    name = "MatchmakingDiscoverSeeder"

    def __init__(
        self,
        target: int | None = None,
        metadata_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        delay: float = REQUEST_DELAY,
        rng: random.Random | None = None,
    ) -> None:
        self._target = target or int(os.getenv("MATCHMAKING_DISCOVER_MAPS") or DEFAULT_TARGET)
        self._metadata_url = (
            metadata_url or os.getenv("MATCHMAKING_SEED_METADATA_URL") or DEFAULT_METADATA_URL
        ).rstrip("/")
        self._http_client = http_client
        self._delay = delay
        self._rng = rng or random.Random()

    async def run(self, session: AsyncSession) -> None:
        client = self._http_client or httpx.AsyncClient(timeout=60.0)
        try:
            totals = {status: await self._page_count(client, status) for status in STATUSES}
            all_pages = sum(totals.values())
            if all_pages == 0:
                self.warn(f"{self._metadata_url}/search returned nothing, no map imported")
                return
            self.info("pages of 100 sets: " + ", ".join(f"{s}={n}" for s, n in totals.items()))

            service = BeatmapService(session)
            imported = 0
            for status, pages in totals.items():
                if pages == 0:
                    continue
                share = round(self._target * pages / all_pages)
                imported += await self._import_status(client, session, service, status, pages, share)
            self.info(f"imported {imported} osu!standard map(s) in total")
        finally:
            if self._http_client is None:
                await client.aclose()

    async def _search(self, client: httpx.AsyncClient, status: str, page: int) -> list[dict[str, Any]]:
        params = {"status": status, "mode": "osu", "sort": "id", "order": "asc", "page": page}
        for attempt in range(3):
            try:
                response = await client.get(f"{self._metadata_url}/search", params=params)
                response.raise_for_status()
                sets = response.json().get("beatmapsets", [])
                return sets if isinstance(sets, list) else []
            except (httpx.HTTPError, ValueError) as e:
                self.warn(f"search {status} page {page} failed (attempt {attempt + 1}): {e}")
                await asyncio.sleep(2 * (attempt + 1))
        return []

    async def _page_count(self, client: httpx.AsyncClient, status: str) -> int:
        """The last page that still has results, found by doubling and then bisecting."""
        if not await self._search(client, status, 1):
            return 0
        low, high = 1, 2
        while await self._search(client, status, high):
            low, high = high, high * 2
            await asyncio.sleep(self._delay)
        while high - low > 1:
            mid = (low + high) // 2
            if await self._search(client, status, mid):
                low = mid
            else:
                high = mid
            await asyncio.sleep(self._delay)
        return low

    async def _import_status(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        service: BeatmapService,
        status: str,
        pages: int,
        wanted: int,
    ) -> int:
        visits = min(pages, MAX_PAGES_VISITED)
        # One random page out of each of `visits` equal slices of the result list.
        slice_size = max(1, int(pages / visits))
        chosen = [min(pages, int(i * pages / visits) + self._rng.randint(1, slice_size)) for i in range(visits)]
        per_page = max(1, round(wanted / visits))
        imported = 0

        for page in chosen:
            if imported >= wanted:
                break
            sets = await self._search(client, status, page)
            self._rng.shuffle(sets)
            taken = 0
            for data in sets:
                maps = sum(1 for b in data.get("beatmaps", []) if b.get("mode") == "osu")
                if maps == 0:
                    continue
                if await session.scalar(select(BeatmapSet.id).where(BeatmapSet.id == data["id"])) is None:
                    await service._cache_beatmapset(data)  # commits by itself
                taken += maps
                imported += maps
                if taken >= per_page:
                    break
            await asyncio.sleep(self._delay)

        self.info(f"{status}: {imported} map(s) from {visits} page(s)")
        return imported
