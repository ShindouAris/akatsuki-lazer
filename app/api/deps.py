"""API dependencies for authentication and database access."""

from typing import TYPE_CHECKING
from typing import Annotated

from fastapi import Depends
from fastapi import Request
from fastapi import status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.error import OsuError
from app.core.security import decode_token
from app.models.user import User

if TYPE_CHECKING:
    from app.hubs.spectator import SpectatorHub

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/oauth/token", auto_error=False)


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str | None, Depends(oauth2_scheme)],
) -> User | None:
    """Get the current authenticated user (optional)."""
    if not token:
        return None

    token_data = decode_token(token)
    if token_data is None:
        return None

    result = await db.execute(select(User).where(User.id == token_data.user_id))
    user = result.scalar_one_or_none()

    if user is None or user.is_restricted:
        return None

    return user


async def get_current_user_required(
    user: Annotated[User | None, Depends(get_current_user)],
) -> User:
    """Get the current authenticated user (required)."""
    if user is None:
        raise OsuError(
            code=status.HTTP_401_UNAUTHORIZED,
            error="Not authenticated",
            message="Not authenticated",
        )
    return user


async def get_current_active_user(
    user: Annotated[User, Depends(get_current_user_required)],
) -> User:
    """Get the current active (non-restricted) user."""
    if not user.is_active:
        raise OsuError(
            code=status.HTTP_403_FORBIDDEN,
            error="Inactive user",
            message="Inactive user",
        )
    return user


def get_spectator_hub(request: Request) -> "SpectatorHub":
    """Get the spectator hub from app state."""
    return request.app.state.spectator_hub


# Type aliases for cleaner dependency injection
DbSession = Annotated[AsyncSession, Depends(get_db)]
OptionalUser = Annotated[User | None, Depends(get_current_user)]
CurrentUser = Annotated[User, Depends(get_current_user_required)]
ActiveUser = Annotated[User, Depends(get_current_active_user)]
SpectatorHubDep = Annotated["SpectatorHub", Depends(get_spectator_hub)]
