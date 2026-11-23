"""Profiles show current leaderboard ranks instead of the (possibly stale) stored ones."""

from collections.abc import Iterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_token_pair
from app.core.security import get_password_hash
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics
from app.services.leaderboard import Leaderboard
from app.services.leaderboard import set_leaderboard
from tests.test_leaderboard import FakeRedis


@pytest.fixture(autouse=True)
def _reset_leaderboard() -> Iterator[None]:
    set_leaderboard(None)
    yield
    set_leaderboard(None)


async def add_player(
    db: AsyncSession,
    name: str,
    pp: float,
    stored_rank: int | None,
    country: str = "VN",
    restricted: bool = False,
) -> User:
    user = User(
        username=name,
        email=f"{name}@example.com",
        password_hash=get_password_hash("password"),
        country_acronym=country,
        is_restricted=restricted,
    )
    db.add(user)
    await db.flush()
    db.add(
        UserStatistics(
            user_id=user.id,
            mode=GameMode.OSU,
            pp=pp,
            global_rank=stored_rank,
            country_rank=stored_rank,
        ),
    )
    return user


@pytest_asyncio.fixture
async def players(db_session: AsyncSession) -> dict[str, User]:
    """`strong` overtook `weak`, but weak's stored rank was written back when they were still first."""
    users = {
        "strong": await add_player(db_session, "strong", pp=200.0, stored_rank=1),
        "weak": await add_player(db_session, "weak", pp=100.0, stored_rank=1),
        "unranked": await add_player(db_session, "unranked", pp=0.0, stored_rank=None),
    }
    await db_session.commit()
    return users


async def build_leaderboard(db: AsyncSession, client: FakeRedis | None = None) -> Leaderboard:
    board = Leaderboard(client or FakeRedis())
    await board.rebuild(db, GameMode.OSU)
    set_leaderboard(board)
    return board


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token_pair(user.id, ['*']).access_token}"}


@pytest.mark.asyncio
async def test_user_profile_shows_live_rank(
    client: AsyncClient,
    db_session: AsyncSession,
    players: dict[str, User],
) -> None:
    await build_leaderboard(db_session)

    for path in (f"/api/v2/users/{players['weak'].id}/osu", f"/api/v2/users/{players['weak'].id}"):
        stats = (await client.get(path)).json()["statistics"]
        assert (stats["global_rank"], stats["country_rank"]) == (2, 2), path

    stats = (await client.get(f"/api/v2/users/{players['strong'].id}/osu")).json()["statistics"]
    assert (stats["global_rank"], stats["country_rank"]) == (1, 1)


@pytest.mark.asyncio
async def test_me_shows_live_rank(
    client: AsyncClient,
    db_session: AsyncSession,
    players: dict[str, User],
) -> None:
    await build_leaderboard(db_session)

    for path in ("/api/v2/me", "/api/v2/me/osu"):
        response = await client.get(path, headers=auth(players["weak"]))
        stats = response.json()["statistics"]
        assert (stats["global_rank"], stats["country_rank"]) == (2, 2), path


@pytest.mark.asyncio
async def test_rank_follows_the_leaderboard_as_others_overtake(
    client: AsyncClient,
    db_session: AsyncSession,
    players: dict[str, User],
) -> None:
    from app.services.leaderboard import LeaderboardEntry

    board = await build_leaderboard(db_session)
    await board.apply(LeaderboardEntry(GameMode.OSU, 9999, "VN", 150.0, ranked=True))

    stats = (await client.get(f"/api/v2/users/{players['weak'].id}/osu")).json()["statistics"]

    assert (stats["global_rank"], stats["country_rank"]) == (3, 3)


@pytest.mark.asyncio
async def test_unranked_players_stay_unranked(
    client: AsyncClient,
    db_session: AsyncSession,
    players: dict[str, User],
) -> None:
    await build_leaderboard(db_session)

    stats = (await client.get(f"/api/v2/users/{players['unranked'].id}/osu")).json()["statistics"]

    assert stats["global_rank"] is None
    assert stats["is_ranked"] is False


@pytest.mark.asyncio
async def test_stored_rank_is_used_without_a_leaderboard(
    client: AsyncClient,
    players: dict[str, User],
) -> None:
    stats = (await client.get(f"/api/v2/users/{players['weak'].id}/osu")).json()["statistics"]

    assert (stats["global_rank"], stats["country_rank"]) == (1, 1)


@pytest.mark.asyncio
async def test_stored_rank_is_used_when_redis_is_down(
    client: AsyncClient,
    db_session: AsyncSession,
    players: dict[str, User],
) -> None:
    redis = FakeRedis()
    await build_leaderboard(db_session, redis)
    redis.fail = True

    stats = (await client.get(f"/api/v2/users/{players['weak'].id}/osu")).json()["statistics"]

    assert (stats["global_rank"], stats["country_rank"]) == (1, 1)


@pytest.mark.asyncio
async def test_stored_rank_is_used_when_the_index_is_not_built(
    client: AsyncClient,
    players: dict[str, User],
) -> None:
    set_leaderboard(Leaderboard(FakeRedis()))  # never rebuilt, so `ready` marker is missing

    stats = (await client.get(f"/api/v2/users/{players['weak'].id}/osu")).json()["statistics"]

    assert (stats["global_rank"], stats["country_rank"]) == (1, 1)


@pytest.mark.asyncio
async def test_restricted_players_keep_their_stored_rank(
    client: AsyncClient,
    db_session: AsyncSession,
    players: dict[str, User],
) -> None:
    restricted = await add_player(db_session, "cheater", pp=500.0, stored_rank=7, restricted=True)
    await db_session.commit()
    await build_leaderboard(db_session)

    stats = (await client.get(f"/api/v2/users/{restricted.id}/osu")).json()["statistics"]

    assert stats["global_rank"] == 7
