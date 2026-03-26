"""User-related business logic."""

from datetime import UTC
from datetime import datetime
from math import pow

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.core.security import verify_password
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics


async def create_user(
    db: AsyncSession,
    username: str,
    email: str,
    password: str,
    country_acronym: str = "XX",
) -> User:
    """Create a new user account."""
    # Hash password
    password_hash = get_password_hash(password)

    # Create user
    user = User(
        username=username,
        email=email,
        password_hash=password_hash,
        country_acronym=country_acronym,
        created_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()

    # Create statistics for each game mode
    for mode in GameMode:
        stats = UserStatistics(
            user_id=user.id,
            mode=mode,
        )
        db.add(stats)

    await db.commit()
    await db.refresh(user)

    return user


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    """Get a user by ID."""
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """Get a user by username."""
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Get a user by email."""
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def authenticate_user(
    db: AsyncSession,
    username_or_email: str,
    password: str,
) -> User | None:
    """Authenticate a user by username/email and password."""
    # Try to find by username or email
    result = await db.execute(
        select(User).where(
            (User.username == username_or_email) | (User.email == username_or_email),
        ),
    )
    user = result.scalar_one_or_none()

    if not user:
        return None

    if not verify_password(password, user.password_hash):
        return None

    return user


async def update_user_last_visit(db: AsyncSession, user: User) -> None:
    """Update user's last visit timestamp."""
    user.last_visit = datetime.now(UTC)
    await db.commit()


async def get_user_statistics(
    db: AsyncSession,
    user_id: int,
    mode: GameMode,
) -> UserStatistics | None:
    """Get user statistics for a specific game mode."""
    result = await db.execute(
        select(UserStatistics).where(
            (UserStatistics.user_id == user_id) & (UserStatistics.mode == mode),
        ),
    )
    return result.scalar_one_or_none()


async def update_user_statistics(
    db: AsyncSession,
    stats: UserStatistics,
    score_data: dict,
) -> None:
    """Update user statistics after a score submission."""
    stats.play_count += 1
    stats.total_score += score_data.get("total_score", 0)

    # Update ranked score if the score is ranked
    if score_data.get("ranked", False):
        stats.ranked_score += score_data.get("total_score", 0)

    # Update max combo
    if score_data.get("max_combo", 0) > stats.maximum_combo:
        stats.maximum_combo = score_data["max_combo"]

    # TODO: Implement PP calculation and ranking updates

    await db.commit()


async def calculate_weighted_pp(db: AsyncSession, user_id: int, mode: GameMode) -> float:
    """Calculate weighted PP from top 100 ranked scores for a user/mode."""
    result = await db.execute(
        select(Score.pp)
        .where(
            and_(
                Score.user_id == user_id,
                Score.ruleset_id == int(mode),
                Score.passed.is_(True),
                Score.ranked.is_(True),
                Score.pp.is_not(None),
            ),
        )
        .order_by(Score.pp.desc())
        .limit(100),
    )
    top_pps = [float(row[0]) for row in result.fetchall() if row[0] is not None]

    weighted_pp = 0.0
    for index, value in enumerate(top_pps):
        weighted_pp += value * pow(0.95, index)

    return round(weighted_pp, 5)


async def refresh_user_pp_and_ranks(db: AsyncSession, user_id: int, mode: GameMode) -> None:
    """Recalculate a user's PP and update global/country rank snapshots."""
    stats_result = await db.execute(
        select(UserStatistics)
        .where(
            and_(
                UserStatistics.user_id == user_id,
                UserStatistics.mode == mode,
            ),
        ),
    )
    stats = stats_result.scalar_one_or_none()
    if stats is None:
        return

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        return

    weighted_pp = await calculate_weighted_pp(db, user_id=user_id, mode=mode)
    stats.pp = weighted_pp

    global_rank_result = await db.execute(
        select(func.count(UserStatistics.id))
        .join(User, User.id == UserStatistics.user_id)
        .where(
            and_(
                UserStatistics.mode == mode,
                UserStatistics.pp > weighted_pp,
                User.is_restricted.is_(False),
                User.is_bot.is_(False),
            ),
        ),
    )
    stats.global_rank = int(global_rank_result.scalar_one()) + 1

    country_rank_result = await db.execute(
        select(func.count(UserStatistics.id))
        .join(User, User.id == UserStatistics.user_id)
        .where(
            and_(
                UserStatistics.mode == mode,
                UserStatistics.pp > weighted_pp,
                User.country_acronym == user.country_acronym,
                User.is_restricted.is_(False),
                User.is_bot.is_(False),
            ),
        ),
    )
    stats.country_rank = int(country_rank_result.scalar_one()) + 1

    await db.flush()
