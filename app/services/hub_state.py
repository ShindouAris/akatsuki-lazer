"""Redis-backed hub state storage for SignalR hubs.

This module provides persistent state storage for hub connections, enabling:
- Server restarts without losing user state
- Future multi-server scaling via Redis pub/sub
- Automatic expiration of stale sessions via TTL

State stored:
- User presence (activity + status) - expires after inactivity
- Playing users (spectator state) - expires after game timeout
- Watch relationships (user -> watched users) - for reconnection restoration
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import redis.asyncio as redis

from app.core.config import get_settings
from app.protocol.enums import UserStatus
from app.protocol.models import FrameDataBundle
from app.protocol.models import SpectatorState
from app.protocol.models import UserActivity
from app.protocol.models import UserPresence

logger = logging.getLogger(__name__)

# Redis key prefixes
PREFIX_PRESENCE = "hub:presence:"  # {user_id} -> UserPresence JSON
PREFIX_PLAYING = "hub:playing:"  # {user_id} -> SpectatorState JSON
PREFIX_WATCHING = "hub:watching:"  # {user_id} -> Set of watched user IDs
PREFIX_WATCHERS = "hub:watchers:"  # {user_id} -> Set of watcher user IDs (reverse index)
PREFIX_PRESENCE_WATCHERS = "hub:presence_watchers"  # Set of user IDs watching presence
PREFIX_REPLAY_FRAMES = "hub:replay:frames:"  # {score_token} -> list of FrameDataBundle payloads
PREFIX_PENDING_SCORE_PROCESSED_USERS = "hub:pending_score_processed:users"  # set of user_ids with queued score events
PREFIX_PENDING_SCORE_PROCESSED_USER = "hub:pending_score_processed:user:"  # {user_id} -> hash score_id -> metadata json
PREFIX_BEATMAP_UPDATE_COUNTER = "hub:metadata:beatmap_updates:counter"  # Monotonic queue id
PREFIX_BEATMAP_UPDATE_ENTRY = "hub:metadata:beatmap_updates:entry:"  # {queue_id} -> JSON list of beatmapset ids
PREFIX_BEATMAP_UPDATE_IDS = "hub:metadata:beatmap_updates:ids"  # Sorted set of known queue IDs

# TTLs for automatic expiration
TTL_PRESENCE = timedelta(minutes=5)  # Presence expires after 5 min of no updates
TTL_PLAYING = timedelta(hours=2)  # Playing state expires after 2 hours max
TTL_WATCHING = timedelta(hours=1)  # Watch relationships expire after 1 hour
TTL_REPLAY_FRAMES = timedelta(hours=1)  # Replay buffers expire if score is never submitted
TTL_PENDING_SCORE_PROCESSED = timedelta(minutes=5)  # Pending score notifications survive brief restarts
TTL_BEATMAP_UPDATES = timedelta(hours=24)  # Keep beatmap update queue history for GetChangesSince

MAX_BEATMAP_UPDATE_ENTRIES = 5000
MAX_REPLAY_FRAME_BUNDLES = 10_000


@dataclass
class StoredPresence:
    """User presence data stored in Redis."""

    user_id: int
    activity: UserActivity | None
    status: UserStatus

    def to_json(self) -> str:
        """Serialize to JSON for Redis storage."""
        return json.dumps({
            "user_id": self.user_id,
            "activity": self.activity.to_msgpack() if self.activity else None,
            "status": int(self.status),
        })

    @classmethod
    def from_json(cls, data: str) -> StoredPresence:
        """Deserialize from JSON."""
        obj = json.loads(data)
        activity_data = obj.get("activity")
        return cls(
            user_id=obj["user_id"],
            activity=UserActivity.from_msgpack(activity_data) if activity_data else None,
            status=UserStatus(obj.get("status", 1)),
        )

    def to_protocol(self) -> UserPresence:
        """Convert to protocol model."""
        return UserPresence(activity=self.activity, status=self.status)


@dataclass
class StoredPlayingState:
    """Playing user state stored in Redis."""

    user_id: int
    state: SpectatorState
    score_token: int | None = None

    def to_json(self) -> str:
        """Serialize to JSON for Redis storage."""
        return json.dumps({
            "user_id": self.user_id,
            "state": self.state.to_msgpack(),
            "score_token": self.score_token,
        })

    @classmethod
    def from_json(cls, data: str) -> StoredPlayingState:
        """Deserialize from JSON."""
        obj = json.loads(data)
        return cls(
            user_id=obj["user_id"],
            state=SpectatorState.from_msgpack(obj["state"]),
            score_token=obj.get("score_token"),
        )


class HubStateService:
    """Redis-backed state storage for SignalR hubs.

    Usage:
        service = HubStateService()
        await service.connect()

        # Store presence
        await service.set_presence(user_id, activity, status)

        # Get presence
        presence = await service.get_presence(user_id)

        # Store playing state
        await service.set_playing(user_id, state, score_token)

        # Clean up
        await service.close()
    """

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        self._settings = get_settings()

    async def connect(self) -> None:
        """Connect to Redis."""
        if self._redis is None:
            self._redis = redis.from_url(
                self._settings.redis_url,
                decode_responses=True,
            )
            logger.info(f"Connected to Redis at {self._settings.redis_url}")

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            logger.info("Closed Redis connection")

    @property
    def redis(self) -> redis.Redis:
        """Get Redis client, raising if not connected."""
        if self._redis is None:
            raise RuntimeError("HubStateService not connected. Call connect() first.")
        return self._redis

    # =========================================================================
    # User Presence
    # =========================================================================

    async def set_presence(
        self,
        user_id: int,
        activity: UserActivity | None,
        status: UserStatus,
    ) -> None:
        """Store user presence with TTL."""
        presence = StoredPresence(user_id=user_id, activity=activity, status=status)
        key = f"{PREFIX_PRESENCE}{user_id}"
        await self.redis.setex(key, TTL_PRESENCE, presence.to_json())
        logger.debug(f"Set presence for user {user_id}: status={status.name}")

    async def get_presence(self, user_id: int) -> StoredPresence | None:
        """Get user presence."""
        key = f"{PREFIX_PRESENCE}{user_id}"
        data = await self.redis.get(key)
        if data:
            return StoredPresence.from_json(data)
        return None

    async def remove_presence(self, user_id: int) -> None:
        """Remove user presence (user went offline)."""
        key = f"{PREFIX_PRESENCE}{user_id}"
        await self.redis.delete(key)
        logger.debug(f"Removed presence for user {user_id}")

    async def get_all_online_users(self) -> list[StoredPresence]:
        """Get all online users with presence data."""
        keys = []
        async for key in self.redis.scan_iter(f"{PREFIX_PRESENCE}*"):
            keys.append(key)

        if not keys:
            return []

        values = await self.redis.mget(keys)
        result = []
        for data in values:
            if data:
                try:
                    result.append(StoredPresence.from_json(data))
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse presence data: {e}")
        return result

    async def refresh_presence_ttl(self, user_id: int) -> bool:
        """Refresh the TTL on a user's presence (keep-alive)."""
        key = f"{PREFIX_PRESENCE}{user_id}"
        return await self.redis.expire(key, TTL_PRESENCE)

    # =========================================================================
    # Presence Watchers (users subscribed to presence updates)
    # =========================================================================

    async def add_presence_watcher(self, user_id: int) -> None:
        """Mark a user as watching presence updates."""
        await self.redis.sadd(PREFIX_PRESENCE_WATCHERS, user_id)

    async def remove_presence_watcher(self, user_id: int) -> None:
        """Remove a user from presence watchers."""
        await self.redis.srem(PREFIX_PRESENCE_WATCHERS, user_id)

    async def is_watching_presence(self, user_id: int) -> bool:
        """Check if user is watching presence."""
        return bool(await self.redis.sismember(PREFIX_PRESENCE_WATCHERS, str(user_id)))

    async def get_presence_watchers(self) -> set[int]:
        """Get all users watching presence."""
        members = await self.redis.smembers(PREFIX_PRESENCE_WATCHERS)
        return {int(m) for m in members}

    # =========================================================================
    # Playing Users (Spectator Hub)
    # =========================================================================

    async def set_playing(
        self,
        user_id: int,
        state: SpectatorState,
        score_token: int | None = None,
    ) -> None:
        """Store that a user is currently playing."""
        playing = StoredPlayingState(user_id=user_id, state=state, score_token=score_token)
        key = f"{PREFIX_PLAYING}{user_id}"
        await self.redis.setex(key, TTL_PLAYING, playing.to_json())
        logger.debug(f"Set playing state for user {user_id}: beatmap={state.beatmap_id}")

    async def get_playing(self, user_id: int) -> StoredPlayingState | None:
        """Get a user's playing state."""
        key = f"{PREFIX_PLAYING}{user_id}"
        data = await self.redis.get(key)
        if data:
            return StoredPlayingState.from_json(data)
        return None

    async def remove_playing(self, user_id: int) -> None:
        """Remove playing state (user finished playing)."""
        key = f"{PREFIX_PLAYING}{user_id}"
        await self.redis.delete(key)
        logger.debug(f"Removed playing state for user {user_id}")

    async def get_all_playing_users(self) -> list[StoredPlayingState]:
        """Get all currently playing users."""
        keys = []
        async for key in self.redis.scan_iter(f"{PREFIX_PLAYING}*"):
            keys.append(key)

        if not keys:
            return []

        values = await self.redis.mget(keys)
        result = []
        for data in values:
            if data:
                try:
                    result.append(StoredPlayingState.from_json(data))
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse playing state: {e}")
        return result

    async def is_playing(self, user_id: int) -> bool:
        """Check if a user is currently playing."""
        key = f"{PREFIX_PLAYING}{user_id}"
        return await self.redis.exists(key) > 0

    async def refresh_playing_ttl(self, user_id: int) -> bool:
        """Refresh a user's playing state TTL (keep-alive)."""
        key = f"{PREFIX_PLAYING}{user_id}"
        return await self.redis.expire(key, TTL_PLAYING)

    # =========================================================================
    # Replay Frame Buffers (Spectator -> Score Submission)
    # =========================================================================

    async def append_replay_frame_bundle(
        self,
        score_token: int,
        frame_bundle: FrameDataBundle,
    ) -> int:
        """Append a replay frame bundle for a score token.

        Returns:
            Current number of buffered bundles for the token.
        """
        key = f"{PREFIX_REPLAY_FRAMES}{score_token}"
        payload = json.dumps(frame_bundle.to_msgpack())
        length = await self.redis.rpush(key, payload)

        if length > MAX_REPLAY_FRAME_BUNDLES:
            await self.redis.ltrim(key, -MAX_REPLAY_FRAME_BUNDLES, -1)
            length = MAX_REPLAY_FRAME_BUNDLES

        await self.redis.expire(key, TTL_REPLAY_FRAMES)
        return length

    async def get_replay_frame_bundles(self, score_token: int) -> list[FrameDataBundle]:
        """Get all buffered replay frame bundles for a score token."""
        key = f"{PREFIX_REPLAY_FRAMES}{score_token}"
        values = await self.redis.lrange(key, 0, -1)
        bundles: list[FrameDataBundle] = []

        for value in values:
            try:
                payload = json.loads(value)
                bundles.append(FrameDataBundle.from_msgpack(payload))
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                logger.warning(
                    "Failed to decode replay frame bundle for token %s: %s",
                    score_token,
                    exc,
                )
        return bundles

    async def clear_replay_frame_bundles(self, score_token: int) -> None:
        """Remove buffered replay frame bundles for a score token."""
        key = f"{PREFIX_REPLAY_FRAMES}{score_token}"
        await self.redis.delete(key)

    async def count_replay_frame_bundles(self, score_token: int) -> int:
        """Return the buffered replay bundle count for a score token."""
        key = f"{PREFIX_REPLAY_FRAMES}{score_token}"
        return await self.redis.llen(key)

    async def refresh_replay_frame_ttl(self, score_token: int) -> bool:
        """Refresh replay buffer TTL for an active score token."""
        key = f"{PREFIX_REPLAY_FRAMES}{score_token}"
        return await self.redis.expire(key, TTL_REPLAY_FRAMES)

    # =========================================================================
    # Pending Score Processed Notifications (Spectator Hub)
    # =========================================================================

    async def upsert_pending_score_processed_event(
        self,
        user_id: int,
        score_id: int,
        next_attempt_at: float,
        expires_at: float,
    ) -> int:
        """Insert or refresh a pending score processed notification.

        Returns:
            Number of queued pending notifications for the user.
        """
        key = f"{PREFIX_PENDING_SCORE_PROCESSED_USER}{user_id}"
        field = str(score_id)

        attempts = 0
        existing_payload = await self.redis.hget(key, field)
        if existing_payload:
            try:
                existing_data = json.loads(existing_payload)
                attempts = int(existing_data.get("attempts", 0))
                next_attempt_at = min(float(existing_data.get("next_attempt_at", next_attempt_at)), next_attempt_at)
                expires_at = max(float(existing_data.get("expires_at", expires_at)), expires_at)
            except (TypeError, ValueError, json.JSONDecodeError):
                attempts = 0

        payload = json.dumps({
            "score_id": score_id,
            "next_attempt_at": next_attempt_at,
            "expires_at": expires_at,
            "attempts": attempts,
        })

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, field, payload)
            pipe.expire(key, TTL_PENDING_SCORE_PROCESSED)
            pipe.sadd(PREFIX_PENDING_SCORE_PROCESSED_USERS, user_id)
            pipe.expire(PREFIX_PENDING_SCORE_PROCESSED_USERS, TTL_PENDING_SCORE_PROCESSED)
            pipe.hlen(key)
            _, _, _, _, event_count = await pipe.execute()

        return int(event_count)

    async def list_pending_score_processed_users(self) -> set[int]:
        """Return user ids that currently have pending score notifications."""
        members = await self.redis.smembers(PREFIX_PENDING_SCORE_PROCESSED_USERS)
        user_ids: set[int] = set()

        for member in members:
            try:
                user_ids.add(int(member))
            except (TypeError, ValueError):
                continue

        return user_ids

    async def get_pending_score_processed_events(self, user_id: int) -> dict[int, dict[str, float | int]]:
        """Fetch queued score processed notifications for one user."""
        key = f"{PREFIX_PENDING_SCORE_PROCESSED_USER}{user_id}"
        rows = await self.redis.hgetall(key)

        events: dict[int, dict[str, float | int]] = {}
        for raw_score_id, raw_payload in rows.items():
            try:
                score_id = int(raw_score_id)
                payload = json.loads(raw_payload)
                next_attempt_at = float(payload["next_attempt_at"])
                expires_at = float(payload["expires_at"])
                attempts = int(payload.get("attempts", 0))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue

            events[score_id] = {
                "score_id": score_id,
                "next_attempt_at": next_attempt_at,
                "expires_at": expires_at,
                "attempts": attempts,
            }

        return events

    async def save_pending_score_processed_event(self, user_id: int, event: dict[str, float | int]) -> None:
        """Persist an updated queued score processed event for one user."""
        score_id = int(event["score_id"])
        payload = json.dumps({
            "score_id": score_id,
            "next_attempt_at": float(event["next_attempt_at"]),
            "expires_at": float(event["expires_at"]),
            "attempts": int(event.get("attempts", 0)),
        })

        key = f"{PREFIX_PENDING_SCORE_PROCESSED_USER}{user_id}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, str(score_id), payload)
            pipe.expire(key, TTL_PENDING_SCORE_PROCESSED)
            pipe.sadd(PREFIX_PENDING_SCORE_PROCESSED_USERS, user_id)
            pipe.expire(PREFIX_PENDING_SCORE_PROCESSED_USERS, TTL_PENDING_SCORE_PROCESSED)
            await pipe.execute()

    async def remove_pending_score_processed_event(self, user_id: int, score_id: int) -> None:
        """Remove one pending score processed event and cleanup empty user queues."""
        key = f"{PREFIX_PENDING_SCORE_PROCESSED_USER}{user_id}"

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hdel(key, str(score_id))
            pipe.hlen(key)
            _, remaining_count = await pipe.execute()

        if int(remaining_count) > 0:
            return

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.srem(PREFIX_PENDING_SCORE_PROCESSED_USERS, user_id)
            await pipe.execute()

    async def clear_pending_score_processed_user(self, user_id: int) -> None:
        """Remove all pending score processed notifications for one user."""
        key = f"{PREFIX_PENDING_SCORE_PROCESSED_USER}{user_id}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.srem(PREFIX_PENDING_SCORE_PROCESSED_USERS, user_id)
            await pipe.execute()

    # =========================================================================
    # Metadata Beatmap Update Queue
    # =========================================================================

    async def append_beatmap_updates(self, beatmap_set_ids: list[int]) -> int:
        """Append a beatmap update batch and return its queue id.

        Queue ids are monotonic and shared across all metadata clients.
        """
        normalized_ids: list[int] = []
        seen_ids: set[int] = set()
        for raw_id in beatmap_set_ids:
            try:
                beatmap_set_id = int(raw_id)
            except (TypeError, ValueError):
                continue

            if beatmap_set_id <= 0 or beatmap_set_id in seen_ids:
                continue

            seen_ids.add(beatmap_set_id)
            normalized_ids.append(beatmap_set_id)

        queue_id = int(await self.redis.incr(PREFIX_BEATMAP_UPDATE_COUNTER))
        payload_key = f"{PREFIX_BEATMAP_UPDATE_ENTRY}{queue_id}"

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.setex(payload_key, TTL_BEATMAP_UPDATES, json.dumps(normalized_ids))
            pipe.zadd(PREFIX_BEATMAP_UPDATE_IDS, {str(queue_id): float(queue_id)})
            pipe.expire(PREFIX_BEATMAP_UPDATE_IDS, TTL_BEATMAP_UPDATES)
            pipe.zcard(PREFIX_BEATMAP_UPDATE_IDS)
            _, _, _, total_entries = await pipe.execute()

        if int(total_entries) > MAX_BEATMAP_UPDATE_ENTRIES:
            trim_count = int(total_entries) - MAX_BEATMAP_UPDATE_ENTRIES
            old_queue_ids = await self.redis.zrange(PREFIX_BEATMAP_UPDATE_IDS, 0, trim_count - 1)

            if old_queue_ids:
                async with self.redis.pipeline(transaction=True) as pipe:
                    pipe.zremrangebyrank(PREFIX_BEATMAP_UPDATE_IDS, 0, trim_count - 1)
                    for old_queue_id in old_queue_ids:
                        pipe.delete(f"{PREFIX_BEATMAP_UPDATE_ENTRY}{old_queue_id}")
                    await pipe.execute()

        return queue_id

    async def get_beatmap_updates_since(self, queue_id: int, limit: int = 500) -> tuple[list[int], int]:
        """Return beatmapset updates after a queue id and the latest delivered queue id."""
        try:
            last_seen_queue_id = max(0, int(queue_id))
        except (TypeError, ValueError):
            last_seen_queue_id = 0

        queue_id_rows = await self.redis.zrangebyscore(
            PREFIX_BEATMAP_UPDATE_IDS,
            min=f"({last_seen_queue_id}",
            max="+inf",
            start=0,
            num=max(1, limit),
        )

        if not queue_id_rows:
            return [], last_seen_queue_id

        payload_keys = [f"{PREFIX_BEATMAP_UPDATE_ENTRY}{row}" for row in queue_id_rows]
        payload_rows = await self.redis.mget(payload_keys)

        beatmap_set_ids: list[int] = []
        seen_ids: set[int] = set()
        latest_queue_id = last_seen_queue_id

        for raw_queue_id, payload in zip(queue_id_rows, payload_rows, strict=False):
            try:
                parsed_queue_id = int(raw_queue_id)
            except (TypeError, ValueError):
                continue

            latest_queue_id = max(latest_queue_id, parsed_queue_id)

            if not payload:
                continue

            try:
                update_ids = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                continue

            if not isinstance(update_ids, list):
                continue

            for update_id in update_ids:
                try:
                    beatmap_set_id = int(update_id)
                except (TypeError, ValueError):
                    continue

                if beatmap_set_id <= 0 or beatmap_set_id in seen_ids:
                    continue

                seen_ids.add(beatmap_set_id)
                beatmap_set_ids.append(beatmap_set_id)

        return beatmap_set_ids, latest_queue_id

    # =========================================================================
    # Watch Relationships (who is watching whom)
    # =========================================================================

    async def add_watcher(self, watcher_user_id: int, target_user_id: int) -> None:
        """Record that watcher is watching target.

        Maintains bidirectional index:
        - watching:{watcher} -> set of targets
        - watchers:{target} -> set of watchers
        """
        # What the watcher is watching
        watching_key = f"{PREFIX_WATCHING}{watcher_user_id}"
        await self.redis.sadd(watching_key, target_user_id)
        await self.redis.expire(watching_key, TTL_WATCHING)

        # Who is watching the target
        watchers_key = f"{PREFIX_WATCHERS}{target_user_id}"
        await self.redis.sadd(watchers_key, watcher_user_id)
        await self.redis.expire(watchers_key, TTL_WATCHING)

        logger.debug(f"User {watcher_user_id} started watching user {target_user_id}")

    async def remove_watcher(self, watcher_user_id: int, target_user_id: int) -> None:
        """Remove a watch relationship."""
        watching_key = f"{PREFIX_WATCHING}{watcher_user_id}"
        await self.redis.srem(watching_key, target_user_id)

        watchers_key = f"{PREFIX_WATCHERS}{target_user_id}"
        await self.redis.srem(watchers_key, watcher_user_id)

        logger.debug(f"User {watcher_user_id} stopped watching user {target_user_id}")

    async def get_watching(self, user_id: int) -> set[int]:
        """Get all users that a user is watching."""
        key = f"{PREFIX_WATCHING}{user_id}"
        members = await self.redis.smembers(key)
        return {int(m) for m in members}

    async def get_watchers(self, target_user_id: int) -> set[int]:
        """Get all users watching a specific user."""
        key = f"{PREFIX_WATCHERS}{target_user_id}"
        members = await self.redis.smembers(key)
        return {int(m) for m in members}

    async def clear_user_watches(self, user_id: int) -> None:
        """Clear all watch relationships for a user (on disconnect)."""
        # Get what they were watching
        watching = await self.get_watching(user_id)

        # Remove from each target's watchers set
        for target_id in watching:
            watchers_key = f"{PREFIX_WATCHERS}{target_id}"
            await self.redis.srem(watchers_key, user_id)

        # Clear their watching set
        watching_key = f"{PREFIX_WATCHING}{user_id}"
        await self.redis.delete(watching_key)

        logger.debug(f"Cleared all watches for user {user_id}")

    async def refresh_user_watch_ttl(self, watcher_user_id: int, target_user_ids: set[int]) -> None:
        """Refresh TTL for a watcher's watch relationships (keep-alive)."""
        watching_key = f"{PREFIX_WATCHING}{watcher_user_id}"
        if target_user_ids:
            await self.redis.expire(watching_key, TTL_WATCHING)

        for target_user_id in target_user_ids:
            watchers_key = f"{PREFIX_WATCHERS}{target_user_id}"
            await self.redis.expire(watchers_key, TTL_WATCHING)

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def clear_all_hub_state(self) -> int:
        """Clear all hub state from Redis. Returns number of keys deleted.

        Use with caution - intended for testing or server maintenance.
        """
        patterns = [
            f"{PREFIX_PRESENCE}*",
            f"{PREFIX_PLAYING}*",
            f"{PREFIX_WATCHING}*",
            f"{PREFIX_WATCHERS}*",
            f"{PREFIX_REPLAY_FRAMES}*",
            f"{PREFIX_PENDING_SCORE_PROCESSED_USER}*",
            f"{PREFIX_BEATMAP_UPDATE_ENTRY}*",
            PREFIX_PRESENCE_WATCHERS,
            PREFIX_PENDING_SCORE_PROCESSED_USERS,
            PREFIX_BEATMAP_UPDATE_COUNTER,
            PREFIX_BEATMAP_UPDATE_IDS,
        ]

        total_deleted = 0
        for pattern in patterns:
            if pattern.endswith("*"):
                async for key in self.redis.scan_iter(pattern):
                    await self.redis.delete(key)
                    total_deleted += 1
            else:
                if await self.redis.delete(pattern):
                    total_deleted += 1

        logger.info(f"Cleared {total_deleted} hub state keys from Redis")
        return total_deleted


# Global service instance (initialized on app startup)
_hub_state_service: HubStateService | None = None


async def get_hub_state_service() -> HubStateService:
    """Get the global hub state service, connecting if needed."""
    global _hub_state_service
    if _hub_state_service is None:
        _hub_state_service = HubStateService()
        await _hub_state_service.connect()
    return _hub_state_service


async def close_hub_state_service() -> None:
    """Close the global hub state service."""
    global _hub_state_service
    if _hub_state_service:
        await _hub_state_service.close()
        _hub_state_service = None
