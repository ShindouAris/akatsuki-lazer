"""Multiplayer room endpoints."""

import json

from fastapi import APIRouter
from fastapi import Query
from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import ActiveUser
from app.api.deps import DbSession
from app.api.v2.schemas import BeatmapCompact
from app.api.v2.schemas import ModResponse
from app.api.v2.schemas import MultiplayerPlaylistItemResponse
from app.api.v2.schemas import MultiplayerRoomCreateRequest
from app.api.v2.schemas import MultiplayerRoomResponse
from app.api.v2.schemas import UserCompact
from app.core.error import OsuError
from app.models.multiplayer import MultiplayerPlaylistItem
from app.models.multiplayer import MultiplayerRoom
from app.models.multiplayer import RoomStatus
from app.models.user import GameMode
from app.models.user import User
from app.services.beatmaps import BeatmapService

router = APIRouter()


def _mode_to_string(mode: GameMode) -> str:
    """Convert GameMode enum to string."""
    return {
        GameMode.OSU: "osu",
        GameMode.TAIKO: "taiko",
        GameMode.CATCH: "fruits",
        GameMode.MANIA: "mania",
    }.get(mode, "osu")


async def _get_room_response(db: DbSession, room: MultiplayerRoom) -> MultiplayerRoomResponse:
    """Convert MultiplayerRoom model to response."""
    # Get host
    host = None
    if room.host_id:
        result = await db.execute(select(User).where(User.id == room.host_id))
        host_user = result.scalar_one_or_none()
        if host_user:
            host = UserCompact(
                id=host_user.id,
                username=host_user.username,
                avatar_url=host_user.avatar_url,
                country_code=host_user.country_acronym,
                is_active=host_user.is_active,
                is_bot=host_user.is_bot,
                is_supporter=host_user.is_supporter,
            )

    # Get playlist items with beatmaps (fetch from mirror if needed)
    playlist = []
    service = BeatmapService(db)
    try:
        for item in room.playlist_items:
            beatmap = await service.get_beatmap(item.beatmap_id)

            beatmap_compact = None
            if beatmap:
                beatmap_compact = BeatmapCompact(
                    id=beatmap.id,
                    beatmapset_id=beatmap.beatmapset_id,
                    version=beatmap.version,
                    mode=_mode_to_string(beatmap.mode),
                    status=beatmap.status.name.lower(),
                    difficulty_rating=beatmap.difficulty_rating,
                    total_length=beatmap.total_length,
                    cs=beatmap.cs,
                    ar=beatmap.ar,
                    od=beatmap.od,
                    hp=beatmap.hp,
                    bpm=beatmap.bpm,
                    max_combo=beatmap.max_combo,
                    checksum=beatmap.checksum,
                )

            required_mods = json.loads(item.required_mods) if item.required_mods else []
            allowed_mods = json.loads(item.allowed_mods) if item.allowed_mods else []

            playlist.append(
                MultiplayerPlaylistItemResponse(
                    id=item.id,
                    room_id=item.room_id,
                    beatmap_id=item.beatmap_id,
                    ruleset_id=item.ruleset_id,
                    required_mods=[
                        ModResponse(
                            acronym=m.get("acronym", ""),
                            settings=m.get("settings", {}),
                        ) for m in required_mods
                    ],
                    allowed_mods=[
                        ModResponse(
                            acronym=m.get("acronym", ""),
                            settings=m.get("settings", {}),
                        ) for m in allowed_mods
                    ],
                    playlist_order=item.playlist_order,
                    played_at=item.played_at,
                    expired=item.expired,
                    beatmap=beatmap_compact,
                ),
            )
    finally:
        await service.close()

    # Get current playlist item
    current_item = None
    if room.current_playlist_item_id:
        for item in playlist:
            if item.id == room.current_playlist_item_id:
                current_item = item
                break

    return MultiplayerRoomResponse(
        id=room.id,
        name=room.name,
        host=host,
        type=room.type,
        status=room.status,
        queue_mode=room.queue_mode,
        max_participants=room.max_participants,
        participant_count=room.participant_count,
        auto_start_duration=room.auto_start_duration,
        auto_skip=room.auto_skip,
        category=room.category,
        has_password=room.password is not None,
        starts_at=room.starts_at,
        ends_at=room.ends_at,
        playlist=playlist,
        current_playlist_item=current_item,
        channel_id=room.channel_id,
    )


@router.get("/rooms", response_model=list[MultiplayerRoomResponse])
async def get_rooms(
    db: DbSession,
    mode: str | None = Query(None, description="Filter by room type"),
    category: str | None = Query(None, description="Filter by category"),
    limit: int = Query(50, ge=1, le=100),
) -> list[MultiplayerRoomResponse]:
    """Get list of multiplayer rooms."""
    query = (
        select(MultiplayerRoom)
        .options(selectinload(MultiplayerRoom.playlist_items))
        .where(MultiplayerRoom.status != RoomStatus.CLOSED)
    )

    if mode:
        query = query.where(MultiplayerRoom.type == mode)
    if category:
        query = query.where(MultiplayerRoom.category == category)

    query = query.order_by(MultiplayerRoom.created_at.desc()).limit(limit)

    result = await db.execute(query)
    rooms = result.scalars().all()

    return [await _get_room_response(db, room) for room in rooms]


@router.get("/rooms/{room_id}", response_model=MultiplayerRoomResponse)
async def get_room(db: DbSession, room_id: int) -> MultiplayerRoomResponse:
    """Get a multiplayer room by ID."""
    result = await db.execute(
        select(MultiplayerRoom)
        .options(selectinload(MultiplayerRoom.playlist_items))
        .where(MultiplayerRoom.id == room_id),
    )
    room = result.scalar_one_or_none()

    if not room:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="Room not found",
            message="Room not found",
        )

    return await _get_room_response(db, room)


@router.post("/rooms", response_model=MultiplayerRoomResponse)
async def create_room(
    db: DbSession,
    user: ActiveUser,
    request: MultiplayerRoomCreateRequest,
) -> MultiplayerRoomResponse:
    """Create a new multiplayer room."""
    # Create room
    room = MultiplayerRoom(
        host_id=user.id,
        name=request.name,
        password=request.password,
        type=request.type,
        queue_mode=request.queue_mode,
        max_participants=request.max_participants,
        auto_start_duration=request.auto_start_duration,
        auto_skip=request.auto_skip,
        category=request.category,
        starts_at=request.starts_at,
        ends_at=request.ends_at,
        participant_count=1,  # Host is first participant
    )
    db.add(room)
    await db.flush()

    # Add playlist items
    for i, item_data in enumerate(request.playlist):
        item = MultiplayerPlaylistItem(
            room_id=room.id,
            owner_id=user.id,
            beatmap_id=item_data.get("beatmap_id"),
            ruleset_id=item_data.get("ruleset_id", 0),
            required_mods=json.dumps(item_data.get("required_mods", [])),
            allowed_mods=json.dumps(item_data.get("allowed_mods", [])),
            playlist_order=i,
        )
        db.add(item)

    await db.flush()

    # Reload room with playlist items
    result = await db.execute(
        select(MultiplayerRoom)
        .options(selectinload(MultiplayerRoom.playlist_items))
        .where(MultiplayerRoom.id == room.id),
    )
    room = result.scalar_one()

    # Set current playlist item to first item
    if room.playlist_items:
        room.current_playlist_item_id = room.playlist_items[0].id

    return await _get_room_response(db, room)


@router.put("/rooms/{room_id}/users/{user_id}")
async def join_room(
    db: DbSession,
    user: ActiveUser,
    room_id: int,
    user_id: int,
    password: str | None = Query(None),
) -> MultiplayerRoomResponse:
    """Join a multiplayer room."""
    if user.id != user_id:
        raise OsuError(
            code=status.HTTP_403_FORBIDDEN,
            error="Cannot join room as another user",
            message="Cannot join room as another user",
        )

    result = await db.execute(
        select(MultiplayerRoom)
        .options(selectinload(MultiplayerRoom.playlist_items))
        .where(MultiplayerRoom.id == room_id),
    )
    room = result.scalar_one_or_none()

    if not room:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="Room not found",
            message="Room not found",
        )

    if room.status == RoomStatus.CLOSED:
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error="Room is closed",
            message="Room is closed",
        )

    if room.password and room.password != password:
        raise OsuError(
            code=status.HTTP_403_FORBIDDEN,
            error="Invalid password",
            message="Invalid password",
        )

    if room.participant_count >= room.max_participants:
        raise OsuError(
            code=status.HTTP_400_BAD_REQUEST,
            error="Room is full",
            message="Room is full",
        )

    # TODO: Track room participants properly
    room.participant_count += 1

    return await _get_room_response(db, room)


@router.delete("/rooms/{room_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def leave_room(
    db: DbSession,
    user: ActiveUser,
    room_id: int,
    user_id: int,
) -> None:
    """Leave a multiplayer room."""
    if user.id != user_id:
        raise OsuError(
            code=status.HTTP_403_FORBIDDEN,
            error="Cannot leave room as another user",
            message="Cannot leave room as another user",
        )

    result = await db.execute(
        select(MultiplayerRoom).where(MultiplayerRoom.id == room_id),
    )
    room = result.scalar_one_or_none()

    if not room:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="Room not found",
            message="Room not found",
        )

    # TODO: Track room participants properly
    room.participant_count = max(0, room.participant_count - 1)

    # Close room if empty
    if room.participant_count == 0:
        room.status = RoomStatus.CLOSED


@router.get("/rooms/{room_id}/leaderboard")
async def get_room_leaderboard(
    db: DbSession,
    room_id: int,
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    """Get the leaderboard for a multiplayer room."""
    result = await db.execute(
        select(MultiplayerRoom).where(MultiplayerRoom.id == room_id),
    )
    room = result.scalar_one_or_none()

    if not room:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="Room not found",
            message="Room not found",
        )

    # TODO: Implement proper leaderboard aggregation
    return {
        "leaderboard": [],
        "user_score": None,
    }
