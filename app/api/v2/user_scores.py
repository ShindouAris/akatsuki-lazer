"""User scores endpoints."""

from fastapi import APIRouter
from fastapi import Query
from fastapi import status
from sqlalchemy import and_
from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.v2.schemas import SoloScoreResponse
from app.api.v2.schemas import UserMostPlayedResponse
from app.core.error import OsuError
from app.models.beatmap import Beatmap
from app.models.score import Score
from app.models.user import User

router = APIRouter()


@router.get("/users/{user_id}/scores/{type}", response_model=list[SoloScoreResponse])
async def get_user_scores_by_type(
    db: DbSession,
    user_id: int,
    type: str,
    mode: str | None = Query(None, description="Ruleset: osu, taiko, fruits, mania"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[SoloScoreResponse]:
    """Get user's scores by type (best, recent, firsts).
    
    Args:
        user_id: ID of the user
        type: One of: best, recent, firsts
        mode: Optional filter by game mode
        limit: Number of results (1-100, default 50)
        offset: Number of results to skip
        
    Returns:
        List of scores
    """
    # Verify user exists
    user = await db.get(User, user_id)
    if not user:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Map mode string to ruleset_id
    mode_to_ruleset = {
        "osu": 0,
        "taiko": 1,
        "fruits": 2,
        "catch": 2,
        "mania": 3,
    }
    ruleset_id = mode_to_ruleset.get(mode.lower()) if mode else None

    type_lower = type.lower()

    if type_lower == "best":
        # Top scores by PP
        query = (
            select(Score)
            .where(
                and_(
                    Score.user_id == user_id,
                    Score.ranked == True,  # noqa: E712
                    Score.passed == True,  # noqa: E712
                ),
            )
            .order_by(desc(Score.pp), desc(Score.ended_at))
        )
        if ruleset_id is not None:
            query = query.where(Score.ruleset_id == ruleset_id)
        query = query.limit(limit).offset(offset)
        result = await db.execute(query)
    elif type_lower == "recent":
        # Recent scores
        query = (
            select(Score)
            .where(Score.user_id == user_id)
            .order_by(desc(Score.ended_at))
        )
        if ruleset_id is not None:
            query = query.where(Score.ruleset_id == ruleset_id)
        query = query.limit(limit).offset(offset)
        result = await db.execute(query)
    elif type_lower == "firsts":
        # First place scores on beatmaps
        # Get beatmaps where user has the top score
        from sqlalchemy.orm import aliased

        # Subquery: find max pp score per beatmap
        max_score_subquery = (
            select(Score.beatmap_id, func.max(Score.pp).label("max_pp"))
            .where(
                and_(
                    Score.ranked == True,  # noqa: E712
                    Score.passed == True,  # noqa: E712
                ),
            )
            .group_by(Score.beatmap_id)
            .subquery()
        )

        # Main query: get user's scores that match the max
        query = (
            select(Score)
            .join(
                max_score_subquery,
                and_(
                    Score.beatmap_id == max_score_subquery.c.beatmap_id,
                    Score.pp == max_score_subquery.c.max_pp,
                ),
            )
            .where(Score.user_id == user_id)
            .order_by(desc(Score.ended_at))
        )
        if ruleset_id is not None:
            query = query.where(Score.ruleset_id == ruleset_id)
        query = query.limit(limit).offset(offset)
        result = await db.execute(query)
    else:
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error=f"Invalid type: {type_lower}. Must be one of: best, recent, firsts",
            message=f"Invalid type: {type_lower}. Must be one of: best, recent, firsts",
        )

    scores = result.scalars().all()

    # Populate beatmap for each score
    responses = []
    for score in scores:
        beatmap = await db.get(Beatmap, score.beatmap_id)
        score.beatmap = beatmap  # type: ignore
        responses.append(score)

    return responses


@router.get("/users/{user_id}/beatmaps/most_played", response_model=list[UserMostPlayedResponse])
async def get_user_most_played_beatmaps(
    db: DbSession,
    user_id: int,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[UserMostPlayedResponse]:
    """Get user's most played beatmaps.
    
    Args:
        user_id: ID of the user
        limit: Number of results (1-100, default 50)
        offset: Number of results to skip
        
    Returns:
        List of most played beatmaps with play counts
    """
    # Verify user exists
    user = await db.get(User, user_id)
    if not user:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Count scores per beatmap
    result = await db.execute(
        select(
            Score.beatmap_id,
            func.count(Score.id).label("count"),
        )
        .where(Score.user_id == user_id)
        .group_by(Score.beatmap_id)
        .order_by(desc("count"))
        .limit(limit)
        .offset(offset)
    )

    beatmap_counts = result.fetchall()

    responses = []
    for beatmap_id, count in beatmap_counts:
        # Get beatmap and beatmapset
        beatmap = await db.get(Beatmap, beatmap_id)
        if beatmap:
            response = UserMostPlayedResponse(
                beatmap_id=beatmap_id,
                beatmapset_id=beatmap.beatmapset_id,
                count=count,
                beatmap=beatmap,
            )
            responses.append(response)

    return responses
