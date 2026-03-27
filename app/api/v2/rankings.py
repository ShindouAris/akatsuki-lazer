"""Ranking endpoints."""

import json

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser
from app.api.deps import DbSession
from app.api.v2.schemas import BeatmapCompact, Statistic
from app.api.v2.schemas import ModResponse
from app.api.v2.schemas import RankingUserEntryResponse
from app.api.v2.schemas import RankingsResponse
from app.api.v2.schemas import ScoreResponse
from app.api.v2.schemas import UserCompact
from app.api.v2.schemas import UserScoreAggregateResponse
from app.models.score import Score
from app.models.user import GameMode
from app.models.user import User
from app.models.user import UserStatistics

router = APIRouter()


def _string_to_mode(mode: str) -> GameMode | None:
    """Convert ruleset string to GameMode enum."""
    return {
        "osu": GameMode.OSU,
        "taiko": GameMode.TAIKO,
        "fruits": GameMode.CATCH,
        "catch": GameMode.CATCH,
        "mania": GameMode.MANIA,
    }.get(mode.lower())


def _mode_to_string(mode: GameMode) -> str:
    """Convert GameMode enum to string."""
    return {
        GameMode.OSU: "osu",
        GameMode.TAIKO: "taiko",
        GameMode.CATCH: "fruits",
        GameMode.MANIA: "mania",
    }.get(mode, "osu")


def _score_to_response(score: Score, rank_global: int | None = None) -> ScoreResponse:
    """Convert Score model to ScoreResponse."""
    data = json.loads(score.data) if score.data else {}
    mods = data.get("mods", [])
    stats = data.get("statistics", {})
    max_stats = data.get("maximum_statistics", {})

    user_compact = None
    if score.user:
        user_compact = UserCompact(
            id=score.user.id,
            username=score.user.username,
            avatar_url=score.user.avatar_url,
            country_code=score.user.country_acronym,
            is_active=score.user.is_active,
            is_bot=score.user.is_bot,
            is_supporter=score.user.is_supporter,
        )

    beatmap_compact = None
    if score.beatmap:
        beatmap_compact = BeatmapCompact(
            id=score.beatmap.id,
            beatmapset_id=score.beatmap.beatmapset_id,
            version=score.beatmap.version,
            mode=_mode_to_string(score.beatmap.mode),
            status=score.beatmap.status.name.lower(),
            difficulty_rating=score.beatmap.difficulty_rating,
            total_length=score.beatmap.total_length,
            cs=score.beatmap.cs,
            ar=score.beatmap.ar,
            od=score.beatmap.od,
            hp=score.beatmap.hp,
            bpm=score.beatmap.bpm,
            max_combo=score.beatmap.max_combo,
            checksum=score.beatmap.checksum,
        )

    return ScoreResponse(
        id=score.id,
        user_id=score.user_id,
        beatmap_id=score.beatmap_id,
        ruleset_id=score.ruleset_id,
        total_score=score.total_score,
        accuracy=score.accuracy,
        pp=score.pp,
        max_combo=score.max_combo,
        rank=score.rank,
        passed=score.passed,
        ranked=score.ranked,
        mods=[ModResponse(acronym=m.get("acronym", ""), settings=m.get("settings", {})) for m in mods],
        statistics=stats,
        maximum_statistics=max_stats,
        ended_at=score.ended_at,
        has_replay=score.has_replay,
        rank_global=rank_global,
        user=user_compact,
        beatmap=beatmap_compact,
    )


@router.get("/rankings/{ruleset}/{type}", response_model=RankingsResponse)
async def get_rankings(
    db: DbSession,
    _: CurrentUser,
    ruleset: str,
    type: str,
    page: int = Query(1, ge=1),
    country: str | None = Query(None, min_length=2, max_length=2),
    filter: str = Query("all"),
) -> RankingsResponse:
    """Get rankings for a ruleset and ranking type."""
    mode = _string_to_mode(ruleset)
    if mode is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid ruleset",
        )

    ranking_type = type.lower()
    if ranking_type not in {"performance", "score"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid ranking type",
        )

    if filter != "all":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only filter=all is supported",
        )

    per_page = 50
    offset = (page - 1) * per_page

    conditions = [
        UserStatistics.mode == mode,
        User.is_restricted.is_(False),
        User.is_bot.is_(False),
    ]
    if country:
        conditions.append(User.country_acronym == country.upper())

    sort_column = UserStatistics.pp if ranking_type == "performance" else UserStatistics.ranked_score

    total_result = await db.execute(
        select(func.count())
        .select_from(UserStatistics)
        .join(User, User.id == UserStatistics.user_id)
        .where(and_(*conditions)),
    )
    total = total_result.scalar_one()

    result = await db.execute(
        select(UserStatistics, User)
        .join(User, User.id == UserStatistics.user_id)
        .where(and_(*conditions))
        .order_by(sort_column.desc(), User.id.asc())
        .limit(per_page)
        .offset(offset),
    )

    ranking: list[RankingUserEntryResponse] = []
    for idx, (stats, user) in enumerate(result.fetchall(), start=1):
        ranking.append(
            RankingUserEntryResponse(
                rank=offset + idx,
                user=UserCompact(
                    id=user.id,
                    username=user.username,
                    avatar_url=user.avatar_url,
                    country_code=user.country_acronym,
                    is_active=user.is_active,
                    is_bot=user.is_bot,
                    is_supporter=user.is_supporter,
                ),
                pp=stats.pp,
                ranked_score=stats.ranked_score,
                country_code=user.country_acronym,
                grade_counts=Statistic(
                    ssh=stats.grade_ssh,
                    ss=stats.grade_ss,
                    sh=stats.grade_sh,
                    s=stats.grade_s,
                    a=stats.grade_a
                ),
                play_count=stats.play_count,
                hit_accuracy=stats.hit_accuracy,
            ),
        )

    return RankingsResponse(
        ranking=ranking,
        total=total,
        page=page,
        per_page=per_page,
        kind=ranking_type,
    )


@router.get("/users/{user_id}/scores/rank", response_model=UserScoreAggregateResponse)
async def get_user_score_rank(
    db: DbSession,
    _: CurrentUser,
    user_id: int,
    mode: str | None = Query(None),
    scoreType: str | None = Query("best"),
) -> UserScoreAggregateResponse:
    """Get aggregated ranking info for a user's score."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    ruleset_id: int | None = None
    if mode:
        mode_enum = _string_to_mode(mode)
        if mode_enum is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid mode",
            )
        ruleset_id = int(mode_enum)

    score_type = (scoreType or "best").lower()
    if score_type not in {"best", "firsts", "recent"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid scoreType",
        )

    base_query = (
        select(Score)
        .options(selectinload(Score.user), selectinload(Score.beatmap))
        .where(
            and_(
                Score.user_id == user_id,
                Score.passed.is_(True),
                Score.ranked.is_(True),
            ),
        )
    )

    if ruleset_id is not None:
        base_query = base_query.where(Score.ruleset_id == ruleset_id)

    if score_type == "best":
        query = base_query.order_by(Score.pp.desc(), Score.ended_at.desc()).limit(1)
    elif score_type == "recent":
        query = base_query.order_by(Score.ended_at.desc()).limit(1)
    else:
        max_total_subquery = (
            select(Score.beatmap_id, func.max(Score.total_score).label("max_total"))
            .where(
                and_(
                    Score.passed.is_(True),
                    Score.ranked.is_(True),
                ),
            )
            .group_by(Score.beatmap_id)
            .subquery()
        )
        query = (
            base_query.join(
                max_total_subquery,
                and_(
                    Score.beatmap_id == max_total_subquery.c.beatmap_id,
                    Score.total_score == max_total_subquery.c.max_total,
                ),
            )
            .order_by(Score.ended_at.desc())
            .limit(1)
        )

    result = await db.execute(query)
    score = result.scalar_one_or_none()

    if score is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No ranked score found for this user",
        )

    position_result = await db.execute(
        select(func.count(Score.id) + 1).where(
            and_(
                Score.beatmap_id == score.beatmap_id,
                Score.passed.is_(True),
                Score.ranked.is_(True),
                Score.total_score > score.total_score,
            ),
        ),
    )
    position = position_result.scalar_one()

    return UserScoreAggregateResponse(
        score=_score_to_response(score, rank_global=position),
        position=position,
        user=UserCompact(
            id=user.id,
            username=user.username,
            avatar_url=user.avatar_url,
            country_code=user.country_acronym,
            is_active=user.is_active,
            is_bot=user.is_bot,
            is_supporter=user.is_supporter,
        ),
    )
