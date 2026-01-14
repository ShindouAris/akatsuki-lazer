"""Current user (/me) endpoints."""

from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.api.v2.schemas import RankHistoryResponse
from app.api.v2.schemas import UserResponse
from app.api.v2.schemas import UserStatisticsResponse
from app.models.user import GameMode

router = APIRouter()


def _mode_to_string(mode: GameMode) -> str:
    """Convert GameMode enum to string."""
    return {
        GameMode.OSU: "osu",
        GameMode.TAIKO: "taiko",
        GameMode.CATCH: "fruits",
        GameMode.MANIA: "mania",
    }.get(mode, "osu")


def _get_user_statistics(user: CurrentUser, mode: GameMode) -> UserStatisticsResponse:
    """Get user statistics for a specific mode."""
    mode_str = _mode_to_string(mode)

    for stats in user.statistics:
        if stats.mode == mode:
            # User is ranked if they have a global rank
            is_ranked = stats.global_rank is not None

            # Only provide rank_history if user is ranked
            rank_history = None
            if is_ranked:
                rank_history = RankHistoryResponse(mode=mode_str, data=[])

            return UserStatisticsResponse(
                ranked_score=stats.ranked_score,
                total_score=stats.total_score,
                pp=stats.pp,
                global_rank=stats.global_rank,
                global_rank_percent=None,
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

    # Return unranked stats if none found
    return UserStatisticsResponse(
        is_ranked=False,
        rank_history=None,
    )


@router.get("/me", response_model=UserResponse)
@router.get("/me/", response_model=UserResponse, include_in_schema=False)
async def get_current_user(user: CurrentUser) -> UserResponse:
    """Get the current authenticated user's profile."""
    mode = user.playmode
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


@router.get("/me/{mode}", response_model=UserResponse)
async def get_current_user_mode(user: CurrentUser, mode: str) -> UserResponse:
    """Get the current authenticated user's profile for a specific mode."""
    mode_enum = {
        "osu": GameMode.OSU,
        "taiko": GameMode.TAIKO,
        "fruits": GameMode.CATCH,
        "mania": GameMode.MANIA,
    }.get(mode, GameMode.OSU)

    stats = _get_user_statistics(user, mode_enum)

    return UserResponse(
        id=user.id,
        username=user.username,
        avatar_url=user.avatar_url,
        cover_url=user.cover_url,
        country_code=user.country_acronym,
        title=user.title,
        playmode=mode,
        playstyle=user.playstyle.split(",") if user.playstyle else None,
        is_active=user.is_active,
        is_bot=user.is_bot,
        is_supporter=user.is_supporter,
        is_restricted=user.is_restricted,
        join_date=user.created_at,
        last_visit=user.last_visit,
        statistics=stats,
    )
