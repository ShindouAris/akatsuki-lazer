"""Tests for the daily rank history (app.services.rank_history) and the profile rank graph."""

from collections.abc import AsyncGenerator
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import Base
from app.core.security import create_token_pair
from app.models import multiplayer as _multiplayer_models  # noqa: F401  (registers FK targets)
from app.models.beatmap import BeatmapStatus
from app.models.user import GameMode
from app.models.user import UserRankHistory
from app.services.leaderboard import Leaderboard
from app.services.leaderboard import set_leaderboard
from app.services.rank_history import HISTORY_LENGTH
from app.services.rank_history import build_history
from app.services.rank_history import get_rank_history
from app.services.rank_history import record_rank
from app.services.rank_history import seconds_until_snapshot
from app.services.rank_history import snapshot_ranks
from app.services.rank_history import today
from app.services.score_stats import process_score
from tests.test_leaderboard import FakeRedis
from tests.test_live_ranks import add_player
from tests.test_score_stats import make_beatmap
from tests.test_score_stats import make_score
from tests.test_score_stats import make_user

OSU = GameMode.OSU
TODAY = date(2026, 6, 15)


def days_ago(n: int, base: date = TODAY) -> date:
    return base - timedelta(days=n)


@pytest_asyncio.fixture
async def session_maker(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rank_history.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def stored_ranks(db: AsyncSession, user_id: int, mode: GameMode = OSU) -> dict[date, int]:
    result = await db.execute(
        select(UserRankHistory.day, UserRankHistory.rank).where(
            UserRankHistory.user_id == user_id,
            UserRankHistory.mode == int(mode),
        ),
    )
    return {day: rank for day, rank in result.all()}


# --- pure logic -------------------------------------------------------------------------------------


def test_history_has_90_values_and_ends_with_the_current_rank() -> None:
    history = build_history([(days_ago(1), 50)], current_rank=42, today_=TODAY)

    assert len(history) == HISTORY_LENGTH == 90
    assert history[-1] == 42
    assert history[-2] == 50  # yesterday


def test_history_is_zero_before_the_first_known_rank() -> None:
    history = build_history([(days_ago(3), 80)], current_rank=70, today_=TODAY)

    assert history[:86] == [0] * 86
    assert history[86:] == [80, 80, 80, 70]  # 3 days ago .. yesterday repeat the last known, then today


def test_history_repeats_the_previous_rank_on_days_without_a_row() -> None:
    rows = [(days_ago(10), 100), (days_ago(4), 90), (days_ago(1), 95)]

    history = build_history(rows, current_rank=95, today_=TODAY)

    assert history[-11:-1] == [100, 100, 100, 100, 100, 100, 90, 90, 90, 95]
    assert history[-1] == 95


def test_history_is_seeded_by_a_row_older_than_the_window() -> None:
    history = build_history([(days_ago(200), 300)], current_rank=250, today_=TODAY)

    assert history[0] == 300
    assert set(history[:-1]) == {300}


def test_history_without_a_current_rank_ends_with_zero() -> None:
    assert build_history([(days_ago(1), 10)], current_rank=None, today_=TODAY)[-1] == 0


def test_snapshot_schedule() -> None:
    noon = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    assert seconds_until_snapshot(noon) == pytest.approx(11 * 3600 + 59 * 60 + 30)

    just_after = datetime(2026, 6, 15, 23, 59, 31, tzinfo=UTC)
    assert seconds_until_snapshot(just_after) == pytest.approx(24 * 3600 - 1)  # tomorrow, not "now"

    exactly = datetime(2026, 6, 15, 23, 59, 30, tzinfo=UTC)
    assert seconds_until_snapshot(exactly) == pytest.approx(24 * 3600)


# --- database ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_rank_keeps_the_latest_rank_of_the_day(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        await db.commit()

        await record_rank(db, user.id, OSU, 500)
        await record_rank(db, user.id, OSU, 480)
        await record_rank(db, user.id, OSU, 7, day=days_ago(1, today()))
        await db.commit()

        ranks = await stored_ranks(db, user.id)

    assert ranks == {today(): 480, days_ago(1, today()): 7}


@pytest.mark.asyncio
async def test_get_rank_history_ignores_today_and_uses_the_current_rank(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        await db.commit()
        await record_rank(db, user.id, OSU, 999)  # today's stored row must not be used
        await record_rank(db, user.id, OSU, 60, day=days_ago(2, today()))
        await db.commit()

        history = await get_rank_history(db, user.id, OSU, current_rank=55)
        other_mode = await get_rank_history(db, user.id, GameMode.MANIA, current_rank=None)

    assert len(history) == 90
    assert history[-1] == 55
    assert history[-3:-1] == [60, 60]
    assert set(other_mode) == {0}


@pytest.mark.asyncio
async def test_processing_a_score_records_todays_rank(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        regular = await make_user(db, "regular")
        restricted = await make_user(db, "restricted")
        restricted.is_restricted = True
        bot = await make_user(db, "bot")
        bot.is_bot = True
        loved_only = await make_user(db, "loved_only")
        loved = await make_beatmap(db, BeatmapStatus.LOVED)
        scores = [
            await make_score(db, regular, beatmap, 900_000, 100.0),
            await make_score(db, restricted, beatmap, 900_000, 100.0),
            await make_score(db, bot, beatmap, 900_000, 100.0),
            await make_score(db, loved_only, loved, 900_000, 100.0),
        ]
        await db.commit()

        for score in scores:
            await process_score(db, score.id)
        await db.commit()

        recorded = {
            name: await stored_ranks(db, user.id)
            for name, user in (
                ("regular", regular),
                ("restricted", restricted),
                ("bot", bot),
                ("loved_only", loved_only),
            )
        }

    assert recorded["regular"] == {today(): 1}
    assert recorded["restricted"] == {}
    assert recorded["bot"] == {}
    assert recorded["loved_only"] == {}  # earns no pp, so there is no rank to chart


@pytest.mark.asyncio
async def test_snapshot_stores_everyones_rank_with_ties_sharing_one(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        users = {
            "first": await add_player(db, "first", pp=300.0, stored_rank=None),
            "tied_a": await add_player(db, "tied_a", pp=200.0, stored_rank=None),
            "tied_b": await add_player(db, "tied_b", pp=200.0, stored_rank=None),
            "last": await add_player(db, "last", pp=50.0, stored_rank=None),
            "idle": await add_player(db, "idle", pp=0.0, stored_rank=None),
            "cheater": await add_player(db, "cheater", pp=900.0, stored_rank=None, restricted=True),
        }
        await db.commit()
        # A rank recorded earlier today is replaced by the end-of-day value.
        await record_rank(db, users["last"].id, OSU, 1)
        await db.commit()

        written = await snapshot_ranks(db, today())
        await db.commit()
        ranks = {name: await stored_ranks(db, user.id) for name, user in users.items()}

    assert written == 4
    assert ranks["first"] == {today(): 1}
    assert ranks["tied_a"] == ranks["tied_b"] == {today(): 2}
    assert ranks["last"] == {today(): 4}  # the two tied players both count as ahead of it
    assert ranks["idle"] == {}
    assert ranks["cheater"] == {}


@pytest.mark.asyncio
async def test_snapshot_is_idempotent_and_prunes_old_rows(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        player = await add_player(db, "player", pp=100.0, stored_rank=None)
        await db.commit()
        await record_rank(db, player.id, OSU, 5, day=days_ago(89, today()))  # oldest day still kept
        await record_rank(db, player.id, OSU, 6, day=days_ago(90, today()))  # outside the window
        await db.commit()

        await snapshot_ranks(db, today())
        await snapshot_ranks(db, today())
        await db.commit()
        ranks = await stored_ranks(db, player.id)

    assert ranks == {days_ago(89, today()): 5, today(): 1}


# --- profile ------------------------------------------------------------------------------------------


@pytest.fixture
def _reset_leaderboard():  # noqa: ANN202
    set_leaderboard(None)
    yield
    set_leaderboard(None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_leaderboard")
async def test_profile_returns_the_rank_graph(client: AsyncClient, db_session: AsyncSession) -> None:
    strong = await add_player(db_session, "strong", pp=200.0, stored_rank=1)
    weak = await add_player(db_session, "weak", pp=100.0, stored_rank=2)
    await db_session.commit()
    for days, rank in [(3, 5), (2, 4), (1, 3)]:
        await record_rank(db_session, weak.id, OSU, rank, day=days_ago(days, today()))
    await db_session.commit()
    board = Leaderboard(FakeRedis())
    await board.rebuild(db_session, OSU)
    set_leaderboard(board)

    headers = {"Authorization": f"Bearer {create_token_pair(weak.id, ['*']).access_token}"}
    paths = [f"/api/v2/users/{weak.id}/osu", f"/api/v2/users/{weak.id}", "/api/v2/me", "/api/v2/me/osu"]
    for path in paths:
        stats = (await client.get(path, headers=headers)).json()["statistics"]
        history = stats["rank_history"]

        assert history["mode"] == "osu", path
        assert len(history["data"]) == 90, path
        assert history["data"][-4:] == [5, 4, 3, 2], path  # last point is the live rank (2nd place)
        assert history["data"][0] == 0, path
        assert stats["global_rank"] == 2, path

    strong_stats = (await client.get(f"/api/v2/users/{strong.id}/osu")).json()["statistics"]
    assert strong_stats["rank_history"]["data"][-1] == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_leaderboard")
async def test_unranked_profiles_have_no_rank_graph(client: AsyncClient, db_session: AsyncSession) -> None:
    unranked = await add_player(db_session, "unranked", pp=0.0, stored_rank=None)
    await db_session.commit()

    stats = (await client.get(f"/api/v2/users/{unranked.id}/osu")).json()["statistics"]

    assert stats["is_ranked"] is False
    assert stats["rank_history"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_leaderboard")
async def test_rank_graph_works_without_a_leaderboard(client: AsyncClient, db_session: AsyncSession) -> None:
    player = await add_player(db_session, "player", pp=100.0, stored_rank=9)
    await db_session.commit()
    await record_rank(db_session, player.id, OSU, 12, day=days_ago(1, today()))
    await db_session.commit()

    stats = (await client.get(f"/api/v2/users/{player.id}/osu")).json()["statistics"]

    assert stats["rank_history"]["data"][-2:] == [12, 9]


# --- user list ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_leaderboard")
async def test_user_list_has_live_ranks_and_graphs(client: AsyncClient, db_session: AsyncSession) -> None:
    strong = await add_player(db_session, "strong", pp=200.0, stored_rank=1)
    weak = await add_player(db_session, "weak", pp=100.0, stored_rank=1)  # stale: strong overtook them
    unranked = await add_player(db_session, "unranked", pp=0.0, stored_rank=None)
    cheater = await add_player(db_session, "cheater", pp=900.0, stored_rank=7, restricted=True)
    await db_session.commit()
    await record_rank(db_session, weak.id, OSU, 3, day=days_ago(1, today()))
    await record_rank(db_session, strong.id, OSU, 1, day=days_ago(1, today()))
    await db_session.commit()
    board = Leaderboard(FakeRedis())
    await board.rebuild(db_session, OSU)
    set_leaderboard(board)

    ids = [weak.id, unranked.id, strong.id, cheater.id]
    response = await client.get("/api/v2/users/", params={"ids[]": ids})
    users = response.json()["users"]

    assert [u["username"] for u in users] == ["weak", "unranked", "strong", "cheater"]
    by_name = {u["username"]: u["statistics"] for u in users}

    assert by_name["weak"]["global_rank"] == 2
    assert by_name["weak"]["rank_history"]["data"][-2:] == [3, 2]
    assert by_name["strong"]["rank_history"]["data"][-2:] == [1, 1]
    assert by_name["unranked"]["rank_history"] is None
    assert by_name["cheater"]["global_rank"] == 7  # restricted: stored rank, no graph
    assert by_name["cheater"]["rank_history"]["data"] == []  # the stored-rank placeholder, no graph


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_leaderboard")
async def test_list_and_single_profile_agree(client: AsyncClient, db_session: AsyncSession) -> None:
    player = await add_player(db_session, "player", pp=100.0, stored_rank=4)
    await db_session.commit()
    for days, rank in [(200, 80), (30, 9), (5, 6)]:
        await record_rank(db_session, player.id, OSU, rank, day=days_ago(days, today()))
    await db_session.commit()

    single = (await client.get(f"/api/v2/users/{player.id}/osu")).json()["statistics"]
    listing = (await client.get("/api/v2/users/", params={"ids[]": [player.id]})).json()
    listed = listing["users"][0]["statistics"]

    assert listed["rank_history"] == single["rank_history"]
    assert single["rank_history"]["data"][0] == 80  # seeded by the row older than the window
    assert listed["global_rank"] == single["global_rank"] == 4


@pytest.mark.asyncio
async def test_histories_for_many_players_use_a_single_query(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import event

    from app.services.rank_history import get_rank_histories

    async with session_maker() as db:
        users = [await make_user(db, f"player{i}") for i in range(6)]
        await db.commit()
        for user in users:
            await record_rank(db, user.id, OSU, 10, day=days_ago(1, today()))
        await db.commit()

        statements: list[str] = []
        sync_engine = db.get_bind()
        event.listen(sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        histories = await get_rank_histories(db, {(u.id, OSU): 5 for u in users})

    assert len(statements) == 1
    assert all(h[-2:] == [10, 5] for h in histories.values())
