"""Beatmap service tests."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beatmap import BeatmapSet
from app.models.user import User
from app.services.beatmaps import BeatmapService


@pytest.mark.asyncio
async def test_cache_beatmapset_ignores_missing_owner_user(db_session: AsyncSession) -> None:
    """Unknown owner IDs should be stored as NULL to avoid FK violations."""
    service = BeatmapService(db_session)

    beatmapset = await service._cache_beatmapset({
        "id": 123456,
        "user_id": 19425672,
        "artist": "artist",
        "title": "title",
        "creator": "creator",
        "status": "pending",
        "beatmaps": [],
    })

    assert beatmapset.user_id is None

    result = await db_session.execute(select(BeatmapSet).where(BeatmapSet.id == 123456))
    persisted = result.scalar_one()
    assert persisted.user_id is None


@pytest.mark.asyncio
async def test_cache_beatmapset_keeps_existing_owner_user(db_session: AsyncSession) -> None:
    """Existing owner IDs should still be persisted normally."""
    owner = User(
        username="beatmap_owner",
        email="beatmap_owner@example.com",
        password_hash="hash",
        country_acronym="US",
    )
    db_session.add(owner)
    await db_session.flush()

    service = BeatmapService(db_session)
    beatmapset = await service._cache_beatmapset({
        "id": 123457,
        "user_id": owner.id,
        "artist": "artist",
        "title": "title",
        "creator": "creator",
        "status": "pending",
        "beatmaps": [],
    })

    assert beatmapset.user_id == owner.id

    result = await db_session.execute(select(BeatmapSet).where(BeatmapSet.id == 123457))
    persisted = result.scalar_one()
    assert persisted.user_id == owner.id
