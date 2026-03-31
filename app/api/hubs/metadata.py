"""Metadata hub for user presence and beatmap updates.

This hub handles:
- User presence tracking (activity and status)
- Presence watching (subscribe to other users' presence changes)
- Beatmap metadata updates (GetChangesSince, BeatmapSetsUpdated)
"""

import logging
from datetime import UTC
from datetime import datetime
from dataclasses import dataclass
from dataclasses import field

from fastapi import APIRouter
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy import or_
from sqlalchemy import select

from app.api.hubs.base import SignalRConnection
from app.api.hubs.base import create_negotiate_response
from app.api.hubs.base import extract_access_token
from app.api.hubs.base import generate_connection_id
from app.api.hubs.base import handle_handshake
from app.api.hubs.base import run_message_loop
from app.api.hubs.base import send_completion
from app.api.hubs.base import send_invocation
from app.api.hubs.base import send_void_completion
from app.core.database import async_session_maker
from app.core.security import decode_token
from app.models.multiplayer import MultiplayerPlaylistItem
from app.models.multiplayer import MultiplayerRoom
from app.models.multiplayer import MultiplayerScore
from app.models.multiplayer import RoomStatus
from app.models.user import User
from app.models.user import UserRelation
from app.protocol.enums import UserStatus
from app.protocol.models import BeatmapUpdates
from app.protocol.models import DailyChallengeInfo
from app.protocol.models import MultiplayerPlaylistItemStats
from app.protocol.models import MultiplayerRoomScoreSetEvent
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
    friend_ids: set[int] = field(default_factory=set)
    watched_room_ids: set[int] = field(default_factory=set)
    version_hash: str | None = None


# In-memory connection tracking (WebSocket objects can't be serialized to Redis)
# User state (presence) is stored in Redis for persistence
connections: dict[str, MetadataConnection] = {}  # connection_id -> connection
connections_by_user: dict[int, set[str]] = {}  # user_id -> connection_ids
presence_watching_connections: dict[int, set[str]] = {}  # user_id -> watching connection_ids
friend_presence_watching_connections: dict[int, set[str]] = {}  # target user_id -> watcher connection_ids
room_watching_connections: dict[int, set[str]] = {}  # room_id -> watcher connection_ids

DAILY_CHALLENGE_ROOM_CATEGORY = "daily_challenge"


def _extract_version_hash(websocket: WebSocket) -> str | None:
    """Extract client version hash from known websocket headers."""
    header_names = (
        "x-client-version-hash",
        "x-client-hash",
        "x-version-hash",
    )

    for header_name in header_names:
        header_value = websocket.headers.get(header_name)
        if not header_value:
            continue

        # osu! embeds the client hash in an 82-char token; extract the 32-char hash when available.
        if len(header_value) >= 82:
            return header_value[len(header_value) - 82:len(header_value) - 50]

        return header_value

    return None


def _presence_payload(activity: UserActivity | None, status: UserStatus | None) -> list | None:
    """Build presence payload from activity and status.

    Offline (or unknown) statuses are broadcast as `None` to match lazer semantics.
    """
    if status in (UserStatus.ONLINE, UserStatus.DO_NOT_DISTURB):
        return UserPresence(activity=activity, status=status).to_msgpack()
    return None


async def _store_presence(conn: MetadataConnection) -> None:
    """Persist or remove presence depending on the current status."""
    hub_state = await get_hub_state_service()
    if conn.status == UserStatus.OFFLINE:
        await hub_state.remove_presence(conn.user_id)
        return

    await hub_state.set_presence(conn.user_id, conn.activity, conn.status)


def _remove_friend_presence_subscription(target_user_id: int, connection_id: str) -> None:
    """Remove a connection from friend-presence watcher tracking for one target user."""
    watcher_conn_ids = friend_presence_watching_connections.get(target_user_id)
    if not watcher_conn_ids:
        return

    watcher_conn_ids.discard(connection_id)
    if watcher_conn_ids:
        friend_presence_watching_connections[target_user_id] = watcher_conn_ids
    else:
        friend_presence_watching_connections.pop(target_user_id, None)


async def _get_friend_ids_for_user(user_id: int) -> set[int]:
    """Fetch active friend IDs for a user."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(UserRelation.target_id)
            .join(User, User.id == UserRelation.target_id)
            .where(
                UserRelation.user_id == user_id,
                UserRelation.friend.is_(True),
                User.is_active.is_(True),
            ),
        )

    friend_ids = {int(row[0]) for row in result.fetchall()}
    friend_ids.discard(user_id)
    return friend_ids


async def _refresh_friend_subscriptions(conn: MetadataConnection) -> None:
    """Refresh friend-presence subscriptions for a connection."""
    latest_friend_ids = await _get_friend_ids_for_user(conn.user_id)

    removed_friend_ids = conn.friend_ids - latest_friend_ids
    for friend_id in removed_friend_ids:
        _remove_friend_presence_subscription(friend_id, conn.connection_id)

    newly_added_friend_ids = latest_friend_ids - conn.friend_ids
    for friend_id in newly_added_friend_ids:
        friend_presence_watching_connections.setdefault(friend_id, set()).add(conn.connection_id)

    conn.friend_ids = latest_friend_ids

    if not newly_added_friend_ids:
        return

    hub_state = await get_hub_state_service()
    for friend_id in newly_added_friend_ids:
        stored_presence = await hub_state.get_presence(friend_id)
        if stored_presence is None:
            continue

        presence_data = _presence_payload(stored_presence.activity, stored_presence.status)
        if presence_data is None:
            continue

        await send_invocation(
            conn.websocket,
            conn.use_messagepack,
            "FriendPresenceUpdated",
            [friend_id, presence_data],
        )


async def _send_self_presence_update(conn: MetadataConnection) -> None:
    """Send caller's own presence snapshot back to the current connection."""
    await send_invocation(
        conn.websocket,
        conn.use_messagepack,
        "UserPresenceUpdated",
        [conn.user_id, _presence_payload(conn.activity, conn.status)],
    )


def _remove_multiplayer_room_subscription(room_id: int, connection_id: str) -> None:
    """Remove a connection from one multiplayer room watcher set."""
    watcher_conn_ids = room_watching_connections.get(room_id)
    if not watcher_conn_ids:
        return

    watcher_conn_ids.discard(connection_id)
    if watcher_conn_ids:
        room_watching_connections[room_id] = watcher_conn_ids
    else:
        room_watching_connections.pop(room_id, None)


async def _build_playlist_stats_for_room(room_id: int) -> list[MultiplayerPlaylistItemStats]:
    """Build multiplayer playlist item stats snapshot for a room."""
    if room_id <= 0:
        return []

    async with async_session_maker() as session:
        playlist_result = await session.execute(
            select(MultiplayerPlaylistItem.id)
            .where(MultiplayerPlaylistItem.room_id == room_id)
            .order_by(MultiplayerPlaylistItem.playlist_order.asc(), MultiplayerPlaylistItem.id.asc()),
        )
        playlist_item_ids = [int(row[0]) for row in playlist_result.fetchall()]

        if not playlist_item_ids:
            return []

        score_result = await session.execute(
            select(
                MultiplayerScore.playlist_item_id,
                MultiplayerScore.total_score,
                MultiplayerScore.score_id,
            )
            .where(
                MultiplayerScore.room_id == room_id,
                MultiplayerScore.playlist_item_id.in_(playlist_item_ids),
                MultiplayerScore.passed.is_(True),
                MultiplayerScore.score_id.is_not(None),
            ),
        )
        score_rows = score_result.fetchall()

    stats_by_playlist_item = {
        item_id: MultiplayerPlaylistItemStats(playlist_item_id=item_id)
        for item_id in playlist_item_ids
    }

    for row in score_rows:
        playlist_item_id = int(row[0])
        total_score = int(row[1] or 0)
        score_id = int(row[2] or 0)

        stats = stats_by_playlist_item.get(playlist_item_id)
        if stats is None:
            continue

        distribution_idx = min(max(total_score // 100_000, 0), len(stats.total_score_distribution) - 1)
        stats.total_score_distribution[distribution_idx] += 1
        stats.cumulative_score += total_score
        if score_id > stats.last_processed_score_id:
            stats.last_processed_score_id = score_id

    return [stats_by_playlist_item[item_id] for item_id in playlist_item_ids]


async def _get_active_daily_challenge_info() -> DailyChallengeInfo | None:
    """Get the active daily challenge room, if present."""
    now = datetime.now(UTC)

    async with async_session_maker() as session:
        result = await session.execute(
            select(MultiplayerRoom.id)
            .where(
                MultiplayerRoom.category == DAILY_CHALLENGE_ROOM_CATEGORY,
                MultiplayerRoom.status != RoomStatus.CLOSED.value,
                or_(MultiplayerRoom.starts_at.is_(None), MultiplayerRoom.starts_at <= now),
                or_(MultiplayerRoom.ends_at.is_(None), MultiplayerRoom.ends_at >= now),
            )
            .order_by(MultiplayerRoom.id.asc())
            .limit(1),
        )

    room_id = result.scalar_one_or_none()
    if room_id is None:
        return None

    return DailyChallengeInfo(room_id=int(room_id))


async def _send_daily_challenge_update(conn: MetadataConnection) -> None:
    """Send active daily challenge snapshot to a caller."""
    try:
        challenge_info = await _get_active_daily_challenge_info()
    except Exception as exc:
        logger.warning("Failed fetching daily challenge info for connection %s: %s", conn.connection_id, exc)
        return

    if challenge_info is None:
        return

    await send_invocation(
        conn.websocket,
        conn.use_messagepack,
        "DailyChallengeUpdated",
        [challenge_info],
    )


async def send_multiplayer_room_score_set(event: MultiplayerRoomScoreSetEvent) -> int:
    """Broadcast a multiplayer room score set event to room metadata watchers."""
    watcher_conn_ids = room_watching_connections.get(event.room_id, set())
    if not watcher_conn_ids:
        return 0

    sent_count = 0
    for conn_id in list(watcher_conn_ids):
        conn = connections.get(conn_id)
        if not conn or not conn.websocket:
            _remove_multiplayer_room_subscription(event.room_id, conn_id)
            continue

        try:
            await send_invocation(
                conn.websocket,
                conn.use_messagepack,
                "MultiplayerRoomScoreSet",
                [event],
            )
            sent_count += 1
        except Exception as exc:
            logger.warning("Failed to send room score update for room %s: %s", event.room_id, exc)

    return sent_count


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
    presence_data = _presence_payload(activity, status)

    hub_state = await get_hub_state_service()
    watcher_user_ids = await hub_state.get_presence_watchers()

    for watcher_user_id in watcher_user_ids:
        watcher_conn_ids = presence_watching_connections.get(watcher_user_id)
        if not watcher_conn_ids:
            continue

        for conn_id in list(watcher_conn_ids):
            conn = connections.get(conn_id)
            if not conn or not conn.websocket:
                continue

            try:
                await send_invocation(
                    conn.websocket,
                    conn.use_messagepack,
                    "UserPresenceUpdated",
                    [user_id, presence_data],
                )
            except Exception as e:
                logger.warning(f"Failed to send presence update to user {watcher_user_id}: {e}")

    friend_watcher_conn_ids = friend_presence_watching_connections.get(user_id, set())
    for conn_id in list(friend_watcher_conn_ids):
        conn = connections.get(conn_id)
        if not conn or not conn.websocket:
            _remove_friend_presence_subscription(user_id, conn_id)
            continue

        try:
            await send_invocation(
                conn.websocket,
                conn.use_messagepack,
                "FriendPresenceUpdated",
                [user_id, presence_data],
            )
        except Exception as e:
            logger.warning(f"Failed to send friend presence update for user {user_id}: {e}")


async def broadcast_beatmap_updates(beatmap_set_ids: list[int], queue_id: int | None = None) -> int:
    """Broadcast beatmap updates to all connected metadata clients.

    This triggers the client to re-fetch metadata for the specified beatmapsets.
    Returns the number of clients notified.
    """
    if not beatmap_set_ids:
        return 0

    hub_state = await get_hub_state_service()
    if queue_id is None:
        queue_id = await hub_state.append_beatmap_updates(beatmap_set_ids)

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
    return len(connections_by_user)


@router.websocket("/metadata")
async def metadata_websocket(websocket: WebSocket) -> None:
    """SignalR WebSocket endpoint for metadata hub with user presence tracking.

    State is persisted to Redis for:
    - User presence (activity + status) - survives brief disconnects
    - Presence watcher subscriptions
    """
    token = extract_access_token(websocket)
    token_data = decode_token(token) if token else None
    if token_data is None:
        logger.warning("Metadata hub rejected unauthorized websocket connection")
        await websocket.close(code=4401)
        return

    await websocket.accept()
    connection_id = websocket.query_params.get("id", generate_connection_id())
    logger.info(f"Metadata hub connected: {connection_id}")

    hub_state = await get_hub_state_service()

    # Create connection tracking with initial online status
    conn = MetadataConnection(
        connection_id=connection_id,
        websocket=websocket,
        user_id=token_data.user_id,
        status=UserStatus.ONLINE,
        version_hash=_extract_version_hash(websocket),
    )
    connections[connection_id] = conn
    connections_by_user.setdefault(conn.user_id, set()).add(connection_id)

    try:
        # Handle handshake
        success, use_messagepack = await handle_handshake(websocket)
        if not success:
            await websocket.close() # What if we didn't close on handshake failure? The client should timeout after a while, but we can also proactively close here.
            return

        conn.use_messagepack = use_messagepack
        logger.info(f"Metadata hub handshake complete: {connection_id} (msgpack={use_messagepack})")

        await _send_daily_challenge_update(conn)

        # Store initial presence in Redis, then refresh friend subscriptions.
        await _store_presence(conn)
        await _refresh_friend_subscriptions(conn)
        await _broadcast_presence_update(conn.user_id, conn.activity, conn.status)

        async def on_ping() -> None:
            """Refresh presence TTL on ping."""
            if conn.status != UserStatus.OFFLINE:
                await hub_state.refresh_presence_ttl(conn.user_id)

        async def handle_message(parsed: dict) -> None:
            target = parsed.get("target", "")
            args = parsed.get("arguments", [])
            invocation_id = parsed.get("invocationId")
            logger.info(f"Metadata hub: {target}({len(args)} args)")

            if target == "BeginWatchingUserPresence":
                if not conn.watching_presence:
                    conn.watching_presence = True
                    watcher_conn_ids = presence_watching_connections.setdefault(conn.user_id, set())
                    watcher_conn_ids.add(connection_id)
                    if len(watcher_conn_ids) == 1:
                        await hub_state.add_presence_watcher(conn.user_id)

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

                if invocation_id is not None:
                    await send_void_completion(websocket, conn.use_messagepack, invocation_id)

            elif target == "EndWatchingUserPresence":
                if conn.watching_presence:
                    conn.watching_presence = False
                    watcher_conn_ids = presence_watching_connections.get(conn.user_id, set())
                    watcher_conn_ids.discard(connection_id)
                    if not watcher_conn_ids:
                        presence_watching_connections.pop(conn.user_id, None)
                        await hub_state.remove_presence_watcher(conn.user_id)
                    else:
                        presence_watching_connections[conn.user_id] = watcher_conn_ids

                if invocation_id is not None:
                    await send_void_completion(websocket, conn.use_messagepack, invocation_id)

            elif target == "UpdateActivity":
                activity_data = args[0] if args else None
                conn.activity = UserActivity.from_msgpack(activity_data)

                await _store_presence(conn)
                await _broadcast_presence_update(conn.user_id, conn.activity, conn.status)
                await _send_self_presence_update(conn)

                if invocation_id is not None:
                    await send_void_completion(websocket, conn.use_messagepack, invocation_id)

            elif target == "UpdateStatus":
                if args and args[0] is not None:
                    try:
                        conn.status = UserStatus(args[0])
                    except ValueError:
                        conn.status = UserStatus.ONLINE
                else:
                    conn.status = UserStatus.ONLINE

                await _store_presence(conn)
                await _broadcast_presence_update(conn.user_id, conn.activity, conn.status)
                await _send_self_presence_update(conn)

                if invocation_id is not None:
                    await send_void_completion(websocket, conn.use_messagepack, invocation_id)

            elif target == "RefreshFriends":
                await _refresh_friend_subscriptions(conn)

                if invocation_id is not None:
                    await send_void_completion(websocket, conn.use_messagepack, invocation_id)

            elif target == "BeginWatchingMultiplayerRoom":
                room_id = 0
                if args:
                    try:
                        room_id = int(args[0])
                    except (TypeError, ValueError):
                        room_id = 0

                stats: list[MultiplayerPlaylistItemStats] = []
                if room_id > 0:
                    conn.watched_room_ids.add(room_id)
                    room_watching_connections.setdefault(room_id, set()).add(connection_id)
                    stats = await _build_playlist_stats_for_room(room_id)

                if invocation_id is not None:
                    await send_completion(websocket, conn.use_messagepack, invocation_id, stats)

            elif target == "EndWatchingMultiplayerRoom":
                room_id = 0
                if args:
                    try:
                        room_id = int(args[0])
                    except (TypeError, ValueError):
                        room_id = 0

                if room_id > 0:
                    _remove_multiplayer_room_subscription(room_id, connection_id)
                    conn.watched_room_ids.discard(room_id)

                if invocation_id is not None:
                    await send_void_completion(websocket, conn.use_messagepack, invocation_id)

            elif target == "GetChangesSince":
                last_queue_id = 0
                if args:
                    try:
                        last_queue_id = int(args[0])
                    except (TypeError, ValueError):
                        last_queue_id = 0

                beatmap_set_ids, latest_queue_id = await hub_state.get_beatmap_updates_since(last_queue_id)
                updates = BeatmapUpdates(
                    beatmap_set_ids=beatmap_set_ids,
                    last_processed_queue_id=latest_queue_id,
                )
                if invocation_id is not None:
                    await send_completion(websocket, conn.use_messagepack, invocation_id, updates)

        # Run message loop
        await run_message_loop(websocket, conn.use_messagepack, handle_message, on_ping=on_ping)

    except WebSocketDisconnect:
        logger.info(f"Metadata hub disconnected: {connection_id}")
    except Exception as e:
        logger.exception(f"Metadata hub error: {e}")
    finally:
        # Remove watcher subscription for this connection and clear user watcher state if this was last watcher.
        watcher_conn_ids = presence_watching_connections.get(conn.user_id)
        if watcher_conn_ids:
            watcher_conn_ids.discard(connection_id)
            if not watcher_conn_ids:
                presence_watching_connections.pop(conn.user_id, None)
                await hub_state.remove_presence_watcher(conn.user_id)
            else:
                presence_watching_connections[conn.user_id] = watcher_conn_ids

        for friend_id in set(conn.friend_ids):
            _remove_friend_presence_subscription(friend_id, connection_id)
        conn.friend_ids.clear()

        for room_id in set(conn.watched_room_ids):
            _remove_multiplayer_room_subscription(room_id, connection_id)
        conn.watched_room_ids.clear()

        # Remove this connection and check if user still has active connections.
        user_conn_ids = connections_by_user.get(conn.user_id)
        has_active_connections = False
        if user_conn_ids:
            user_conn_ids.discard(connection_id)
            if user_conn_ids:
                connections_by_user[conn.user_id] = user_conn_ids
                has_active_connections = True
            else:
                connections_by_user.pop(conn.user_id, None)

        # Cleanup Redis state only when this was the user's last active connection.
        if not has_active_connections:
            await hub_state.remove_presence(conn.user_id)
            await _broadcast_presence_update(conn.user_id, None, None)

        connections.pop(connection_id, None)
        logger.info(f"Metadata hub closed: {connection_id}")
