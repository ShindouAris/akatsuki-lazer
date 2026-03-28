"""User activity and kudosu endpoints."""

from fastapi import APIRouter
from fastapi import Query
from fastapi import status
from sqlalchemy import desc
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.v2.schemas import KudosuHistoryResponse
from app.api.v2.schemas import UserActivityResponse
from app.core.error import OsuError
from app.models.beatmap import Beatmap
from app.models.user import KudosuHistory
from app.models.user import User
from app.models.user import UserActivity

router = APIRouter()


@router.get("/users/{user_id}/recent_activity", response_model=list[UserActivityResponse])
async def get_user_recent_activity(
    db: DbSession,
    user_id: int,
    limit: int = Query(51, ge=1, le=51),  # osu! standard is 51
) -> list[UserActivityResponse]:
    """Get user's recent activity.
    
    Args:
        user_id: ID of the user
        limit: Number of results (1-51, default 51)
        
    Returns:
        List of recent activities
    """
    # Verify user exists
    user = await db.get(User, user_id)
    if not user:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Get recent activities
    result = await db.execute(
        select(UserActivity)
        .where(UserActivity.user_id == user_id)
        .order_by(desc(UserActivity.created_at))
        .limit(limit)
    )

    activities = result.scalars().all()

    # Populate beatmap for activities that have beatmap_id
    responses = []
    for activity in activities:
        if activity.beatmap_id:
            beatmap = await db.get(Beatmap, activity.beatmap_id)
            activity.beatmap = beatmap  # type: ignore
        responses.append(activity)

    return responses


@router.get("/users/{user_id}/kudosu", response_model=list[KudosuHistoryResponse])
async def get_user_kudosu_history(
    db: DbSession,
    user_id: int,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[KudosuHistoryResponse]:
    """Get user's kudosu voting history.
    
    Args:
        user_id: ID of the user
        limit: Number of results (1-100, default 50)
        offset: Number of results to skip
        
    Returns:
        List of kudosu history
    """
    # Verify user exists
    user = await db.get(User, user_id)
    if not user:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Get kudosu history
    result = await db.execute(
        select(KudosuHistory)
        .where(KudosuHistory.user_id == user_id)
        .order_by(desc(KudosuHistory.created_at))
        .limit(limit)
        .offset(offset)
    )

    histories = result.scalars().all()

    # Populate beatmap for kudosu entries that have beatmap_id
    responses = []
    for history in histories:
        if history.beatmap_id:
            beatmap = await db.get(Beatmap, history.beatmap_id)
            history.beatmap = beatmap  # type: ignore
        responses.append(history)

    return responses
