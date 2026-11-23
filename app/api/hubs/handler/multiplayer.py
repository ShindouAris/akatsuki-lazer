import logging

from fastapi import WebSocket

from app.models.multiplayer import RoomStatus
from app.protocol.models import MultiplayerRoom
from app.protocol.models import MultiplayerRoomSettings
from app.protocol.models import MultiplayerRoomUser
from app.protocol.enums import MultiplayerUserState

from app.api.hubs.base import send_completion
import app.api.hubs.multiplayer as multiplayer_hub

logger = logging.getLogger(__name__)


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


async def handle_create_room(
    conn,
    websocket: WebSocket,
    room_data,
    invocation_id: str | None,
) -> None:
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

    room_model, error = await multiplayer_hub._create_room_in_db(conn.user_id, requested_room)
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
        previous_room_payload = await multiplayer_hub._leave_room_connection(conn)

    creator_state = MultiplayerRoomUser(user_id=conn.user_id, state=MultiplayerUserState.IDLE)
    multiplayer_hub.room_user_states[room_model.id] = {conn.user_id: creator_state}
    multiplayer_hub.room_connections.setdefault(room_model.id, set()).add(conn.connection_id)
    conn.current_room_id = room_model.id

    room_payload = multiplayer_hub._build_protocol_room(room_model)
    await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)

    if previous_room_id is not None and previous_room_payload is not None:
        await multiplayer_hub._broadcast_to_room(
            previous_room_id,
            "RoomStateUpdated",
            [previous_room_payload],
        )

    await multiplayer_hub._broadcast_to_room(room_model.id, "RoomStateUpdated", [room_payload])


async def handle_join_room(
    conn,
    websocket: WebSocket,
    args: list,
    invocation_id: str | None,
) -> None:
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
    room_payload, error, user_joined = await multiplayer_hub._join_room_connection(conn, room_id, password)
    if error is not None:
        await _send_error_completion(websocket, conn.use_messagepack, invocation_id, error)
        return

    await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)
    if user_joined:
        user_state = multiplayer_hub.room_user_states.get(room_id, {}).get(conn.user_id)
        if user_state is not None:
            await multiplayer_hub._broadcast_to_room(
                room_id,
                "UserJoinedRoom",
                [conn.user_id, user_state],
                exclude_connection_id=conn.connection_id,
            )
    await multiplayer_hub._broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])


async def handle_leave_room(
    conn,
    websocket: WebSocket,
    invocation_id: str | None,
) -> None:
    room_id = conn.current_room_id
    room_payload = await multiplayer_hub._leave_room_connection(conn)
    await _send_success_completion(
        websocket,
        conn.use_messagepack,
        invocation_id,
        {"success": True},
    )
    if room_id is not None and room_payload is not None:
        await multiplayer_hub._broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])


async def handle_get_room_state(
    conn,
    websocket: WebSocket,
    args: list,
    invocation_id: str | None,
) -> None:
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

    room_model = await multiplayer_hub._load_room_model(room_id)
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
        multiplayer_hub._build_protocol_room(room_model),
    )


async def handle_ready_up(
    conn,
    websocket: WebSocket,
    target: str,
    args: list,
    invocation_id: str | None,
) -> None:
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

    users_by_id = multiplayer_hub.room_user_states.setdefault(room_id, {})
    user_state = users_by_id.get(conn.user_id)
    if user_state is None:
        user_state = MultiplayerRoomUser(user_id=conn.user_id)
        users_by_id[conn.user_id] = user_state

    user_state.state = new_state
    await multiplayer_hub._broadcast_to_room(room_id, "UserStateChanged", [conn.user_id, int(new_state)])

    room_model = await multiplayer_hub._load_room_model(room_id)
    if room_model is not None:
        await multiplayer_hub._broadcast_to_room(
            room_id,
            "RoomStateUpdated",
            [multiplayer_hub._build_protocol_room(room_model)],
        )

    await _send_success_completion(
        websocket,
        conn.use_messagepack,
        invocation_id,
        {"success": True},
    )


async def handle_start_match(
    conn,
    websocket: WebSocket,
    invocation_id: str | None,
) -> None:
    room_id = conn.current_room_id
    if room_id is None:
        await _send_error_completion(
            websocket,
            conn.use_messagepack,
            invocation_id,
            "You are not in a room",
        )
        return

    status = RoomStatus.PLAYING.value
    room_model, error = await multiplayer_hub._set_room_status_in_db(room_id, status, conn.user_id)
    if error is not None:
        await _send_error_completion(websocket, conn.use_messagepack, invocation_id, error)
        return

    users_by_id = multiplayer_hub.room_user_states.get(room_id, {})
    for user_state in users_by_id.values():
        user_state.state = MultiplayerUserState.PLAYING

    room_payload = multiplayer_hub._build_protocol_room(room_model)
    await multiplayer_hub._broadcast_to_room(room_id, "MatchStarted", [room_id])
    await multiplayer_hub._broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])
    await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)


async def handle_abort_match(
    conn,
    websocket: WebSocket,
    invocation_id: str | None,
) -> None:
    room_id = conn.current_room_id
    if room_id is None:
        await _send_error_completion(
            websocket,
            conn.use_messagepack,
            invocation_id,
            "You are not in a room",
        )
        return

    status = RoomStatus.IDLE.value
    room_model, error = await multiplayer_hub._set_room_status_in_db(room_id, status, conn.user_id)
    if error is not None:
        await _send_error_completion(websocket, conn.use_messagepack, invocation_id, error)
        return

    users_by_id = multiplayer_hub.room_user_states.get(room_id, {})
    for user_state in users_by_id.values():
        user_state.state = MultiplayerUserState.IDLE

    room_payload = multiplayer_hub._build_protocol_room(room_model)
    await multiplayer_hub._broadcast_to_room(room_id, "MatchAborted", [room_id])
    await multiplayer_hub._broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])
    await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)


async def handle_change_settings(
    conn,
    websocket: WebSocket,
    args: list,
    invocation_id: str | None,
) -> None:
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
    room_model, error = await multiplayer_hub._update_room_settings_in_db(room_id, settings, conn.user_id)
    if error is not None:
        await _send_error_completion(websocket, conn.use_messagepack, invocation_id, error)
        return

    room_payload = multiplayer_hub._build_protocol_room(room_model)
    await multiplayer_hub._broadcast_to_room(room_id, "SettingsChanged", [room_payload.settings])
    await multiplayer_hub._broadcast_to_room(room_id, "RoomStateUpdated", [room_payload])
    await _send_success_completion(websocket, conn.use_messagepack, invocation_id, room_payload)