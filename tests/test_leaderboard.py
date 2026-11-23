"""Tests for the Redis leaderboard index (app.services.leaderboard) and its use by the score worker.

A small in-memory stand-in implements the handful of Redis commands the leaderboard uses, so no Redis server
is needed. Its semantics mirror Redis for those commands (notably ``ZCOUNT`` with exclusive "(" bounds).
"""

import fnmatch
from pathlib import Path
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import Base
from app.models import multiplayer as _multiplayer_models  # noqa: F401  (registers FK targets)
from app.models.beatmap import BeatmapStatus
from app.models.user import GameMode
from app.services.leaderboard import Leaderboard
from app.services.leaderboard import LeaderboardEntry
from app.services.leaderboard import LeaderboardUnavailable
from app.services.score_stats import process_score
from app.services.score_worker import ScoreStatisticsWorker
from app.services.variant import Variant
from tests.test_score_stats import get_stats
from tests.test_score_stats import make_beatmap
from tests.test_score_stats import make_score
from tests.test_score_stats import make_user

OSU = GameMode.OSU


@pytest_asyncio.fixture
async def session_maker(tmp_path: Path):  # noqa: ANN201
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'leaderboard.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


class FakeRedis:
    """Minimal async Redis double: strings, hashes and sorted sets."""

    def __init__(self, fail: bool = False) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.fail = fail

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis is down")

    @staticmethod
    def _bound(raw: str) -> tuple[float, bool]:
        exclusive = raw.startswith("(")
        raw = raw.lstrip("(")
        value = {"+inf": float("inf"), "-inf": float("-inf")}.get(raw)
        return (float(raw) if value is None else value), exclusive

    async def exists(self, key: str) -> int:
        self._check()
        return int(key in self.strings or key in self.hashes or key in self.zsets)

    async def zcount(self, key: str, low: str, high: str) -> int:
        self._check()
        lo, lo_excl = self._bound(low)
        hi, hi_excl = self._bound(high)
        return sum(
            1
            for score in self.zsets.get(key, {}).values()
            if (score > lo if lo_excl else score >= lo) and (score < hi if hi_excl else score <= hi)
        )

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self._check()
        self.zsets.setdefault(key, {}).update({str(m): float(s) for m, s in mapping.items()})
        return len(mapping)

    async def zrem(self, key: str, *members: str) -> int:
        self._check()
        zset = self.zsets.get(key, {})
        return sum(1 for member in members if zset.pop(str(member), None) is not None)

    async def zcard(self, key: str) -> int:
        self._check()
        return len(self.zsets.get(key, {}))

    async def hget(self, key: str, field: str) -> str | None:
        self._check()
        return self.hashes.get(key, {}).get(field)

    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> int:
        self._check()
        target = self.hashes.setdefault(key, {})
        if field is not None:
            target[field] = str(value)
        target.update(mapping or {})
        return 1

    async def hdel(self, key: str, *fields: str) -> int:
        self._check()
        return sum(1 for f in fields if self.hashes.get(key, {}).pop(f, None) is not None)

    async def delete(self, *keys: str) -> int:
        self._check()
        removed = 0
        for key in keys:
            for store in (self.strings, self.hashes, self.zsets):
                removed += int(store.pop(key, None) is not None)
        return removed

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self._check()
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def scan_iter(self, match: str = "*") -> AsyncIterator[str]:
        self._check()
        keys = [*self.strings, *self.hashes, *self.zsets]
        for key in keys:
            if fnmatch.fnmatch(key, match):
                yield key


def entry(user_id: int, pp: float, country: str = "VN", ranked: bool = True) -> LeaderboardEntry:
    return LeaderboardEntry(OSU, user_id, country, pp, ranked)


async def ready(board: Leaderboard, session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        await board.rebuild(db, OSU)


# --- the index itself -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ranks_count_strictly_greater_and_ties_share_a_rank(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    board = Leaderboard(FakeRedis())
    await ready(board, session_maker)
    for user_id, pp, country in [(1, 300.0, "VN"), (2, 200.0, "VN"), (3, 200.0, "JP"), (4, 100.0, "VN")]:
        await board.apply(entry(user_id, pp, country))

    assert await board.ranks(OSU, 300.0, "VN") == (1, 1)
    assert await board.ranks(OSU, 200.0, "VN") == (2, 2)  # tied with user 3, both rank 2
    assert await board.ranks(OSU, 200.0, "JP") == (2, 1)
    assert await board.ranks(OSU, 250.0, "VN") == (2, 2)  # a pp value nobody has yet
    assert await board.ranks(OSU, 50.0, "VN") == (5, 4)


@pytest.mark.asyncio
async def test_ranks_refuse_to_answer_until_the_index_is_built() -> None:
    board = Leaderboard(FakeRedis())

    with pytest.raises(LeaderboardUnavailable):
        await board.ranks(OSU, 100.0, "VN")


@pytest.mark.asyncio
async def test_apply_moves_a_player_between_countries(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeRedis()
    board = Leaderboard(client)
    await ready(board, session_maker)

    await board.apply(entry(7, 100.0, "VN"))
    await board.apply(entry(7, 120.0, "JP"))

    assert "7" not in client.zsets["leaderboard:pp:v0:0:VN"]
    assert client.zsets["leaderboard:pp:v0:0:JP"] == {"7": 120.0}
    assert client.zsets["leaderboard:pp:v0:0"] == {"7": 120.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("ranked", [True, False])
async def test_apply_removes_unranked_and_zero_pp_players(
    session_maker: async_sessionmaker[AsyncSession],
    ranked: bool,
) -> None:
    client = FakeRedis()
    board = Leaderboard(client)
    await ready(board, session_maker)
    await board.apply(entry(7, 100.0))

    await board.apply(entry(7, 100.0 if not ranked else 0.0, ranked=ranked))

    assert client.zsets["leaderboard:pp:v0:0"] == {}
    assert client.zsets["leaderboard:pp:v0:0:VN"] == {}
    assert client.hashes["leaderboard:pp:v0:0:country_of"] == {}


@pytest.mark.asyncio
async def test_rebuild_indexes_only_ranked_players_with_pp(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        regular = await make_user(db, "regular")
        restricted = await make_user(db, "restricted")
        restricted.is_restricted = True
        bot = await make_user(db, "bot")
        bot.is_bot = True
        idle = await make_user(db, "idle")
        scores = [
            await make_score(db, regular, beatmap, 900_000, 100.0),
            await make_score(db, restricted, beatmap, 900_000, 100.0),
            await make_score(db, bot, beatmap, 900_000, 100.0),
        ]
        await db.commit()
        # Statistics are normally produced by the worker; here they only need to exist for the rebuild.
        for score in scores:
            await process_score(db, score.id)
        await db.commit()
        regular_id, idle_id = regular.id, idle.id

        client = FakeRedis()
        board = Leaderboard(client)
        count = await board.rebuild(db, OSU)

    assert count == 1
    assert list(client.zsets["leaderboard:pp:v0:0"]) == [str(regular_id)]
    assert str(idle_id) not in client.zsets["leaderboard:pp:v0:0"]
    assert await board.is_ready(OSU)


@pytest.mark.asyncio
async def test_ensure_built_only_builds_missing_modes_and_respects_the_lock(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeRedis()
    board = Leaderboard(client)

    async with session_maker() as db:
        built = set(await board.ensure_built(db))
        # Vanilla for every mode, Relax for osu/taiko/catch, Autopilot for osu only.
        assert {(m, v) for m, v in built if v == Variant.VANILLA} == {(m, Variant.VANILLA) for m in GameMode}
        assert {m for m, v in built if v == Variant.RELAX} == {GameMode.OSU, GameMode.TAIKO, GameMode.CATCH}
        assert {m for m, v in built if v == Variant.AUTOPILOT} == {GameMode.OSU}
        assert await board.ensure_built(db) == []  # already built

        await client.delete("leaderboard:pp:v0:2:ready")
        client.strings["leaderboard:pp:rebuild_lock"] = "1"  # another process is rebuilding
        assert await board.ensure_built(db) == []
        assert not await board.is_ready(GameMode.CATCH)


# --- use by the statistics pipeline -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_score_takes_ranks_from_the_leaderboard(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeRedis()
    board = Leaderboard(client)

    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        strong = await make_user(db, "strong", "VN")
        weak = await make_user(db, "weak", "VN")
        scores = [
            await make_score(db, strong, beatmap, 900_000, 200.0),
            await make_score(db, weak, beatmap, 700_000, 100.0),
        ]
        await db.commit()
        await board.rebuild(db, OSU)

        for score in scores:
            result = await process_score(db, score.id, board)
            await db.commit()
            assert result is not None and result.leaderboard_entry is not None
            await board.apply(result.leaderboard_entry)  # what the worker does after the commit

        strong_stats = await get_stats(db, strong.id)
        weak_stats = await get_stats(db, weak.id)

    assert (strong_stats.global_rank, strong_stats.country_rank) == (1, 1)
    assert (weak_stats.global_rank, weak_stats.country_rank) == (2, 2)


@pytest.mark.asyncio
async def test_ranks_fall_back_to_the_database_when_redis_is_down(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    board = Leaderboard(FakeRedis(fail=True))

    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        strong = await make_user(db, "strong", "VN")
        weak = await make_user(db, "weak", "VN")
        strong_score = await make_score(db, strong, beatmap, 900_000, 200.0)
        weak_score = await make_score(db, weak, beatmap, 700_000, 100.0)
        await db.commit()

        await process_score(db, strong_score.id, board)
        await process_score(db, weak_score.id, board)
        await db.commit()
        weak_stats = await get_stats(db, weak.id)

    assert (weak_stats.global_rank, weak_stats.country_rank) == (2, 2)


# --- worker -------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_builds_the_index_on_start_and_keeps_it_updated(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    client = FakeRedis()
    async with session_maker() as db:
        user = await make_user(db, "player", "VN")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        score = await make_score(db, user, beatmap, 900_000, 150.0)
        await db.commit()
        user_id, score_id = user.id, score.id

    worker = ScoreStatisticsWorker(session_maker, leaderboard=Leaderboard(client))
    worker.start()
    await worker.drain()
    assert await worker.leaderboard.is_ready(OSU)  # type: ignore[union-attr]

    worker.enqueue(score_id)
    await worker.drain()
    await worker.stop()

    async with session_maker() as db:
        stats = await get_stats(db, user_id)

    assert client.zsets["leaderboard:pp:v0:0"] == {str(user_id): stats.pp}
    assert client.zsets["leaderboard:pp:v0:0:VN"] == {str(user_id): stats.pp}
    assert stats.pp > 150.0  # includes bonus pp
    assert (stats.global_rank, stats.country_rank) == (1, 1)


@pytest.mark.asyncio
async def test_worker_rebuilds_a_flushed_index(session_maker: async_sessionmaker[AsyncSession]) -> None:
    client = FakeRedis()
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        first = await make_score(db, user, beatmap, 500_000, 50.0)
        await db.commit()
        user_id = user.id

    worker = ScoreStatisticsWorker(session_maker, leaderboard=Leaderboard(client))
    worker.start()
    await worker.drain()
    worker.enqueue(first.id)
    await worker.drain()

    client.strings.clear()  # simulate FLUSHALL: index and `ready` marker disappear
    client.zsets.clear()
    client.hashes.clear()

    async with session_maker() as db:
        second = await make_score(db, user, beatmap, 800_000, 80.0, rank="S")
        await db.commit()
    worker.enqueue(second.id)
    await worker.drain()
    if worker._leaderboard_build is not None:
        await worker._leaderboard_build
    await worker.stop()

    async with session_maker() as db:
        stats = await get_stats(db, user_id)

    assert await Leaderboard(client).is_ready(OSU)
    assert client.zsets["leaderboard:pp:v0:0"] == {str(user_id): stats.pp}
    assert (stats.global_rank, stats.country_rank) == (1, 1)


@pytest.mark.asyncio
async def test_worker_survives_redis_failures(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        score = await make_score(db, user, beatmap, 500_000, 50.0)
        await db.commit()
        user_id, score_id = user.id, score.id

    worker = ScoreStatisticsWorker(session_maker, leaderboard=Leaderboard(FakeRedis(fail=True)))
    worker.start()
    worker.enqueue(score_id)
    await worker.drain()
    await worker.stop()

    async with session_maker() as db:
        stats = await get_stats(db, user_id)

    assert stats.play_count == 1
    assert (stats.global_rank, stats.country_rank) == (1, 1)  # counted in the database
