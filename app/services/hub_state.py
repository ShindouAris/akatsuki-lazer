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

# TTLs for automatic expiration
TTL_PRESENCE = timedelta(minutes=5)  # Presence expires after 5 min of no updates
TTL_PLAYING = timedelta(hours=2)  # Playing state expires after 2 hours max
TTL_WATCHING = timedelta(hours=1)  # Watch relationships expire after 1 hour


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
        return await self.redis.sismember(PREFIX_PRESENCE_WATCHERS, user_id)

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
            PREFIX_PRESENCE_WATCHERS,
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
