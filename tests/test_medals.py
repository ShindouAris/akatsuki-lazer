"""Medals awarded while a score is processed (app.services.medals), following score-service-rs."""

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
from app.models.user import GameMode
from app.models.user import UserStatistics
from app.services.medals import CATALOGUE
from app.services.score_stats import process_score
from app.services.variant import Variant
from tests.test_score_stats import get_stats
from tests.test_score_stats import make_beatmap
from tests.test_score_stats import make_score
from tests.test_score_stats import make_user

TAIKO = 1


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


async def play(db: AsyncSession, score_id: int):  # noqa: ANN201
    result = await process_score(db, score_id)
    await db.commit()
    return result


def only(result, *ids: int) -> tuple[int, ...]:  # noqa: ANN001
    """The medals of a play restricted to ``ids`` (a lone player is rank 1, which earns rank medals too)."""
    return tuple(sorted(set(result.medals) & set(ids)))


OSU_RANK = (50, 51, 52, 53)


def test_the_catalogue_has_unique_ids_and_slugs_and_the_akatsuki_set() -> None:
    assert len({r.id for r in CATALOGUE}) == len(CATALOGUE)
    assert len({r.slug for r in CATALOGUE}) == len(CATALOGUE)
    by_id = {r.id: r for r in CATALOGUE}
    assert by_id[1].mode == 0 and by_id[20].mode == 0, "combo and plays medals are osu! only"
    assert by_id[31].mode == 1 and by_id[13].mode == 2 and by_id[46].mode == 3, "hits are per ruleset"
    assert {119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 131} <= set(by_id), "mod introductions"
    # skill 4x16 + osu 9/10 stars 4, combo 4, plays 4, hits 12, rank 16, mod introductions 11, hush-hush 3
    assert len(CATALOGUE) == 4 * 16 + 4 + 4 + 4 + 12 + 16 + 11 + 3


@pytest.mark.asyncio
async def test_combo_medals_are_osu_only_and_awarded_once(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        first = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=760)
        second = await make_score(db, player, beatmap, 810_000, 101.0, max_combo=900)
        taiko = await make_score(db, player, beatmap, 820_000, 102.0, max_combo=3000, ruleset=TAIKO)
        await db.commit()

        result = await play(db, first.id)
        assert only(result, 1, 3, 4, 5) == (1, 3), "500 and 750 combo, not 1000"
        assert only(result, *OSU_RANK) == OSU_RANK, "the only ranked player is rank 1"

        assert (await play(db, second.id)).medals == (), "a medal is only awarded the first time"
        assert only(await play(db, taiko.id), 1, 3, 4, 5) == (), "a taiko combo is not an osu! combo"
        assert await owned(db, player.id) >= {1, 3}
        assert (await db.execute(select(Medal.id).where(Medal.id == 1))).scalar_one() == 1, "catalogue was filled"


@pytest.mark.asyncio
async def test_relax_and_autopilot_earn_medals_but_autoplay_and_failures_do_not(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        relax = await make_score(
            db, player, beatmap, 900_000, 300.0, max_combo=760, mods=[{"acronym": "RX"}], variant=int(Variant.RELAX),
        )
        auto = await make_score(
            db, player, beatmap, 900_000, 300.0, max_combo=2500, mods=[{"acronym": "AP"}],
            variant=int(Variant.AUTOPILOT),
        )
        autoplay = await make_score(
            db, player, beatmap, 950_000, 310.0, max_combo=2500, mods=[{"acronym": "AT"}],
        )
        failed = await make_score(db, player, beatmap, 150_000, None, rank="F", passed=False, max_combo=2500)
        await db.commit()

        assert only(await play(db, relax.id), 1, 3, 4, 5) == (1, 3)
        assert only(await play(db, auto.id), 1, 3, 4, 5) == (4, 5)
        assert (await play(db, autoplay.id)).medals == (), "autoplay earns nothing (and the combo medals are owned)"
        assert (await play(db, failed.id)).medals == ()


@pytest.mark.asyncio
async def test_play_count_and_hit_medals_read_the_boards_statistics(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        stats = await get_stats(db, player.id)
        stats.play_count = 4_999
        db.add(UserStatistics(user_id=player.id, mode=GameMode.TAIKO, total_hits=29_950))
        await db.flush()
        osu = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=10)
        taiko = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=10, ruleset=TAIKO)
        await db.commit()

        assert only(await play(db, osu.id), 20, 31, 13, 46) == (20,), "the 5,000th osu! play"
        assert only(await play(db, taiko.id), 20, 31, 13, 46) == (31,), "30,000 taiko hits, not an osu! play"


@pytest.mark.asyncio
async def test_rank_medals_are_per_ruleset(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        taiko = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=10, ruleset=TAIKO)
        await db.commit()

        result = await play(db, taiko.id)

    assert only(result, *OSU_RANK) == (), "osu! rank medals need an osu! rank"
    assert only(result, 1101, 1102, 1103, 1104) == (1101, 1102, 1103, 1104)


@pytest.mark.asyncio
async def test_skill_medals_use_the_stars_with_the_plays_mods(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        beatmap.difficulty_rating = 5.4
        beatmap.max_combo = 300
        player = await make_user(db, "player")
        plain = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=250, mods=[{"acronym": "HD"}])
        full = await make_score(db, player, beatmap, 850_000, 110.0, max_combo=300)
        doubled = await make_score(
            db, player, beatmap, 860_000, 120.0, max_combo=250, mods=[{"acronym": "DT"}], star_rating=7.3,
        )
        doubled_fc = await make_score(
            db, player, beatmap, 870_000, 121.0, max_combo=300, mods=[{"acronym": "DT"}], star_rating=7.3,
        )
        await db.commit()

        assert only(await play(db, plain.id), 59, 67) == (59,), "5★ pass: HD keeps the stars, no full combo"
        assert only(await play(db, full.id), 59, 67) == (67,), "5★ full combo"
        assert only(await play(db, doubled.id), 59, 60, 61, 62) == (61,), "DT made it 7.3★: the 7★ pass"
        assert only(await play(db, doubled_fc.id), 69) == (69,), "and the 7★ full combo"


@pytest.mark.asyncio
async def test_skill_medals_skip_nofail_passes_and_unknown_stars(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        beatmap.difficulty_rating = 5.4
        beatmap.max_combo = 300
        player = await make_user(db, "player")
        nofail = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=250, mods=[{"acronym": "NF"}])
        nofail_fc = await make_score(db, player, beatmap, 810_000, 100.0, max_combo=300, mods=[{"acronym": "NF"}])
        unknown = await make_score(db, player, beatmap, 820_000, 100.0, max_combo=250, mods=[{"acronym": "HR"}])
        await db.commit()

        assert only(await play(db, nofail.id), 59, 67) == (), "NoFail never passes a skill medal"
        assert only(await play(db, nofail_fc.id), 59, 67) == (67,), "but it can full combo"
        assert only(await play(db, unknown.id), 59, 60, 61, 62) == (), "HR with no stored stars: unknown"


@pytest.mark.asyncio
async def test_mod_introduction_medals(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        hard_rock = await make_score(db, player, beatmap, 800_000, 100.0, max_combo=10, mods=[{"acronym": "HR"}])
        failed_hd = await make_score(
            db, player, beatmap, 150_000, None, rank="F", passed=False, max_combo=10, mods=[{"acronym": "HD"}],
        )
        await db.commit()

        assert only(await play(db, hard_rock.id), 121, 124) == (121,)
        assert only(await play(db, failed_hd.id), 124) == (), "the play has to be passed"


@pytest.mark.asyncio
async def test_the_hush_hush_medals_akatsuki_implements(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        beatmap.max_combo = 100
        player = await make_user(db, "player")
        consolation = await make_score(db, player, beatmap, 150_000, 5.0, rank="D", max_combo=50)
        stumbler = await make_score(db, player, beatmap, 820_001, 40.0, accuracy=0.80, max_combo=100)
        jackpot = await make_score(db, player, beatmap, 777_777, 40.0, max_combo=50)
        await db.commit()

        assert only(await play(db, consolation.id), 1201, 1202, 1203) == (1201,)
        assert only(await play(db, stumbler.id), 1201, 1202, 1203) == (1202,)
        assert only(await play(db, jackpot.id), 1201, 1202, 1203) == (1203,)


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
