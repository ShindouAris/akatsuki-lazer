"""Main application entry point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from app.core.error import OsuError
from .logger import LOGGING_CONFIG, setup_logger

from fastapi import FastAPI
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.hubs.metadata import get_online_count
from app.api.hubs.metadata import router as metadata_router
from app.api.hubs.multiplayer import router as multiplayer_router
from app.api.internal.matchmaking import router as internal_matchmaking_router
from app.api.internal.multiplayer import router as internal_multiplayer_router
from app.api.hubs.spectator import resume_score_processed_dispatcher
from app.api.hubs.spectator import router as spectator_router
from app.api.hubs.spectator import send_user_score_processed
from app.api.v2 import router as api_v2_router
from app.api.v2.oauth import router as oauth_router
from app.api.v2.users import root_registration_router
from app.core.config import get_settings
from app.core.database import async_session_maker
from app.core.database import close_db
# from app.core.database import init_db
from app.services.active_board import ActiveBoard
from app.services.active_board import set_active_board
from app.services.hub_state import close_hub_state_service
from app.services.hub_state import get_hub_state_service
from app.services.leaderboard import Leaderboard
from app.services.leaderboard import set_leaderboard
from app.services.rank_history import RankHistorySnapshotter
from app.services.score_worker import get_score_worker

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup
    logger.info("Starting py-lazer-server...")
    # await init_db()
    # logger.info("Database initialized")

    # Initialize Redis hub state service
    hub_state = await get_hub_state_service()
    app.state.hub_state = hub_state
    logger.info("Redis hub state service initialized")
    resume_score_processed_dispatcher()
    logger.info("Spectator score-processed dispatcher resumed")

    # Score statistics worker: updates user stats after submission, then tells the client via the spectator hub.
    score_worker = get_score_worker()
    score_worker.on_processed = send_user_score_processed
    leaderboard = Leaderboard(hub_state.redis)
    set_leaderboard(leaderboard)  # profiles read live ranks from it
    set_active_board(ActiveBoard(hub_state.redis))  # which board (vanilla / RX / AP) each player is on
    score_worker.leaderboard = leaderboard
    score_worker.start()
    rank_snapshotter = RankHistorySnapshotter(async_session_maker)
    rank_snapshotter.start()
    logger.info("Score statistics worker started")

    yield

    # Shutdown
    logger.info("Shutting down py-lazer-server...")
    await rank_snapshotter.stop()
    await score_worker.stop()
    set_leaderboard(None)
    logger.info("Score statistics worker stopped")
    await close_hub_state_service()
    logger.info("Redis hub state service closed")
    await close_db()
    logger.info("Database connections closed")


# Create FastAPI application
app = FastAPI(
    title="py-lazer-server",
    description="Python implementation of osu! lazer server",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.debug else None,
    redoc_url="/api/redoc" if settings.debug else None,
    redirect_slashes=False,  # Don't redirect /me/ to /me - loses auth headers
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(api_v2_router)

# Include root registration endpoint for osu! compatibility (/users)
app.include_router(root_registration_router, tags=["Users"])

# Also include OAuth at root level (osu! client expects /oauth/token, not /api/v2/oauth/token)
app.include_router(oauth_router, tags=["OAuth"])

# Include SignalR hub endpoints at root level
app.include_router(spectator_router, tags=["SignalR"])
app.include_router(metadata_router, tags=["SignalR"])
if settings.use_external_multiplayer:
    logger.info("Realtime multiplayer is served by the external service; /multiplayer is not mounted here")
else:
    app.include_router(multiplayer_router, tags=["SignalR"])

# Persistence API for the external multiplayer service (disabled unless INTERNAL_API_KEY is set)
app.include_router(internal_multiplayer_router, tags=["Internal"])
app.include_router(internal_matchmaking_router, tags=["Internal"])

@app.exception_handler(OsuError)
async def custom_handler(request: Request, exc: OsuError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.code,
        content={
            "error": exc.error,
            "hint": exc.hint,
            "message": exc.message
        }
    )

@app.get("/")
async def root() -> dict:
    """Root endpoint."""
    return {
        "name": "py-lazer-server",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "online_users": get_online_count(),
    }


@app.get("/api/v2/changelog/builds")
async def get_builds() -> dict:
    """Get available client builds (required for client startup)."""
    return {
        "builds": [
            {
                "id": 1,
                "version": "2024.101.0",
                "display_version": "2024.101.0",
                "users": 0,
                "created_at": "2024-01-01T00:00:00Z",
                "update_stream": {
                    "id": 1,
                    "name": "lazer",
                    "display_name": "lazer",
                    "is_featured": True,
                },
            },
        ],
        "search": {"stream": "lazer", "limit": 1},
    }


@app.get("/api/v2/seasonal-backgrounds")
async def get_seasonal_backgrounds() -> dict:
    """Get seasonal backgrounds."""
    # ends_at must be a valid ISO 8601 datetime - C# DateTimeOffset can't be null
    return {"backgrounds": [], "ends_at": "2099-12-31T23:59:59+00:00"}


@app.get("/api/v2/news")
async def get_news() -> dict:
    """Get news posts."""
    return {
        "news_posts": [],
        "news_sidebar": {"current_year": 2024, "years": [2024]},
        "cursor_string": None,
    }


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Global exception handler."""
    logger.exception(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "message": (
                str(exc) if settings.debug else "An internal server error occurred"
            ),
        },
    )


# Main entry point
def create_app() -> FastAPI:
    """Create the ASGI application."""
    setup_logger()
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_config=LOGGING_CONFIG,
    )
