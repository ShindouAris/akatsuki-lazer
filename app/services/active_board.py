"""The board a player is currently "on".

Submitting a Relax or Autopilot score puts the player on that board: for the next 30 minutes the game's
leaderboards and the player's profile show it, unless the request names a board itself. Every submit renews
the timer and sets the board to the one of the submitted play, so a vanilla play (or 30 quiet minutes) takes
the player back to the vanilla board until they submit another RX / AP score.

The state lives in Redis (shared by every worker process, expires by itself). Without Redis, or when it is
down, a per-process dictionary is used so the feature degrades instead of failing.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from app.services.variant import Variant

logger = logging.getLogger(__name__)

ACTIVE_BOARD_SECONDS = 30 * 60


def _key(user_id: int) -> str:
    return f"active_board:{user_id}"


class ActiveBoard:
    def __init__(
        self,
        client: Any | None = None,
        ttl_seconds: int = ACTIVE_BOARD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._clock = clock
        self._local: dict[int, tuple[Variant, float]] = {}

    async def set(self, user_id: int, variant: Variant) -> None:
        """Put the player on ``variant`` for the next 30 minutes (vanilla simply clears it)."""
        self._local.pop(user_id, None)
        if variant == Variant.VANILLA:
            await self._redis_call("delete", _key(user_id))
            return
        self._local[user_id] = (variant, self._clock() + self._ttl)
        await self._redis_call("set", _key(user_id), str(int(variant)), ex=self._ttl)

    async def get(self, user_id: int) -> Variant:
        """The board the player is on now."""
        if self._client is not None:
            try:
                raw = await self._client.get(_key(user_id))
                if isinstance(raw, bytes):
                    raw = raw.decode()
                return Variant(int(raw)) if raw is not None else Variant.VANILLA
            except Exception as exc:
                logger.warning("Active board unavailable in Redis (%s); using the local copy", exc)

        entry = self._local.get(user_id)
        if entry is None:
            return Variant.VANILLA
        variant, expires = entry
        if self._clock() >= expires:
            self._local.pop(user_id, None)
            return Variant.VANILLA
        return variant

    async def _redis_call(self, method: str, *args: Any, **kwargs: Any) -> None:
        if self._client is None:
            return
        try:
            await getattr(self._client, method)(*args, **kwargs)
        except Exception as exc:
            logger.warning("Could not store the active board in Redis: %s", exc)


_instance = ActiveBoard()


def set_active_board(board: ActiveBoard) -> None:
    global _instance
    _instance = board


def get_active_board() -> ActiveBoard:
    return _instance
