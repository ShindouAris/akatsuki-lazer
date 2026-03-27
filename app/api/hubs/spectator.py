"""Spectator hub for live gameplay streaming.

This hub handles:
- Players broadcasting their gameplay (BeginPlaySession, SendFrameData, EndPlaySession)
- Spectators watching players (StartWatchingUser, EndWatchingUser)
- Score processing notifications (UserScoreProcessed)
"""

import logging
from dataclasses import dataclass
from dataclasses import field

from fastapi import APIRouter
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.api.hubs.base import SignalRConnection
from app.api.hubs.base import create_negotiate_response
from app.api.hubs.base import generate_connection_id
from app.api.hubs.base import handle_handshake
from app.api.hubs.base import run_message_loop
from app.api.hubs.base import send_invocation
from app.core.security import decode_token
from app.protocol.models import FrameDataBundle
from app.protocol.models import SpectatorState
from app.protocol.models import SpectatorUser
from app.services.hub_state import get_hub_state_service

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class SpectatorConnection(SignalRConnection):
    """Connection state for spectator hub."""

    watching_users: set[int] = field(default_factory=set)
    is_playing: bool = False


# In-memory connection tracking (WebSocket objects can't be serialized to Redis)
# User state (playing, watches) is stored in Redis for persistence
connections: dict[str, SpectatorConnection] = {}  # connection_id -> connection
user_connections: dict[int, str] = {}  # user_id -> connection_id


@router.post("/spectator/negotiate")
async def spectator_negotiate(request: Request) -> JSONResponse:
    """SignalR negotiate endpoint for spectator hub."""
    return JSONResponse(create_negotiate_response())


async def _broadcast_to_watchers(target_user_id: int, target: str, arguments: list) -> None:
    """Broadcast a message to all users watching a specific user."""
    hub_state = await get_hub_state_service()
    watcher_user_ids = await hub_state.get_watchers(target_user_id)

    for watcher_user_id in watcher_user_ids:
        conn_id = user_connections.get(watcher_user_id)
        if not conn_id:
            continue

        conn = connections.get(conn_id)
        if not conn or not conn.websocket:
            continue

        try:
            await send_invocation(conn.websocket, conn.use_messagepack, target, arguments)
        except Exception as e:
            logger.warning(f"Failed to send to spectator watcher user {watcher_user_id}: {e}")


async def _send_to_user(user_id: int, target: str, arguments: list) -> None:
    """Send a message to a specific user on the spectator hub."""
    conn_id = user_connections.get(user_id)
    if not conn_id:
        return

    conn = connections.get(conn_id)
    if not conn or not conn.websocket:
        return

    try:
        await send_invocation(conn.websocket, conn.use_messagepack, target, arguments)
    except Exception as e:
        logger.warning(f"Failed to send to spectator user {user_id}: {e}")


async def send_user_score_processed(user_id: int, score_id: int) -> None:
    """Notify a user that their score has been processed.

    This is called after score submission to trigger the client to
    fetch updated user statistics for the "Overall Ranking" panel.
    """
    await _send_to_user(user_id, "UserScoreProcessed", [user_id, score_id])
    logger.info(f"Sent UserScoreProcessed for user {user_id}, score {score_id}")


def _extract_access_token(websocket: WebSocket) -> str | None:
    """Extract bearer token from websocket headers or query parameters."""
    auth_header = websocket.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    return websocket.query_params.get("access_token") or websocket.query_params.get("token")


@router.websocket("/spectator")
async def spectator_websocket(websocket: WebSocket) -> None:
    """SignalR WebSocket endpoint for spectator hub.

    State is persisted to Redis for:
    - Playing users (survives brief disconnects)
    - Watch relationships (can be restored on reconnect)
    """
    token = _extract_access_token(websocket)
    token_data = decode_token(token) if token else None
    if token_data is None:
        logger.warning("Spectator hub rejected unauthorized websocket connection")
        await websocket.close(code=4401)
        return

    await websocket.accept()
    connection_id = websocket.query_params.get("id", generate_connection_id())
    logger.info(f"Spectator hub connected: {connection_id}")

    hub_state = await get_hub_state_service()

    # Create connection tracking
    conn = SpectatorConnection(
        connection_id=connection_id,
        websocket=websocket,
        user_id=token_data.user_id,
    )
    connections[connection_id] = conn
    user_connections[conn.user_id] = connection_id

    # Track current play state locally (also persisted to Redis)
    current_state: SpectatorState | None = None
    score_token: int | None = None

    try:
        # Handle handshake
        success, use_messagepack = await handle_handshake(websocket)
        if not success:
            await websocket.close()
            return

        conn.use_messagepack = use_messagepack
        logger.info(f"Spectator hub handshake complete: {connection_id} (msgpack={use_messagepack})")

        # Restore previous watch state on reconnect
        previous_watches = await hub_state.get_watching(conn.user_id)
        if previous_watches:
            logger.info(f"User {conn.user_id} reconnected, restoring {len(previous_watches)} watches")
            conn.watching_users = previous_watches

        async def handle_message(parsed: dict) -> None:
            nonlocal current_state, score_token

            target = parsed.get("target", "")
            args = parsed.get("arguments", [])
            logger.info(f"Spectator hub: {target}({len(args)} args)")

            if target == "BeginPlaySession":
                raw_score_token = args[0] if args else None
                if raw_score_token is None:
                    logger.warning("BeginPlaySession missing score token from user %s", conn.user_id)
                    return

                try:
                    score_token = int(raw_score_token)
                except (TypeError, ValueError):
                    logger.warning(
                        "BeginPlaySession invalid score token %r from user %s",
                        raw_score_token,
                        conn.user_id,
                    )
                    return

                state_data = args[1] if len(args) > 1 else {}
                current_state = SpectatorState.from_msgpack(state_data)
                conn.is_playing = True

                await hub_state.set_playing(conn.user_id, current_state, score_token)
                await _broadcast_to_watchers(
                    conn.user_id,
                    "UserBeganPlaying",
                    [conn.user_id, current_state.to_msgpack()],
                )
                logger.info(f"User {conn.user_id} began playing beatmap {current_state.beatmap_id}")

            elif target == "SendFrameData":
                frame_data = args[0] if args else {}
                frame_bundle = FrameDataBundle.from_msgpack(frame_data)

                if score_token is not None:
                    buffered_count = await hub_state.append_replay_frame_bundle(score_token, frame_bundle)
                    if buffered_count % 25 == 0:
                        logger.debug(
                            "Buffered %s frame bundles for score token %s",
                            buffered_count,
                            score_token,
                        )

                await _broadcast_to_watchers(
                    conn.user_id,
                    "UserSentFrames",
                    [conn.user_id, frame_bundle.to_msgpack()],
                )

            elif target == "EndPlaySession":
                state_data = args[0] if args else {}
                final_state = SpectatorState.from_msgpack(state_data)
                conn.is_playing = False

                await hub_state.remove_playing(conn.user_id)
                await _broadcast_to_watchers(
                    conn.user_id,
                    "UserFinishedPlaying",
                    [conn.user_id, final_state.to_msgpack()],
                )
                current_state = None
                score_token = None
                logger.info(f"User {conn.user_id} finished playing")

            elif target == "StartWatchingUser":
                target_user_id = args[0] if args else 0
                conn.watching_users.add(target_user_id)

                await hub_state.add_watcher(conn.user_id, target_user_id)

                # Send current playing state if target is playing
                target_playing = await hub_state.get_playing(target_user_id)
                if target_playing:
                    await send_invocation(
                        websocket,
                        conn.use_messagepack,
                        "UserBeganPlaying",
                        [target_user_id, target_playing.state.to_msgpack()],
                    )
                    logger.info(f"Sent playing state: user {target_user_id} is playing")

                # Notify target user
                watcher = SpectatorUser(online_id=conn.user_id, username=f"User {conn.user_id}")
                await _send_to_user(
                    target_user_id,
                    "UserStartedWatching",
                    [[watcher.to_msgpack()]],
                )
                logger.info(f"User {conn.user_id} started watching user {target_user_id}")

            elif target == "EndWatchingUser":
                target_user_id = args[0] if args else 0
                conn.watching_users.discard(target_user_id)

                await hub_state.remove_watcher(conn.user_id, target_user_id)
                await _send_to_user(target_user_id, "UserEndedWatching", [conn.user_id])
                logger.info(f"User {conn.user_id} stopped watching user {target_user_id}")

        # Run message loop
        await run_message_loop(websocket, conn.use_messagepack, handle_message)

    except WebSocketDisconnect:
        logger.info(f"Spectator hub disconnected: {connection_id}")
    except Exception as e:
        logger.exception(f"Spectator hub error: {e}")
    finally:
        # Cleanup Redis state
        if conn.is_playing:
            await hub_state.remove_playing(conn.user_id)
            if current_state:
                await _broadcast_to_watchers(
                    conn.user_id,
                    "UserFinishedPlaying",
                    [conn.user_id, current_state.to_msgpack()],
                )

        await hub_state.clear_user_watches(conn.user_id)

        # Remove from in-memory tracking
        if user_connections.get(conn.user_id) == connection_id:
            user_connections.pop(conn.user_id, None)
        connections.pop(connection_id, None)
        logger.info(f"Spectator hub closed: {connection_id}")
