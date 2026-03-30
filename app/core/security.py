"""Security utilities for authentication and authorization."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import bcrypt
from jose import JWTError
from jose import jwt
from pydantic import BaseModel
from pathlib import Path
from app.core.config import get_settings

settings = get_settings()


class TokenData(BaseModel):
    """Data extracted from JWT token."""

    user_id: int
    scopes: list[str] = []
    exp: datetime | None = None


class TokenPair(BaseModel):
    """OAuth2 token response."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_token: str | None = None

pj_root = Path(__file__).parent.parent.parent # core -> app -> parent
private_key_path = Path(f"{pj_root}/cert/private.pem")
public_key_path = Path(f"{pj_root}/cert/public.pem")

with open(private_key_path, "r") as f:
    PRIVATE_KEY = f.read()

with open(public_key_path, "r") as f:
    PUBLIC_KEY = f.read()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def get_password_hash(password: str) -> str:
    """Hash a password."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    # Convert sub to string (JWT spec requires sub to be a string)
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(
            minutes=settings.access_token_expire_minutes,
        )
    to_encode.update({"exp": expire, "type": "access", "aud": settings.oauth_client_id})
    return jwt.encode(to_encode, PRIVATE_KEY, algorithm="RS256")


def create_refresh_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT refresh token."""
    to_encode = data.copy()
    # Convert sub to string (JWT spec requires sub to be a string)
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, PRIVATE_KEY, algorithm="RS256")


def decode_token(token: str) -> TokenData | None:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(
            token,
            PUBLIC_KEY,
            algorithms=["RS256"],
            audience=settings.oauth_client_id,
        )
        sub = payload.get("sub")
        if sub is None:
            return None
        # Convert sub back to int (it's stored as string in JWT)
        user_id = int(sub)
        scopes: list[str] = payload.get("scopes", [])
        exp = payload.get("exp")
        exp_datetime = datetime.fromtimestamp(exp, tz=UTC) if exp else None
        return TokenData(user_id=user_id, scopes=scopes, exp=exp_datetime)
    except (JWTError, ValueError):
        return None


def create_token_pair(user_id: int, scopes: list[str]) -> TokenPair:
    """Create an access/refresh token pair for a user."""
    access_token = create_access_token(
        data={"sub": user_id, "scopes": scopes},
    )
    refresh_token = create_refresh_token(
        data={"sub": user_id, "scopes": scopes},
    )
    return TokenPair(
        access_token=access_token,
        expires_in=settings.access_token_expire_minutes * 60,
        refresh_token=refresh_token,
    )
