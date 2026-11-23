"""Seeder runner, mirroring Laravel's ``DatabaseSeeder``.

Runs each seeder in order inside a single shared :class:`AsyncSession`,
committing once at the end (or rolling back on failure).

Use it from the CLI:

.. code-block:: bash

    python -m app.seeders             # run the full pipeline
    python -m app.seeders beatmaps    # run a subset by short name
"""

from __future__ import annotations

import asyncio
import logging
import sys

# The mappers refer to each other by name (User -> Score, ...), so every model module must be imported before
# the first query. The web app gets that from its routers; this entry point has to do it itself.
import app.models.multiplayer  # noqa: F401
import app.models.score  # noqa: F401
from app.core.database import async_session_maker
from app.core.database import close_db
from app.seeders.base import Seeder
from app.seeders.beatmap_seeder import BeatmapSeeder
from app.seeders.matchmaking_seeder import MatchmakingSeeder

logger = logging.getLogger(__name__)

# Ordered pipeline; mirrors ``DatabaseSeeder::run`` in the PHP codebase.
# Seeders are added here as they are ported.
DEFAULT_PIPELINE: list[tuple[str, type[Seeder]]] = [
    ("beatmaps", BeatmapSeeder),
    ("matchmaking", MatchmakingSeeder),
]


def _resolve(selection: list[str]) -> list[type[Seeder]]:
    if not selection:
        return [cls for _, cls in DEFAULT_PIPELINE]
    name_map = dict(DEFAULT_PIPELINE)
    chosen: list[type[Seeder]] = []
    for name in selection:
        key = name.lower()
        if key not in name_map:
            raise SystemExit(
                f"Unknown seeder '{name}'. Available: {', '.join(name_map)}",
            )
        chosen.append(name_map[key])
    return chosen


async def run_seeders(selection: list[str] | None = None) -> None:
    """Execute the seeder pipeline inside a single transaction."""
    classes = _resolve(selection or [])

    async with async_session_maker() as session:
        try:
            for cls in classes:
                seeder = cls()
                logger.info("--> Running %s", cls.__name__)
                await seeder.run(session)
                await session.flush()
            await session.commit()
            logger.info("All seeders completed successfully")
        except Exception:
            await session.rollback()
            logger.exception("Seeder pipeline failed, rolled back")
            raise


async def _run_and_close(selection: list[str]) -> None:
    # One event loop for both steps: asyncpg connections can only be closed on the loop that opened them.
    try:
        await run_seeders(selection)
    finally:
        await close_db()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    selection = sys.argv[1:]
    asyncio.run(_run_and_close(selection))


if __name__ == "__main__":
    main()
