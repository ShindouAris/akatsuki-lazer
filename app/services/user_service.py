"""User-related business logic."""

from datetime import UTC
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.core.security import verify_password
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
