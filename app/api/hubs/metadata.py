"""Metadata hub for user presence and beatmap updates.

This hub handles:
- User presence tracking (activity and status)
- Presence watching (subscribe to other users' presence changes)
- Beatmap metadata updates (GetChangesSince, BeatmapSetsUpdated)
"""

import logging
from dataclasses import dataclass

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
from app.api.hubs.base import send_completion
from app.api.hubs.base import send_invocation
from app.protocol.enums import UserStatus
from app.protocol.models import BeatmapUpdates
from app.protocol.models import UserActivity
from app.protocol.models import UserPresence
from app.services.hub_state import get_hub_state_service

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class MetadataConnection(SignalRConnection):
    """Connection state for metadata hub."""

    watching_presence: bool = False
    activity: UserActivity | None = None
    status: UserStatus = UserStatus.ONLINE


# In-memory connection tracking (WebSocket objects can't be serialized to Redis)
# User state (presence) is stored in Redis for persistence
connections: dict[str, MetadataConnection] = {}  # connection_id -> connection
user_connections: dict[int, str] = {}  # user_id -> connection_id


@router.post("/metadata/negotiate")
async def metadata_negotiate(request: Request) -> JSONResponse:
    """SignalR negotiate endpoint for metadata hub."""
    return JSONResponse(create_negotiate_response())


async def _broadcast_presence_update(
    user_id: int,
    activity: UserActivity | None,
    status: UserStatus | None,
) -> None:
    """Broadcast a user presence update to all watchers.

    Args:
        user_id: The user whose presence changed
        activity: The user's current activity (None if offline)
        status: The user's status (None if offline)
    """
    hub_state = await get_hub_state_service()
    watcher_user_ids = await hub_state.get_presence_watchers()

    for watcher_user_id in watcher_user_ids:
        conn_id = user_connections.get(watcher_user_id)
        if not conn_id:
            continue

        conn = connections.get(conn_id)
        if not conn or not conn.websocket:
            continue

        try:
            if status is not None:
                presence = UserPresence(activity=activity, status=status)
                presence_data = presence.to_msgpack()
            else:
                presence_data = None

            await send_invocation(
                conn.websocket,
                conn.use_messagepack,
                "UserPresenceUpdated",
                [user_id, presence_data],
            )
        except Exception as e:
            logger.warning(f"Failed to send presence update to user {watcher_user_id}: {e}")


async def broadcast_beatmap_updates(beatmap_set_ids: list[int], queue_id: int = 1) -> int:
    """Broadcast beatmap updates to all connected metadata clients.

    This triggers the client to re-fetch metadata for the specified beatmapsets.
    Returns the number of clients notified.
    """
    if not beatmap_set_ids:
        return 0

    updates = BeatmapUpdates(
        beatmap_set_ids=beatmap_set_ids,
        last_processed_queue_id=queue_id,
    )

    notified = 0
    for conn in list(connections.values()):
        try:
            await send_invocation(
                conn.websocket,
                conn.use_messagepack,
                "BeatmapSetsUpdated",
                [updates],
            )
            notified += 1
            logger.info(f"Sent beatmap update to connection {conn.connection_id}")
        except Exception as e:
            logger.warning(f"Failed to send beatmap update to {conn.connection_id}: {e}")

    return notified


@router.post("/metadata/trigger-refresh")
async def trigger_metadata_refresh(beatmap_set_ids: list[int]) -> dict:
    """Trigger metadata refresh for specified beatmapsets on all connected clients.

    This is a debug/admin endpoint to force clients to re-fetch beatmap metadata.
    """
    notified = await broadcast_beatmap_updates(beatmap_set_ids)
    return {
        "success": True,
        "beatmap_set_ids": beatmap_set_ids,
        "clients_notified": notified,
    }


def get_online_count() -> int:
    """Get the number of unique online users."""
    user_ids = {conn.user_id for conn in connections.values() if conn.user_id}
    return len(user_ids)


@router.websocket("/metadata")
async def metadata_websocket(websocket: WebSocket) -> None:
    """SignalR WebSocket endpoint for metadata hub with user presence tracking.

    State is persisted to Redis for:
    - User presence (activity + status) - survives brief disconnects
    - Presence watcher subscriptions
    """
    await websocket.accept()
    connection_id = websocket.query_params.get("id", generate_connection_id())
    logger.info(f"Metadata hub connected: {connection_id}")

    hub_state = await get_hub_state_service()

    # Create connection tracking with initial online status
    conn = MetadataConnection(
        connection_id=connection_id,
        websocket=websocket,
        user_id=2,  # TODO: Get from auth token
        status=UserStatus.ONLINE,
    )
    connections[connection_id] = conn
    user_connections[conn.user_id] = connection_id

    try:
        # Handle handshake
        success, use_messagepack = await handle_handshake(websocket)
        if not success:
            await websocket.close()
            return

        conn.use_messagepack = use_messagepack
        logger.info(f"Metadata hub handshake complete: {connection_id} (msgpack={use_messagepack})")

        # Store initial presence in Redis and broadcast to watchers
        await hub_state.set_presence(conn.user_id, conn.activity, conn.status)
        await _broadcast_presence_update(conn.user_id, conn.activity, conn.status)

        async def on_ping() -> None:
            """Refresh presence TTL on ping."""
            await hub_state.refresh_presence_ttl(conn.user_id)

        async def handle_message(parsed: dict) -> None:
            target = parsed.get("target", "")
            args = parsed.get("arguments", [])
            invocation_id = parsed.get("invocationId")
            logger.info(f"Metadata hub: {target}({len(args)} args)")

            if target == "BeginWatchingUserPresence":
                await hub_state.add_presence_watcher(conn.user_id)
                conn.watching_presence = True

                # Send all currently online users from Redis
                online_users = await hub_state.get_all_online_users()
                for stored_presence in online_users:
                    if stored_presence.user_id != conn.user_id:
                        presence = stored_presence.to_protocol()
                        await send_invocation(
                            websocket,
                            conn.use_messagepack,
                            "UserPresenceUpdated",
                            [stored_presence.user_id, presence.to_msgpack()],
                        )

            elif target == "EndWatchingUserPresence":
                await hub_state.remove_presence_watcher(conn.user_id)
                conn.watching_presence = False

            elif target == "UpdateActivity":
                activity_data = args[0] if args else None
                conn.activity = UserActivity.from_msgpack(activity_data)

                await hub_state.set_presence(conn.user_id, conn.activity, conn.status)
                await _broadcast_presence_update(conn.user_id, conn.activity, conn.status)

            elif target == "UpdateStatus":
                if args and args[0] is not None:
                    try:
                        conn.status = UserStatus(args[0])
                    except ValueError:
                        conn.status = UserStatus.ONLINE
                else:
                    conn.status = UserStatus.ONLINE

                await hub_state.set_presence(conn.user_id, conn.activity, conn.status)
                await _broadcast_presence_update(conn.user_id, conn.activity, conn.status)

            elif target == "GetChangesSince":
                last_queue_id = args[0] if args else 0
                updates = BeatmapUpdates(
                    beatmap_set_ids=[],
                    last_processed_queue_id=last_queue_id,
                )
                await send_completion(websocket, conn.use_messagepack, invocation_id, updates)

        # Run message loop
        await run_message_loop(websocket, conn.use_messagepack, handle_message, on_ping=on_ping)

    except WebSocketDisconnect:
        logger.info(f"Metadata hub disconnected: {connection_id}")
    except Exception as e:
        logger.exception(f"Metadata hub error: {e}")
    finally:
        # Cleanup Redis state
        await hub_state.remove_presence(conn.user_id)
        await hub_state.remove_presence_watcher(conn.user_id)

        # Broadcast that user is offline
        await _broadcast_presence_update(conn.user_id, None, None)

        # Remove from in-memory tracking
        user_connections.pop(conn.user_id, None)
        connections.pop(connection_id, None)
        logger.info(f"Metadata hub closed: {connection_id}")
