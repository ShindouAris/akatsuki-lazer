"""Beatmap and beatmapset endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import ActiveUser
from app.api.deps import DbSession
from app.api.v2.schemas import BeatmapCompact
from app.api.v2.schemas import GetBeatmapsResponse
from app.api.v2.schemas import BeatmapResponse
from app.api.v2.schemas import BeatmapsetCompact
from app.api.v2.schemas import BeatmapsetResponse
from app.api.v2.schemas import ScoreTokenResponse
from app.models.beatmap import Beatmap
from app.models.beatmap import BeatmapSet
from app.models.beatmap import BeatmapStatus
from app.models.score import ScoreToken
from app.models.user import GameMode
from app.services.beatmaps import BeatmapService

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

def _mode_to_int(mode: GameMode) -> int:
    """Convert GameMode enum to int."""
    return {
        GameMode.OSU: 0,
        GameMode.TAIKO: 1,
        GameMode.CATCH: 2,
        GameMode.MANIA: 3,
    }.get(mode, 0)


def _status_to_string(status: BeatmapStatus) -> str:
    """Convert BeatmapStatus enum to string."""
    return {
        BeatmapStatus.GRAVEYARD: "graveyard",
        BeatmapStatus.WIP: "wip",
        BeatmapStatus.PENDING: "pending",
        BeatmapStatus.RANKED: "ranked",
        BeatmapStatus.APPROVED: "approved",
        BeatmapStatus.QUALIFIED: "qualified",
        BeatmapStatus.LOVED: "loved",
    }.get(status, "pending")


def _beatmap_to_compact(beatmap: Beatmap) -> BeatmapCompact:
    """Convert Beatmap model to BeatmapCompact."""
    return BeatmapCompact(
        id=beatmap.id,
        beatmapset_id=beatmap.beatmapset_id,
        version=beatmap.version,
        mode = _mode_to_int(beatmap.mode),
        mode_int=_mode_to_int(beatmap.mode),
        status=_status_to_string(beatmap.status),
        difficulty_rating=beatmap.difficulty_rating,
        total_length=beatmap.total_length,
        cs=beatmap.cs,
        ar=beatmap.ar,
        accuracy=beatmap.od,
        drain=beatmap.hp,
        bpm=beatmap.bpm,
        max_combo=beatmap.max_combo,
        checksum=beatmap.checksum,
        count_circles=beatmap.count_circles,
        count_sliders=beatmap.count_sliders,
        count_spinners=beatmap.count_spinners,
    )


def _beatmapset_to_compact(beatmapset: BeatmapSet) -> BeatmapsetCompact:
    """Convert BeatmapSet model to BeatmapsetCompact."""
    return BeatmapsetCompact(
        id=beatmapset.id,
        artist=beatmapset.artist,
        artist_unicode=beatmapset.artist_unicode,
        title=beatmapset.title,
        title_unicode=beatmapset.title_unicode,
        creator=beatmapset.creator,
        user_id=beatmapset.user_id,
        status=_status_to_string(beatmapset.status),
        play_count=beatmapset.play_count,
        favourite_count=beatmapset.favourite_count,
    )


def _beatmapset_to_response(beatmapset: BeatmapSet) -> BeatmapsetResponse:
    """Convert BeatmapSet model to BeatmapsetResponse."""
    return BeatmapsetResponse(
        id=beatmapset.id,
        artist=beatmapset.artist,
        artist_unicode=beatmapset.artist_unicode,
        title=beatmapset.title,
        title_unicode=beatmapset.title_unicode,
        creator=beatmapset.creator,
        user_id=beatmapset.user_id,
        status=_status_to_string(beatmapset.status),
        play_count=beatmapset.play_count,
        favourite_count=beatmapset.favourite_count,
        source=beatmapset.source,
        tags=beatmapset.tags,
        ranked_date=beatmapset.ranked_date,
        submitted_date=beatmapset.submitted_date,
        last_updated=beatmapset.last_updated,
        bpm=beatmapset.bpm,
        preview_url=beatmapset.preview_url,
        has_video=beatmapset.has_video,
        has_storyboard=beatmapset.has_storyboard,
        nsfw=beatmapset.nsfw,
        beatmaps=[_beatmap_to_compact(beatmap) for beatmap in beatmapset.beatmaps],
    )


@router.get("/beatmaps/lookup", response_model=BeatmapResponse)
@router.get("/beatmapset/lookup", response_model=BeatmapResponse, include_in_schema=False)
async def lookup_beatmap(
    db: DbSession,
    checksum: str | None = Query(None),
    filename: str | None = Query(None),
    beatmap_id: int | None = Query(None),
) -> BeatmapResponse:
    """Lookup a beatmap by checksum, filename, or ID.

    Checks local database first, then fetches from external source if not found.
    Note: Checksum lookup requires osu! API (mirror doesn't support it).
    """
    service = BeatmapService(db)

    try:
        beatmap: Beatmap | None = None

        if beatmap_id:
            # ID lookup supports both mirror and osu! API
            beatmap = await service.get_beatmap(beatmap_id)
        elif checksum:
            # Checksum lookup - local DB first, then osu! API (not mirror)
            beatmap = await service.get_beatmap_by_checksum(checksum, filename)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Must provide beatmap_id, checksum, or filename",
            )

        if not beatmap:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Beatmap not found",
            )

        compact = _beatmap_to_compact(beatmap)
        beatmapset = (
            _beatmapset_to_compact(beatmap.beatmapset) if beatmap.beatmapset else None
        )

        return BeatmapResponse(
            **compact.model_dump(),
            beatmapset=beatmapset,
        )
    finally:
        await service.close()


@router.get("/beatmaps/", response_model=GetBeatmapsResponse)
@router.get("/beatmaps", response_model=GetBeatmapsResponse, include_in_schema=False)
async def get_beatmaps(
    db: DbSession,
    ids_bracket: list[int] = Query(default_factory=list, alias="ids[]"),
    ids: list[int] = Query(default_factory=list),
) -> GetBeatmapsResponse:
    """Get multiple beatmaps by IDs.

    This endpoint is used by lazer multiplayer requests, which send ids[] query values.
    """
    requested_ids: list[int] = []
    for beatmap_id in [*ids_bracket, *ids]:
        if beatmap_id not in requested_ids:
            requested_ids.append(beatmap_id)

    if not requested_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide at least one beatmap id via ids[]",
        )

    service = BeatmapService(db)

    try:
        beatmaps: list[BeatmapResponse] = []

        for beatmap_id in requested_ids:
            beatmap = await service.get_beatmap(beatmap_id)
            if not beatmap:
                continue

            compact = _beatmap_to_compact(beatmap)
            beatmapset = (
                _beatmapset_to_compact(beatmap.beatmapset) if beatmap.beatmapset else None
            )

            beatmaps.append(
                BeatmapResponse(
                    **compact.model_dump(),
                    beatmapset=beatmapset,
                ),
            )

        return GetBeatmapsResponse(beatmaps=beatmaps)
    finally:
        await service.close()


@router.get("/beatmaps/{beatmap_id}", response_model=BeatmapResponse)
async def get_beatmap(db: DbSession, beatmap_id: int) -> BeatmapResponse:
    """Get a beatmap by ID.

    Checks local database first, then fetches from mirror if not found.
    """
    service = BeatmapService(db)

    try:
        beatmap = await service.get_beatmap(beatmap_id)

        if not beatmap:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Beatmap not found",
            )

        compact = _beatmap_to_compact(beatmap)
        beatmapset = (
            _beatmapset_to_compact(beatmap.beatmapset) if beatmap.beatmapset else None
        )

        return BeatmapResponse(
            **compact.model_dump(),
            beatmapset=beatmapset,
        )
    finally:
        await service.close()


async def _search_beatmapsets_db(
    db: DbSession,
    q: str | None,
    m: int | None,
    s: str | None,
    sort: str,
    cursor_string: str | None,
) -> dict:
    """Search for beatmapsets in the local database.

    This is currently unused but kept for future use when we have indexed more maps.
    """
    query = select(BeatmapSet).options(selectinload(BeatmapSet.beatmaps))

    # Search filter
    if q:
        search_term = f"%{q}%"
        query = query.where(
            or_(
                BeatmapSet.title.ilike(search_term),
                BeatmapSet.artist.ilike(search_term),
                BeatmapSet.creator.ilike(search_term),
                BeatmapSet.tags.ilike(search_term),
            ),
        )

    # Status filter
    if s:
        status_map = {
            "ranked": BeatmapStatus.RANKED,
            "qualified": BeatmapStatus.QUALIFIED,
            "loved": BeatmapStatus.LOVED,
            "pending": BeatmapStatus.PENDING,
            "wip": BeatmapStatus.WIP,
            "graveyard": BeatmapStatus.GRAVEYARD,
        }
        if s in status_map:
            query = query.where(BeatmapSet.status == status_map[s])

    # Sort
    if sort == "ranked_desc":
        query = query.order_by(BeatmapSet.ranked_date.desc().nullslast())
    elif sort == "plays_desc":
        query = query.order_by(BeatmapSet.play_count.desc())
    elif sort == "favourites_desc":
        query = query.order_by(BeatmapSet.favourite_count.desc())
    else:
        query = query.order_by(BeatmapSet.id.desc())

    # Pagination
    query = query.limit(50)

    result = await db.execute(query)
    beatmapsets = result.scalars().all()

    return {
        "beatmapsets": [
            BeatmapsetResponse(
                id=bs.id,
                artist=bs.artist,
                artist_unicode=bs.artist_unicode,
                title=bs.title,
                title_unicode=bs.title_unicode,
                creator=bs.creator,
                user_id=bs.user_id,
                status=_status_to_string(bs.status),
                play_count=bs.play_count,
                favourite_count=bs.favourite_count,
                source=bs.source,
                tags=bs.tags,
                ranked_date=bs.ranked_date,
                submitted_date=bs.submitted_date,
                last_updated=bs.last_updated,
                bpm=bs.bpm,
                preview_url=bs.preview_url,
                has_video=bs.has_video,
                has_storyboard=bs.has_storyboard,
                nsfw=bs.nsfw,
                beatmaps=[_beatmap_to_compact(b) for b in bs.beatmaps],
            )
            for bs in beatmapsets
        ],
        "cursor_string": None,
        "total": len(beatmapsets),
    }


async def _search_beatmapsets_api(
    db: DbSession,
    q: str | None,
    m: int | None,
    s: str | None,
    sort: str,
    cursor_string: str | None,
) -> dict:
    """Search for beatmapsets using the mirror API."""
    service = BeatmapService(db)

    try:
        result = await service.search_beatmapsets(
            query=q,
            mode=m,
            status=s,
            sort=sort,
            cursor_string=cursor_string,
        )

        return {
            "beatmapsets": result.beatmapsets,
            "cursor_string": result.cursor_string,
            "total": result.total,
        }
    finally:
        await service.close()


@router.get("/beatmapsets/search")
async def search_beatmapsets(
    db: DbSession,
    q: str | None = Query(None, description="Search query"),
    m: int | None = Query(None, description="Game mode (0-3)"),
    s: str | None = Query(None, description="Status filter"),
    sort: str = Query("relevance_desc", description="Sort order"),
    cursor_string: str | None = Query(None, description="Pagination cursor"),
) -> dict:
    """Search for beatmapsets.

    Uses the mirror API to search for beatmapsets. The local database search
    is available via _search_beatmapsets_db but currently unused.
    """
    # Use API-based search (mirror service)
    return await _search_beatmapsets_api(db, q, m, s, sort, cursor_string)

@router.get("/beatmapsets/lookup", response_model=BeatmapsetResponse)
async def lookup_beatmapset(
    db: DbSession,
    beatmap_id: int = Query(...),
) -> BeatmapsetResponse:
    """Lookup a beatmapset by beatmap ID."""
    service = BeatmapService(db)

    try:
        beatmap = await service.get_beatmap(beatmap_id)
        if not beatmap:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Beatmap not found",
            )

        beatmapset = await service.get_beatmapset(beatmap.beatmapset_id)
        if not beatmapset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Beatmapset not found",
            )

        return _beatmapset_to_response(beatmapset)
    finally:
        await service.close()


@router.get("/beatmapsets/{beatmapset_id}", response_model=BeatmapsetResponse)
async def get_beatmapset(db: DbSession, beatmapset_id: int) -> BeatmapsetResponse:
    """Get a beatmapset by ID.

    Checks local database first, then fetches from mirror if not found.
    """
    service = BeatmapService(db)

    try:
        beatmapset = await service.get_beatmapset(beatmapset_id)

        if not beatmapset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Beatmapset not found",
            )

        return _beatmapset_to_response(beatmapset)
    finally:
        await service.close()


@router.get("/beatmapsets/{beatmapset_id}/download")
async def download_beatmapset(
    beatmapset_id: int,
    noVideo: int = Query(0, alias="noVideo"),
) -> StreamingResponse:
    """Download a beatmapset as .osz file."""
    service = BeatmapService()

    return StreamingResponse(
        service.download_beatmapset(beatmapset_id, no_video=bool(noVideo)),
        media_type="application/x-osu-beatmap-archive",
        headers={
            "Content-Disposition": f'attachment; filename="{beatmapset_id}.osz"',
        },
    )


@router.post("/beatmaps/{beatmap_id}/solo/scores", response_model=ScoreTokenResponse)
async def create_score_token(
    db: DbSession,
    user: ActiveUser,
    beatmap_id: int,
    beatmap_hash: str = Form(...),
    ruleset_id: int = Form(0),
    version_hash: str = Form(None),  # Client sends this but we don't use it currently
) -> ScoreTokenResponse:
    """Request a score token for score submission.

    Fetches beatmap from mirror if not in local database.
    Validates beatmap_hash but doesn't store it (matches official behavior).
    """
    service = BeatmapService(db)

    try:
        # Verify beatmap exists (fetches from mirror if needed)
        beatmap = await service.get_beatmap(beatmap_id)

        if not beatmap:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Beatmap not found",
            )

        # Validate beatmap hash (official does this but doesn't store it)
        if beatmap.checksum and beatmap.checksum != beatmap_hash:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Beatmap hash mismatch",
            )

        # Create score token (no expires_at - official tokens don't expire)
        token = ScoreToken(
            user_id=user.id,
            beatmap_id=beatmap_id,
            ruleset_id=ruleset_id,
            build_id=None,  # Could be set from version_hash lookup
        )
        db.add(token)
        await db.flush()
        return ScoreTokenResponse(
            id=token.id,
            created_at=token.created_at,
        )
    finally:
        await service.close()
