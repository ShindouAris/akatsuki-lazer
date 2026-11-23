"""Redis sorted-set leaderboards for performance rankings.

Redis is only an *index*; the database stays the source of truth (``user_statistics.pp``). A rank is
"how many ranked players have strictly more pp than you, plus one", which Redis answers in O(log N)
with ``ZCOUNT`` instead of scanning the whole statistics table. Players with equal pp share a rank,
exactly like the SQL ``COUNT(pp > x) + 1`` this replaces.

Every board (variant: vanilla, Relax, Autopilot) of every game mode ``m`` has its own index
(``v`` is the variant number, see ``app.services.variant``)::

    leaderboard:pp:v:m               ZSET  user_id -> pp                (every ranked player)
    leaderboard:pp:v:m:VN            ZSET  user_id -> pp                (per country)
    leaderboard:pp:v:m:country_of    HASH  user_id -> country           (to move players between countries)
    leaderboard:pp:v:m:ready         STRING marker, set once the index was fully built from the database

While the ``ready`` marker is missing the index must not be trusted, so ``ranks`` raises
``LeaderboardUnavailable`` and callers fall back to the database.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics
from app.services.variant import Variant
from app.services.variant import supports_variant

logger = logging.getLogger(__name__)

_REBUILD_LOCK_KEY = "leaderboard:pp:rebuild_lock"
_REBUILD_LOCK_SECONDS = 300
_REBUILD_CHUNK = 1000


class LeaderboardUnavailable(RuntimeError):
    """The index is not built (yet), so it cannot answer rank queries."""


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """A player's position data to push into the index after their statistics were committed."""

    mode: GameMode
    user_id: int
    country: str
    pp: float
    ranked: bool  # False for restricted players and bots; they are removed from the index
    variant: Variant = Variant.VANILLA


def _key(mode: GameMode, country: str | None = None, variant: Variant = Variant.VANILLA) -> str:
    base = f"leaderboard:pp:v{int(variant)}:{int(mode)}"
    return base if country is None else f"{base}:{country.upper()}"


def _owner_key(mode: GameMode, variant: Variant = Variant.VANILLA) -> str:
    return f"{_key(mode, None, variant)}:country_of"


def _ready_key(mode: GameMode, variant: Variant = Variant.VANILLA) -> str:
    return f"{_key(mode, None, variant)}:ready"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


class Leaderboard:
    """Performance leaderboard backed by Redis sorted sets."""

    def __init__(self, client: Any) -> None:
        # `client` is a redis.asyncio.Redis (decode_responses is optional).
        self._client = client

    async def is_ready(self, mode: GameMode, variant: Variant = Variant.VANILLA) -> bool:
        return bool(await self._client.exists(_ready_key(mode, variant)))

    async def ranks(
        self,
        mode: GameMode,
        pp: float,
        country: str,
        variant: Variant = Variant.VANILLA,
    ) -> tuple[int, int]:
        """Return (global rank, country rank) for a player with ``pp``; ties share a rank."""
        if not await self.is_ready(mode, variant):
            raise LeaderboardUnavailable(f"leaderboard for mode {int(mode)} variant {int(variant)} is not built")

        above = f"({float(pp)!r}"  # exclusive lower bound => strictly more pp
        global_ahead = await self._client.zcount(_key(mode, None, variant), above, "+inf")
        country_ahead = await self._client.zcount(_key(mode, country, variant), above, "+inf")
        return int(global_ahead) + 1, int(country_ahead) + 1

    async def apply(self, entry: LeaderboardEntry) -> None:
        """Insert, move or remove a player. Call this after the statistics transaction committed."""
        member = str(entry.user_id)
        country = entry.country.upper()
        mode, variant = entry.mode, entry.variant

        previous = _text(await self._client.hget(_owner_key(mode, variant), member))
        if previous and previous != country:
            await self._client.zrem(_key(mode, previous, variant), member)

        if entry.ranked and entry.pp > 0:
            await self._client.zadd(_key(mode, None, variant), {member: entry.pp})
            await self._client.zadd(_key(mode, country, variant), {member: entry.pp})
            await self._client.hset(_owner_key(mode, variant), member, country)
        else:
            await self._client.zrem(_key(mode, None, variant), member)
            await self._client.zrem(_key(mode, country, variant), member)
            await self._client.hdel(_owner_key(mode, variant), member)

    async def rebuild(self, db: AsyncSession, mode: GameMode, variant: Variant = Variant.VANILLA) -> int:
        """Rebuild one board's index from the database. Returns the number of ranked players."""
        # Without the marker readers fall back to the database, so a half-built index is never trusted.
        await self._client.delete(_ready_key(mode, variant))

        base = _key(mode, None, variant)
        stale = [base]
        stale += [_text(k) async for k in self._client.scan_iter(match=f"{base}:*")]
        await self._client.delete(*[k for k in stale if k])

        result = await db.execute(
            select(UserStatistics.user_id, UserStatistics.pp, User.country_acronym)
            .join(User, User.id == UserStatistics.user_id)
            .where(
                UserStatistics.mode == mode,
                UserStatistics.variant == int(variant),
                UserStatistics.pp > 0,
                User.is_restricted.is_(False),
                User.is_bot.is_(False),
            ),
        )
        rows = result.all()

        for start in range(0, len(rows), _REBUILD_CHUNK):
            chunk = rows[start : start + _REBUILD_CHUNK]
            await self._client.zadd(base, {str(uid): pp for uid, pp, _ in chunk})
            await self._client.hset(
                _owner_key(mode, variant),
                mapping={str(uid): cc.upper() for uid, _, cc in chunk},
            )

            by_country: dict[str, dict[str, float]] = {}
            for uid, pp, cc in chunk:
                by_country.setdefault(cc.upper(), {})[str(uid)] = pp
            for cc, members in by_country.items():
                await self._client.zadd(_key(mode, cc, variant), members)

        await self._client.set(_ready_key(mode, variant), "1")
        logger.info("Rebuilt leaderboard for mode %s variant %s with %d players", int(mode), int(variant), len(rows))
        return len(rows)

    async def ensure_built(self, db: AsyncSession) -> list[tuple[GameMode, Variant]]:
        """Build every board whose index is missing. Returns the (mode, variant) pairs that were built.

        A short-lived lock keeps several processes from rebuilding at the same time.
        """
        missing = [
            (mode, variant)
            for variant in Variant
            for mode in GameMode
            if supports_variant(mode, variant) and not await self.is_ready(mode, variant)
        ]
        if not missing:
            return []

        if not await self._client.set(_REBUILD_LOCK_KEY, "1", nx=True, ex=_REBUILD_LOCK_SECONDS):
            logger.info("Leaderboard rebuild already running elsewhere")
            return []

        try:
            for mode, variant in missing:
                await self.rebuild(db, mode, variant)
        finally:
            await self._client.delete(_REBUILD_LOCK_KEY)
        return missing


_instance: Leaderboard | None = None


def set_leaderboard(leaderboard: Leaderboard | None) -> None:
    """Register the process-wide leaderboard (done at startup; ``None`` at shutdown)."""
    global _instance
    _instance = leaderboard


def get_leaderboard() -> Leaderboard | None:
    """The process-wide leaderboard, or ``None`` when Redis is not configured (e.g. in tests)."""
    return _instance
