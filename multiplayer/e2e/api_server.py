"""Runs the REAL internal multiplayer router (app/api/internal/multiplayer.py) on a throwaway SQLite database.

Used by the cross-language end-to-end test; see README.md. Run from the repository root:

    E2E_DB=/tmp/e2e.db INTERNAL_API_KEY=e2e-key python multiplayer/e2e/api_server.py
"""

import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DB = os.environ["E2E_DB"]
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB}"
os.environ.setdefault("INTERNAL_API_KEY", "e2e-key")

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import app.models.beatmap  # noqa: E402,F401
import app.models.legacy  # noqa: E402,F401
import app.models.multiplayer  # noqa: E402,F401
import app.models.score  # noqa: E402,F401
import app.models.user  # noqa: E402,F401
from app.api.internal.multiplayer import router  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.core.database import async_session_maker  # noqa: E402
from app.core.database import engine  # noqa: E402
from app.core.error import OsuError  # noqa: E402
from app.models.beatmap import Beatmap  # noqa: E402
from app.models.beatmap import BeatmapSet  # noqa: E402
from app.models.beatmap import BeatmapStatus  # noqa: E402
from app.models.multiplayer import MultiplayerRoom  # noqa: E402
from app.models.user import GameMode  # noqa: E402
from app.models.user import User  # noqa: E402


async def seed() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_maker() as db:
        for user_id in (1, 2):
            db.add(User(id=user_id, username=f"e2e{user_id}", email=f"e2e{user_id}@example.com", password_hash="x"))
        beatmapset = BeatmapSet(id=1, user_id=None, artist="a", title="t", creator="c", status=BeatmapStatus.RANKED)
        db.add(beatmapset)
        await db.flush()
        db.add(
            Beatmap(
                id=1, beatmapset_id=1, version="v", mode=GameMode.OSU, status=BeatmapStatus.RANKED,
                checksum="abc123", difficulty_rating=4.2,
            ),
        )
        # A room a crashed multiplayer service left open: must be closed at the next start.
        db.add(MultiplayerRoom(id=900, name="stale", type="head_to_head", status="idle", participant_count=5))
        await db.commit()


app = FastAPI()
app.include_router(router)


@app.exception_handler(OsuError)
async def osu_error(_: Request, exc: OsuError) -> JSONResponse:
    return JSONResponse(status_code=exc.code, content={"error": exc.error, "message": exc.message})


if __name__ == "__main__":
    Path(DB).unlink(missing_ok=True)
    asyncio.run(seed())
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("E2E_PORT", "18000")), log_level="warning")
