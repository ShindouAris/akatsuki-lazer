"""Daily rank history for the 90 day rank graph.

Rows (``user_rank_history``) are written two ways:

* when a player's score is processed, today's row is set to their new rank, and
* once a day, shortly before UTC midnight, a snapshot stores *everyone's* final rank for that day, so
  players who did not play still get a true history (their rank drifts as others overtake them).

``get_rank_history`` returns what the client expects (see osu-web ``RankHistory``): exactly 90 values,
oldest first, the 89 previous days followed by the current rank. ``0`` means "no data".
"""

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserRankHistory
from app.models.user import UserStatistics

logger = logging.getLogger(__name__)

HISTORY_LENGTH = 90  # 89 stored days + the current rank
STORED_DAYS = HISTORY_LENGTH - 1
SNAPSHOT_TIME = time(23, 59, 30)  # UTC
_INSERT_CHUNK = 1000


def today() -> date:
    return datetime.now(UTC).date()


async def record_rank(
    db: AsyncSession,
    user_id: int,
    mode: GameMode,
    rank: int,
    day: date | None = None,
) -> None:
    """Set a player's rank for ``day`` (default today). Does not commit.

    Callers hold the player's statistics row lock, so there is no race on the read-then-write.
    """
    day = day or today()
    row = await db.get(UserRankHistory, (user_id, int(mode), day))
    if row is None:
        db.add(UserRankHistory(user_id=user_id, mode=int(mode), day=day, rank=rank))
    else:
        row.rank = rank


def build_history(rows: Sequence[tuple[date, int]], current_rank: int | None, today_: date) -> list[int]:
    """Assemble the 90 value list from stored (day, rank) rows.

    A day without a row repeats the previous known rank (no change observed); days before the first
    known rank are 0. A row older than the window may be passed to seed the first value.
    """
    by_day = dict(rows)
    window_start = today_ - timedelta(days=STORED_DAYS)

    older = [day for day in by_day if day < window_start]
    last_known = by_day[max(older)] if older else 0

    history: list[int] = []
    for offset in range(STORED_DAYS, 0, -1):
        last_known = by_day.get(today_ - timedelta(days=offset), last_known)
        history.append(last_known)

    history.append(current_rank or 0)
    return history


async def get_rank_history(
    db: AsyncSession,
    user_id: int,
    mode: GameMode,
    current_rank: int | None,
) -> list[int]:
    """The 90 value rank history of a player; the last value is ``current_rank``."""
    key = (user_id, mode)
    histories = await get_rank_histories(db, {key: current_rank})
    return histories[key]


async def get_rank_histories(
    db: AsyncSession,
    current_ranks: Mapping[tuple[int, GameMode], int | None],
) -> dict[tuple[int, GameMode], list[int]]:
    """Rank histories of many (user, mode) pairs in one query; values are their current ranks.

    Each result equals what ``get_rank_history`` returns for that pair.
    """
    if not current_ranks:
        return {}

    today_ = today()
    user_ids = {user_id for user_id, _ in current_ranks}
    modes = {int(mode) for _, mode in current_ranks}

    # Newest STORED_DAYS + 1 rows per (user, mode): the window plus one older row that seeds the first value.
    numbered = (
        select(
            UserRankHistory.user_id.label("user_id"),
            UserRankHistory.mode.label("mode"),
            UserRankHistory.day.label("day"),
            UserRankHistory.rank.label("rank"),
            func.row_number()
            .over(
                partition_by=[UserRankHistory.user_id, UserRankHistory.mode],
                order_by=UserRankHistory.day.desc(),
            )
            .label("n"),
        )
        .where(
            UserRankHistory.user_id.in_(user_ids),
            UserRankHistory.mode.in_(modes),
            UserRankHistory.day < today_,
        )
        .subquery()
    )
    result = await db.execute(
        select(numbered.c.user_id, numbered.c.mode, numbered.c.day, numbered.c.rank).where(
            numbered.c.n <= STORED_DAYS + 1,
        ),
    )

    rows_by_key: dict[tuple[int, int], list[tuple[date, int]]] = {}
    for user_id, mode, day, rank in result.all():
        rows_by_key.setdefault((user_id, mode), []).append((day, rank))

    return {
        key: build_history(rows_by_key.get((key[0], int(key[1])), []), current, today_)
        for key, current in current_ranks.items()
    }


async def snapshot_ranks(db: AsyncSession, day: date) -> int:
    """Store every ranked player's current rank for ``day`` and prune old rows. Does not commit.

    Equal pp share a rank (SQL ``RANK()``), matching the leaderboard. Returns the number of rows written.
    """
    written = 0
    for mode in GameMode:
        ranked = (
            select(
                UserStatistics.user_id.label("user_id"),
                func.rank().over(order_by=UserStatistics.pp.desc()).label("rank"),
            )
            .join(User, User.id == UserStatistics.user_id)
            .where(
                UserStatistics.mode == mode,
                UserStatistics.pp > 0,
                User.is_restricted.is_(False),
                User.is_bot.is_(False),
            )
        )
        rows = (await db.execute(ranked)).all()

        await db.execute(
            delete(UserRankHistory).where(UserRankHistory.day == day, UserRankHistory.mode == int(mode)),
        )
        values = [{"user_id": uid, "mode": int(mode), "day": day, "rank": int(rank)} for uid, rank in rows]
        for start in range(0, len(values), _INSERT_CHUNK):
            await db.execute(insert(UserRankHistory), values[start : start + _INSERT_CHUNK])
        written += len(values)

    await db.execute(delete(UserRankHistory).where(UserRankHistory.day < day - timedelta(days=STORED_DAYS)))
    return written


def seconds_until_snapshot(now: datetime) -> float:
    """Seconds from ``now`` (UTC) to the next snapshot time; never zero, so a run cannot repeat itself."""
    target = datetime.combine(now.date(), SNAPSHOT_TIME, tzinfo=UTC)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class RankHistorySnapshotter:
    """Background task that takes the daily end-of-day snapshot."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="rank-history-snapshot")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def snapshot_now(self) -> int:
        async with self._session_factory() as db:
            written = await snapshot_ranks(db, today())
            await db.commit()
        return written

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(seconds_until_snapshot(datetime.now(UTC)))
            try:
                written = await self.snapshot_now()
                logger.info("Stored rank history snapshot (%d rows)", written)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Rank history snapshot failed; the next run is tomorrow")
