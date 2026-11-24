"""Beatmap tags endpoints."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/tags")
async def get_tags() -> dict:
    """Get available beatmap tags (the client expects ``{"tags": [...]}``).

    Empty for now - the tags feature is not implemented.
    """
    return {"tags": []}
