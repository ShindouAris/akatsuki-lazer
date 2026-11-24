"""Chat of multiplayer rooms: channels, membership, messages and delivery over the notifications websocket.

A room's channel id is the room's ``channel_id`` and it is open to exactly the users in the room: the realtime
service reports the participants with every room state it saves, and :func:`sync_room_members` mirrors them. There
are no other channels (no public channels, no private messages).
"""

import asyncio
import json
import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import WebSocket
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatChannelMember
from app.models.chat import ChatMessage
from app.models.multiplayer import MultiplayerRoom
from app.models.multiplayer import RoomStatus
from app.models.user import User

logger = logging.getLogger(__name__)

MESSAGE_LENGTH_LIMIT = 450
HISTORY_PAGE = 50
# The client does not ask to join the channel of a room (joining the room is joining the channel) and reads its
# history at once, while the room's participants reach this process a moment later. Reads and writes by a user
# who is not (yet) a member therefore wait this long for them to appear.
MEMBERSHIP_WAIT_SECONDS = 3.0
MEMBERSHIP_POLL_SECONDS = 0.1


# --- channels ------------------------------------------------------------------------------------------------


async def open_room_for_channel(db: AsyncSession, channel_id: int) -> MultiplayerRoom | None:
    """The room a channel belongs to, if it exists and is not closed."""
    result = await db.execute(
        select(MultiplayerRoom).where(MultiplayerRoom.channel_id == channel_id).limit(1),
    )
    room = result.scalar_one_or_none()
    if room is None or room.status == RoomStatus.CLOSED.value:
        return None
    return room


async def member_ids(db: AsyncSession, channel_id: int) -> list[int]:
    rows = await db.execute(
        select(ChatChannelMember.user_id)
        .where(ChatChannelMember.channel_id == channel_id)
        .order_by(ChatChannelMember.user_id),
    )
    return list(rows.scalars().all())


async def is_member(db: AsyncSession, channel_id: int, user_id: int) -> bool:
    found = await db.get(ChatChannelMember, (channel_id, user_id), populate_existing=True)
    return found is not None


async def wait_for_membership(db: AsyncSession, channel_id: int, user_id: int) -> bool:
    """Whether the user is a member, giving a room's participants a moment to arrive."""
    waited = 0.0
    while True:
        if await is_member(db, channel_id, user_id):
            return True
        if waited >= MEMBERSHIP_WAIT_SECONDS:
            return False
        await asyncio.sleep(MEMBERSHIP_POLL_SECONDS)
        waited += MEMBERSHIP_POLL_SECONDS


async def last_message_id(db: AsyncSession, channel_id: int) -> int | None:
    return await db.scalar(select(func.max(ChatMessage.id)).where(ChatMessage.channel_id == channel_id))


async def channel_json(
    db: AsyncSession, room: MultiplayerRoom, last_read_id: int | None = None,
) -> dict[str, Any]:
    """A channel as the client's ``Channel`` reads it."""
    channel_id = room.channel_id or room.id
    members = await member_ids(db, channel_id)
    return {
        "channel_id": channel_id,
        "name": f"#lazermp_{room.id}",
        "description": room.name,
        "type": "MULTIPLAYER",
        "last_message_id": await last_message_id(db, channel_id),
        "last_read_id": last_read_id,
        "active_user_count": len(members),
        "message_length_limit": MESSAGE_LENGTH_LIMIT,
        "users": members,
    }


async def channels_of(db: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    """The open channels a user is in."""
    rows = await db.execute(
        select(ChatChannelMember, MultiplayerRoom)
        .join(MultiplayerRoom, MultiplayerRoom.channel_id == ChatChannelMember.channel_id)
        .where(ChatChannelMember.user_id == user_id, MultiplayerRoom.status != RoomStatus.CLOSED.value)
        .order_by(ChatChannelMember.channel_id),
    )
    return [await channel_json(db, room, member.last_read_id) for member, room in rows.all()]


async def sync_room_members(db: AsyncSession, room: MultiplayerRoom, participant_ids: list[int]) -> None:
    """Make the room's channel open to exactly the given users (to nobody once the room is closed)."""
    channel_id = room.channel_id or room.id
    wanted: set[int] = set()
    if room.status != RoomStatus.CLOSED.value and participant_ids:
        known = await db.execute(select(User.id).where(User.id.in_(set(participant_ids))))
        wanted = set(known.scalars().all())

    current = set(await member_ids(db, channel_id))
    gone = current - wanted
    if gone:
        await db.execute(
            delete(ChatChannelMember).where(
                ChatChannelMember.channel_id == channel_id, ChatChannelMember.user_id.in_(gone),
            ),
        )
    for user_id in sorted(wanted - current):
        db.add(ChatChannelMember(channel_id=channel_id, user_id=user_id))
    await db.flush()


# --- messages ------------------------------------------------------------------------------------------------


def user_json(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "avatar_url": user.avatar_url,
        "country_code": getattr(user, "country_code", None) or "XX",
        "is_active": True,
        "is_bot": bool(user.is_bot),
    }


async def users_json(db: AsyncSession, user_ids: list[int]) -> list[dict[str, Any]]:
    if not user_ids:
        return []
    rows = await db.execute(select(User).where(User.id.in_(user_ids)).order_by(User.id))
    return [user_json(u) for u in rows.scalars().all()]


def message_json(message: ChatMessage) -> dict[str, Any]:
    created = message.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {
        "message_id": message.id,
        "channel_id": message.channel_id,
        "is_action": message.is_action,
        "timestamp": created.isoformat(),
        "content": message.content,
        "sender_id": message.sender_id,
        "uuid": message.uuid or "",
    }


async def senders_of(db: AsyncSession, messages: list[ChatMessage]) -> list[User]:
    ids = {m.sender_id for m in messages}
    if not ids:
        return []
    rows = await db.execute(select(User).where(User.id.in_(ids)))
    return list(rows.scalars().all())


async def with_senders(db: AsyncSession, messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Messages with their sender embedded, as the REST endpoints return them."""
    users = {u.id: u for u in await senders_of(db, messages)}
    out = []
    for message in messages:
        data = message_json(message)
        if message.sender_id in users:
            data["sender"] = user_json(users[message.sender_id])
        out.append(data)
    return out


async def history(
    db: AsyncSession, channel_id: int, *, limit: int = HISTORY_PAGE, since: int | None = None,
    until: int | None = None,
) -> list[ChatMessage]:
    """Messages in ascending order: the ones after ``since``, or the latest ones (before ``until``)."""
    limit = max(1, min(limit, HISTORY_PAGE))
    query = select(ChatMessage).where(ChatMessage.channel_id == channel_id)
    if since is not None:
        rows = await db.execute(query.where(ChatMessage.id > since).order_by(ChatMessage.id).limit(limit))
        return list(rows.scalars().all())

    if until is not None:
        query = query.where(ChatMessage.id < until)
    rows = await db.execute(query.order_by(ChatMessage.id.desc()).limit(limit))
    return list(reversed(rows.scalars().all()))


async def post_message(
    db: AsyncSession, channel_id: int, sender: User, content: str, *, is_action: bool, uuid: str | None,
) -> ChatMessage:
    message = ChatMessage(
        channel_id=channel_id, sender_id=sender.id, content=content, is_action=is_action, uuid=uuid or None,
        created_at=datetime.now(UTC),
    )
    db.add(message)
    await db.flush()
    return message


async def updates_for(db: AsyncSession, user_id: int, since: int) -> list[ChatMessage]:
    """Messages after ``since`` in the channels of the user. Nothing for 0: that is a fresh start, and the history
    of a channel is read when it is opened."""
    if since <= 0:
        return []
    rows = await db.execute(
        select(ChatMessage)
        .join(ChatChannelMember, ChatChannelMember.channel_id == ChatMessage.channel_id)
        .where(ChatChannelMember.user_id == user_id, ChatMessage.id > since)
        .order_by(ChatMessage.id)
        .limit(100),
    )
    return list(rows.scalars().all())


# --- delivery ------------------------------------------------------------------------------------------------


class _Connection:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.chat = False
        self.lock = asyncio.Lock()


class ChatConnections:
    """The notifications websockets of this process. A user who has sent ``chat.start`` gets chat events."""

    def __init__(self) -> None:
        self._by_user: dict[int, list[_Connection]] = {}

    def register(self, user_id: int, websocket: WebSocket) -> _Connection:
        connection = _Connection(websocket)
        self._by_user.setdefault(user_id, []).append(connection)
        return connection

    def unregister(self, user_id: int, connection: _Connection) -> None:
        connections = self._by_user.get(user_id, [])
        if connection in connections:
            connections.remove(connection)
        if not connections:
            self._by_user.pop(user_id, None)

    @staticmethod
    def start_chat(connection: _Connection) -> None:
        connection.chat = True

    def listening(self, user_id: int) -> int:
        return sum(1 for c in self._by_user.get(user_id, []) if c.chat)

    async def send(
        self, user_ids: list[int], event: str, data: dict[str, Any], *, chat_only: bool = True,
    ) -> None:
        """Deliver an event to the users' connections (only chat-listening ones unless ``chat_only`` is off).

        Failures drop the connection.
        """
        text = json.dumps({"event": event, "data": data})
        for user_id in user_ids:
            for connection in list(self._by_user.get(user_id, [])):
                if chat_only and not connection.chat:
                    continue
                try:
                    async with connection.lock:
                        await connection.websocket.send_text(text)
                except Exception:  # the socket is gone; its own handler cleans up
                    logger.debug("chat delivery to user %s failed", user_id, exc_info=True)
                    self.unregister(user_id, connection)


connections = ChatConnections()


async def broadcast_message(db: AsyncSession, message: ChatMessage) -> None:
    """Tell everybody in the channel about a new message."""
    recipients = await member_ids(db, message.channel_id)
    users = await senders_of(db, [message])
    await connections.send(
        recipients,
        "chat.message.new",
        {"messages": [message_json(message)], "users": [user_json(u) for u in users]},
    )
