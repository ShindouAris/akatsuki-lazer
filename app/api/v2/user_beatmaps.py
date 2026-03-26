"""User beatmaps endpoints."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy import and_
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.v2.schemas import BeatmapsetResponse
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus
from app.models.user import User
from app.models.user import UserBeatmapFavorite

router = APIRouter()


@router.get("/users/{user_id}/beatmapsets/{type}", response_model=list[BeatmapsetResponse])
async def get_user_beatmaps(
    db: DbSession,
    user_id: int,
    type: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[BeatmapsetResponse]:
    """Get user's beatmapsets by type (favourite, ranked, loved, pending, graveyard).
    
    Args:
        user_id: ID of the user
        type: One of: favourite, ranked, loved, pending, graveyard
        limit: Number of results (1-100, default 50)
        offset: Number of results to skip
        
    Returns:
        List of beatmapsets
    """
    # Verify user exists
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Map type to status or query
    type_lower = type.lower()

    if type_lower == "favourite":
        # Get favourite beatmapsets
        result = await db.execute(
            select(BeatmapSet)
            .join(UserBeatmapFavorite, BeatmapSet.id == UserBeatmapFavorite.beatmapset_id)
            .where(UserBeatmapFavorite.user_id == user_id)
            .order_by(desc(UserBeatmapFavorite.created_at))
            .limit(limit)
            .offset(offset)
        )
    elif type_lower == "ranked":
        # Ranked beatmapsets by this user
        result = await db.execute(
            select(BeatmapSet)
            .where(
                and_(
                    BeatmapSet.user_id == user_id,
                    BeatmapSet.status == BeatmapStatus.RANKED,
                ),
            )
            .order_by(desc(BeatmapSet.last_updated))
            .limit(limit)
            .offset(offset)
        )
    elif type_lower == "loved":
        # Loved beatmapsets by this user
        result = await db.execute(
            select(BeatmapSet)
            .where(
                and_(
                    BeatmapSet.user_id == user_id,
                    BeatmapSet.status == BeatmapStatus.LOVED,
                ),
            )
            .order_by(desc(BeatmapSet.last_updated))
            .limit(limit)
            .offset(offset)
        )
    elif type_lower == "pending":
        # Pending beatmapsets by this user
        result = await db.execute(
            select(BeatmapSet)
            .where(
                and_(
                    BeatmapSet.user_id == user_id,
                    BeatmapSet.status == BeatmapStatus.PENDING,
                ),
            )
            .order_by(desc(BeatmapSet.last_updated))
            .limit(limit)
            .offset(offset)
        )
    elif type_lower == "graveyard":
        # Graveyard beatmapsets by this user
        result = await db.execute(
            select(BeatmapSet)
            .where(
                and_(
                    BeatmapSet.user_id == user_id,
                    BeatmapSet.status == BeatmapStatus.GRAVEYARD,
                ),
            )
            .order_by(desc(BeatmapSet.last_updated))
            .limit(limit)
            .offset(offset)
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid type: {type_lower}. Must be one of: favourite, ranked, loved, pending, graveyard",
        )

    beatmapsets = result.scalars().all()

    # Get beatmaps for each beatmapset
    responses = []
    for beatmapset in beatmapsets:
        # Query beatmaps for this beatmapset
        beatmaps_result = await db.execute(
            select(Beatmap).where(Beatmap.beatmapset_id == beatmapset.id),
        )
        beatmap_list = beatmaps_result.scalars().all()

        beatmapset.beatmaps = beatmap_list
        responses.append(beatmapset)

    return responses
