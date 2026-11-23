"""Medals awarded while a score is processed (app.services.medals)."""

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import Base
from app.models import multiplayer as _multiplayer_models  # noqa: F401  (registers FK targets)
from app.models.beatmap import BeatmapStatus
from app.models.medal import Medal
from app.models.medal import UserMedal
from app.services.medals import CATALOGUE
from app.services.score_stats import process_score
from app.services.variant import Variant
from tests.test_score_stats import make_beatmap
from tests.test_score_stats import make_score
from tests.test_score_stats import make_user


@pytest_asyncio.fixture
async def session_maker(tmp_path: Path):  # noqa: ANN201
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'medals.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def owned(db: AsyncSession, user_id: int) -> set[int]:
    rows = await db.execute(select(UserMedal.medal_id).where(UserMedal.user_id == user_id))
    return set(rows.scalars())


# A lone player with pp is rank 1, so every "Top N" medal comes with their first ranked play.
RANK_MEDALS = {50, 51, 52, 53}


async def play(db: AsyncSession, score_id: int):  # noqa: ANN201
    result = await process_score(db, score_id)
    await db.commit()
    return result


def skill(result) -> tuple[int, ...]:  # noqa: ANN001
    """The medals of a play without the rank milestones."""
    return tuple(m for m in result.medals if m not in RANK_MEDALS)


def test_the_catalogue_has_unique_ids_and_slugs() -> None:
    assert len({r.id for r in CATALOGUE}) == len(CATALOGUE)
    assert len({r.slug for r in CATALOGUE}) == len(CATALOGUE)
    assert {1, 3, 4, 5, 20, 50, 55, 63, 242} <= {r.id for r in CATALOGUE}


@pytest.mark.asyncio
async def test_a_combo_medal_is_awarded_once(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        first = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=760)
        second = await make_score(db, player, beatmap, 810_000, 101.0, max_combo=900)
        await db.commit()

        result = await play(db, first.id)
        assert set(skill(result)) == {1, 3}, "500 and 750 combo, not 1000"
        assert set(result.medals) >= RANK_MEDALS, "the only ranked player is rank 1"
        assert await owned(db, player.id) == {1, 3} | RANK_MEDALS

        again = await play(db, second.id)
        assert again.medals == (), "a medal is only awarded the first time"
        assert await owned(db, player.id) == {1, 3} | RANK_MEDALS
        assert (await db.execute(select(Medal.id).where(Medal.id == 1))).scalar_one() == 1, "catalogue was filled"


@pytest.mark.asyncio
async def test_relax_autopilot_and_failed_plays_earn_nothing(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        relax = await make_score(
            db, player, beatmap, 900_000, 300.0, max_combo=2500, mods=[{"acronym": "RX"}], variant=int(Variant.RELAX),
        )
        auto = await make_score(
            db, player, beatmap, 900_000, 300.0, max_combo=2500, mods=[{"acronym": "AP"}],
            variant=int(Variant.AUTOPILOT),
        )
        failed = await make_score(db, player, beatmap, 100_000, None, rank="F", passed=False, max_combo=2500)
        await db.commit()

        for score in (relax, auto, failed):
            assert (await play(db, score.id)).medals == ()

        assert await owned(db, player.id) == set()


@pytest.mark.asyncio
async def test_statistics_medals_follow_the_fresh_statistics(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from tests.test_score_stats import get_stats

    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        stats = await get_stats(db, player.id)
        stats.play_count = 4_999
        await db.flush()
        score = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=10)
        await db.commit()

        result = await play(db, score.id)

    assert skill(result) == (20,), "the 5,000th play"


@pytest.mark.asyncio
async def test_star_rating_medals(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        beatmap.difficulty_rating = 5.4
        beatmap.max_combo = 300
        player = await make_user(db, "player")
        easy = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=250, mods=[{"acronym": "HD"}])
        full = await make_score(db, player, beatmap, 850_000, 110.0, max_combo=300)
        await db.commit()

        assert skill(await play(db, easy.id)) == (59,), "5★ pass (HD does not change the stars), no FC"
        assert skill(await play(db, full.id)) == (67,), "5★ full combo"


@pytest.mark.asyncio
async def test_star_rating_medals_ignore_plays_with_star_changing_mods(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        beatmap.difficulty_rating = 5.4
        player = await make_user(db, "player")
        doubled = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=10, mods=[{"acronym": "DT"}])
        await db.commit()

        assert skill(await play(db, doubled.id)) == ()


@pytest.mark.asyncio
async def test_restricted_players_and_unranked_maps_earn_nothing(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        ranked = await make_beatmap(db, BeatmapStatus.RANKED)
        pending = await make_beatmap(db, BeatmapStatus.PENDING)
        banned = await make_user(db, "banned")
        banned.is_restricted = True
        fair = await make_user(db, "fair")
        banned_score = await make_score(db, banned, ranked, 800_000, 100.0, max_combo=600)
        pending_score = await make_score(db, fair, pending, 800_000, None, max_combo=600)
        await db.commit()

        assert (await play(db, banned_score.id)).medals == ()
        assert (await play(db, pending_score.id)).medals == ()


@pytest.mark.asyncio
async def test_the_profile_lists_earned_medals(client: AsyncClient, db_session: AsyncSession) -> None:
    beatmap = await make_beatmap(db_session, BeatmapStatus.RANKED)
    player = await make_user(db_session, "medalist")
    score = await make_score(db_session, player, beatmap, 800_000, 100.0, max_combo=600)
    await db_session.commit()
    await process_score(db_session, score.id)
    await db_session.commit()

    response = await client.get(f"/api/v2/users/{player.id}/osu")

    assert response.status_code == 200, response.text
    assert [a["achievement_id"] for a in response.json()["user_achievements"]] == [1, 50, 51, 52, 53]
