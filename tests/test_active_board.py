"""A player stays on the board of their last RX / AP submit for 30 minutes."""

import json

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_token_pair
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import UserStatistics
from app.services.active_board import ActiveBoard
from app.services.active_board import get_active_board
from app.services.active_board import set_active_board
from app.services.variant import Variant
from tests.test_internal_multiplayer import make_beatmap
from tests.test_internal_multiplayer import make_user


class FakeRedis:
    """Just get / set(ex) / delete."""

    def __init__(self, fail: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.fail = fail
        self.ttl: int | None = None

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail:
            raise ConnectionError("down")
        self.data[key] = value
        self.ttl = ex

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


@pytest.mark.asyncio
async def test_the_board_lasts_30_minutes_and_a_new_submit_renews_it() -> None:
    now = [0.0]
    board = ActiveBoard(ttl_seconds=1800, clock=lambda: now[0])

    assert await board.get(1) == Variant.VANILLA
    await board.set(1, Variant.RELAX)
    now[0] = 1799
    assert await board.get(1) == Variant.RELAX

    now[0] = 1799
    await board.set(1, Variant.RELAX)  # another RX submit renews the timer
    now[0] = 3000
    assert await board.get(1) == Variant.RELAX
    now[0] = 1799 + 1800
    assert await board.get(1) == Variant.VANILLA, "30 quiet minutes release the player"


@pytest.mark.asyncio
async def test_a_vanilla_submit_takes_the_player_back_and_boards_are_per_player() -> None:
    board = ActiveBoard()
    await board.set(1, Variant.AUTOPILOT)
    await board.set(2, Variant.RELAX)

    await board.set(1, Variant.VANILLA)

    assert await board.get(1) == Variant.VANILLA
    assert await board.get(2) == Variant.RELAX


@pytest.mark.asyncio
async def test_redis_holds_the_state_with_a_30_minute_expiry_and_a_failure_falls_back() -> None:
    redis = FakeRedis()
    board = ActiveBoard(redis)
    await board.set(7, Variant.RELAX)
    assert redis.data == {"active_board:7": "1"} and redis.ttl == 1800
    assert await ActiveBoard(redis).get(7) == Variant.RELAX, "another process sees it"

    redis.fail = True
    assert await board.get(7) == Variant.RELAX, "Redis down: the local copy answers"


@pytest.mark.asyncio
async def test_the_game_leaderboard_and_profile_follow_the_players_board(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    beatmap = await make_beatmap(db_session, checksum="sticky")
    player = await make_user(db_session, "stickyplayer")
    other = await make_user(db_session, "stickyother")
    data = json.dumps({"mods": [], "statistics": {"great": 10}, "maximum_statistics": {"great": 10}})
    for user, total, variant in ((player, 500_000, 0), (other, 400_000, 0), (player, 900_000, 1)):
        db_session.add(Score(
            user_id=user.id, beatmap_id=beatmap.id, ruleset_id=0, variant=variant, data=data, total_score=total,
            accuracy=99.0, pp=1.0, max_combo=10, rank="A", passed=True, ranked=True,
        ))
    db_session.add(UserStatistics(user_id=player.id, mode=GameMode.OSU, variant=0, pp=100.0))
    db_session.add(UserStatistics(user_id=player.id, mode=GameMode.OSU, variant=1, pp=900.0))
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token_pair(player.id, ['*']).access_token}"}

    async def totals(**params: str) -> list[int]:
        response = await client.get(f"/api/v2/beatmaps/{beatmap.id}/scores", params=params, headers=headers)
        assert response.status_code == 200, response.text
        return [row["total_score"] for row in response.json()["scores"]]

    async def profile_pp() -> float:
        response = await client.get("/api/v2/me/osu", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["statistics"]["pp"]

    previous = get_active_board()
    set_active_board(ActiveBoard())
    try:
        assert await totals() == [500_000, 400_000] and await profile_pp() == 100.0

        await get_active_board().set(player.id, Variant.RELAX)  # what an RX submit does
        assert await totals() == [900_000], "the game asks for no board: it gets RX"
        assert await profile_pp() == 900.0
        assert await totals(variant="vanilla") == [500_000, 400_000], "an explicit board still wins"

        await get_active_board().set(player.id, Variant.VANILLA)  # 30 quiet minutes later
        assert await totals() == [500_000, 400_000] and await profile_pp() == 100.0
    finally:
        set_active_board(previous)
