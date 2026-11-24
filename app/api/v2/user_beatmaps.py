"""User beatmaps endpoints."""

from fastapi import APIRouter
from fastapi import Query
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy import and_
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.v2.beatmaps import _beatmap_to_compact
from app.api.v2.beatmaps import _beatmapset_to_compact
from app.api.v2.schemas import BeatmapsetResponse
from app.api.v2.schemas import UserMostPlayedResponse
from app.models.score import Score
from app.core.error import OsuError
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
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Map type to status or query
    type_lower = type.lower()

    if type_lower in {"nominated", "guest"}:  # asked by the profile page; this server has neither
        return []

    if type_lower == "most_played":
        rows = (
            await db.execute(
                select(Score.beatmap_id, func.count(Score.id).label("play_count"))
                .where(Score.user_id == user_id)
                .group_by(Score.beatmap_id)
                .order_by(desc("play_count"))
                .limit(limit)
                .offset(offset),
            )
        ).all()
        items = []
        for beatmap_id, play_count in rows:
            beatmap = await db.get(Beatmap, beatmap_id)
            beatmapset = await db.get(BeatmapSet, beatmap.beatmapset_id) if beatmap else None
            if beatmap is None or beatmapset is None:
                continue
            items.append(
                UserMostPlayedResponse(
                    beatmap_id=beatmap_id,
                    beatmapset_id=beatmap.beatmapset_id,
                    count=play_count,
                    beatmap=_beatmap_to_compact(beatmap),
                    beatmapset=_beatmapset_to_compact(beatmapset),
                ).model_dump(mode="json"),
            )
        return JSONResponse(items)

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
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error=(
                f"Invalid type: {type_lower}. Must be one of: favourite, ranked, loved, pending, graveyard"
            ),
            message=(
                f"Invalid type: {type_lower}. Must be one of: favourite, ranked, loved, pending, graveyard"
            ),
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
