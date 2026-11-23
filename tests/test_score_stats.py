"""Tests for the score statistics pipeline (app.services.score_stats / score_worker).

These tests create their own database and do not depend on tests/conftest.py fixtures.
"""

import json
from collections.abc import AsyncGenerator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import Base
from app.models import multiplayer as _multiplayer_models  # noqa: F401  (registers FK targets)
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus
from app.models.legacy import ScoreProcessHistory
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics
from app.services.score_stats import ScoreRow
from app.services.score_stats import calculate_total_performance
from app.services.score_stats import clock_rate_from_mods
from app.services.score_stats import level_from_total_score
from app.services.score_stats import process_score
from app.services.score_stats import rebuild_user_statistics
from app.services.score_worker import ScoreStatisticsWorker

BONUS_ONE_MAP = (417.0 - 1.0 / 3.0) * (1.0 - 0.995)


@pytest_asyncio.fixture
async def session_maker(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stats.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def make_user(db: AsyncSession, name: str, country: str = "VN") -> User:
    user = User(username=name, email=f"{name}@example.com", password_hash="x", country_acronym=country)
    db.add(user)
    await db.flush()
    db.add(UserStatistics(user_id=user.id, mode=GameMode.OSU))
    return user


async def make_beatmap(db: AsyncSession, status: BeatmapStatus, length: int = 120) -> Beatmap:
    beatmapset = BeatmapSet(user_id=None, artist="a", title="t", creator="c", status=status)
    db.add(beatmapset)
    await db.flush()
    beatmap = Beatmap(
        beatmapset_id=beatmapset.id,
        version="v",
        mode=GameMode.OSU,
        status=status,
        checksum=f"{beatmapset.id:032d}",
        total_length=length,
    )
    db.add(beatmap)
    await db.flush()
    return beatmap


async def make_score(
    db: AsyncSession,
    user: User,
    beatmap: Beatmap,
    total_score: int,
    pp: float | None,
    rank: str = "A",
    passed: bool = True,
    accuracy: float = 0.98,
    max_combo: int = 100,
    mods: list[dict] | None = None,
    statistics: dict[str, int] | None = None,
    seconds: int = 100,
    variant: int = 0,
) -> Score:
    ended = datetime.now(UTC)
    score = Score(
        user_id=user.id,
        beatmap_id=beatmap.id,
        ruleset_id=0,
        variant=variant,
        data=json.dumps(
            {
                "mods": mods or [],
                "statistics": statistics or {"great": 100, "ok": 5, "miss": 0},
                "maximum_statistics": {"great": 105},
            },
        ),
        total_score=total_score,
        accuracy=accuracy,
        pp=pp,
        max_combo=max_combo,
        rank=rank,
        passed=passed,
        ranked=passed,
        preserve=passed,
        started_at=ended - timedelta(seconds=seconds),
        ended_at=ended,
    )
    db.add(score)
    await db.flush()
    return score


async def get_stats(db: AsyncSession, user_id: int) -> UserStatistics:
    result = await db.execute(select(UserStatistics).where(UserStatistics.user_id == user_id))
    return result.scalar_one()


def snapshot(stats: UserStatistics) -> dict[str, object]:
    return {
        column.name: getattr(stats, column.name)
        for column in UserStatistics.__table__.columns
        if column.name not in {"id", "replays_watched"}
    }


# --- pure functions -------------------------------------------------------------------------------


def row(beatmap_id: int, pp: float, accuracy: float = 0.98, score_id: int = 1) -> ScoreRow:
    return ScoreRow(score_id, beatmap_id, 1_000_000, "A", pp, accuracy, BeatmapStatus.RANKED)


def test_total_performance_single_score_includes_bonus() -> None:
    pp, accuracy = calculate_total_performance([row(1, 100.0)])

    assert pp == pytest.approx(100.0 + BONUS_ONE_MAP)
    assert accuracy == pytest.approx(98.0)


def test_total_performance_uses_best_pp_per_beatmap_only() -> None:
    once = calculate_total_performance([row(1, 100.0)])
    farmed = calculate_total_performance(
        [row(1, 100.0, score_id=1), row(1, 80.0, score_id=2), row(1, 60.0, score_id=3)],
    )

    assert farmed == once


def test_total_performance_weights_scores_descending() -> None:
    pp, _ = calculate_total_performance([row(1, 50.0), row(2, 100.0)])

    bonus = (417.0 - 1.0 / 3.0) * (1.0 - 0.995**2)
    assert pp == pytest.approx(100.0 + 50.0 * 0.95 + bonus)


def test_total_performance_without_scores() -> None:
    assert calculate_total_performance([]) == (0.0, 100.0)


def test_accuracy_accepts_percentage_and_fraction() -> None:
    assert calculate_total_performance([row(1, 10.0, accuracy=98.0)])[1] == pytest.approx(98.0)


def test_level_boundaries() -> None:
    assert level_from_total_score(0) == (1, 0)
    assert level_from_total_score(29_999)[0] == 1
    assert level_from_total_score(30_000) == (2, 0)


def test_clock_rate_from_mods() -> None:
    assert clock_rate_from_mods([]) == 1.0
    assert clock_rate_from_mods([{"acronym": "DT"}]) == 1.5
    assert clock_rate_from_mods([{"acronym": "HT", "settings": {"speed_change": 0.6}}]) == 0.6


# --- database pipeline ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_best_score_per_beatmap_counts(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        low = await make_score(db, user, beatmap, total_score=500_000, pp=50.0, rank="A")
        high = await make_score(db, user, beatmap, total_score=800_000, pp=80.0, rank="S")
        await db.commit()

        assert await process_score(db, low.id) is not None
        assert await process_score(db, high.id) is not None
        await db.commit()

        stats = await get_stats(db, user.id)

    assert stats.ranked_score == 800_000  # not 1_300_000
    assert (stats.grade_s, stats.grade_a) == (1, 0)  # the A was superseded
    assert stats.pp == pytest.approx(80.0 + BONUS_ONE_MAP, abs=1e-4)
    assert stats.play_count == 2
    assert stats.total_score == 1_300_000  # total score still counts every play
    assert stats.total_hits == 210
    assert stats.maximum_combo == 100
    assert stats.global_rank == 1


@pytest.mark.asyncio
async def test_processing_is_idempotent(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        score = await make_score(db, user, beatmap, total_score=700_000, pp=70.0)
        await db.commit()

        assert await process_score(db, score.id) is not None
        await db.commit()
        before = snapshot(await get_stats(db, user.id))

        assert await process_score(db, score.id) is None
        await db.commit()
        after = snapshot(await get_stats(db, user.id))

    assert before == after
    assert before["play_count"] == 1


@pytest.mark.asyncio
async def test_loved_maps_count_for_ranked_score_but_give_no_pp(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.LOVED)
        score = await make_score(db, user, beatmap, total_score=600_000, pp=123.0, rank="SH")
        await db.commit()

        await process_score(db, score.id)
        await db.commit()
        stats = await get_stats(db, user.id)

    assert stats.ranked_score == 600_000
    assert stats.grade_sh == 1
    assert stats.pp == 0.0  # loved maps never award pp, even if a pp value was stored


@pytest.mark.asyncio
async def test_qualified_maps_do_not_count_for_ranked_score(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.QUALIFIED)
        score = await make_score(db, user, beatmap, total_score=600_000, pp=None, rank="S")
        await db.commit()

        await process_score(db, score.id)
        await db.commit()
        stats = await get_stats(db, user.id)

    assert (stats.ranked_score, stats.grade_s, stats.pp) == (0, 0, 0.0)
    assert stats.play_count == 1


@pytest.mark.asyncio
async def test_trivial_failed_play_is_not_counted(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        quit_early = await make_score(
            db, user, beatmap, total_score=100, pp=None, rank="F", passed=False, seconds=3,
            statistics={"great": 1},
        )
        await db.commit()

        await process_score(db, quit_early.id)
        await db.commit()
        stats = await get_stats(db, user.id)

    assert stats.play_count == 0
    assert stats.play_time == 0
    assert stats.ranked_score == 0


@pytest.mark.asyncio
async def test_automation_mods_do_not_set_max_combo(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        relax = await make_score(db, user, beatmap, 900_000, 90.0, max_combo=999, mods=[{"acronym": "RX"}])
        await db.commit()

        await process_score(db, relax.id)
        await db.commit()
        stats = await get_stats(db, user.id)

    assert stats.maximum_combo == 0


@pytest.mark.asyncio
async def test_global_and_country_rank(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        strong = await make_user(db, "strong", country="VN")
        weak = await make_user(db, "weak", country="VN")
        foreign = await make_user(db, "foreign", country="JP")
        scores = [
            await make_score(db, strong, beatmap, 900_000, 200.0),
            await make_score(db, foreign, beatmap, 800_000, 150.0),
            await make_score(db, weak, beatmap, 700_000, 100.0),
        ]
        await db.commit()

        for score in scores:
            await process_score(db, score.id)
        await db.commit()

        ranks = {
            name: (stats.global_rank, stats.country_rank)
            for name, stats in (
                ("strong", await get_stats(db, strong.id)),
                ("foreign", await get_stats(db, foreign.id)),
                ("weak", await get_stats(db, weak.id)),
            )
        }

    # Ranks are computed at processing time, so `strong` (processed first) still shows rank 1 and
    # `weak` (processed last) sees both others above it.
    assert ranks["strong"] == (1, 1)
    assert ranks["weak"] == (3, 2)
    assert ranks["foreign"] == (2, 1)


@pytest.mark.asyncio
async def test_restricted_user_pp_is_not_updated(session_maker: async_sessionmaker[AsyncSession]) -> None:
    async with session_maker() as db:
        user = await make_user(db, "cheater")
        user.is_restricted = True
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        score = await make_score(db, user, beatmap, 900_000, 300.0)
        await db.commit()

        await process_score(db, score.id)
        await db.commit()
        stats = await get_stats(db, user.id)

    assert stats.pp == 0.0
    assert stats.global_rank is None
    assert stats.play_count == 1  # counters still advance


@pytest.mark.asyncio
async def test_rebuild_matches_incremental_processing(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        ranked = await make_beatmap(db, BeatmapStatus.RANKED)
        loved = await make_beatmap(db, BeatmapStatus.LOVED)
        scores = [
            await make_score(db, user, ranked, 500_000, 50.0, rank="A", max_combo=50),
            await make_score(db, user, ranked, 800_000, 80.0, rank="S", max_combo=120),
            await make_score(db, user, loved, 650_000, None, rank="X", max_combo=80),
            await make_score(db, user, ranked, 100, None, rank="F", passed=False, seconds=2),
        ]
        await db.commit()

        for score in scores:
            await process_score(db, score.id)
        await db.commit()
        incremental = snapshot(await get_stats(db, user.id))

        replayed = await rebuild_user_statistics(db, user.id, GameMode.OSU)
        await db.commit()
        rebuilt = snapshot(await get_stats(db, user.id))

    assert replayed == 4
    assert rebuilt == incremental


@pytest.mark.asyncio
async def test_rebuild_marks_existing_scores_processed(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        score = await make_score(db, user, beatmap, 700_000, 70.0)
        await db.commit()

        await rebuild_user_statistics(db, user.id, GameMode.OSU)
        await db.commit()

        # A rebuilt score must not be applied a second time by the queue.
        assert await process_score(db, score.id) is None
        history = (await db.execute(select(ScoreProcessHistory.score_id))).scalars().all()
        stats = await get_stats(db, user.id)

    assert history == [score.id]
    assert stats.play_count == 1


# --- worker -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_processes_queue_and_notifies(session_maker: async_sessionmaker[AsyncSession]) -> None:
    notified: list[tuple[int, int]] = []

    async def on_processed(user_id: int, score_id: int) -> None:
        notified.append((user_id, score_id))

    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        score = await make_score(db, user, beatmap, 700_000, 70.0)
        await db.commit()
        user_id, score_id = user.id, score.id

    worker = ScoreStatisticsWorker(session_maker, on_processed)
    worker.start()
    worker.enqueue(score_id)
    worker.enqueue(score_id)  # duplicate deliveries must be harmless
    await worker.drain()
    await worker.stop()

    async with session_maker() as db:
        stats = await get_stats(db, user_id)

    assert stats.play_count == 1
    assert notified == [(user_id, score_id)]


@pytest.mark.asyncio
async def test_worker_requeues_unprocessed_scores_on_start(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        first = await make_score(db, user, beatmap, 500_000, 50.0)
        await db.commit()
        await process_score(db, first.id)  # establishes the "adoption point"
        await db.commit()

        # Committed after the previous run stopped, before it could be processed (simulated crash).
        crashed = await make_score(db, user, beatmap, 800_000, 80.0, rank="S")
        await db.commit()
        user_id = user.id

    worker = ScoreStatisticsWorker(session_maker)
    worker.start()
    await worker.drain()
    await worker.stop()

    async with session_maker() as db:
        stats = await get_stats(db, user_id)
        processed = (await db.execute(select(ScoreProcessHistory.score_id))).scalars().all()

    assert sorted(processed) == [first.id, crashed.id]
    assert stats.ranked_score == 800_000


@pytest.mark.asyncio
async def test_worker_does_not_replay_legacy_scores(session_maker: async_sessionmaker[AsyncSession]) -> None:
    """Scores that predate the worker must not be double counted into stats that already include them."""
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        await make_score(db, user, beatmap, 500_000, 50.0)
        await db.commit()
        user_id = user.id

    worker = ScoreStatisticsWorker(session_maker)
    worker.start()
    await worker.drain()
    await worker.stop()

    async with session_maker() as db:
        stats = await get_stats(db, user_id)

    assert stats.play_count == 0


@pytest.mark.asyncio
async def test_enqueue_without_running_worker_is_a_noop(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    worker = ScoreStatisticsWorker(session_maker)

    worker.enqueue(1)  # must not raise; the score stays pending in the database

    assert not worker.running


# --- grade / failure rules carried over from the previous implementation ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rank", "field"),
    [("X", "grade_ss"), ("XH", "grade_ssh"), ("S", "grade_s"), ("SH", "grade_sh"), ("A", "grade_a")],
)
async def test_tracked_grades_map_to_counters(
    session_maker: async_sessionmaker[AsyncSession],
    rank: str,
    field: str,
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        score = await make_score(db, user, beatmap, 999_999, 250.0, rank=rank)
        await db.commit()

        await process_score(db, score.id)
        await db.commit()
        stats = await get_stats(db, user.id)

    names = ("grade_ss", "grade_ssh", "grade_s", "grade_sh", "grade_a")
    grades = {name: getattr(stats, name) for name in names}
    assert grades == {name: int(name == field) for name in grades}
    assert stats.ranked_score == 999_999


@pytest.mark.asyncio
async def test_lower_grades_are_not_tracked_but_still_score(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        score = await make_score(db, user, beatmap, 321_654, 120.0, rank="B")
        await db.commit()

        await process_score(db, score.id)
        await db.commit()
        stats = await get_stats(db, user.id)

    assert (stats.grade_ss, stats.grade_ssh, stats.grade_s, stats.grade_sh, stats.grade_a) == (0, 0, 0, 0, 0)
    assert stats.ranked_score == 321_654


@pytest.mark.asyncio
async def test_failed_play_counts_as_a_play_but_not_ranked_stats(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as db:
        user = await make_user(db, "player")
        beatmap = await make_beatmap(db, BeatmapStatus.RANKED)
        failed = await make_score(db, user, beatmap, 222_222, None, rank="F", passed=False, accuracy=0.9)
        await db.commit()

        await process_score(db, failed.id)
        await db.commit()
        stats = await get_stats(db, user.id)

    assert stats.play_count == 1
    assert stats.total_score == 222_222
    assert (stats.ranked_score, stats.pp) == (0, 0.0)
    assert stats.maximum_combo == 0
