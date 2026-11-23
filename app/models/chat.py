"""Chat models. Only the chat of multiplayer rooms exists: a room's channel is open to the users in the room."""

from datetime import UTC
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class ChatChannelMember(Base):
    """A user who may read and write a channel. For room channels the realtime service keeps this in step with
    who is in the room."""

    __tablename__ = "chat_channel_members"

    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, autoincrement=False,
    )
    last_read_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ChatMessage(Base):
    """A message in a channel."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    sender_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    is_action: Mapped[bool] = mapped_column(Boolean, default=False)
    # chosen by the sending client, echoed back so that it can match its local copy
    uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
