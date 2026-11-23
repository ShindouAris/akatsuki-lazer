"""Live ranks and rank history for profile responses.

The ranks stored in ``user_statistics`` are only refreshed when their owner finishes a play, so they go
stale as other players overtake them. Profiles therefore ask the leaderboard for the current rank and fall
back to the stored one when the leaderboard cannot answer. The rank graph is read from ``user_rank_history``
with the current rank as its last point.

Both a single profile and a list of profiles go through ``with_live_ranks_many``: ranks are looked up
concurrently and the histories of all players are loaded with one query.
"""

import asyncio
import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas import RankHistoryResponse
from app.api.v2.schemas import UserResponse
from app.models.user import GameMode
from app.models.user import User
from app.services.leaderboard import get_leaderboard
from app.services.rank_history import get_rank_histories

logger = logging.getLogger(__name__)

_MODE_NAMES = {
    GameMode.OSU: "osu",
    GameMode.TAIKO: "taiko",
    GameMode.CATCH: "fruits",
    GameMode.MANIA: "mania",
}

ProfileItem = tuple[UserResponse, User, GameMode]


def _has_rank(response: UserResponse, user: User) -> bool:
    """Restricted players and bots are not ranked; unranked players have nothing to refresh."""
    return not (user.is_restricted or user.is_bot or not response.statistics.is_ranked)


async def _current_ranks(response: UserResponse, user: User, mode: GameMode) -> tuple[int | None, int | None]:
    stats = response.statistics
    leaderboard = get_leaderboard()
    if leaderboard is not None and stats.pp is not None:
        try:
            return await leaderboard.ranks(mode, stats.pp, user.country_acronym)
        except Exception as exc:
            logger.debug("Live ranks unavailable for user %s (%s); using stored ranks", user.id, exc)
    return stats.global_rank, stats.country_rank


async def with_live_ranks_many(items: Sequence[ProfileItem], db: AsyncSession) -> list[UserResponse]:
    """Return the responses (same order) with current ranks and rank graphs where they can be determined."""
    ranked = [(i, item) for i, item in enumerate(items) if _has_rank(item[0], item[1])]
    if not ranked:
        return [response for response, _, _ in items]

    ranks = await asyncio.gather(*(_current_ranks(*item) for _, item in ranked))

    histories = {}
    try:
        histories = await get_rank_histories(
            db,
            {(user.id, mode): global_rank for (_, (_, user, mode)), (global_rank, _) in zip(ranked, ranks)},
        )
    except Exception:
        logger.warning("Could not load rank histories", exc_info=True)

    results = [response for response, _, _ in items]
    for (index, (response, user, mode)), (global_rank, country_rank) in zip(ranked, ranks):
        update: dict[str, object] = {"global_rank": global_rank, "country_rank": country_rank}
        history = histories.get((user.id, mode))
        if history is not None:
            update["rank_history"] = RankHistoryResponse(mode=_MODE_NAMES.get(mode, "osu"), data=history)
        stats = response.statistics.model_copy(update=update)
        results[index] = response.model_copy(update={"statistics": stats})
    return results


async def with_live_ranks(
    response: UserResponse,
    user: User,
    mode: GameMode,
    db: AsyncSession,
) -> UserResponse:
    """Single profile version of ``with_live_ranks_many``."""
    return (await with_live_ranks_many([(response, user, mode)], db))[0]
