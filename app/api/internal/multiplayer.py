"""Internal persistence API for the realtime multiplayer service (see ./multiplayer).

The Go service owns live room state but this process owns the database. Everything is authenticated with the
shared ``INTERNAL_API_KEY`` (header ``X-Internal-Key``); with no key configured the API is disabled.

Design notes:

* Only operations that must return database ids are meant to be awaited by the caller: creating a room and
  adding a playlist item.
* Everything else is a *full state* ``PUT``. The caller coalesces and retries these, so every handler here is
  idempotent and order independent: the latest state simply wins.
"""

import json
import logging
import secrets
from datetime import UTC
from datetime import datetime
from typing import Annotated
from typing import Any
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Header
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbSession
from app.core.config import get_settings
from app.core.error import OsuError
from app.models.multiplayer import MultiplayerPlaylistItem
from app.models.multiplayer import MultiplayerRoom
from app.models.multiplayer import RoomStatus
from app.models.multiplayer import RoomType
from app.models.user import User
from app.models.user import UserRelation
from app.services.beatmaps import BeatmapService
from app.services.chat import sync_room_members

logger = logging.getLogger(__name__)

DEFAULT_MAX_PARTICIPANTS = 16
# Rooms created by the realtime hub (as opposed to REST created playlists and the daily challenge).
REALTIME_ROOM_TYPES = (RoomType.HEAD_TO_HEAD.value, RoomType.TEAM_VERSUS.value, RoomType.RANKED_PLAY.value)


async def require_internal_key(x_internal_key: Annotated[str | None, Header()] = None) -> None:
    """Reject callers without the shared key. Constant time comparison."""
    expected = get_settings().internal_api_key
    if not expected:
        raise OsuError(
            code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error="internal_api_disabled",
            message="INTERNAL_API_KEY is not configured",
        )
    if x_internal_key is None or not secrets.compare_digest(x_internal_key.encode(), expected.encode()):
        raise OsuError(
            code=status.HTTP_401_UNAUTHORIZED,
            error="unauthorized",
            message="Invalid internal API key",
        )


router = APIRouter(prefix="/internal/multiplayer", dependencies=[Depends(require_internal_key)])


class ItemIn(BaseModel):
    owner_id: int | None = None
    beatmap_id: int
    ruleset_id: int = Field(default=0, ge=0, le=3)
    required_mods: list[dict[str, Any]] = Field(default_factory=list)
    allowed_mods: list[dict[str, Any]] = Field(default_factory=list)
    playlist_order: int = Field(default=0, ge=0)
    expired: bool = False
    played_at: datetime | None = None


class RoomStateIn(BaseModel):
    """Complete state of a room. Sent whole every time so that updates can be coalesced and retried."""

    host_id: int | None = None
    name: str = Field(min_length=1, max_length=255)
    password: str | None = None
    type: Literal["playlists", "head_to_head", "team_versus", "ranked_play"] = "head_to_head"
    queue_mode: Literal["host_only", "all_players", "all_players_round_robin"] = "host_only"
    status: Literal["idle", "playing", "closed"] = "idle"
    participant_count: int = Field(default=0, ge=0)
    # the users in the room: its chat channel is open to exactly them
    participant_ids: list[int] = Field(default_factory=list)
    max_participants: int | None = Field(default=None, ge=1)
    auto_skip: bool = False
    auto_start_seconds: int = Field(default=0, ge=0)
    current_playlist_item_id: int | None = None


class CreateRoomIn(BaseModel):
    state: RoomStateIn
    items: list[ItemIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_items(self) -> "CreateRoomIn":
        # Ranked play rooms start without an item: the first one is added when a card is played.
        if not self.items and self.state.type != RoomType.RANKED_PLAY.value:
            raise ValueError("a room needs at least one playlist item")
        return self


class CreatedRoomOut(BaseModel):
    room_id: int
    channel_id: int
    item_ids: list[int]  # same order as the request's items


class CreatedItemOut(BaseModel):
    id: int


class BeatmapOut(BaseModel):
    id: int
    checksum: str
    mode: int
    star_rating: float


def _apply_state(room: MultiplayerRoom, state: RoomStateIn) -> None:
    room.host_id = state.host_id
    room.name = state.name
    room.password = state.password or None
    room.type = state.type
    room.queue_mode = state.queue_mode
    room.status = state.status
    room.participant_count = state.participant_count
    room.max_participants = state.max_participants or DEFAULT_MAX_PARTICIPANTS
    room.auto_skip = state.auto_skip
    room.auto_start_duration = state.auto_start_seconds
    room.current_playlist_item_id = state.current_playlist_item_id
    if state.status == RoomStatus.CLOSED.value and room.ends_at is None:
        room.ends_at = datetime.now(UTC)


def _apply_item(item: MultiplayerPlaylistItem, data: ItemIn) -> None:
    item.owner_id = data.owner_id
    item.beatmap_id = data.beatmap_id
    item.ruleset_id = data.ruleset_id
    item.required_mods = json.dumps(data.required_mods)
    item.allowed_mods = json.dumps(data.allowed_mods)
    item.playlist_order = data.playlist_order
    item.expired = data.expired
    item.played_at = data.played_at


def _not_found(what: str) -> OsuError:
    return OsuError(code=status.HTTP_404_NOT_FOUND, error="not_found", message=f"{what} not found")


@router.post("/rooms", response_model=CreatedRoomOut)
async def create_room(db: DbSession, body: CreateRoomIn) -> CreatedRoomOut:
    """Create a room and its initial playlist in one transaction; returns the database ids."""
    room = MultiplayerRoom()
    _apply_state(room, body.state)
    room.ends_at = None
    db.add(room)

    try:
        await db.flush()
        room.channel_id = room.id
        await sync_room_members(db, room, body.state.participant_ids)

        items: list[MultiplayerPlaylistItem] = []
        for data in body.items:
            item = MultiplayerPlaylistItem(room_id=room.id)
            _apply_item(item, data)
            db.add(item)
            items.append(item)
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        logger.warning("Rejected multiplayer room creation: %s", exc)
        raise OsuError(
            code=status.HTTP_409_CONFLICT,
            error="conflict",
            message="Unable to create room with the provided payload",
        ) from exc

    created = CreatedRoomOut(room_id=room.id, channel_id=room.id, item_ids=[item.id for item in items])
    # Commit before answering: the session would otherwise commit after the response went out, and the
    # caller's first follow-up request (which comes at once) could find no room.
    await db.commit()
    return created


@router.put("/rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def put_room_state(db: DbSession, room_id: int, body: RoomStateIn) -> Response:
    """Replace the mutable state of a room."""
    result = await db.execute(
        select(MultiplayerRoom)
        .where(MultiplayerRoom.id == room_id)
        .execution_options(populate_existing=True),
    )
    room = result.scalar_one_or_none()
    if room is None:
        raise _not_found("Room")

    _apply_state(room, body)
    if room.channel_id is not None:
        await sync_room_members(db, room, body.participant_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/rooms/{room_id}/items", response_model=CreatedItemOut)
async def add_item(db: DbSession, room_id: int, body: ItemIn) -> CreatedItemOut:
    """Add a playlist item; returns its database id."""
    exists = await db.execute(select(MultiplayerRoom.id).where(MultiplayerRoom.id == room_id))
    if exists.scalar_one_or_none() is None:
        raise _not_found("Room")

    item = MultiplayerPlaylistItem(room_id=room_id)
    _apply_item(item, body)
    db.add(item)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise OsuError(
            code=status.HTTP_409_CONFLICT,
            error="conflict",
            message="Unable to add the playlist item",
        ) from exc
    created = CreatedItemOut(id=item.id)
    await db.commit()  # see create_room: the caller uses the id right away
    return created


@router.put("/rooms/{room_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def put_item(db: DbSession, room_id: int, item_id: int, body: ItemIn) -> Response:
    """Replace a playlist item's state."""
    result = await db.execute(
        select(MultiplayerPlaylistItem).where(
            MultiplayerPlaylistItem.id == item_id,
            MultiplayerPlaylistItem.room_id == room_id,
        ),
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise _not_found("Playlist item")

    _apply_item(item, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/rooms/{room_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(db: DbSession, room_id: int, item_id: int) -> Response:
    """Delete a playlist item. Deleting something already gone succeeds (retries are safe)."""
    await db.execute(
        delete(MultiplayerPlaylistItem).where(
            MultiplayerPlaylistItem.id == item_id,
            MultiplayerPlaylistItem.room_id == room_id,
        ),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/rooms/close-stale")
async def close_stale_rooms(db: DbSession) -> dict[str, int]:
    """Close realtime rooms left open by a previous run of the multiplayer service.

    Called once at startup, before any room exists, so every open realtime room is by definition orphaned.
    REST created playlists and the daily challenge are left alone.
    """
    result = await db.execute(
        update(MultiplayerRoom)
        .where(
            MultiplayerRoom.status != RoomStatus.CLOSED.value,
            MultiplayerRoom.type.in_(REALTIME_ROOM_TYPES),
            MultiplayerRoom.category.is_(None),
        )
        .values(status=RoomStatus.CLOSED.value, participant_count=0, ends_at=datetime.now(UTC)),
    )
    return {"closed": int(result.rowcount or 0)}


class InviteCheckOut(BaseModel):
    restricted: bool  # the invited user is restricted
    blocked: bool  # either of the two has blocked the other
    blocks_pms: bool  # the invited user takes no messages from non-friends, and the inviter is no friend


@router.get("/invites/{from_id}/{to_id}", response_model=InviteCheckOut)
async def check_invite(db: DbSession, from_id: int, to_id: int) -> InviteCheckOut:
    """What stands between a user and inviting another to a room (the reference server's checks)."""
    invited = await db.get(User, to_id)
    if invited is None or await db.get(User, from_id) is None:
        raise _not_found("User")

    relation = await db.get(UserRelation, (from_id, to_id), populate_existing=True)
    inverse = await db.get(UserRelation, (to_id, from_id), populate_existing=True)
    return InviteCheckOut(
        restricted=bool(invited.is_restricted),
        blocked=bool((relation and relation.foe) or (inverse and inverse.foe)),
        blocks_pms=not (inverse and inverse.friend) and not invited.allow_pm_from_non_friends,
    )


@router.get("/beatmaps/{beatmap_id}", response_model=BeatmapOut)
async def get_beatmap(db: DbSession, beatmap_id: int) -> BeatmapOut:
    """Beatmap facts the room needs to validate playlist items (fetching it from the mirror if needed)."""
    service = BeatmapService(db)
    try:
        beatmap = await service.get_beatmap(beatmap_id)
    finally:
        await service.close()

    if beatmap is None:
        raise _not_found("Beatmap")
    return BeatmapOut(
        id=beatmap.id,
        checksum=beatmap.checksum or "",
        mode=int(beatmap.mode),
        star_rating=float(beatmap.difficulty_rating),
    )
