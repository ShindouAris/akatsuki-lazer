"""Internal API for the external spectator service (see ./multiplayer/cmd/spectator).

The Go service owns the live stream; this process owns tokens and users. Authenticated with the shared
``INTERNAL_API_KEY`` (header ``X-Internal-Key``).
"""

import logging

import httpx
from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.internal.multiplayer import require_internal_key
from app.core.config import get_settings
from app.core.error import OsuError
from app.models.score import ScoreToken
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/spectator", dependencies=[Depends(require_internal_key)])


class ValidatePlayRequest(BaseModel):
    user_id: int
    score_token: int
    beatmap_id: int | None = None
    ruleset_id: int | None = None


class ValidatePlayResponse(BaseModel):
    valid: bool
    reason: str = ""


@router.post("/validate-play", response_model=ValidatePlayResponse)
async def validate_play(body: ValidatePlayRequest, session: DbSession) -> ValidatePlayResponse:
    """Whether the user may start streaming a play with this score token."""
    token = (
        await session.execute(
            select(ScoreToken).where(
                ScoreToken.id == body.score_token,
                ScoreToken.user_id == body.user_id,
                ScoreToken.score_id.is_(None),
            ),
        )
    ).scalar_one_or_none()
    if token is None:
        return ValidatePlayResponse(valid=False, reason="unknown or used score token")
    if body.beatmap_id is not None and token.beatmap_id != body.beatmap_id:
        return ValidatePlayResponse(valid=False, reason="beatmap mismatch")
    if body.ruleset_id is not None and token.ruleset_id != body.ruleset_id:
        return ValidatePlayResponse(valid=False, reason="ruleset mismatch")
    return ValidatePlayResponse(valid=True)


class UsernameResponse(BaseModel):
    username: str


@router.get("/users/{user_id}", response_model=UsernameResponse)
async def get_username(user_id: int, session: DbSession) -> UsernameResponse:
    username = (await session.execute(select(User.username).where(User.id == user_id))).scalar_one_or_none()
    if username is None:
        raise OsuError(code=404, error="not_found", message="user not found")
    return UsernameResponse(username=username)


async def notify_score_processed(user_id: int, score_id: int) -> None:
    """Tell the spectator service a score was processed (replaces the in-process dispatcher)."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{settings.spectator_internal_url.rstrip('/')}/internal/score-processed",
                json={"user_id": user_id, "score_id": score_id},
                headers={"X-Internal-Key": settings.internal_api_key},
            )
            response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Could not notify the spectator service of score %s", score_id, exc_info=True)
