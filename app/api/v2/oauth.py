"""OAuth2 authentication endpoints."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Union

from fastapi import APIRouter
from fastapi import Form
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi import status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession
from app.api.v2.schemas import UserResponse
from app.core.security import create_token_pair
from app.core.security import decode_token
from app.core.security import verify_password
from app.models.user import User

router = APIRouter()


class TokenResponse(BaseModel):
    """OAuth2 token response."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_token: str | None = None


@router.post("/oauth/token", response_model=TokenResponse)
async def get_token(
    db: DbSession,
    grant_type: str = Form(...),
    username: str | None = Form(None),
    password: str | None = Form(None),
    refresh_token: str | None = Form(None),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),
    scope: str = Form("*"),
) -> TokenResponse:
    """
    OAuth2 token endpoint.

    Supports:
    - password grant (username + password)
    - refresh_token grant
    - client_credentials grant
    """
    if grant_type == "password":
        if not username or not password:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_request",
                    "hint": "Username and password are required for password grant",
                    "message": "Missing username or password",
                }
            )

        # Find user by username or email
        result = await db.execute(
            select(User).where(
                (User.username == username) | (User.email == username),
            ),
        )
        user = result.scalar_one_or_none()

        if not user or not verify_password(password, user.password_hash):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "error": "invalid_grant",
                    "hint": "Invalid username or password",
                    "message": "Invalid credentials",
                }
            )

        if user.is_restricted:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": "access_denied",
                    "hint": "User account is restricted",
                    "message": "User is restricted from accessing the service",
                })

        # Parse scopes
        scopes = scope.split() if scope else ["*"]

        # Create token pair
        token_pair = create_token_pair(user.id, scopes)

        # Update last visit
        user.last_visit = datetime.now(UTC)
        await db.commit()

        return TokenResponse(
            access_token=token_pair.access_token,
            expires_in=token_pair.expires_in,
            refresh_token=token_pair.refresh_token,
        )

    elif grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Refresh token required",
            )

        token_data = decode_token(refresh_token)
        if token_data is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )

        # Verify user still exists and is active
        result = await db.execute(select(User).where(User.id == token_data.user_id))
        user = result.scalar_one_or_none()

        if not user or user.is_restricted:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or restricted",
            )

        # Create new token pair
        token_pair = create_token_pair(user.id, token_data.scopes)

        return TokenResponse(
            access_token=token_pair.access_token,
            expires_in=token_pair.expires_in,
            refresh_token=token_pair.refresh_token,
        )

    elif grant_type == "client_credentials":
        # Client credentials flow - used for service-to-service
        # For now, return a limited token
        if not client_id or not client_secret:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Client credentials required",
            )

        # TODO: Validate client credentials against oauth_clients table
        # For now, return a token with limited scopes
        from app.core.security import create_access_token

        access_token = create_access_token(
            data={"sub": 0, "scopes": ["public"]},
            expires_delta=timedelta(hours=1),
        )

        return TokenResponse(
            access_token=access_token,
            expires_in=3600,
            refresh_token=None,
        )

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported grant type: {grant_type}",
        )


@router.post("/oauth/tokens/current", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_current_token() -> None:
    """Revoke the current access token."""
    # TODO: Implement token revocation
    pass
