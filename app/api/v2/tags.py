"""Beatmap tags endpoints."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/tags")
async def get_tags() -> list:
    """Get available beatmap tags.

    Returns an empty list for now - tags feature not yet implemented.
    """
    return []
