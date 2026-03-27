"""Multiplayer hub for real-time room management.

This hub handles multiplayer room functionality including:
- Room creation and management
- Player joining/leaving rooms
- Match state synchronization
- Playlist item management

Note: Currently a basic implementation - can be extended as needed.
"""

import json
import logging
from dataclasses import dataclass
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.hubs.base import SignalRConnection
from app.api.hubs.base import create_negotiate_response
from app.api.hubs.base import extract_access_token
from app.api.hubs.base import generate_connection_id
from app.api.hubs.base import handle_handshake
from app.api.hubs.base import run_message_loop
from app.api.hubs.base import send_completion
from app.api.hubs.base import send_invocation
from app.core.database import async_session_maker
from app.core.security import decode_token
from app.models.multiplayer import MultiplayerRoom as DbMultiplayerRoom
from app.models.multiplayer import MultiplayerPlaylistItem as DbMultiplayerPlaylistItem
from app.models.multiplayer import QueueMode as DbQueueMode
from app.models.multiplayer import RoomStatus
from app.models.multiplayer import RoomType as DbRoomType
from app.protocol.enums import MatchType
from app.protocol.enums import MultiplayerRoomState
from app.protocol.enums import MultiplayerUserState
from app.protocol.enums import QueueMode as ProtocolQueueMode
from app.protocol.models import APIMod
from app.protocol.models import MultiplayerPlaylistItem
from app.protocol.models import MultiplayerRoom
from app.protocol.models import MultiplayerRoomSettings
from app.protocol.models import MultiplayerRoomUser

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class MultiplayerConnection(SignalRConnection):
    """Connection state for multiplayer hub."""

    current_room_id: int | None = None


connections: dict[str, MultiplayerConnection] = {}
connections_by_user: dict[int, set[str]] = {}
room_connections: dict[int, set[str]] = {}
room_user_states: dict[int, dict[int, MultiplayerRoomUser]] = {}

_ROOM_TYPE_TO_PROTOCOL: dict[str, MatchType] = {
    DbRoomType.PLAYLISTS.value: MatchType.PLAYLISTS,
    DbRoomType.HEAD_TO_HEAD.value: MatchType.HEAD_TO_HEAD,
    DbRoomType.TEAM_VERSUS.value: MatchType.TEAM_VERSUS,
}
_QUEUE_MODE_TO_PROTOCOL: dict[str, ProtocolQueueMode] = {
    DbQueueMode.HOST_ONLY.value: ProtocolQueueMode.HOST_ONLY,
    DbQueueMode.ALL_PLAYERS.value: ProtocolQueueMode.ALL_PLAYERS,
    DbQueueMode.ALL_PLAYERS_ROUND_ROBIN.value: ProtocolQueueMode.ALL_PLAYERS_ROUND_ROBIN,
}


def _room_state_from_db_status(status: str) -> MultiplayerRoomState:
    if status == RoomStatus.PLAYING.value:
        return MultiplayerRoomState.PLAYING
    if status == RoomStatus.CLOSED.value:
        return MultiplayerRoomState.CLOSED
    return MultiplayerRoomState.OPEN


def _room_type_from_protocol(match_type: MatchType) -> str:
    if match_type == MatchType.PLAYLISTS:
        return DbRoomType.PLAYLISTS.value
    if match_type == MatchType.TEAM_VERSUS:
        return DbRoomType.TEAM_VERSUS.value
    return DbRoomType.HEAD_TO_HEAD.value


def _queue_mode_from_protocol(queue_mode: ProtocolQueueMode) -> str:
    if queue_mode == ProtocolQueueMode.ALL_PLAYERS:
        return DbQueueMode.ALL_PLAYERS.value
    if queue_mode == ProtocolQueueMode.ALL_PLAYERS_ROUND_ROBIN:
        return DbQueueMode.ALL_PLAYERS_ROUND_ROBIN.value
    return DbQueueMode.HOST_ONLY.value


def _parse_mods(mods_json: str | None) -> list[APIMod]:
    if not mods_json:
        return []

    try:
        raw_mods = json.loads(mods_json)
    except json.JSONDecodeError:
        logger.warning("Failed to parse room mods JSON: %s", mods_json)
        return []

    if not isinstance(raw_mods, list):
        return []

    return APIMod.from_list(raw_mods)


def _mods_to_json(mods: list[APIMod]) -> str:
    return json.dumps([
        {
            "acronym": mod.acronym,
            "settings": mod.settings,
        }
        for mod in mods
    ])


async def _load_room_model(room_id: int) -> DbMultiplayerRoom | None:
    async with async_session_maker() as session:
        result = await session.execute(
            select(DbMultiplayerRoom)
            .options(selectinload(DbMultiplayerRoom.playlist_items))
            .where(DbMultiplayerRoom.id == room_id),
        )
        return result.scalar_one_or_none()


async def _join_room_in_db(
    user_id: int,
    room_id: int,
    password: str | None,
) -> tuple[DbMultiplayerRoom | None, str | None]:
    async with async_session_maker() as session:
        async with session.begin():
            result = await session.execute(
                select(DbMultiplayerRoom)
                .options(selectinload(DbMultiplayerRoom.playlist_items))
                .where(DbMultiplayerRoom.id == room_id),
            )
            room = result.scalar_one_or_none()
            if room is None:
                return None, "Room not found"

            if room.status == RoomStatus.CLOSED.value:
                return None, "Room is closed"

            if room.password and room.password != (password or ""):
                return None, "Invalid password"

            if room.participant_count >= room.max_participants:
                return None, "Room is full"

            if room.host_id is None:
                room.host_id = user_id

            room.participant_count += 1
            return room, None


async def _leave_room_in_db(room_id: int) -> DbMultiplayerRoom | None:
    async with async_session_maker() as session:
        async with session.begin():
            result = await session.execute(
                select(DbMultiplayerRoom)
                .options(selectinload(DbMultiplayerRoom.playlist_items))
                .where(DbMultiplayerRoom.id == room_id),
            )
            room = result.scalar_one_or_none()
            if room is None:
                return None

            room.participant_count = max(0, room.participant_count - 1)
            if room.participant_count == 0:
                room.status = RoomStatus.CLOSED.value

            return room


async def _set_room_status_in_db(
    room_id: int,
    status: str,
    host_user_id: int,
) -> tuple[DbMultiplayerRoom | None, str | None]:
    async with async_session_maker() as session:
        async with session.begin():
            result = await session.execute(
                select(DbMultiplayerRoom)
                .options(selectinload(DbMultiplayerRoom.playlist_items))
                .where(DbMultiplayerRoom.id == room_id),
            )
            room = result.scalar_one_or_none()
            if room is None:
                return None, "Room not found"

            if room.host_id != host_user_id:
                return None, "Only host can perform this action"

            room.status = status
            return room, None


async def _update_room_settings_in_db(
    room_id: int,
    settings: MultiplayerRoomSettings,
    host_user_id: int,
) -> tuple[DbMultiplayerRoom | None, str | None]:
    async with async_session_maker() as session:
        async with session.begin():
            result = await session.execute(
                select(DbMultiplayerRoom)
                .options(selectinload(DbMultiplayerRoom.playlist_items))
                .where(DbMultiplayerRoom.id == room_id),
            )
            room = result.scalar_one_or_none()
            if room is None:
                return None, "Room not found"

            if room.host_id != host_user_id:
                return None, "Only host can update settings"

            room.name = settings.name or room.name
            room.password = settings.password or None
            room.type = _room_type_from_protocol(settings.match_type)
            room.queue_mode = _queue_mode_from_protocol(settings.queue_mode)
            room.auto_start_duration = int(settings.auto_start_duration.total_seconds())
            room.auto_skip = settings.auto_skip
            if settings.playlist_item_id:
                room.current_playlist_item_id = settings.playlist_item_id

            return room, None


async def _create_room_in_db(
    user_id: int,
    requested_room: MultiplayerRoom,
) -> tuple[DbMultiplayerRoom | None, str | None]:
    settings = requested_room.settings
    room_name = settings.name.strip() if settings.name else ""
    if not room_name:
        room_name = f"User {user_id}'s room"

    try:
        async with async_session_maker() as session:
            async with session.begin():
                room = DbMultiplayerRoom(
                    host_id=user_id,
                    name=room_name,
                    password=settings.password or None,
                    type=_room_type_from_protocol(settings.match_type),
                    status=RoomStatus.IDLE.value,
                    queue_mode=_queue_mode_from_protocol(settings.queue_mode),
                    participant_count=1,
                    auto_start_duration=int(settings.auto_start_duration.total_seconds()),
                    auto_skip=settings.auto_skip,
                )
                session.add(room)
                await session.flush()

                room.channel_id = room.id

                requested_playlist = sorted(requested_room.playlist, key=lambda item: item.playlist_order)
                playlist_id_by_client_id: dict[int, int] = {}
                first_playlist_item_id: int | None = None

                for order, item in enumerate(requested_playlist):
                    playlist_item = DbMultiplayerPlaylistItem(
                        room_id=room.id,
                        owner_id=item.owner_id or user_id,
                        beatmap_id=item.beatmap_id,
                        ruleset_id=item.ruleset_id,
                        required_mods=_mods_to_json(item.required_mods),
                        allowed_mods=_mods_to_json(item.allowed_mods),
                        playlist_order=order,
                        expired=item.expired,
                        played_at=item.played_at,
                    )
                    session.add(playlist_item)
                    await session.flush()

                    if first_playlist_item_id is None:
                        first_playlist_item_id = playlist_item.id
                    if item.id > 0:
                        playlist_id_by_client_id[item.id] = playlist_item.id

                if settings.playlist_item_id > 0 and settings.playlist_item_id in playlist_id_by_client_id:
                    room.current_playlist_item_id = playlist_id_by_client_id[settings.playlist_item_id]
                else:
                    room.current_playlist_item_id = first_playlist_item_id

                await session.flush()

                result = await session.execute(
                    select(DbMultiplayerRoom)
                    .options(selectinload(DbMultiplayerRoom.playlist_items))
                    .where(DbMultiplayerRoom.id == room.id),
                )
                return result.scalar_one_or_none(), None
    except IntegrityError as exc:
        logger.warning("Failed to create multiplayer room: %s", exc)
        return None, "Unable to create room with provided payload"


def _build_protocol_room(room_model: DbMultiplayerRoom) -> MultiplayerRoom:
    users_by_id = room_user_states.get(room_model.id, {})
    users = [users_by_id[user_id] for user_id in sorted(users_by_id.keys())]
    host = users_by_id.get(room_model.host_id) if room_model.host_id is not None else None
    if host is None and room_model.host_id is not None:
        host = MultiplayerRoomUser(user_id=room_model.host_id, state=MultiplayerUserState.IDLE)

    playlist = []
    for item in sorted(room_model.playlist_items, key=lambda entry: entry.playlist_order):
        playlist.append(
            MultiplayerPlaylistItem(
                id=item.id,
                owner_id=item.owner_id or 0,
                beatmap_id=item.beatmap_id,
                ruleset_id=item.ruleset_id,
                required_mods=_parse_mods(item.required_mods),
                allowed_mods=_parse_mods(item.allowed_mods),
                expired=item.expired,
                playlist_order=item.playlist_order,
                played_at=item.played_at,
            ),
        )

    settings = MultiplayerRoomSettings(
        name=room_model.name,
        playlist_item_id=room_model.current_playlist_item_id or 0,
        password=room_model.password or "",
        match_type=_ROOM_TYPE_TO_PROTOCOL.get(room_model.type, MatchType.HEAD_TO_HEAD),
        queue_mode=_QUEUE_MODE_TO_PROTOCOL.get(room_model.queue_mode, ProtocolQueueMode.HOST_ONLY),
        auto_start_duration=timedelta(seconds=room_model.auto_start_duration),
        auto_skip=room_model.auto_skip,
    )

    return MultiplayerRoom(
        room_id=room_model.id,
        state=_room_state_from_db_status(room_model.status),
        settings=settings,
        users=users,
        host=host,
        playlist=playlist,
        channel_id=room_model.channel_id or 0,
    )


async def _broadcast_to_room(
    room_id: int,
    target: str,
    arguments: list,
    exclude_connection_id: str | None = None,
) -> None:
    conn_ids = room_connections.get(room_id, set())
    if not conn_ids:
        return

    for conn_id in list(conn_ids):
        if exclude_connection_id is not None and conn_id == exclude_connection_id:
            continue

        conn = connections.get(conn_id)
        if not conn:
            conn_ids.discard(conn_id)
            continue

        try:
            await send_invocation(conn.websocket, conn.use_messagepack, target, arguments)
        except Exception as e:
            logger.warning("Failed to send multiplayer invocation %s to %s: %s", target, conn_id, e)


async def _send_error_completion(
    websocket: WebSocket,
    use_messagepack: bool,
    invocation_id: str | None,
    error: str,
) -> None:
    if invocation_id is None:
        return
    await send_completion(websocket, use_messagepack, invocation_id, {"success": False, "error": error})


async def _send_success_completion(
    websocket: WebSocket,
    use_messagepack: bool,
    invocation_id: str | None,
    result,
) -> None:
    if invocation_id is None:
        return
    await send_completion(websocket, use_messagepack, invocation_id, result)


async def _join_room_connection(
    conn: MultiplayerConnection,
    room_id: int,
    password: str | None,
) -> tuple[MultiplayerRoom | None, str | None, bool]:
    if conn.current_room_id == room_id:
        room_model = await _load_room_model(room_id)
        if room_model is None:
            return None, "Room not found", False
        return _build_protocol_room(room_model), None, False

    if conn.current_room_id is not None:
        await _leave_room_connection(conn)

    users_by_id = room_user_states.setdefault(room_id, {})
    is_new_user = conn.user_id not in users_by_id

    if is_new_user:
        room_model, error = await _join_room_in_db(conn.user_id, room_id, password)
        if error is not None:
            room_user_states.pop(room_id, None)
            return None, error, False
        users_by_id[conn.user_id] = MultiplayerRoomUser(user_id=conn.user_id, state=MultiplayerUserState.IDLE)
    else:
        room_model = await _load_room_model(room_id)
        if room_model is None:
            return None, "Room not found", False

    room_connections.setdefault(room_id, set()).add(conn.connection_id)
    conn.current_room_id = room_id
    room_payload = _build_protocol_room(room_model)
    return room_payload, None, is_new_user


async def _leave_room_connection(conn: MultiplayerConnection) -> MultiplayerRoom | None:
    room_id = conn.current_room_id
    if room_id is None:
        return None

    conn.current_room_id = None

    conn_ids = room_connections.get(room_id)
    if conn_ids:
        conn_ids.discard(conn.connection_id)
        if not conn_ids:
            room_connections.pop(room_id, None)

    user_conn_ids = connections_by_user.get(conn.user_id, set())
    has_other_room_connection = False
    for other_conn_id in user_conn_ids:
        if other_conn_id == conn.connection_id:
            continue

        other_conn = connections.get(other_conn_id)
        if other_conn and other_conn.current_room_id == room_id:
            has_other_room_connection = True
            break

    if has_other_room_connection:
        room_model = await _load_room_model(room_id)
        if room_model is None:
            return None
        return _build_protocol_room(room_model)

    users_by_id = room_user_states.get(room_id)
    if users_by_id:
        users_by_id.pop(conn.user_id, None)
        if not users_by_id:
            room_user_states.pop(room_id, None)

    room_model = await _leave_room_in_db(room_id)
    if room_model is None:
        return None

    await _broadcast_to_room(room_id, "UserLeftRoom", [conn.user_id])
    return _build_protocol_room(room_model)


@router.post("/multiplayer/negotiate")
async def multiplayer_negotiate(request: Request) -> JSONResponse:
    """SignalR negotiate endpoint for multiplayer hub."""
    return JSONResponse(create_negotiate_response())


@router.websocket("/multiplayer")
async def multiplayer_websocket(websocket: WebSocket) -> None:
    """SignalR WebSocket endpoint for multiplayer hub.

    Currently provides basic SignalR protocol handling.
    Room management logic can be added as needed.
    """
    token = extract_access_token(websocket)
    token_data = decode_token(token) if token else None
    if token_data is None:
        logger.warning("Multiplayer hub rejected unauthorized websocket connection")
        await websocket.close(code=4401)
        return

    await websocket.accept()
    connection_id = websocket.query_params.get("id", generate_connection_id())
    logger.info(f"Multiplayer hub connected: {connection_id}")

    conn = MultiplayerConnection(
        connection_id=connection_id,
        websocket=websocket,
        user_id=token_data.user_id,
    )
    connections[connection_id] = conn
    connections_by_user.setdefault(conn.user_id, set()).add(connection_id)

    try:
        # Handle handshake
        success, use_messagepack = await handle_handshake(websocket)
        if not success:
            await websocket.close()
            return

        conn.use_messagepack = use_messagepack
        logger.info(f"Multiplayer hub handshake complete: {connection_id} (msgpack={use_messagepack})")

        async def handle_message(parsed: dict) -> None:
            target = parsed.get("target", "")
            args = parsed.get("arguments", [])
            invocation_id = parsed.get("invocationId")
            logger.info(f"Multiplayer hub: {target}({len(args)} args)")

            if target == "CreateRoom":
                room_data = args[0] if args else None
                if room_data is None:
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "room is required",
                    )
                    return

                try:
                    requested_room = MultiplayerRoom.from_msgpack(room_data)
                except (TypeError, ValueError, KeyError, IndexError):
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "Invalid room payload",
                    )
                    return

                room_model, error = await _create_room_in_db(conn.user_id, requested_room)
                if error is not None or room_model is None:
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        error or "Failed to create room",
                    )
                    return

                previous_room_id = conn.current_room_id
                previous_room_payload = None
                if previous_room_id is not None:
                    previous_room_payload = await _leave_room_connection(conn)

                creator_state = MultiplayerRoomUser(user_id=conn.user_id, state=MultiplayerUserState.IDLE)
                room_user_states[room_model.id] = {conn.user_id: creator_state}
                room_connections.setdefault(room_model.id, set()).add(conn.connection_id)
                conn.current_room_id = room_model.id

                room_payload = _build_protocol_room(room_model)
                await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)

                if previous_room_id is not None and previous_room_payload is not None:
                    await _broadcast_to_room(previous_room_id, "RoomStateUpdated", [previous_room_payload])

                await _broadcast_to_room(room_model.id, "RoomStateUpdated", [room_payload])
                return

            if target == "JoinRoom":
                room_id_raw = args[0] if args else None
                if room_id_raw is None:
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "room_id is required",
                    )
                    return

                try:
                    room_id = int(room_id_raw)
                except (TypeError, ValueError):
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "room_id must be an integer",
                    )
                    return

                password = str(args[1]) if len(args) > 1 and args[1] is not None else None
                room_payload, error, user_joined = await _join_room_connection(conn, room_id, password)
                if error is not None:
                    await _send_error_completion(websocket, conn.use_messagepack, invocation_id, error)
                    return

                await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)
                if user_joined:
                    user_state = room_user_states.get(room_id, {}).get(conn.user_id)
                    if user_state is not None:
                        await _broadcast_to_room(
                            room_id,
                            "UserJoinedRoom",
                            [conn.user_id, user_state],
                            exclude_connection_id=conn.connection_id,
                        )
                await _broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])
                return

            if target == "LeaveRoom":
                room_id = conn.current_room_id
                room_payload = await _leave_room_connection(conn)
                await _send_success_completion(
                    websocket,
                    conn.use_messagepack,
                    invocation_id,
                    {"success": True},
                )
                if room_id is not None and room_payload is not None:
                    await _broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])
                return

            if target == "GetRoomState":
                room_id_raw = args[0] if args else conn.current_room_id
                if room_id_raw is None:
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "room_id is required",
                    )
                    return

                try:
                    room_id = int(room_id_raw)
                except (TypeError, ValueError):
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "room_id must be an integer",
                    )
                    return

                room_model = await _load_room_model(room_id)
                if room_model is None:
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "Room not found",
                    )
                    return

                await _send_success_completion(
                    websocket,
                    conn.use_messagepack,
                    invocation_id,
                    _build_protocol_room(room_model),
                )
                return

            if target in {"ChangeState", "ReadyUp", "Unready"}:
                room_id = conn.current_room_id
                if room_id is None:
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "You are not in a room",
                    )
                    return

                if target == "ReadyUp":
                    new_state = MultiplayerUserState.READY
                elif target == "Unready":
                    new_state = MultiplayerUserState.IDLE
                else:
                    raw_state = args[0] if args else MultiplayerUserState.IDLE
                    try:
                        new_state = MultiplayerUserState(int(raw_state))
                    except (TypeError, ValueError):
                        await _send_error_completion(
                            websocket,
                            conn.use_messagepack,
                            invocation_id,
                            "Invalid user state",
                        )
                        return

                users_by_id = room_user_states.setdefault(room_id, {})
                user_state = users_by_id.get(conn.user_id)
                if user_state is None:
                    user_state = MultiplayerRoomUser(user_id=conn.user_id)
                    users_by_id[conn.user_id] = user_state

                user_state.state = new_state
                await _broadcast_to_room(room_id, "UserStateChanged", [conn.user_id, int(new_state)])

                room_model = await _load_room_model(room_id)
                if room_model is not None:
                    await _broadcast_to_room(
                        room_id,
                        "RoomStateUpdated",
                        [_build_protocol_room(room_model)],
                    )

                await _send_success_completion(
                    websocket,
                    conn.use_messagepack,
                    invocation_id,
                    {"success": True},
                )
                return

            if target in {"StartMatch", "AbortMatch"}:
                room_id = conn.current_room_id
                if room_id is None:
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "You are not in a room",
                    )
                    return

                status = RoomStatus.PLAYING.value if target == "StartMatch" else RoomStatus.IDLE.value
                room_model, error = await _set_room_status_in_db(room_id, status, conn.user_id)
                if error is not None:
                    await _send_error_completion(websocket, conn.use_messagepack, invocation_id, error)
                    return

                users_by_id = room_user_states.get(room_id, {})
                if target == "StartMatch":
                    for user_state in users_by_id.values():
                        user_state.state = MultiplayerUserState.PLAYING
                else:
                    for user_state in users_by_id.values():
                        user_state.state = MultiplayerUserState.IDLE

                room_payload = _build_protocol_room(room_model)
                if target == "StartMatch":
                    await _broadcast_to_room(room_id, "MatchStarted", [room_id])
                else:
                    await _broadcast_to_room(room_id, "MatchAborted", [room_id])
                await _broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])
                await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)
                return

            if target == "ChangeSettings":
                room_id = conn.current_room_id
                if room_id is None:
                    await _send_error_completion(
                        websocket,
                        conn.use_messagepack,
                        invocation_id,
                        "You are not in a room",
                    )
                    return

                settings_data = args[0] if args else {}
                settings = MultiplayerRoomSettings.from_msgpack(settings_data)
                room_model, error = await _update_room_settings_in_db(room_id, settings, conn.user_id)
                if error is not None:
                    await _send_error_completion(websocket, conn.use_messagepack, invocation_id, error)
                    return

                room_payload = _build_protocol_room(room_model)
                await _broadcast_to_room(room_id, "SettingsChanged", [room_payload.settings])
                await _broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])
                await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)
                return

            await _send_error_completion(
                websocket,
                conn.use_messagepack,
                invocation_id,
                f"Unknown method: {target}",
            )

        # Run message loop
        await run_message_loop(websocket, conn.use_messagepack, handle_message)

    except WebSocketDisconnect:
        logger.info(f"Multiplayer hub disconnected: {connection_id}")
    except Exception as e:
        logger.exception(f"Multiplayer hub error: {e}")
    finally:
        last_room_id = conn.current_room_id
        room_payload = await _leave_room_connection(conn)
        if last_room_id is not None and room_payload is not None:
            await _broadcast_to_room(last_room_id, "RoomStateUpdated", [room_payload])

        user_conn_ids = connections_by_user.get(conn.user_id)
        if user_conn_ids:
            user_conn_ids.discard(connection_id)
            if user_conn_ids:
                connections_by_user[conn.user_id] = user_conn_ids
            else:
                connections_by_user.pop(conn.user_id, None)

        connections.pop(connection_id, None)
        logger.info(f"Multiplayer hub closed: {connection_id}")
