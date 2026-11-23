"""Chat endpoints.

Only the chat of multiplayer rooms exists (see ``app.services.chat``): a room's channel is open to the users in the
room. Public channels and private messages are not available.
"""

from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import Form
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.api.deps import CurrentUser
from app.api.deps import DbSession
from app.core.error import OsuError
from app.models.chat import ChatChannelMember
from app.models.chat import ChatMessage
from app.services import chat as chat_service

router = APIRouter()


def _error(code: int, error: str, message: str) -> OsuError:
    return OsuError(code=code, error=error, message=message)


async def _member_channel(db: DbSession, user_id: int, channel_id: int):
    """The open room behind a channel the user is in; anything else is not found (not even a hint that it exists)."""
    room = await chat_service.open_room_for_channel(db, channel_id)
    if room is None or not await chat_service.wait_for_membership(db, channel_id, user_id):
        raise _error(status.HTTP_404_NOT_FOUND, "not_found", "Channel not found")
    return room


@router.post("/chat/ack")
async def chat_ack(user: CurrentUser) -> dict:
    """Acknowledge chat messages."""
    return {"silences": []}


@router.get("/chat/channels")
async def get_channels(user: CurrentUser, db: DbSession) -> list:
    """The channels the user is in."""
    return await chat_service.channels_of(db, user.id)


@router.post("/chat/channels")
async def create_channel(user: CurrentUser) -> dict:
    """Private messages are not supported."""
    raise _error(422, "unsupported", "Private messages are not supported")


@router.get("/chat/channels/{channel_id}")
async def get_channel(channel_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    room = await _member_channel(db, user.id, channel_id)
    channel = await chat_service.channel_json(db, room)
    users = await chat_service.users_json(db, channel["users"])
    return {"channel": channel, "users": users}


@router.put("/chat/channels/{channel_id}/users/{user_id}")
async def join_channel(channel_id: int, user_id: int, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """Room channels are joined by joining the room, so this only confirms a membership that exists."""
    if user_id != user.id:
        raise _error(status.HTTP_403_FORBIDDEN, "forbidden", "You can only join channels yourself")
    room = await _member_channel(db, user.id, channel_id)
    return await chat_service.channel_json(db, room)


@router.delete("/chat/channels/{channel_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def leave_channel(channel_id: int, user_id: int, user: CurrentUser) -> Response:
    """Leaving a room's channel is leaving the room, which the realtime service reports; nothing to do here."""
    if user_id != user.id:
        raise _error(status.HTTP_403_FORBIDDEN, "forbidden", "You can only leave channels yourself")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chat/channels/{channel_id}/messages")
async def get_messages(
    channel_id: int,
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=chat_service.HISTORY_PAGE)] = chat_service.HISTORY_PAGE,
    since: int | None = None,
    until: int | None = None,
) -> list:
    await _member_channel(db, user.id, channel_id)
    messages = await chat_service.history(db, channel_id, limit=limit, since=since, until=until)
    return await chat_service.with_senders(db, messages)


@router.post("/chat/channels/{channel_id}/messages")
async def post_message(
    channel_id: int,
    user: CurrentUser,
    db: DbSession,
    message: Annotated[str, Form()],
    is_action: Annotated[bool, Form()] = False,
    uuid: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    content = message.strip()
    if not content:
        raise _error(422, "empty_message", "Message must not be empty")
    if len(content) > chat_service.MESSAGE_LENGTH_LIMIT:
        raise _error(422, "message_too_long", "Message is too long")
    await _member_channel(db, user.id, channel_id)

    saved = await chat_service.post_message(db, channel_id, user, content, is_action=is_action, uuid=uuid)
    await chat_service.broadcast_message(db, saved)
    return (await chat_service.with_senders(db, [saved]))[0]


@router.put("/chat/channels/{channel_id}/mark-as-read/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def mark_as_read(channel_id: int, message_id: int, user: CurrentUser, db: DbSession) -> Response:
    await _member_channel(db, user.id, channel_id)
    member = await db.get(ChatChannelMember, (channel_id, user.id), populate_existing=True)
    if member is not None and (member.last_read_id or 0) < message_id:
        member.last_read_id = message_id
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chat/presence")
async def get_presence(user: CurrentUser, db: DbSession) -> list:
    """The channels the user is in."""
    return await chat_service.channels_of(db, user.id)


@router.get("/chat/updates")
async def get_chat_updates(user: CurrentUser, db: DbSession, since: int = 0) -> dict:
    """The user's channels and the messages in them after ``since`` (polling, and what the client reads on connect)."""
    messages: list[ChatMessage] = await chat_service.updates_for(db, user.id, since)
    return {
        "messages": await chat_service.with_senders(db, messages),
        "presence": await chat_service.channels_of(db, user.id),
        "silences": [],
    }
