"""Let a player set their own avatar, profile cover and flag.

``PUT /api/v2/me/avatar`` and ``PUT /api/v2/me/cover`` take an image (multipart field ``file``),
``PUT /api/v2/me/country`` takes the form field ``country_code`` (two letters). ``DELETE`` on the first two
removes the image. The images live in the object storage and are served, public, by ``assets_router``
(``/avatars/{id}``, ``/covers/{id}``), which is mounted at the root of the app like osu!'s asset hosts.
"""

import time

from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import Response
from fastapi import UploadFile
from fastapi import status

from app.api.deps import CurrentUser
from app.api.deps import DbSession
from app.core.config import get_settings
from app.core.error import OsuError
from app.models.user import User
from app.services.storage import get_storage

router = APIRouter()
assets_router = APIRouter()

MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_COVER_BYTES = 6 * 1024 * 1024

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_type(data: bytes) -> str | None:
    """The image type by its first bytes (the client's content type is not trusted)."""
    for signature, content_type in _SIGNATURES:
        if data.startswith(signature):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _avatar_key(user_id: int) -> str:
    return f"avatars/{user_id}"


def _cover_key(user_id: int) -> str:
    return f"covers/{user_id}"


def _asset_url(kind: str, user_id: int) -> str:
    # ``v`` changes with every upload so the client does not keep showing the old picture
    return f"{get_settings().api_base_url.rstrip('/')}/{kind}/{user_id}?v={int(time.time())}"


async def _read_image(file: UploadFile, limit: int) -> bytes:
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise OsuError(
            code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            error="File too large",
            message=f"The image must be at most {limit // (1024 * 1024)} MB.",
        )
    if sniff_image_type(data) is None:
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error="Unsupported image",
            message="Use a PNG, JPEG, GIF or WebP image.",
        )
    return data


async def _own_row(db: DbSession, user: CurrentUser) -> User:
    row = await db.get(User, user.id)
    assert row is not None
    return row


@router.put("/me/avatar")
async def set_avatar(db: DbSession, user: CurrentUser, file: UploadFile = File(...)) -> dict:
    data = await _read_image(file, MAX_AVATAR_BYTES)
    await get_storage().put_bytes(_avatar_key(user.id), data)
    row = await _own_row(db, user)
    row.avatar_url = _asset_url("avatars", user.id)
    await db.commit()
    return {"avatar_url": row.avatar_url}


@router.put("/me/cover")
async def set_cover(db: DbSession, user: CurrentUser, file: UploadFile = File(...)) -> dict:
    data = await _read_image(file, MAX_COVER_BYTES)
    await get_storage().put_bytes(_cover_key(user.id), data)
    row = await _own_row(db, user)
    row.cover_url = _asset_url("covers", user.id)
    await db.commit()
    return {"cover_url": row.cover_url}


@router.delete("/me/avatar")
async def delete_avatar(db: DbSession, user: CurrentUser) -> dict:
    row = await _own_row(db, user)
    row.avatar_url = None
    await db.commit()
    return {"avatar_url": None}


@router.delete("/me/cover")
async def delete_cover(db: DbSession, user: CurrentUser) -> dict:
    row = await _own_row(db, user)
    row.cover_url = None
    await db.commit()
    return {"cover_url": None}


@router.put("/me/country")
async def set_country(db: DbSession, user: CurrentUser, country_code: str = Form(...)) -> dict:
    code = country_code.strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error="Invalid country",
            message="country_code must be two letters (ISO 3166-1 alpha-2), for example VN.",
        )
    row = await _own_row(db, user)
    row.country_acronym = code
    await db.commit()
    return {"country_code": code}


async def _serve(key: str) -> Response:
    data = await get_storage().get_bytes(key)
    if data is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(
        content=data,
        media_type=sniff_image_type(data) or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@assets_router.get("/avatars/{user_id}")
async def get_avatar(user_id: int) -> Response:
    return await _serve(_avatar_key(user_id))


@assets_router.get("/covers/{user_id}")
async def get_cover(user_id: int) -> Response:
    return await _serve(_cover_key(user_id))
