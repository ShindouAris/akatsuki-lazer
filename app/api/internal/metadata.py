"""Internal API for the external metadata service (see ./multiplayer/cmd/metadata).

The Go service owns presence and the live connections; this process owns the database. Authenticated with the
shared ``INTERNAL_API_KEY`` (header ``X-Internal-Key``).
"""

from fastapi import APIRouter
from fastapi import Depends

from app.api.hubs.metadata import _build_playlist_stats_for_room
from app.api.hubs.metadata import _get_active_daily_challenge_info
from app.api.hubs.metadata import _get_friend_ids_for_user
from app.api.internal.multiplayer import require_internal_key

router = APIRouter(prefix="/internal/metadata", dependencies=[Depends(require_internal_key)])


@router.get("/friends/{user_id}")
async def friends(user_id: int) -> dict:
    return {"friend_ids": sorted(await _get_friend_ids_for_user(user_id))}


@router.get("/rooms/{room_id}/stats")
async def room_stats(room_id: int) -> dict:
    return {
        "items": [
            {
                "playlist_item_id": s.playlist_item_id,
                "total_score_distribution": s.total_score_distribution,
                "cumulative_score": s.cumulative_score,
                "last_processed_score_id": s.last_processed_score_id,
            }
            for s in await _build_playlist_stats_for_room(room_id)
        ],
    }


@router.get("/daily-challenge")
async def daily_challenge() -> dict:
    info = await _get_active_daily_challenge_info()
    return {"room_id": info.room_id if info else None}
