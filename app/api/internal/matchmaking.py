"""Internal API of the matchmaking queue and ranked play matches, used by the Go multiplayer service.

Same rules as ``multiplayer.py``: the caller authenticates with the shared ``INTERNAL_API_KEY``, and this
process owns the database. Everything here is a plain read, except recording the result of a match, which
is idempotent.
"""

from __future__ import annotations

import logging
from typing import Annotated
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession
from app.api.internal.multiplayer import require_internal_key
from app.core.error import OsuError
from app.models.beatmap import RANKED_STATUSES
from app.models.beatmap import Beatmap
from app.models.legacy import MatchmakingPool
from app.models.legacy import MatchmakingPoolBeatmap
from app.models.legacy import MatchmakingUserEloHistory
from app.models.legacy import MatchmakingUserStats
from app.models.multiplayer import MultiplayerScore
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics
from app.services.variant import Variant
from app.services.rating import Elo
from app.services.rating import card_rating
from app.services.rating import contests_in
from app.services.rating import initial_in
from app.services.rating import initial_rating_from_pp
from app.services.rating import parse_elo_data
from app.services.rating import rate_match
from app.services.rating import serialize_elo_data

logger = logging.getLogger(__name__)

TOP_RATINGS = 100

router = APIRouter(prefix="/internal/matchmaking", dependencies=[Depends(require_internal_key)])


def _not_found(what: str) -> OsuError:
    return OsuError(code=status.HTTP_404_NOT_FOUND, error="not_found", message=f"{what} not found")


class PoolOut(BaseModel):
    id: int
    ruleset_id: int
    variant_id: int
    name: str
    type: str
    active: bool
    lobby_size: int
    rating_search_radius: int
    rating_search_radius_max: int
    rating_search_radius_exp: int


class PoolBeatmapOut(BaseModel):
    beatmap_id: int
    checksum: str
    mode: int
    star_rating: float
    # Required mods of the card as the JSON the realtime service reserialises them to; empty for no mods.
    mods: str
    rating: int
    selection_count: int


class UserRatingOut(BaseModel):
    user_id: int
    mu: float
    sigma: float
    # 1 + the number of players rated higher; null for somebody with no finished ranked match in the pool
    rank: int | None = None


class UserOut(BaseModel):
    id: int
    username: str
    is_restricted: bool


class ScoresOut(BaseModel):
    # user id -> total score of their play of the playlist item
    scores: dict[int, int]


class RatingsOut(BaseModel):
    ratings: list[int]


class TopRatingOut(BaseModel):
    rating: int


class PlayerResultIn(BaseModel):
    user_id: int
    life: int = Field(ge=0)


class MatchResultIn(BaseModel):
    room_id: int
    ranked: bool = False
    # False when the match ended before its first round: nobody won or lost anything.
    played: bool = True
    winner: int | None = None
    players: list[PlayerResultIn]

    @model_validator(mode="after")
    def _winner_took_part(self) -> MatchResultIn:
        if self.winner is not None and self.winner not in {p.user_id for p in self.players}:
            raise ValueError("the winner must be one of the players")
        return self


class MatchResultOut(BaseModel):
    # user id -> rating after the match (only for ranked matches that were played)
    ratings: dict[int, int]
    result: Literal["rated", "unranked", "not_played", "already_recorded"]


def _pool_out(pool: MatchmakingPool) -> PoolOut:
    return PoolOut(
        id=pool.id,
        ruleset_id=pool.ruleset_id,
        variant_id=pool.variant_id,
        name=pool.name,
        type=pool.type,
        active=pool.active,
        lobby_size=pool.lobby_size,
        rating_search_radius=pool.rating_search_radius,
        rating_search_radius_max=pool.rating_search_radius_max,
        rating_search_radius_exp=pool.rating_search_radius_exp,
    )


async def _pool(db: AsyncSession, pool_id: int) -> MatchmakingPool:
    pool = await db.get(MatchmakingPool, pool_id)
    if pool is None:
        raise _not_found("Pool")
    return pool


async def _stats(db: AsyncSession, pool: MatchmakingPool, user_id: int) -> MatchmakingUserStats:
    """The player's stats in the pool.

    Somebody who never played it starts at a rating estimated from their pp.
    """
    stats = await db.get(MatchmakingUserStats, (user_id, pool.id))
    if stats is not None:
        return stats

    if await db.get(User, user_id) is None:
        raise _not_found("User")

    # "1v1 RX" and "1v1 AP" pools start from the player's Relax / Autopilot pp, the others from their vanilla pp.
    pool_words = set(pool.name.upper().split())
    board = Variant.RELAX if "RX" in pool_words else Variant.AUTOPILOT if "AP" in pool_words else Variant.VANILLA
    pp = await db.scalar(
        select(UserStatistics.pp).where(
            UserStatistics.user_id == user_id,
            UserStatistics.mode == GameMode(pool.ruleset_id),
            UserStatistics.variant == int(board),
        ),
    )
    start = initial_rating_from_pp(pp or 0.0)
    stats = MatchmakingUserStats(
        user_id=user_id, pool_id=pool.id, elo_data=serialize_elo_data(start, initial=start)
    )
    try:
        # A savepoint, so that losing a race with another request for the same player (the client asks for
        # several pools at once) only undoes this insert.
        async with db.begin_nested():
            db.add(stats)
    except IntegrityError:
        existing = await db.get(MatchmakingUserStats, (user_id, pool.id), populate_existing=True)
        if existing is None:
            raise
        return existing
    return stats


@router.get("/pools", response_model=list[PoolOut])
async def list_pools(
    db: DbSession,
    pool_type: Annotated[str | None, Query(alias="type")] = None,
    active_only: bool = True,
) -> list[PoolOut]:
    """Pools, by default only those that can be queued for."""
    query = select(MatchmakingPool).order_by(MatchmakingPool.id)
    if pool_type is not None:
        query = query.where(MatchmakingPool.type == pool_type)
    if active_only:
        query = query.where(MatchmakingPool.active.is_(True))

    return [_pool_out(pool) for pool in (await db.execute(query)).scalars().all()]


@router.get("/pools/{pool_id}", response_model=PoolOut)
async def get_pool(db: DbSession, pool_id: int) -> PoolOut:
    return _pool_out(await _pool(db, pool_id))


@router.get("/pools/{pool_id}/beatmaps", response_model=list[PoolBeatmapOut])
async def pool_beatmaps(db: DbSession, pool_id: int) -> list[PoolBeatmapOut]:
    """The cards of a pool: its beatmaps with the mods they are played with and how hard they are rated."""
    await _pool(db, pool_id)

    rows = await db.execute(
        select(MatchmakingPoolBeatmap, Beatmap)
        .join(Beatmap, Beatmap.id == MatchmakingPoolBeatmap.beatmap_id)
        .where(
            MatchmakingPoolBeatmap.pool_id == pool_id,
            # Only ranked maps are ever dealt, whatever else a pool happens to hold.
            Beatmap.status.in_(RANKED_STATUSES),
            Beatmap.difficulty_rating > 0,
        )
        .order_by(MatchmakingPoolBeatmap.id),
    )
    return [
        PoolBeatmapOut(
            beatmap_id=beatmap.id,
            checksum=beatmap.checksum or "",
            mode=int(beatmap.mode),
            star_rating=float(beatmap.difficulty_rating),
            mods=card.mods or "",
            rating=card.rating
            if card.rating is not None
            else card_rating(float(beatmap.difficulty_rating)),
            selection_count=card.selection_count,
        )
        for card, beatmap in rows.all()
    ]


@router.get("/pools/{pool_id}/users/{user_id}", response_model=UserRatingOut)
async def user_rating(db: DbSession, pool_id: int, user_id: int) -> UserRatingOut:
    """A player's rating in a pool, creating their stats on first use."""
    pool = await _pool(db, pool_id)
    stats = await _stats(db, pool, user_id)
    elo = parse_elo_data(stats.elo_data)
    rank = None
    if contests_in(stats.elo_data) > 0:
        rows = await db.execute(
            select(MatchmakingUserStats.elo_data).where(MatchmakingUserStats.pool_id == pool.id),
        )
        mine = round(elo.mu)
        higher = sum(
            1
            for data in rows.scalars().all()
            if contests_in(data) > 0 and round(parse_elo_data(data).mu) > mine
        )
        rank = higher + 1
    return UserRatingOut(user_id=user_id, mu=elo.mu, sigma=elo.sigma, rank=rank)


@router.get("/pools/{pool_id}/ratings", response_model=RatingsOut)
async def pool_ratings(db: DbSession, pool_id: int) -> RatingsOut:
    """Every rated player's rating, for the rating distribution shown in the lobby."""
    await _pool(db, pool_id)
    rows = await db.execute(
        select(MatchmakingUserStats.elo_data).where(MatchmakingUserStats.pool_id == pool_id)
    )
    return RatingsOut(ratings=[round(parse_elo_data(data).mu) for data in rows.scalars().all()])


@router.get("/pools/{pool_id}/top-rating", response_model=TopRatingOut)
async def top_rating(db: DbSession, pool_id: int) -> TopRatingOut:
    """The rating of the 100th best player (the last of fewer than 100), or 0 for an empty pool."""
    await _pool(db, pool_id)
    rows = await db.execute(
        select(MatchmakingUserStats.elo_data).where(MatchmakingUserStats.pool_id == pool_id)
    )
    ratings = sorted(
        (round(parse_elo_data(data).mu) for data in rows.scalars().all()), reverse=True
    )
    return TopRatingOut(rating=ratings[:TOP_RATINGS][-1] if ratings else 0)


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(db: DbSession, user_id: int) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise _not_found("User")
    return UserOut(id=user.id, username=user.username, is_restricted=user.is_restricted)


@router.get("/items/{item_id}/scores", response_model=ScoresOut)
async def item_scores(db: DbSession, item_id: int) -> ScoresOut:
    """The total score of everybody who played a playlist item (their best, if they somehow have several)."""
    rows = await db.execute(
        select(MultiplayerScore.user_id, func.max(MultiplayerScore.total_score))
        .where(MultiplayerScore.playlist_item_id == item_id)
        .group_by(MultiplayerScore.user_id),
    )
    return ScoresOut(scores={user_id: int(total) for user_id, total in rows.all()})


@router.post("/pools/{pool_id}/results", response_model=MatchResultOut)
async def record_result(db: DbSession, pool_id: int, body: MatchResultIn) -> MatchResultOut:
    """Record a finished match: rate the players and keep a history entry for each.

    Only ranked matches that were played change ratings. Calling this again for the same room does nothing, so
    the caller may retry freely.
    """
    pool = await _pool(db, pool_id)

    if not body.played or len(body.players) < 2:
        return MatchResultOut(ratings={}, result="not_played")
    if not body.ranked:
        return MatchResultOut(ratings={}, result="unranked")

    recorded = await db.execute(
        select(MatchmakingUserEloHistory).where(
            MatchmakingUserEloHistory.room_id == body.room_id,
            MatchmakingUserEloHistory.pool_id == pool_id,
        ),
    )
    previous = recorded.scalars().all()
    if previous:
        return MatchResultOut(
            ratings={row.user_id: row.elo_after for row in previous}, result="already_recorded"
        )

    stats = {player.user_id: await _stats(db, pool, player.user_id) for player in body.players}
    before = {user_id: parse_elo_data(s.elo_data) for user_id, s in stats.items()}
    after = rate_match(before, {player.user_id: player.life for player in body.players})

    ratings: dict[int, int] = {}
    for user_id, s in stats.items():
        opponent = next(other for other in stats if other != user_id)

        if body.winner is None:
            outcome = "draw"
        elif body.winner == user_id:
            outcome = "win"
            s.first_placements += 1
        else:
            outcome = "loss"

        ratings[user_id] = round(after[user_id].mu)
        db.add(
            MatchmakingUserEloHistory(
                room_id=body.room_id,
                pool_id=pool_id,
                user_id=user_id,
                opponent_id=opponent,
                result=outcome,
                elo_before=round(before[user_id].mu),
                elo_after=ratings[user_id],
            ),
        )
        s.elo_data = serialize_elo_data(
            after[user_id],
            initial=initial_in(s.elo_data) or Elo(mu=before[user_id].mu),
            contests=contests_in(s.elo_data) + 1,
        )

    await db.flush()
    return MatchResultOut(ratings=ratings, result="rated")
