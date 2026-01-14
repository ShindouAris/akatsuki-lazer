"""Chat endpoints."""

from fastapi import APIRouter

from app.api.deps import CurrentUser

router = APIRouter()


@router.post("/chat/ack")
async def chat_ack(user: CurrentUser) -> dict:
    """Acknowledge chat messages."""
    return {"silences": []}


@router.get("/chat/channels")
async def get_channels(user: CurrentUser) -> list:
    """Get chat channels."""
    return []


@router.get("/chat/presence")
async def get_presence(user: CurrentUser) -> list:
    """Get chat presence (online users in channels)."""
    return []


@router.get("/chat/updates")
async def get_chat_updates(user: CurrentUser, since: int = 0) -> dict:
    """Get chat updates since a given message ID.

    This endpoint is used to poll for new messages and presence updates.
    """
    return {
        "messages": [],
        "presence": [],
        "silences": [],
    }
