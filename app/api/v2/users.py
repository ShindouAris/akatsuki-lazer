"""User endpoints."""

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.v2.schemas import RankHistoryResponse
from app.api.v2.schemas import UserCompact
from app.api.v2.schemas import UserResponse
from app.api.v2.schemas import UserStatisticsResponse
from app.models.user import GameMode
from app.models.user import User

router = APIRouter()


def _mode_to_string(mode: GameMode) -> str:
    """Convert GameMode enum to string."""
    return {
        GameMode.OSU: "osu",
        GameMode.TAIKO: "taiko",
        GameMode.CATCH: "fruits",
        GameMode.MANIA: "mania",
    }.get(mode, "osu")


def _string_to_mode(mode: str) -> GameMode:
    """Convert string to GameMode enum."""
    return {
        "osu": GameMode.OSU,
        "taiko": GameMode.TAIKO,
        "fruits": GameMode.CATCH,
        "mania": GameMode.MANIA,
    }.get(mode, GameMode.OSU)


def _get_user_statistics(user: User, mode: GameMode) -> UserStatisticsResponse:
    """Get user statistics for a specific mode."""
    mode_str = _mode_to_string(mode)

    for stats in user.statistics:
        if stats.mode == mode:
            # User is ranked if they have a global rank
            is_ranked = stats.global_rank is not None

            # Only provide rank_history if user is ranked
            # If is_ranked is True but rank_history is None, the client will show loading
            rank_history = None
            if is_ranked:
                # Provide empty history for now (no historical data yet)
                rank_history = RankHistoryResponse(mode=mode_str, data=[])

            return UserStatisticsResponse(
                ranked_score=stats.ranked_score,
                total_score=stats.total_score,
                pp=stats.pp,
                global_rank=stats.global_rank,
                global_rank_percent=None,  # Would need total player count to calculate
                country_rank=stats.country_rank,
                is_ranked=is_ranked,
                rank_history=rank_history,
                hit_accuracy=stats.hit_accuracy,
                play_count=stats.play_count,
                play_time=stats.play_time,
                total_hits=stats.total_hits,
                maximum_combo=stats.maximum_combo,
                replays_watched=stats.replays_watched,
                grade_counts={
                    "ss": stats.grade_ss,
                    "ssh": stats.grade_ssh,
                    "s": stats.grade_s,
                    "sh": stats.grade_sh,
                    "a": stats.grade_a,
                },
                level={
                    "current": stats.level,
                    "progress": stats.level_progress,
                },
            )

    # Return unranked stats for users with no statistics
    return UserStatisticsResponse(
        is_ranked=False,
        rank_history=None,
    )


def _user_to_response(user: User, mode: GameMode | None = None) -> UserResponse:
    """Convert User model to UserResponse."""
    mode = mode or user.playmode
    stats = _get_user_statistics(user, mode)

    return UserResponse(
        id=user.id,
        username=user.username,
        avatar_url=user.avatar_url,
        cover_url=user.cover_url,
        country_code=user.country_acronym,
        title=user.title,
        playmode=_mode_to_string(mode),
        playstyle=user.playstyle.split(",") if user.playstyle else None,
        is_active=user.is_active,
        is_bot=user.is_bot,
        is_supporter=user.is_supporter,
        is_restricted=user.is_restricted,
        join_date=user.created_at,
        last_visit=user.last_visit,
        statistics=stats,
    )


@router.get("/users/{user_id}", response_model=UserResponse)
@router.get("/users/{user_id}/", response_model=UserResponse, include_in_schema=False)
async def get_user(
    db: DbSession,
    user_id: int | str,
    key: str | None = Query(None, description="Lookup type: id, username"),
) -> UserResponse:
    """Get a user by ID or username."""
    # Determine lookup method
    if key == "username" or (isinstance(user_id, str) and not user_id.isdigit()):
        result = await db.execute(select(User).where(User.username == str(user_id)))
    else:
        uid = int(user_id) if isinstance(user_id, str) else user_id
        result = await db.execute(select(User).where(User.id == uid))

    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return _user_to_response(user)


@router.get("/users/{user_id}/{mode}", response_model=UserResponse)
async def get_user_mode(
    db: DbSession,
    user_id: int | str,
    mode: str,
    key: str | None = Query(None),
) -> UserResponse:
    """Get a user by ID or username with specific mode statistics."""
    # Determine lookup method
    if key == "username" or (isinstance(user_id, str) and not user_id.isdigit()):
        result = await db.execute(select(User).where(User.username == str(user_id)))
    else:
        uid = int(user_id) if isinstance(user_id, str) else user_id
        result = await db.execute(select(User).where(User.id == uid))

    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    mode_enum = _string_to_mode(mode)
    return _user_to_response(user, mode_enum)


@router.get("/users/lookup", response_model=UserCompact)
async def lookup_user(
    db: DbSession,
    id: int | None = Query(None),
    username: str | None = Query(None),
) -> UserCompact:
    """Lookup a user by ID or username."""
    if id:
        result = await db.execute(select(User).where(User.id == id))
    elif username:
        result = await db.execute(select(User).where(User.username == username))
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide id or username",
        )

    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    return UserCompact(
        id=user.id,
        username=user.username,
        avatar_url=user.avatar_url,
        country_code=user.country_acronym,
        is_active=user.is_active,
        is_bot=user.is_bot,
        is_supporter=user.is_supporter,
    )
