"""Spectator hub for live gameplay streaming.

This hub handles:
- Players broadcasting their gameplay (BeginPlaySession, SendFrameData, EndPlaySession)
- Spectators watching players (StartWatchingUser, EndWatchingUser)
- Score processing notifications (UserScoreProcessed)
"""

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field
import time

from fastapi import APIRouter
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.hubs.base import SignalRConnection
from app.api.hubs.base import create_negotiate_response
from app.api.hubs.base import extract_access_token
from app.api.hubs.base import generate_connection_id
from app.api.hubs.base import handle_handshake
from app.api.hubs.base import run_message_loop
from app.api.hubs.base import send_invocation
from app.api.hubs.base import send_void_completion
from app.core.database import async_session_maker
from app.core.security import decode_token
from app.models.score import ScoreToken
from app.models.user import User
from app.protocol.enums import SpectatedUserState
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


@dataclass
class PendingScoreProcessedEvent:
    """Queued score processed event with retry scheduling metadata."""

    score_id: int
    next_attempt_at: float
    expires_at: float
    attempts: int = 0


# In-memory connection tracking (WebSocket objects can't be serialized to Redis)
# User state (playing, watches) is stored in Redis for persistence
connections: dict[str, SpectatorConnection] = {}  # connection_id -> connection
connections_by_user: dict[int, set[str]] = {}  # user_id -> connection_ids
# Deprecated in-memory cache kept only for test compatibility.
pending_score_processed_events: dict[int, dict[int, PendingScoreProcessedEvent]] = {}
score_processed_dispatch_task: asyncio.Task | None = None

SCORE_PROCESSED_INITIAL_DELAY_SECONDS = 0.5
SCORE_PROCESSED_RETRY_INTERVAL_SECONDS = 0.5
SCORE_PROCESSED_RETRY_WINDOW_SECONDS = 5.0
SCORE_PROCESSED_RETRY_WARNING_ATTEMPTS = 3
LEGACY_TIMESTAMP_EPOCH_FLOOR = 946684800.0  # 2000-01-01 UTC
# Compatibility: allow metadata presence watchers to receive spectator stream broadcasts.
ENABLE_PRESENCE_WATCHERS_FOR_SPECTATOR = True


def _remove_connection_for_user(user_id: int, connection_id: str) -> bool:
    """Remove one connection from a user's connection set.

    Returns True if the user still has at least one active connection after removal.
    """
    user_conn_ids = connections_by_user.get(user_id)
    if not user_conn_ids:
        return False

    user_conn_ids.discard(connection_id)
    if user_conn_ids:
        connections_by_user[user_id] = user_conn_ids
        return True

    connections_by_user.pop(user_id, None)
    return False


def _normalize_finished_state(state: SpectatorState) -> SpectatorState:
    """Normalize final spectator state before emitting UserFinishedPlaying."""
    if state.state == SpectatedUserState.PLAYING:
        state.state = SpectatedUserState.QUIT

    return state


async def _get_valid_score_token(score_token_id: int, user_id: int) -> ScoreToken | None:
    """Return an unused score token for user if valid."""
    async with async_session_maker() as session:
        result = await session.execute(
            select(ScoreToken).where(
                ScoreToken.id == score_token_id,
                ScoreToken.user_id == user_id,
                ScoreToken.score_id.is_(None),
            ),
        )

    return result.scalar_one_or_none()


async def _get_username_for_user(user_id: int) -> str | None:
    """Fetch a user's username for spectator notifications."""
    async with async_session_maker() as session:
        result = await session.execute(select(User.username).where(User.id == user_id))

    username = result.scalar_one_or_none()
    if not username:
        return None

    return str(username)


def _ensure_score_processed_dispatch_task() -> None:
    """Start background dispatcher for queued score processed events."""
    global score_processed_dispatch_task

    if score_processed_dispatch_task is None or score_processed_dispatch_task.done():
        logger.debug("Starting score processed dispatcher task")
        score_processed_dispatch_task = asyncio.create_task(_dispatch_pending_score_processed_events())


async def _dispatch_pending_score_processed_events() -> None:
    """Dispatch queued score processed events with retries for late client registration."""
    global score_processed_dispatch_task

    try:
        hub_state = await get_hub_state_service()

        while True:
            pending_user_ids = await hub_state.list_pending_score_processed_users()
            if not pending_user_ids:
                break

            now = time.time()
            cycle_events = 0
            cycle_delivered = 0
            cycle_retried = 0
            cycle_expired = 0
            cycle_migrated = 0

            for user_id in pending_user_ids:
                user_events = await hub_state.get_pending_score_processed_events(user_id)
                if not user_events:
                    await hub_state.clear_pending_score_processed_user(user_id)
                    continue

                cycle_events += len(user_events)

                for score_id, event in user_events.items():
                    expires_at = float(event["expires_at"])
                    next_attempt_at = float(event["next_attempt_at"])
                    attempts = int(event.get("attempts", 0))
                    migrated_event = False

                    # Pre-epoch values are legacy monotonic timestamps from older builds.
                    if expires_at < LEGACY_TIMESTAMP_EPOCH_FLOOR:
                        expires_at = now + SCORE_PROCESSED_RETRY_WINDOW_SECONDS
                        migrated_event = True

                    if next_attempt_at < LEGACY_TIMESTAMP_EPOCH_FLOOR:
                        next_attempt_at = now
                        migrated_event = True

                    if migrated_event:
                        cycle_migrated += 1

                    if now >= expires_at:
                        logger.debug(
                            "Dropping expired UserScoreProcessed notification user=%s score=%s attempts=%s",
                            user_id,
                            score_id,
                            attempts,
                        )
                        await hub_state.remove_pending_score_processed_event(user_id, score_id)
                        cycle_expired += 1
                        continue

                    if now < next_attempt_at:
                        if migrated_event:
                            await hub_state.save_pending_score_processed_event(
                                user_id,
                                {
                                    "score_id": score_id,
                                    "next_attempt_at": next_attempt_at,
                                    "expires_at": expires_at,
                                    "attempts": attempts,
                                },
                            )
                        continue

                    delivered_count = await _send_to_user(user_id, "UserScoreProcessed", [user_id, score_id])

                    if delivered_count > 0:
                        logger.debug(
                            "Delivered UserScoreProcessed notification user=%s score=%s attempts=%s deliveries=%s",
                            user_id,
                            score_id,
                            attempts + 1,
                            delivered_count,
                        )
                        await hub_state.remove_pending_score_processed_event(user_id, score_id)
                        cycle_delivered += 1
                        continue

                    retry_attempt = attempts + 1
                    await hub_state.save_pending_score_processed_event(
                        user_id,
                        {
                            "score_id": score_id,
                            "next_attempt_at": now + SCORE_PROCESSED_RETRY_INTERVAL_SECONDS,
                            "expires_at": expires_at,
                            "attempts": retry_attempt,
                        },
                    )
                    cycle_retried += 1

                    if retry_attempt >= SCORE_PROCESSED_RETRY_WARNING_ATTEMPTS:
                        logger.warning(
                            "Retrying UserScoreProcessed notification user=%s score=%s attempts=%s time_left=%.2fs",
                            user_id,
                            score_id,
                            retry_attempt,
                            max(0.0, expires_at - now),
                        )

            if cycle_events > 0:
                logger.debug(
                    (
                        "Score processed dispatcher cycle users=%s events=%s delivered=%s "
                        "retried=%s expired=%s migrated=%s"
                    ),
                    len(pending_user_ids),
                    cycle_events,
                    cycle_delivered,
                    cycle_retried,
                    cycle_expired,
                    cycle_migrated,
                )

            if await hub_state.list_pending_score_processed_users():
                await asyncio.sleep(0.1)
    except Exception as exc:
        logger.exception("Score processed dispatcher failed: %s", exc)
    finally:
        logger.debug("Score processed dispatcher task finished")
        score_processed_dispatch_task = None


@router.post("/spectator/negotiate")
async def spectator_negotiate(request: Request) -> JSONResponse:
    """SignalR negotiate endpoint for spectator hub."""
    return JSONResponse(create_negotiate_response())


async def _broadcast_to_watchers(target_user_id: int, target: str, arguments: list) -> None:
    """Broadcast a message to all users watching a specific user."""
    logger.info("Broadcasting %s to watchers of user %s", target, target_user_id)
    hub_state = await get_hub_state_service()
    explicit_watcher_user_ids = await hub_state.get_watchers(target_user_id)
    watcher_user_ids = set(explicit_watcher_user_ids)

    presence_watcher_count = 0
    if ENABLE_PRESENCE_WATCHERS_FOR_SPECTATOR:
        presence_watcher_user_ids = await hub_state.get_presence_watchers()
        presence_watcher_user_ids.discard(target_user_id)
        presence_watcher_count = len(presence_watcher_user_ids)
        watcher_user_ids.update(presence_watcher_user_ids)

    logger.info(
        (
            "Found %s watchers for user %s (explicit=%s, presence=%s)"
        ),
        len(watcher_user_ids),
        target_user_id,
        len(explicit_watcher_user_ids),
        presence_watcher_count,
    )
    for watcher_user_id in watcher_user_ids:
        watcher_conn_ids = connections_by_user.get(watcher_user_id)
        if not watcher_conn_ids:
            logger.info("Watcher user %s has no active connections, skipping", watcher_user_id)
            continue

        for conn_id in list(watcher_conn_ids):
            conn = connections.get(conn_id)
            if not conn or not conn.websocket:
                logger.warning(f"Failed to find connection for watcher user {watcher_user_id}, connection {conn_id}")
                _remove_connection_for_user(watcher_user_id, conn_id)
                continue

            try:
                await send_invocation(conn.websocket, conn.use_messagepack, target, arguments)
            except Exception as e:
                logger.warning(f"Failed to send to spectator watcher user {watcher_user_id}: {e}")
                _remove_connection_for_user(watcher_user_id, conn_id)


async def _send_to_user(user_id: int, target: str, arguments: list) -> int:
    """Send a message to a specific user on the spectator hub."""
    user_conn_ids = connections_by_user.get(user_id)
    if not user_conn_ids:
        return 0

    delivered_count = 0

    for conn_id in list(user_conn_ids):
        conn = connections.get(conn_id)
        if not conn or not conn.websocket:
            _remove_connection_for_user(user_id, conn_id)
            continue

        try:
            await send_invocation(conn.websocket, conn.use_messagepack, target, arguments)
            delivered_count += 1
        except Exception as e:
            logger.warning(f"Failed to send to spectator user {user_id}: {e}")
            _remove_connection_for_user(user_id, conn_id)

    return delivered_count


async def send_user_score_processed(user_id: int, score_id: int) -> int:
    """Notify a user that their score has been processed.

    This is called after score submission to trigger the client to
    fetch updated user statistics for the "Overall Ranking" panel.
    """
    now = time.time()
    hub_state = await get_hub_state_service()
    event_count = await hub_state.upsert_pending_score_processed_event(
        user_id=user_id,
        score_id=score_id,
        next_attempt_at=now + SCORE_PROCESSED_INITIAL_DELAY_SECONDS,
        expires_at=now + SCORE_PROCESSED_RETRY_WINDOW_SECONDS,
    )

    logger.debug(
        "Queued UserScoreProcessed notification user=%s score=%s queued_user_events=%s",
        user_id,
        score_id,
        event_count,
    )

    _ensure_score_processed_dispatch_task()

    return event_count


def resume_score_processed_dispatcher() -> None:
    """Resume pending score-processed dispatching (used on app startup)."""
    logger.debug("Resuming score processed dispatcher")
    _ensure_score_processed_dispatch_task()


@router.websocket("/spectator")
async def spectator_websocket(websocket: WebSocket) -> None:
    """SignalR WebSocket endpoint for spectator hub.

    State is persisted to Redis for:
    - Playing users (survives brief disconnects)
    - Watch relationships (can be restored on reconnect)
    """
    token = extract_access_token(websocket)
    token_data = decode_token(token) if token else None
    if token_data is None:
        logger.warning("Spectator hub rejected unauthorized websocket connection")
        await websocket.close(code=4401)
        return

    logger.debug("Spectator hub authenticated websocket for user %s", token_data.user_id)
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
    connections_by_user.setdefault(conn.user_id, set()).add(connection_id)
    logger.debug(
        "Spectator hub tracking connection %s for user %s (%s active connections)",
        connection_id,
        conn.user_id,
        len(connections),
    )

    # Track current play state locally (also persisted to Redis)
    current_state: SpectatorState | None = None
    score_token: int | None = None

    try:
        # Handle handshake
        logger.debug("Spectator hub waiting for handshake: %s", connection_id)
        success, use_messagepack = await handle_handshake(websocket)
        if not success:
            logger.warning("Spectator hub handshake failed: %s", connection_id)
            await websocket.close() # What if we didn't close on handshake failure? The client should timeout after a while, but we can also proactively close here.
            return

        conn.use_messagepack = use_messagepack
        logger.info(f"Spectator hub handshake complete: {connection_id} (msgpack={use_messagepack})")

        # Restore previous watch state on reconnect
        previous_watches = await hub_state.get_watching(conn.user_id)
        if previous_watches:
            logger.info(f"User {conn.user_id} reconnected, restoring {len(previous_watches)} watches")
            conn.watching_users = set(previous_watches)

            for watched_user_id in sorted(conn.watching_users):
                target_playing = await hub_state.get_playing(watched_user_id)
                if not target_playing:
                    continue

                print(f"Restoring watch state [UserBeganPlaying] for user {conn.user_id} on target {watched_user_id} (state={target_playing.state})")

                await send_invocation(
                    websocket,
                    conn.use_messagepack,
                    "UserBeganPlaying",
                    [watched_user_id, target_playing.state.to_msgpack()], # user_id, SpectatorState
                )
                logger.debug(
                    "Restored watch state for user %s on target %s",
                    conn.user_id,
                    watched_user_id,
                )

        async def on_ping() -> None:
            """Refresh Redis TTL for active spectator state while connection is alive."""
            if conn.is_playing:
                await hub_state.refresh_playing_ttl(conn.user_id)

                if score_token is not None:
                    await hub_state.refresh_replay_frame_ttl(score_token)

            if conn.watching_users:
                await hub_state.refresh_user_watch_ttl(conn.user_id, conn.watching_users)

        async def handle_message(parsed: dict) -> None:
            nonlocal current_state, score_token

            target = parsed.get("target", "")
            args = parsed.get("arguments", [])
            invocation_id = parsed.get("invocationId")
            logger.info(
                "Spectator hub message from user %s: %s(%s args)",
                conn.user_id,
                target,
                len(args),
            )

            try:
                if target == "BeginPlaySession":
                    raw_score_token = args[0] if args else None
                    if raw_score_token is None:
                        logger.warning("BeginPlaySession missing score token from user %s", conn.user_id)
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    try:
                        parsed_score_token = int(raw_score_token)
                    except (TypeError, ValueError):
                        logger.warning(
                            "BeginPlaySession invalid score token %r from user %s",
                            raw_score_token,
                            conn.user_id,
                        )
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    state_data = args[1] if len(args) > 1 else {}
                    parsed_state = SpectatorState.from_msgpack(state_data)
                    token = await _get_valid_score_token(parsed_score_token, conn.user_id)
                    if token is None:
                        logger.warning(
                            "BeginPlaySession rejected unknown/used token %s from user %s",
                            parsed_score_token,
                            conn.user_id,
                        )
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    if parsed_state.beatmap_id is not None and token.beatmap_id != parsed_state.beatmap_id:
                        logger.warning(
                            "BeginPlaySession beatmap mismatch for user %s token=%s token_beatmap=%s state_beatmap=%s",
                            conn.user_id,
                            parsed_score_token,
                            token.beatmap_id,
                            parsed_state.beatmap_id,
                        )
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    if parsed_state.ruleset_id is not None and token.ruleset_id != parsed_state.ruleset_id:
                        logger.warning(
                            "BeginPlaySession ruleset mismatch for user %s token=%s token_ruleset=%s state_ruleset=%s",
                            conn.user_id,
                            parsed_score_token,
                            token.ruleset_id,
                            parsed_state.ruleset_id,
                        )
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    score_token = parsed_score_token
                    current_state = parsed_state
                    conn.is_playing = True

                    await hub_state.set_playing(conn.user_id, current_state, score_token)
                    print(f"User {conn.user_id} began playing beatmap {current_state.beatmap_id} with token {score_token} and state {current_state}")
                    await _broadcast_to_watchers(
                        conn.user_id,
                        "UserBeganPlaying",
                        [conn.user_id, current_state.to_msgpack()],
                    )
                    logger.info(f"User {conn.user_id} began playing beatmap {current_state.beatmap_id}")

                    if invocation_id is not None:
                        await send_void_completion(websocket, conn.use_messagepack, invocation_id)

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
                    else:
                        logger.warning(
                            "SendFrameData received without active score token from user %s",
                            conn.user_id,
                        )

                    await _broadcast_to_watchers(
                        conn.user_id,
                        "UserSentFrames",
                        [conn.user_id, frame_bundle.to_msgpack()],
                    )

                    if invocation_id is not None:
                        await send_void_completion(websocket, conn.use_messagepack, invocation_id)

                elif target == "EndPlaySession":
                    state_data = args[0] if args else {}
                    final_state = _normalize_finished_state(SpectatorState.from_msgpack(state_data))
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

                    if invocation_id is not None:
                        await send_void_completion(websocket, conn.use_messagepack, invocation_id)

                elif target == "StartWatchingUser":
                    raw_target_user_id = args[0] if args else None
                    if raw_target_user_id is None:
                        logger.warning("StartWatchingUser missing target user id from user %s", conn.user_id)
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    try:
                        target_user_id = int(raw_target_user_id)
                    except (TypeError, ValueError):
                        logger.warning(
                            "StartWatchingUser invalid target user id %r from user %s",
                            raw_target_user_id,
                            conn.user_id,
                        )
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    conn.watching_users.add(target_user_id)

                    await hub_state.add_watcher(conn.user_id, target_user_id)

                    # Send current playing state if target is playing
                    target_playing = await hub_state.get_playing(target_user_id)
                    if target_playing:
                        print(f"User {conn.user_id} started watching user {target_user_id} who is currently playing (state={target_playing.state}), sending UserBeganPlaying")
                        await send_invocation(
                            websocket,
                            conn.use_messagepack,
                            "UserBeganPlaying",
                            [target_user_id, target_playing.state.to_msgpack()],
                        )
                        logger.info(f"Sent playing state: user {target_user_id} is playing")

                    # Notify target user
                    watcher_username = await _get_username_for_user(conn.user_id)
                    if watcher_username is None:
                        logger.warning(
                            "Failed to resolve watcher username for user %s, using fallback",
                            conn.user_id,
                        )
                        watcher_username = f"User {conn.user_id}"

                    watcher = SpectatorUser(online_id=conn.user_id, username=watcher_username)
                    await _send_to_user(
                        target_user_id,
                        "UserStartedWatching",
                        [[watcher.to_msgpack()]],
                    )
                    logger.info(f"User {conn.user_id} started watching user {target_user_id}")

                    if invocation_id is not None:
                        await send_void_completion(websocket, conn.use_messagepack, invocation_id)

                elif target == "EndWatchingUser":
                    raw_target_user_id = args[0] if args else None
                    if raw_target_user_id is None:
                        logger.warning("EndWatchingUser missing target user id from user %s", conn.user_id)
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    try:
                        target_user_id = int(raw_target_user_id)
                    except (TypeError, ValueError):
                        logger.warning(
                            "EndWatchingUser invalid target user id %r from user %s",
                            raw_target_user_id,
                            conn.user_id,
                        )
                        if invocation_id is not None:
                            await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                        return

                    conn.watching_users.discard(target_user_id)

                    await hub_state.remove_watcher(conn.user_id, target_user_id)
                    await _send_to_user(target_user_id, "UserEndedWatching", [conn.user_id])
                    logger.info(f"User {conn.user_id} stopped watching user {target_user_id}")

                    if invocation_id is not None:
                        await send_void_completion(websocket, conn.use_messagepack, invocation_id)

                else:
                    logger.warning(
                        "Spectator hub received unknown target %r from user %s",
                        target,
                        conn.user_id,
                    )
                    if invocation_id is not None:
                        await send_void_completion(websocket, conn.use_messagepack, invocation_id)
            except Exception:
                logger.exception(
                    "Spectator hub failed to process target %r for user %s",
                    target,
                    conn.user_id,
                )
                if invocation_id is not None:
                    try:
                        await send_void_completion(websocket, conn.use_messagepack, invocation_id)
                    except Exception:
                        logger.exception(
                            "Spectator hub failed to send completion for invocation %s",
                            invocation_id,
                        )

        # Run message loop
        await run_message_loop(websocket, conn.use_messagepack, handle_message, on_ping=on_ping)

    except WebSocketDisconnect:
        logger.info(f"Spectator hub disconnected: {connection_id}")
    except Exception as e:
        logger.exception(f"Spectator hub error: {e}")
    finally:
        logger.debug(
            "Spectator hub cleanup starting for connection %s user %s (is_playing=%s, watching=%s)",
            connection_id,
            conn.user_id,
            conn.is_playing,
            len(conn.watching_users),
        )
        # Cleanup Redis state
        if conn.is_playing:
            await hub_state.remove_playing(conn.user_id)
            if current_state:
                normalized_state = _normalize_finished_state(current_state)
                await _broadcast_to_watchers(
                    conn.user_id,
                    "UserFinishedPlaying",
                    [conn.user_id, normalized_state.to_msgpack()],
                )

        has_other_connections = _remove_connection_for_user(conn.user_id, connection_id)
        if not has_other_connections:
            watcher_targets = set(conn.watching_users)
            if not watcher_targets:
                watcher_targets = await hub_state.get_watching(conn.user_id)

            for watcher_target_id in watcher_targets:
                await _send_to_user(watcher_target_id, "UserEndedWatching", [conn.user_id])

            await hub_state.clear_user_watches(conn.user_id)

        # Remove from in-memory tracking
        connections.pop(connection_id, None)
        logger.debug(
            "Spectator hub cleanup complete for connection %s (%s active connections)",
            connection_id,
            len(connections),
        )
        logger.info(f"Spectator hub closed: {connection_id}")
