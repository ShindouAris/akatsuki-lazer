"""The discover seeder imports a spread of ranked and loved osu!standard maps from a mirror's search."""

import random

import httpx
import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beatmap import Beatmap
from app.seeders.matchmaking_discover_seeder import MatchmakingDiscoverSeeder

PAGES = {"ranked": 3, "loved": 2}  # pages of 4 sets each


def _set(set_id: int, status: str) -> dict:
    return {
        "id": set_id,
        "artist": "a",
        "title": f"t{set_id}",
        "creator": "c",
        "status": status,
        "beatmaps": [
            {"id": set_id * 10 + 1, "mode": "osu", "status": status, "difficulty_rating": 3.0, "version": "x",
             "checksum": f"c{set_id}1"},
            {"id": set_id * 10 + 2, "mode": "mania", "status": status, "difficulty_rating": 2.0, "version": "y",
             "checksum": f"c{set_id}2"},
        ],
    }


def _handler(request: httpx.Request) -> httpx.Response:
    status = request.url.params["status"]
    page = int(request.url.params["page"])
    assert request.url.params["mode"] == "osu"
    if page > PAGES[status]:
        return httpx.Response(200, json={"beatmapsets": []})
    base = (1000 if status == "ranked" else 5000) + page * 10
    return httpx.Response(200, json={"beatmapsets": [_set(base + i, status) for i in range(4)]})


@pytest.mark.asyncio
async def test_imports_ranked_and_loved_osu_maps(db_session: AsyncSession) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    seeder = MatchmakingDiscoverSeeder(
        target=10, http_client=client, delay=0, rng=random.Random(1), metadata_url="http://mirror/api",
    )

    await seeder.run(db_session)

    statuses = set((await db_session.execute(select(Beatmap.status))).scalars().all())
    assert {s.name for s in statuses} >= {"RANKED", "LOVED"}
    assert await db_session.scalar(select(func.count(Beatmap.id))) > 0
