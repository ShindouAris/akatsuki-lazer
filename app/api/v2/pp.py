"""PP calculation endpoints."""

from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from pydantic import BaseModel
from pydantic import Field

from app.api.deps import DbSession
from app.models.user import GameMode
from app.services.beatmaps import BeatmapService
from app.services.pp import PPCalculationParams
from app.services.pp import PPService

router = APIRouter()


class PPCalculationRequest(BaseModel):
    """Request payload for PP calculation."""

    beatmap_id: int
    mode: int = Field(default=0, ge=0, le=3)
    mods: int = 0
    combo: int | None = None
    acc: float | None = Field(default=None, ge=0.0, le=100.0)
    n300: int | None = None
    n100: int | None = None
    n50: int | None = None
    ngeki: int | None = None
    nkatu: int | None = None
    nmiss: int | None = None


class PPCalculationResponse(BaseModel):
    """Response payload for PP calculation."""

    pp: float
    stars: float
    details: dict[str, float | None] = Field(default_factory=dict)


@router.post("/pp/calculate", response_model=PPCalculationResponse)
async def calculate_pp_post(db: DbSession, payload: PPCalculationRequest) -> PPCalculationResponse:
    """Calculate PP for a beatmap using POST payload."""
    return await _calculate_pp(
        db=db,
        beatmap_id=payload.beatmap_id,
        mode=payload.mode,
        mods=payload.mods,
        combo=payload.combo,
        acc=payload.acc,
        n300=payload.n300,
        n100=payload.n100,
        n50=payload.n50,
        ngeki=payload.ngeki,
        nkatu=payload.nkatu,
        nmiss=payload.nmiss,
    )


@router.get("/pp/calculate", response_model=PPCalculationResponse)
async def calculate_pp_get(
    db: DbSession,
    beatmap_id: int = Query(...),
    mode: int = Query(0, ge=0, le=3),
    mods: int = Query(0),
    combo: int | None = Query(None),
    acc: float | None = Query(None, ge=0.0, le=100.0),
    n300: int | None = Query(None),
    n100: int | None = Query(None),
    n50: int | None = Query(None),
    ngeki: int | None = Query(None),
    nkatu: int | None = Query(None),
    nmiss: int | None = Query(None),
) -> PPCalculationResponse:
    """Calculate PP for a beatmap using query parameters."""
    return await _calculate_pp(
        db=db,
        beatmap_id=beatmap_id,
        mode=mode,
        mods=mods,
        combo=combo,
        acc=acc,
        n300=n300,
        n100=n100,
        n50=n50,
        ngeki=ngeki,
        nkatu=nkatu,
        nmiss=nmiss,
    )


async def _calculate_pp(
    db: DbSession,
    beatmap_id: int,
    mode: int,
    mods: int,
    combo: int | None,
    acc: float | None,
    n300: int | None,
    n100: int | None,
    n50: int | None,
    ngeki: int | None,
    nkatu: int | None,
    nmiss: int | None,
) -> PPCalculationResponse:
    """Shared implementation for PP calculation endpoints."""
    try:
        mode_enum = GameMode(mode)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid mode") from exc

    service = BeatmapService(db)
    try:
        beatmap = await service.get_beatmap(beatmap_id)
        if beatmap is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Beatmap not found")

        osu_file_path = await service.ensure_osu_file(beatmap)
        if not osu_file_path:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Beatmap .osu file is unavailable for PP calculation",
            )
    finally:
        await service.close()

    try:
        pp_service = PPService()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PP calculation engine is unavailable",
        ) from exc

    params = PPCalculationParams(
        mode=mode_enum,
        mods=mods,
        combo=combo,
        accuracy=acc,
        n300=n300,
        n100=n100,
        n50=n50,
        ngeki=ngeki,
        nkatu=nkatu,
        nmiss=nmiss,
    )
    result = pp_service.calculate_pp(osu_file_path, params)

    pp_value = _coerce_float(result.get("pp"), default=0.0)
    stars_value = _coerce_float(result.get("stars"), default=0.0)

    return PPCalculationResponse(pp=pp_value, stars=stars_value, details=result)


def _coerce_float(value: Any, default: float) -> float:
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return float(value)
    return default
