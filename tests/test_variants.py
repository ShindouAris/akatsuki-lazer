"""Vanilla / Relax / Autopilot boards: classification, statistics, leaderboards and rankings."""

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import Base
from app.models import multiplayer as _multiplayer_models  # noqa: F401  (registers FK targets)
from app.models.beatmap import BeatmapStatus
from app.models.user import GameMode
from app.models.user import UserStatistics
from app.services.leaderboard import Leaderboard
from app.services.score_stats import process_score
from app.services.score_stats import rebuild_user_statistics
from app.services.variant import Variant
from app.services.variant import board_for_request
from app.services.variant import parse_variant
from app.services.variant import supports_variant
from app.services.variant import variant_for
from app.services.variant import variant_for_mods
from tests.test_leaderboard import FakeRedis
from tests.test_score_stats import make_beatmap
from tests.test_score_stats import make_score
from tests.test_score_stats import make_user

OSU, TAIKO, MANIA = GameMode.OSU, GameMode.TAIKO, GameMode.MANIA


@pytest_asyncio.fixture
async def session_maker(tmp_path: Path):  # noqa: ANN201
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'variants.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def stats_of(db: AsyncSession, user_id: int, variant: Variant) -> UserStatistics | None:
    result = await db.execute(
        select(UserStatistics).where(
            UserStatistics.user_id == user_id,
            UserStatistics.mode == OSU,
            UserStatistics.variant == int(variant),
        ),
    )
    return result.scalar_one_or_none()


# --- classification --------------------------------------------------------------------------------------


def test_rx_goes_to_the_relax_board_and_ap_to_the_autopilot_board() -> None:
    assert variant_for(["HD", "RX"], 0) == Variant.RELAX
    assert variant_for(["ap"], 0) == Variant.AUTOPILOT
    assert variant_for(["HD", "DT"], 0) == Variant.VANILLA
    assert variant_for([], 0) == Variant.VANILLA


def test_boards_follow_what_each_ruleset_supports() -> None:
    assert variant_for(["RX"], int(TAIKO)) == Variant.RELAX
    assert variant_for(["RX"], int(GameMode.CATCH)) == Variant.RELAX
    assert variant_for(["AP"], int(TAIKO)) == Variant.VANILLA, "autopilot exists for osu! only"
    assert variant_for(["RX"], int(MANIA)) == Variant.VANILLA, "mania has no relax board"
    assert variant_for(["RX"], 99) == Variant.VANILLA, "an unknown ruleset never raises"
    assert supports_variant(OSU, Variant.AUTOPILOT) and not supports_variant(TAIKO, Variant.AUTOPILOT)


def test_lazer_mod_objects_are_classified_too() -> None:
    mods = [{"acronym": "HD", "settings": {}}, {"acronym": "RX"}, "junk"]
    assert variant_for_mods(mods, 0) == Variant.RELAX
    assert variant_for_mods(None, 0) == Variant.VANILLA


def test_a_leaderboard_request_names_its_board() -> None:
    assert parse_variant("rx") == Variant.RELAX and parse_variant("AutoPilot") == Variant.AUTOPILOT
    assert parse_variant(None) == Variant.VANILLA and parse_variant("2") == Variant.AUTOPILOT
    assert board_for_request(None, None) == Variant.VANILLA
    assert board_for_request(None, "HDRX") == Variant.RELAX
    assert board_for_request(None, "AP,HD") == Variant.AUTOPILOT
    assert board_for_request("vanilla", "RX") == Variant.VANILLA, "an explicit variant wins"


def test_a_play_above_its_boards_pp_cap_awards_no_pp() -> None:
    from app.core.config import Settings
    from app.services.pp import apply_pp_cap

    settings = Settings(pp_cap_relax=1000.0, pp_cap_autopilot=600.0)  # vanilla stays uncapped

    assert apply_pp_cap(999.0, Variant.RELAX, settings) == 999.0
    assert apply_pp_cap(1000.0, Variant.RELAX, settings) == 1000.0, "the cap itself is still allowed"
    assert apply_pp_cap(1000.5, Variant.RELAX, settings) is None
    assert apply_pp_cap(700.0, Variant.AUTOPILOT, settings) is None
    assert apply_pp_cap(5000.0, Variant.VANILLA, settings) == 5000.0, "0 means no cap"
    assert apply_pp_cap(None, Variant.RELAX, settings) is None


def test_relax_and_autopilot_pp_is_not_cut_like_osus_own_formula(tmp_path: Path) -> None:
    from app.services.pp import PPCalculationParams
    from app.services.pp import PPService
    from app.services.pp import pp_mods_bitwise

    lines = [
        "osu file format v14", "", "[General]", "Mode: 0", "", "[Difficulty]", "HPDrainRate:5", "CircleSize:4",
        "OverallDifficulty:8", "ApproachRate:9", "SliderMultiplier:1.4", "SliderTickRate:1", "",
        "[TimingPoints]", "0,400,4,2,0,60,1,0", "", "[HitObjects]",
    ]
    lines += [f"{60 + (i % 7) * 60},{80 + (i % 5) * 50},{1000 + i * 150},1,0,0:0:0:0:" for i in range(120)]
    path = tmp_path / "map.osu"
    path.write_text("\n".join(lines), encoding="utf-8")

    def pp(mods: list[dict], board: Variant, unnerfed: bool) -> float:
        bits = pp_mods_bitwise(mods, board, unnerfed)
        params = PPCalculationParams(mode=OSU, mods=bits, accuracy=98.5, nmiss=0)
        return PPService().calculate_pp(str(path), params)["pp"]

    rx, hd_dt_rx = [{"acronym": "RX"}], [{"acronym": "HD"}, {"acronym": "DT"}, {"acronym": "RX"}]
    nomod = pp([], Variant.VANILLA, True)

    assert pp(rx, Variant.RELAX, False) < nomod * 0.6, "osu!'s formula cuts relax pp a lot"
    assert pp(rx, Variant.RELAX, True) == pytest.approx(nomod), "unnerfed: worth what the play is without RX"
    assert pp([{"acronym": "AP"}], Variant.AUTOPILOT, True) == pytest.approx(nomod)
    assert pp(hd_dt_rx, Variant.RELAX, True) > nomod, "DT and HD still count"
    assert pp(rx, Variant.VANILLA, True) < nomod * 0.6, "a vanilla board play is never touched"


# --- statistics ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_board_has_its_own_statistics(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        vanilla = await make_score(db, player, beatmap, 800_000, 100.0)
        relax = await make_score(db, player, beatmap, 900_000, 300.0, variant=int(Variant.RELAX))
        auto = await make_score(db, player, beatmap, 850_000, 200.0, variant=int(Variant.AUTOPILOT))
        await db.commit()

        for score in (vanilla, relax, auto):
            await process_score(db, score.id)
            await db.commit()

        van = await stats_of(db, player.id, Variant.VANILLA)
        rx = await stats_of(db, player.id, Variant.RELAX)
        ap = await stats_of(db, player.id, Variant.AUTOPILOT)

    assert (van.play_count, rx.play_count, ap.play_count) == (1, 1, 1)
    assert (van.ranked_score, rx.ranked_score, ap.ranked_score) == (800_000, 900_000, 850_000)
    assert van.pp < ap.pp < rx.pp, "pp is summed per board, not across boards"
    assert van.pp == pytest.approx(100.0 + 416.667 * 0.005, abs=0.01)


@pytest.mark.asyncio
async def test_the_boards_own_mod_does_not_block_the_max_combo(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        relax = await make_score(
            db, player, beatmap, 900_000, 300.0, max_combo=700, mods=[{"acronym": "RX"}],
            variant=int(Variant.RELAX),
        )
        # An autoplay-ish mod still blocks it on every board.
        cheated = await make_score(
            db, player, beatmap, 910_000, 310.0, max_combo=900, mods=[{"acronym": "RX"}, {"acronym": "AT"}],
            variant=int(Variant.RELAX),
        )
        await db.commit()
        for score in (relax, cheated):
            await process_score(db, score.id)
            await db.commit()

        rx = await stats_of(db, player.id, Variant.RELAX)

    assert rx.maximum_combo == 700


@pytest.mark.asyncio
async def test_rebuild_only_touches_the_asked_board(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        player = await make_user(db, "player")
        await make_score(db, player, beatmap, 800_000, 100.0)
        await make_score(db, player, beatmap, 900_000, 300.0, variant=int(Variant.RELAX))
        await db.commit()

        replayed = await rebuild_user_statistics(db, player.id, OSU, Variant.RELAX)
        await db.commit()

        assert replayed == 1
        assert (await stats_of(db, player.id, Variant.RELAX)).ranked_score == 900_000
        # The vanilla row is the one created with the user and was not recomputed.
        assert (await stats_of(db, player.id, Variant.VANILLA)).ranked_score == 0


# --- leaderboards ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ranks_are_counted_per_board(session_maker: async_sessionmaker[AsyncSession]) -> None:
    client = FakeRedis()
    board = Leaderboard(client)

    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        top = await make_user(db, "top")
        other = await make_user(db, "other")
        # `top` leads on vanilla, `other` leads on relax.
        scores = [
            await make_score(db, top, beatmap, 900_000, 500.0),
            await make_score(db, other, beatmap, 800_000, 100.0),
            await make_score(db, top, beatmap, 700_000, 50.0, variant=int(Variant.RELAX)),
            await make_score(db, other, beatmap, 950_000, 400.0, variant=int(Variant.RELAX)),
        ]
        await db.commit()
        await board.ensure_built(db)

        for score in scores:
            result = await process_score(db, score.id, board)
            await db.commit()
            await board.apply(result.leaderboard_entry)

        top_rx = await stats_of(db, top.id, Variant.RELAX)
        other_rx = await stats_of(db, other.id, Variant.RELAX)
        top_van = await stats_of(db, top.id, Variant.VANILLA)

    assert top_van.global_rank == 1
    # Stored ranks are refreshed when their owner plays; the live board has the final order.
    assert await board.ranks(OSU, other_rx.pp, "VN", Variant.RELAX) == (1, 1)
    assert await board.ranks(OSU, top_rx.pp, "VN", Variant.RELAX) == (2, 2)
    # Only `top` (about 502 pp) is ahead of 402 pp on the vanilla board; the relax scores are not counted there.
    assert await board.ranks(OSU, other_rx.pp, "VN", Variant.VANILLA) == (2, 2)
    assert set(client.zsets["leaderboard:pp:v1:0"]) == {str(top.id), str(other.id)}
    assert str(other.id) in client.zsets["leaderboard:pp:v0:0"]


@pytest.mark.asyncio
async def test_a_board_that_is_not_built_does_not_answer(session_maker: async_sessionmaker[AsyncSession]) -> None:
    board = Leaderboard(FakeRedis())
    async with session_maker() as db:
        await board.rebuild(db, OSU, Variant.VANILLA)

    assert await board.is_ready(OSU, Variant.VANILLA)
    assert not await board.is_ready(OSU, Variant.RELAX)
