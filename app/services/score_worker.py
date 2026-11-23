"""Background worker that processes submitted scores into user statistics.

The database is the durable queue: a score is "pending" until it has a ``score_process_history`` row.
The in-memory ``asyncio.Queue`` only wakes the worker, so nothing is lost if the process dies - pending
scores are re-queued on the next start (see ``_enqueue_backlog``).

After a score's transaction commits, the player's new pp is pushed to the Redis leaderboard. If Redis is
down or its index is missing, ranks are counted in the database instead and the index is rebuilt in the
background, so statistics are always correct and only the speed changes.

One-off CLI::

    python -m app.services.score_worker rebuild [--user-id N] [--mode N]   # statistics, then leaderboard
    python -m app.services.score_worker rebuild-leaderboard                # leaderboard index only
"""

import argparse
import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from collections.abc import Callable

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.legacy import ScoreProcessHistory
from app.models.score import Score
from app.models.user import GameMode
from app.services.leaderboard import Leaderboard
from app.services.score_stats import ProcessedScore
from app.services.variant import Variant
from app.services.variant import supports_variant
from app.services.score_stats import process_score
from app.services.score_stats import rebuild_user_statistics

logger = logging.getLogger(__name__)

OnProcessed = Callable[[int, int], Awaitable[object]]

_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 1.0
_DRAIN_TIMEOUT_SECONDS = 5.0


class ScoreStatisticsWorker:
    """Single-consumer queue that turns submitted scores into statistics."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        on_processed: OnProcessed | None = None,
        leaderboard: Leaderboard | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.on_processed = on_processed
        self.leaderboard = leaderboard
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._backlog_loaded = asyncio.Event()
        self._leaderboard_build: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start consuming. Must be called from a running event loop; safe to call twice."""
        if self.running:
            return
        self._queue = asyncio.Queue()
        self._backlog_loaded = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="score-statistics-worker")

    async def stop(self) -> None:
        """Finish queued work (bounded) and stop."""
        if self._task is None:
            return
        await self.drain()
        for task in (self._task, self._leaderboard_build):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = None
        self._leaderboard_build = None

    async def drain(self, timeout: float = _DRAIN_TIMEOUT_SECONDS) -> None:
        """Wait until the startup backlog is queued and every queued score has been handled."""
        if not self.running:
            return
        try:
            # The queue is empty until the backlog scan finishes, so `join()` alone could return too early.
            await asyncio.wait_for(self._backlog_loaded.wait(), timeout)
            await asyncio.wait_for(self._queue.join(), timeout)
        except TimeoutError:
            logger.warning("Score statistics queue did not drain within %.0fs", timeout)

    def enqueue(self, score_id: int) -> None:
        """Queue a committed score. If the worker is not running the score stays pending in the database."""
        if not self.running:
            logger.warning("Score worker not running; score %s will be processed on next start", score_id)
            return
        self._queue.put_nowait(score_id)

    async def _run(self) -> None:
        try:
            await self._build_leaderboard()
            await self._enqueue_backlog()
        except Exception:
            logger.exception("Failed to enqueue backlog of unprocessed scores")
        finally:
            self._backlog_loaded.set()

        while True:
            score_id = await self._queue.get()
            try:
                await self._handle(score_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Giving up on score %s; it will be retried on next start", score_id)
            finally:
                self._queue.task_done()

    async def _enqueue_backlog(self) -> None:
        """Queue scores that were committed but never processed (e.g. the server stopped mid-queue).

        Scores older than the first processed score are treated as pre-existing and are skipped, because
        their effects are already part of the stored statistics. Run ``rebuild`` once after adopting this
        worker on an existing database.
        """
        async with self._session_factory() as db:
            first_processed = (await db.execute(select(func.min(ScoreProcessHistory.score_id)))).scalar()

            if first_processed is None:
                total = (await db.execute(select(func.count(Score.id)))).scalar_one()
                if total:
                    logger.warning(
                        "%d scores exist but none were ever processed. Run "
                        "`python -m app.services.score_worker rebuild` once to compute statistics.",
                        total,
                    )
                return

            pending = (
                await db.execute(
                    select(Score.id)
                    .outerjoin(ScoreProcessHistory, ScoreProcessHistory.score_id == Score.id)
                    .where(ScoreProcessHistory.id.is_(None), Score.id > first_processed)
                    .order_by(Score.id),
                )
            ).scalars().all()

        for score_id in pending:
            self._queue.put_nowait(score_id)
        if pending:
            logger.info("Re-queued %d unprocessed scores", len(pending))

    async def _handle(self, score_id: int) -> None:
        result: ProcessedScore | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._session_factory() as db:
                    try:
                        result = await process_score(db, score_id, self.leaderboard)
                        await db.commit()
                    except IntegrityError:
                        # Another worker processed the same score first.
                        await db.rollback()
                        return
                    except Exception:
                        await db.rollback()
                        raise
                break
            except Exception:
                if attempt == _MAX_ATTEMPTS:
                    raise
                logger.warning("Score %s failed (attempt %d), retrying", score_id, attempt, exc_info=True)
                await asyncio.sleep(_RETRY_DELAY_SECONDS)

        if result is not None:
            await self._apply_leaderboard(result)

        if result is not None and self.on_processed is not None:
            try:
                await self.on_processed(result.user_id, result.score_id)
            except Exception:
                logger.exception("Score-processed notification failed for score %s", score_id)

    async def _apply_leaderboard(self, result: ProcessedScore) -> None:
        """Push the committed pp to the index; heal the index in the background if it is missing."""
        if self.leaderboard is None or result.leaderboard_entry is None:
            return

        try:
            await self.leaderboard.apply(result.leaderboard_entry)
            if not await self.leaderboard.is_ready(result.mode, result.variant):
                self._schedule_leaderboard_build()
        except Exception:
            logger.warning("Could not update leaderboard for score %s", result.score_id, exc_info=True)

    def _schedule_leaderboard_build(self) -> None:
        if self._leaderboard_build is not None and not self._leaderboard_build.done():
            return
        self._leaderboard_build = asyncio.get_running_loop().create_task(
            self._build_leaderboard(),
            name="leaderboard-build",
        )

    async def _build_leaderboard(self) -> None:
        """Build any missing leaderboard index from the database. Never raises."""
        if self.leaderboard is None:
            return
        try:
            async with self._session_factory() as db:
                built = await self.leaderboard.ensure_built(db)
            if built:
                logger.info("Built leaderboard index for (mode, variant) %s", [(int(m), int(v)) for m, v in built])
        except Exception:
            logger.warning("Could not build the leaderboard index; ranks use the database", exc_info=True)


_worker: ScoreStatisticsWorker | None = None


def get_score_worker() -> ScoreStatisticsWorker:
    """Process-wide worker bound to the application database."""
    global _worker
    if _worker is None:
        from app.core.database import async_session_maker

        _worker = ScoreStatisticsWorker(async_session_maker)
    return _worker


async def _rebuild(user_id: int | None, mode: int | None) -> None:
    from app.core.database import async_session_maker
    from app.core.database import close_db

    try:
        async with async_session_maker() as db:
            query = select(Score.user_id, Score.ruleset_id, Score.variant).distinct()
            if user_id is not None:
                query = query.where(Score.user_id == user_id)
            if mode is not None:
                query = query.where(Score.ruleset_id == mode)
            targets = (await db.execute(query)).all()

        for target_user_id, ruleset_id, variant_id in targets:
            try:
                game_mode = GameMode(ruleset_id)
                variant = Variant(variant_id)
            except ValueError:
                logger.warning("Skipping unsupported ruleset %s for user %s", ruleset_id, target_user_id)
                continue

            async with async_session_maker() as db:
                replayed = await rebuild_user_statistics(db, target_user_id, game_mode, variant)
                await db.commit()
            logger.info(
                "Rebuilt user=%s mode=%s variant=%s from %d scores",
                target_user_id, game_mode.name, variant.label, replayed,
            )

        logger.info("Rebuilt statistics for %d user/mode pairs", len(targets))
    finally:
        await close_db()


async def _rebuild_leaderboard() -> None:
    """Rebuild the Redis leaderboard index from the database (all modes)."""
    import redis.asyncio as redis

    from app.core.config import get_settings
    from app.core.database import async_session_maker
    from app.core.database import close_db

    client = redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        leaderboard = Leaderboard(client)
        async with async_session_maker() as db:
            for variant in Variant:
                for mode in GameMode:
                    if supports_variant(mode, variant):
                        await leaderboard.rebuild(db, mode, variant)
    finally:
        await client.aclose()
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description="Score statistics maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    rebuild = sub.add_parser("rebuild", help="Recompute statistics, then rebuild the leaderboard")
    rebuild.add_argument("--user-id", type=int, default=None)
    rebuild.add_argument("--mode", type=int, default=None, help="Ruleset id (0-3)")
    sub.add_parser("rebuild-leaderboard", help="Rebuild only the Redis leaderboard index")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    async def run() -> None:
        if args.command == "rebuild":
            await _rebuild(args.user_id, args.mode)
        await _rebuild_leaderboard()

    asyncio.run(run())


if __name__ == "__main__":
    main()
