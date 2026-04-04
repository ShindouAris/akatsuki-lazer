"""User endpoints."""

from fastapi import APIRouter
from fastapi import Form
from fastapi import Query
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from sqlalchemy import and_

from app.api.deps import CurrentUser
from app.api.deps import DbSession
from app.api.v2.schemas import RankHistoryResponse, UserLookupResponse
from app.api.v2.schemas import UserCompact
from app.api.v2.schemas import UserResponse
from app.api.v2.schemas import UserStatisticsResponse
from app.core.error import OsuError
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserRelation
from app.services.user_service import create_user
from app.services.user_service import get_user_by_email
from app.services.user_service import get_user_by_username

router = APIRouter()
root_registration_router = APIRouter()


def _is_valid_email(email: str) -> bool:
    """Basic email validation for registration contract."""
    if "@" not in email:
        return False

    local, _, domain = email.partition("@")
    return bool(local and domain and "." in domain and not domain.startswith(".") and not domain.endswith("."))


def _form_error_response(errors: dict[str, list[str]]) -> JSONResponse:
    """Return field validation errors in osu!web-compatible shape."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"form_error": {"user": errors}},
    )


async def _register_user_impl(
    db: DbSession,
    username: str,
    user_email: str,
    password: str,
) -> JSONResponse:
    """Register a user using Rails-style form fields and error contract."""
    errors: dict[str, list[str]] = {}

    if not _is_valid_email(user_email):
        errors["user_email"] = ["Email is invalid"]

    if len(password) < 6:
        errors["password"] = ["Password too short"]

    if await get_user_by_username(db, username) is not None:
        errors["username"] = ["Username already taken"]

    if errors:
        return _form_error_response(errors)

    try:
        if await get_user_by_email(db, user_email) is not None:
            return _form_error_response({"user_email": ["Email is invalid"]})

        await create_user(db, username=username, email=user_email, password=password)
        return JSONResponse(status_code=status.HTTP_200_OK, content={})
    except IntegrityError as exc:
        await db.rollback()

        error_text = str(exc.orig).lower() if exc.orig is not None else str(exc).lower()
        db_errors: dict[str, list[str]] = {}

        if "username" in error_text:
            db_errors["username"] = ["Username already taken"]

        if "email" in error_text:
            db_errors["user_email"] = ["Email is invalid"]

        if db_errors:
            return _form_error_response(db_errors)

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Something went wrong"},
        )
    except Exception:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Something went wrong"},
        )


@router.post("/users")
async def register_user(
    db: DbSession,
    username: str = Form(..., alias="user[username]"),
    user_email: str = Form(..., alias="user[user_email]"),
    password: str = Form(..., alias="user[password]"),
) -> JSONResponse:
    """Register a new user via /api/v2/users."""
    return await _register_user_impl(db, username, user_email, password)


@root_registration_router.post("/users")
async def register_user_root(
    db: DbSession,
    username: str = Form(..., alias="user[username]"),
    user_email: str = Form(..., alias="user[user_email]"),
    password: str = Form(..., alias="user[password]"),
) -> JSONResponse:
    """Register a new user via root /users endpoint."""
    return await _register_user_impl(db, username, user_email, password)


def _mode_to_string(mode: GameMode) -> str:
    """Convert GameMode enum to string."""
    return {
        GameMode.OSU: "osu",
        GameMode.TAIKO: "taiko",
        GameMode.CATCH: "fruits",
        GameMode.MANIA: "mania",
    }.get(mode, "osu")


def _string_to_mode(mode: str) -> GameMode | None:
    """Convert string to GameMode enum."""
    return {
        "osu": GameMode.OSU,
        "taiko": GameMode.TAIKO,
        "fruits": GameMode.CATCH,
        "mania": GameMode.MANIA,
    }.get(mode)


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

@router.get("/users/")
async def get_user_by_id(
    db: DbSession,
    ids: list[int] | list[str] = Query(..., description="User IDs or usernames", alias="ids[]"),
) -> dict[str, list[UserResponse]]:
    """Get a user by ID or username."""
    if len(ids) > 50:
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error="Too many IDs",
            message="Maximum 50 IDs allowed",
        )
    # Determine lookup method
    result = await db.execute(select(User).where(User.id.in_(ids)))
    users = result.scalars().all()

    if len(users) != len(ids):
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="One or more users not found",
        )

    return {
        "users": [_user_to_response(u) for u in users]
    }


@router.get("/users/lookup/", response_model=UserLookupResponse, include_in_schema=False)
@router.get("/users/lookup", response_model=UserLookupResponse, include_in_schema=False)
async def lookup_user(
    db: DbSession,
    id: int | None = Query(None),
    username: str | None = Query(None),
    ids_bracket: list[int] = Query(default_factory=list, alias="ids[]"),
    ids: list[int] = Query(default_factory=list),
) -> UserLookupResponse:
    """Lookup a user by ID or username."""
    resolved_id = id
    if resolved_id is None:
        requested_ids = [*ids_bracket, *ids]
        if requested_ids:
            resolved_id = requested_ids[0]

    if resolved_id is not None:
        result = await db.execute(select(User).where(User.id == resolved_id))
    elif username:
        result = await db.execute(select(User).where(User.username == username))
    else:
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error="Must provide id or username",
            message="Must provide id or username",
        )

    user = result.scalar_one_or_none()

    if not user:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    return UserLookupResponse(
        users=[
            UserCompact(
                id=user.id,
                username=user.username,
                avatar_url=user.avatar_url,
                country_code=user.country_acronym,
                is_active=user.is_active,
                is_bot=user.is_bot,
                is_supporter=user.is_supporter,
            )
        ]
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
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
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
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    mode_enum = _string_to_mode(mode)
    if mode_enum is None:
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error=f"Invalid ruleset: {mode}",
            message=f"Invalid ruleset: {mode}",
        )

    return _user_to_response(user, mode_enum)


@router.post("/users/{user_id}/block")
async def block_user(
    user_id: int,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Block a user."""
    # Can't block yourself
    if user_id == user.id:
        raise OsuError(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error="Cannot block yourself",
            message="Cannot block yourself",
        )

    # Check if target user exists and is active
    target = await db.get(User, user_id)
    if not target or not target.is_active:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Check block limit
    block_count_result = await db.execute(
        select(UserRelation).where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.foe == True,  # noqa: E712
            ),
        ),
    )
    block_count = len(block_count_result.fetchall())

    if block_count >= user.max_blocks:
        raise OsuError(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error=f"Block limit reached ({user.max_blocks})",
            message=f"Block limit reached ({user.max_blocks})",
        )

    # Check if relation already exists
    existing = await db.execute(
        select(UserRelation).where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.target_id == user_id,
            ),
        ),
    )
    relation = existing.scalar_one_or_none()

    if relation:
        if relation.foe:
            raise OsuError(
                code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                error="Already blocking this user",
                message="Already blocking this user",
            )
        # Was friend, now becoming blocked - remove friend, add block
        relation.friend = False
        relation.foe = True
    else:
        relation = UserRelation(
            user_id=user.id,
            target_id=user_id,
            friend=False,
            foe=True,
        )
        db.add(relation)

    await db.commit()
    return {}


@router.delete("/users/{user_id}/block")
async def unblock_user(
    user_id: int,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Unblock a user."""
    # Find the relation
    result = await db.execute(
        select(UserRelation).where(
            and_(
                UserRelation.user_id == user.id,
                UserRelation.target_id == user_id,
                UserRelation.foe == True,  # noqa: E712
            ),
        ),
    )
    relation = result.scalar_one_or_none()

    if not relation:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="Block not found",
            message="Block not found",
        )

    # If they're also a friend, just remove block status; otherwise delete
    if relation.friend:
        relation.foe = False
    else:
        await db.delete(relation)

    await db.commit()
    return {}
