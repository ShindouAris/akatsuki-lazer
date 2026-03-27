"""Score endpoints."""

import json
import logging
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from fastapi.responses import FileResponse
from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import ActiveUser
from app.api.deps import DbSession
from app.api.hubs.spectator import send_user_score_processed
from app.api.v2.schemas import BeatmapCompact
from app.api.v2.schemas import ModResponse
from app.api.v2.schemas import ScoreResponse
from app.api.v2.schemas import ScoreSubmissionRequest
from app.api.v2.schemas import UserCompact
from app.core.config import get_settings
from app.models.beatmap import BeatmapStatus
from app.models.score import Score
from app.models.score import ScoreToken
from app.models.user import GameMode
from app.models.user import User
from app.services.beatmaps import BeatmapService
from app.services.hub_state import get_hub_state_service
from app.services.pp import PPService
from app.services.pp import mods_to_bitwise
from app.services.replay import ReplayStorageService
from app.services.user_service import get_user_statistics
from app.services.user_service import refresh_user_pp_and_ranks
from app.services.user_service import update_user_statistics

router = APIRouter()
logger = logging.getLogger(__name__)


def _mode_to_string(mode: GameMode) -> str:
    """Convert GameMode enum to string."""
    return {
        GameMode.OSU: "osu",
        GameMode.TAIKO: "taiko",
        GameMode.CATCH: "fruits",
        GameMode.MANIA: "mania",
    }.get(mode, "osu")


def _normalize_to_utc(value: datetime | None) -> datetime | None:
    """Normalize incoming datetimes to UTC-aware values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _score_to_response(
    score: Score,
    include_user: bool = True,
    include_beatmap: bool = False,
    rank_global: int | None = None,
) -> ScoreResponse:
    """Convert Score model to ScoreResponse."""
    # Parse data JSON column (contains mods, statistics, maximum_statistics, total_score_without_mods)
    data = json.loads(score.data) if score.data else {}
    mods = data.get("mods", [])
    stats = data.get("statistics", {})
    max_stats = data.get("maximum_statistics", {})

    user_compact = None
    if include_user and score.user:
        user_compact = UserCompact(
            id=score.user.id,
            username=score.user.username,
            avatar_url=score.user.avatar_url,
            country_code=score.user.country_acronym,  # Map to API field name
            is_active=score.user.is_active,
            is_bot=score.user.is_bot,
            is_supporter=score.user.is_supporter,
        )

    beatmap_compact = None
    if include_beatmap and score.beatmap:
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


@router.get("/scores/{score_id}", response_model=ScoreResponse)
async def get_score(db: DbSession, score_id: int) -> ScoreResponse:
    """Get a score by ID."""
    result = await db.execute(
        select(Score)
        .options(selectinload(Score.user), selectinload(Score.beatmap))
        .where(Score.id == score_id),
    )
    score = result.scalar_one_or_none()

    if not score:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Score not found",
        )

    # Calculate rank on leaderboard
    rank_global = None
    if score.passed and score.ranked:
        rank_result = await db.execute(
            select(func.count(Score.id) + 1).where(
                and_(
                    Score.beatmap_id == score.beatmap_id,
                    Score.passed.is_(True),
                    Score.ranked.is_(True),
                    Score.total_score > score.total_score,
                ),
            ),
        )
        rank_global = rank_result.scalar()

    return _score_to_response(score, include_beatmap=True, rank_global=rank_global)


@router.get("/scores/{score_id}/download")
async def download_score_replay(
    db: DbSession,
    user: ActiveUser,
    score_id: int,
) -> FileResponse:
    """Download replay file for a submitted score."""
    result = await db.execute(select(Score).where(Score.id == score_id))
    score = result.scalar_one_or_none()

    if not score or not score.has_replay:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Replay not found",
        )

    replay_service = ReplayStorageService()
    replay_path = replay_service.get_score_replay_path(score.id)
    if not replay_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Replay file missing",
        )

    return FileResponse(
        path=str(replay_path),
        media_type="application/octet-stream",
        filename=f"{score.id}.osr",
    )

@router.get("/beatmaps/{beatmap_id}/scores")
async def get_beatmap_scores(
    db: DbSession,
    beatmap_id: int,
    mode: str | None = Query(None),
    mods: str | None = Query(None),
    type: str = Query("global"),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    """Get top scores on a beatmap."""
    # Fetch beatmap from mirror if not in local database
    service = BeatmapService(db)
    try:
        beatmap = await service.get_beatmap(beatmap_id)
    finally:
        await service.close()

    if not beatmap:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Beatmap not found",
        )

    # Build query
    query = (
        select(Score)
        .options(selectinload(Score.user))
        .where(
            and_(
                Score.beatmap_id == beatmap_id,
                Score.passed.is_(True),
                Score.ranked.is_(True),
            ),
        )
        .order_by(Score.total_score.desc())
        .limit(limit)
    )

    result = await db.execute(query)
    scores = result.scalars().all()

    return {
        "scores": [_score_to_response(s) for s in scores],
    }


@router.get("/beatmaps/{beatmap_id}/solo-scores")
async def get_beatmap_solo_scores(
    db: DbSession,
    beatmap_id: int,
    mode: str | None = Query(None),
    mods: str | None = Query(None),
    type: str = Query("global"),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    """Get top solo scores on a beatmap (new lazer format)."""
    return await get_beatmap_scores(db, beatmap_id, mode, mods, type, limit)


@router.put("/beatmaps/{beatmap_id}/solo/scores/{token_id}", response_model=ScoreResponse)
async def submit_score(
    db: DbSession,
    user: ActiveUser,
    beatmap_id: int,
    token_id: int,
    score_data: ScoreSubmissionRequest,
) -> ScoreResponse:
    """Submit a score using a score token."""
    # Verify token (check score_id is None to see if unused - official doesn't use is_used flag)
    result = await db.execute(
        select(ScoreToken).where(
            and_(
                ScoreToken.id == token_id,
                ScoreToken.user_id == user.id,
                ScoreToken.beatmap_id == beatmap_id,
                ScoreToken.score_id.is_(None),  # Token is unused if score_id is null
            ),
        ),
    )
    token = result.scalar_one_or_none()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Score token not found or already used",
        )

    # Note: Official implementation doesn't expire tokens, so we skip expiry check

    # Fetch beatmap (should already exist from token creation, but verify)
    service = BeatmapService(db)
    osu_file_path: str | None = None
    try:
        beatmap = await service.get_beatmap(beatmap_id)
        if beatmap is not None:
            osu_file_path = await service.ensure_osu_file(beatmap)
    finally:
        await service.close()

    if not beatmap:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Beatmap not found",
        )

    # Determine if score should be ranked (anything with leaderboard)
    ranked = score_data.passed and beatmap.status in (
        BeatmapStatus.RANKED,
        BeatmapStatus.APPROVED,
        BeatmapStatus.LOVED,
        BeatmapStatus.QUALIFIED,
    )

    # Build data JSON (matches official ScoreData format)
    data = {
        "mods": [m.model_dump() for m in score_data.mods],
        "statistics": score_data.statistics,
        "maximum_statistics": score_data.maximum_statistics,
    }
    if score_data.total_score_without_mods:
        data["total_score_without_mods"] = score_data.total_score_without_mods

    settings = get_settings()
    calculated_pp: float | None = None
    if score_data.passed and osu_file_path:
        try:
            pp_service = PPService()
            pp_result = pp_service.calculate_for_score(
                osu_file_path,
                {
                    "ruleset_id": token.ruleset_id,
                    "mods_bitwise": mods_to_bitwise(data["mods"]),
                    "mods": data["mods"],
                    "max_combo": score_data.max_combo,
                    "accuracy": score_data.accuracy,
                    "statistics": score_data.statistics,
                },
            )
            result_pp = pp_result.get("pp")
            if isinstance(result_pp, float):
                calculated_pp = result_pp
        except Exception as exc:
            logger.warning("Server-side PP calculation failed for score token %s: %s", token_id, exc)

    final_pp = score_data.pp
    if calculated_pp is not None:
        if final_pp is None:
            final_pp = calculated_pp
        elif settings.pp_validate_client_value:
            pp_diff = abs(final_pp - calculated_pp)
            if pp_diff > settings.pp_tolerance:
                logger.warning(
                    "PP mismatch for user %s on beatmap %s: client=%.5f server=%.5f diff=%.5f",
                    user.id,
                    beatmap_id,
                    final_pp,
                    calculated_pp,
                    pp_diff,
                )
                if settings.pp_use_server_value_on_mismatch:
                    final_pp = calculated_pp

    # Create score
    started_at = _normalize_to_utc(score_data.started_at) or _normalize_to_utc(token.created_at)
    if started_at is None:
        started_at = datetime.now(UTC)

    ended_at = _normalize_to_utc(score_data.ended_at) or datetime.now(UTC)
    if ended_at < started_at:
        ended_at = started_at

    score = Score(
        user_id=user.id,
        beatmap_id=beatmap_id,
        ruleset_id=token.ruleset_id,
        data=json.dumps(data),
        total_score=score_data.total_score,
        accuracy=score_data.accuracy,
        pp=final_pp,
        max_combo=score_data.max_combo,
        rank=score_data.rank,
        passed=score_data.passed,
        ranked=ranked,
        preserve=score_data.passed,  # Preserve passing scores like official
        started_at=started_at,
        ended_at=ended_at,
        build_id=token.build_id,
    )
    db.add(score)
    await db.flush()  # Get score ID

    # Mark token as used by setting score_id
    token.score_id = score.id

    # Update beatmap play count
    beatmap.play_count += 1
    if score_data.passed:
        beatmap.pass_count += 1

    await db.flush()

    # Persist replay buffered from spectator frame stream for successful plays only.
    try:
        hub_state = await get_hub_state_service()
        replay_bundles = await hub_state.get_replay_frame_bundles(token.id)
        is_failed_score = (not score_data.passed) or score_data.rank.strip().upper() == "F"

        if is_failed_score:
            await hub_state.clear_replay_frame_bundles(token.id)
            logger.debug(
                "Skipping replay persistence for failed score %s (token %s)",
                score.id,
                token.id,
            )
        elif replay_bundles:
            replay_service = ReplayStorageService()
            replay_path = await replay_service.persist_score_replay(
                score_id=score.id,
                username=user.username,
                beatmap_checksum=beatmap.checksum,
                ruleset_id=token.ruleset_id,
                ended_at=ended_at,
                build_id=token.build_id,
                total_score=score.total_score,
                max_combo=score.max_combo,
                beatmap_max_combo=beatmap.max_combo,
                statistics=score_data.statistics,
                mods=data["mods"],
                frame_bundles=replay_bundles,
            )
            if replay_path is not None:
                score.has_replay = True
                await hub_state.clear_replay_frame_bundles(token.id)
                logger.info("Stored replay for score %s at %s", score.id, replay_path)
        else:
            logger.debug("No replay frame bundles buffered for token %s", token.id)
    except Exception as exc:
        logger.warning("Replay persistence failed for score token %s: %s", token.id, exc)

    stats = await get_user_statistics(db, user.id, GameMode(token.ruleset_id))
    if stats is not None:
        await update_user_statistics(db, stats, score)

    if score_data.passed and ranked:
        await refresh_user_pp_and_ranks(db, user_id=user.id, mode=GameMode(token.ruleset_id))

    # TODO: Check for new personal best

    # Calculate rank on leaderboard (count scores with higher total_score)
    rank_global = None
    if score_data.passed and ranked:
        rank_result = await db.execute(
            select(func.count(Score.id) + 1).where(
                and_(
                    Score.beatmap_id == beatmap_id,
                    Score.passed.is_(True),
                    Score.ranked.is_(True),
                    Score.total_score > score.total_score,
                ),
            ),
        )
        rank_global = rank_result.scalar()

    # Load user for response
    result = await db.execute(select(User).where(User.id == user.id))
    score.user = result.scalar_one()

    # Notify client that score has been processed (for "Overall Ranking" panel)
    await send_user_score_processed(user.id, score.id)

    return _score_to_response(score, rank_global=rank_global)


@router.get("/users/{user_id}/scores/{type}")
async def get_user_scores(
    db: DbSession,
    user_id: int,
    type: str,
    mode: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[ScoreResponse]:
    """Get a user's scores by type (best, recent, firsts)."""
    # Verify user exists
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Build query based on type
    query = (
        select(Score)
        .options(selectinload(Score.beatmap))
        .where(Score.user_id == user_id)
    )

    if type == "best":
        query = query.where(
            and_(Score.passed.is_(True), Score.ranked.is_(True)),
        ).order_by(Score.pp.desc().nullslast())
    elif type == "recent":
        query = query.order_by(Score.ended_at.desc())
    elif type == "firsts":
        # TODO: Implement first place scores
        query = query.where(Score.passed.is_(True)).order_by(Score.ended_at.desc())
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid score type: {type}",
        )

    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    scores = result.scalars().all()

    # Attach user to scores for response
    for score in scores:
        score.user = user

    return [_score_to_response(s, include_beatmap=True) for s in scores]
