"""User activity and kudosu endpoints."""

from fastapi import APIRouter
from fastapi import Query
from fastapi import status
from datetime import datetime

from sqlalchemy import desc
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.api.deps import DbSession
from app.api.v2.schemas import KudosuHistoryResponse
from app.core.error import OsuError
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.medal import UserMedal
from app.models.score import Score
from app.models.user import KudosuHistory
from app.models.user import User
from app.services.medals import CATALOGUE

router = APIRouter()


_MODE_NAMES = ("osu", "taiko", "fruits", "mania")
_MEDAL_EVENT_ID_BASE = 500_000_000
_RANK_EVENT_MAX_POSITION = 50


@router.get("/users/{user_id}/recent_activity")
async def get_user_recent_activity(
    db: DbSession,
    user_id: int,
    limit: int = Query(51, ge=1, le=51),  # osu! standard is 51
) -> list[dict]:
    """Get the user's recent activity in the shape the client's profile reads (``APIRecentActivity``).

    Built from what the server knows: medals earned and good placements on a beatmap's leaderboard.
    """
    user = await db.get(User, user_id)
    if not user:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    user_json = {"username": user.username, "url": f"/users/{user.id}"}
    events: list[tuple[datetime, dict]] = []

    # Medals
    rules = {rule.id: rule for rule in CATALOGUE}
    medal_rows = await db.execute(
        select(UserMedal.medal_id, UserMedal.awarded_at).where(UserMedal.user_id == user_id),
    )
    for medal_id, awarded_at in medal_rows.all():
        rule = rules.get(medal_id)
        if rule is None:
            continue
        events.append((awarded_at, {
            "id": _MEDAL_EVENT_ID_BASE + medal_id,
            "createdAt": awarded_at.isoformat(),
            "type": "achievement",
            "mode": _MODE_NAMES[rule.mode] if rule.mode is not None else "osu",
            "achievement": {"slug": rule.icon_slug, "name": rule.name},
            "user": user_json,
        }))

    # Leaderboard placements: a passed, ranked play that sits in the top positions of its beatmap's board.
    other = aliased(Score)
    position = (
        select(func.count(other.id) + 1)
        .where(
            other.beatmap_id == Score.beatmap_id,
            other.variant == Score.variant,
            other.passed.is_(True),
            other.ranked.is_(True),
            other.total_score > Score.total_score,
        )
        .correlate(Score)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Score, position.label("position"), BeatmapSet.artist, BeatmapSet.title, Beatmap.version, Beatmap.id)
        .join(Beatmap, Beatmap.id == Score.beatmap_id)
        .join(BeatmapSet, BeatmapSet.id == Beatmap.beatmapset_id)
        .where(Score.user_id == user_id, Score.passed.is_(True), Score.ranked.is_(True), Score.ended_at.is_not(None))
        .order_by(desc(Score.ended_at))
        .limit(200),
    )
    for score, place, artist, title, version, beatmap_id in rows.all():
        if place > _RANK_EVENT_MAX_POSITION:
            continue
        events.append((score.ended_at, {
            "id": score.id,
            "createdAt": score.ended_at.isoformat(),
            "type": "rank",
            "scoreRank": (score.rank or "A").strip().upper(),
            "rank": int(place),
            "mode": _MODE_NAMES[score.ruleset_id] if 0 <= score.ruleset_id < 4 else "osu",
            "beatmap": {"title": f"{artist} - {title} [{version}]", "url": f"/b/{beatmap_id}"},
            "user": user_json,
        }))

    events.sort(key=lambda event: event[0], reverse=True)
    return [event for _, event in events[:limit]]


@router.get("/users/{user_id}/kudosu", response_model=list[KudosuHistoryResponse])
async def get_user_kudosu_history(
    db: DbSession,
    user_id: int,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[KudosuHistoryResponse]:
    """Get user's kudosu voting history.
    
    Args:
        user_id: ID of the user
        limit: Number of results (1-100, default 50)
        offset: Number of results to skip
        
    Returns:
        List of kudosu history
    """
    # Verify user exists
    user = await db.get(User, user_id)
    if not user:
        raise OsuError(
            code=status.HTTP_404_NOT_FOUND,
            error="User not found",
            message="User not found",
        )

    # Get kudosu history
    result = await db.execute(
        select(KudosuHistory)
        .where(KudosuHistory.user_id == user_id)
        .order_by(desc(KudosuHistory.created_at))
        .limit(limit)
        .offset(offset)
    )

    histories = result.scalars().all()

    # Populate beatmap for kudosu entries that have beatmap_id
    responses = []
    for history in histories:
        if history.beatmap_id:
            beatmap = await db.get(Beatmap, history.beatmap_id)
            history.beatmap = beatmap  # type: ignore
        responses.append(history)

    return responses
