"""Calls to the external metadata service (see ./multiplayer/cmd/metadata)."""

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def _call(method: str, path: str, json: dict | None = None) -> dict | None:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.request(
                method,
                f"{settings.metadata_internal_url.rstrip('/')}{path}",
                json=json,
                headers={"X-Internal-Key": settings.internal_api_key},
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        logger.warning("Metadata service call %s %s failed", method, path, exc_info=True)
        return None


async def notify_room_score_set(event: dict) -> int:
    result = await _call("POST", "/internal/room-score-set", event)
    return int(result["sent"]) if result else 0


async def notify_beatmap_updates(beatmap_set_ids: list[int], queue_id: int) -> int:
    result = await _call("POST", "/internal/beatmap-updates", {"beatmap_set_ids": beatmap_set_ids, "queue_id": queue_id})
    return int(result["notified"]) if result else 0


async def online_users() -> int | None:
    result = await _call("GET", "/internal/online-count")
    return int(result["online_users"]) if result else None
