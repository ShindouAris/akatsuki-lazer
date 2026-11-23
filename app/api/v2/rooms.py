"""Multiplayer room endpoints."""

import json

from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import Form
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
from app.api.v2.schemas import ScoreResponse
from app.api.v2.schemas import ScoreSubmissionRequest
from app.api.v2.schemas import ScoreTokenResponse
from app.api.v2.schemas import UserCompact
from app.api.v2.beatmaps import _beatmap_to_compact
from app.api.v2.beatmaps import create_score_token
from app.api.v2.scores import submit_score
from app.core.error import OsuError
from app.models.multiplayer import MultiplayerPlaylistItem
from app.models.multiplayer import MultiplayerRoom
from app.models.multiplayer import MultiplayerScore
from app.models.score import Score
from app.models.multiplayer import RoomStatus
from app.models.user import GameMode
from app.models.user import User
from app.services.beatmaps import BeatmapService

router = APIRouter()

# The types of room listed in the realtime multiplayer lobby.
LOBBY_ROOM_TYPES = ("head_to_head", "team_versus")


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
                # (Building it by hand here passed mode as a string, which made listing any room with a beatmap
                # fail with a 500.)
                beatmap_compact = _beatmap_to_compact(beatmap)

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
        # The client's category is a non-nullable enum: a null (rooms made by the multiplayer service have none)
        # fails parsing the whole list, leaving the lobby spinning.
        category=room.category or "normal",
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
    if category == "realtime":
        # The multiplayer lobby asks for "realtime": that is a group of room types, not a stored category (rooms
        # made by the multiplayer service have none). Matchmaking rooms are joined through the queue, not listed.
        query = query.where(MultiplayerRoom.type.in_(LOBBY_ROOM_TYPES))
    elif category:
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


async def _room_item_or_404(db: DbSession, room_id: int, item_id: int) -> MultiplayerPlaylistItem:
    result = await db.execute(
        select(MultiplayerPlaylistItem).where(
            MultiplayerPlaylistItem.id == item_id,
            MultiplayerPlaylistItem.room_id == room_id,
        ),
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="Playlist item not found",
            message="Playlist item not found",
        )
    return item


@router.post("/rooms/{room_id}/playlist/{item_id}/scores", response_model=ScoreTokenResponse)
async def create_room_score_token(
    db: DbSession,
    user: ActiveUser,
    room_id: int,
    item_id: int,
    beatmap_id: int = Form(...),
    beatmap_hash: str = Form(...),
    ruleset_id: int = Form(0),
    version_hash: str | None = Form(default=None),
) -> ScoreTokenResponse:
    """Request a score token for playing a playlist item of a room."""
    await _room_item_or_404(db, room_id, item_id)
    return await create_score_token(
        db=db,
        user=user,
        beatmap_id=beatmap_id,
        beatmap_hash=beatmap_hash,
        ruleset_id=ruleset_id,
        version_hash=version_hash,
        playlist_item_id=item_id,
    )


@router.put("/rooms/{room_id}/playlist/{item_id}/scores/{token_id}", response_model=ScoreResponse)
async def submit_room_score(
    db: DbSession,
    user: ActiveUser,
    background_tasks: BackgroundTasks,
    room_id: int,
    item_id: int,
    token_id: int,
    score_data: ScoreSubmissionRequest,
) -> ScoreResponse:
    """Submit the score of a playlist item of a room."""
    item = await _room_item_or_404(db, room_id, item_id)
    return await submit_score(
        db=db,
        user=user,
        background_tasks=background_tasks,
        beatmap_id=item.beatmap_id,
        token_id=token_id,
        score_data=score_data,
    )


def _multiplayer_score_json(
    row: MultiplayerScore, score: Score | None, user: User, item: MultiplayerPlaylistItem, position: int,
) -> dict:
    """One score of a playlist item in the shape of the client's MultiplayerScore."""
    try:
        data = json.loads(score.data) if score is not None and score.data else {}
    except ValueError:
        data = {}
    ended = (score.ended_at if score is not None and score.ended_at else None) or row.created_at
    return {
        "id": row.id,
        "user_id": row.user_id,
        "user": {
            "id": user.id,
            "username": user.username,
            "avatar_url": user.avatar_url,
            "country_code": user.country_acronym,
            "is_active": user.is_active,
            "is_bot": user.is_bot,
            "is_supporter": user.is_supporter,
        },
        "rank": row.rank,
        "total_score": row.total_score,
        "accuracy": row.accuracy,
        "max_combo": row.max_combo,
        "mods": data.get("mods", []),
        "statistics": data.get("statistics", {}),
        "maximum_statistics": data.get("maximum_statistics", {}),
        "passed": row.passed,
        "ended_at": ended.isoformat(),
        "position": position,
        "pp": row.pp,
        "has_replay": bool(score.has_replay) if score is not None else False,
        "ranked": bool(score.ranked) if score is not None else False,
        "ruleset_id": score.ruleset_id if score is not None else item.ruleset_id,
        "beatmap_id": item.beatmap_id,
    }


@router.get("/rooms/{room_id}/playlist/{item_id}/scores")
async def index_room_scores(db: DbSession, user: ActiveUser, room_id: int, item_id: int) -> dict:
    """The scores of a playlist item, best first, with the requesting user's own score."""
    item = await _room_item_or_404(db, room_id, item_id)
    result = await db.execute(
        select(MultiplayerScore, Score, User)
        .join(User, User.id == MultiplayerScore.user_id)
        .outerjoin(Score, Score.id == MultiplayerScore.score_id)
        .where(MultiplayerScore.playlist_item_id == item_id)
        .order_by(MultiplayerScore.total_score.desc(), MultiplayerScore.id.asc()),
    )
    # A user may have several attempts; only their best counts.
    best: dict[int, tuple] = {}
    for row, score, owner in result.all():
        best.setdefault(row.user_id, (row, score, owner))

    scores = [
        _multiplayer_score_json(row, score, owner, item, position)
        for position, (row, score, owner) in enumerate(best.values(), start=1)
    ]
    own = next((s for s in scores if s["user_id"] == user.id), None)
    return {
        "scores": scores,
        "total": len(scores),
        "user_score": own,
        "cursor": None,
        "params": {},
    }
