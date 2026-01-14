"""Multiplayer room and related models."""

from datetime import UTC
from datetime import datetime
from enum import IntEnum
from enum import StrEnum

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.core.database import Base


class RoomType(StrEnum):
    """Multiplayer room type."""

    PLAYLISTS = "playlists"
    HEAD_TO_HEAD = "head_to_head"
    TEAM_VERSUS = "team_versus"


class RoomStatus(StrEnum):
    """Multiplayer room status."""

    IDLE = "idle"
    PLAYING = "playing"
    CLOSED = "closed"


class QueueMode(StrEnum):
    """Playlist queue mode."""

    HOST_ONLY = "host_only"
    ALL_PLAYERS = "all_players"
    ALL_PLAYERS_ROUND_ROBIN = "all_players_round_robin"


class MultiplayerUserState(IntEnum):
    """User state in multiplayer room."""

    IDLE = 0
    READY = 1
    WAITING_FOR_LOAD = 2
    LOADED = 3
    READY_FOR_GAMEPLAY = 4
    PLAYING = 5
    FINISHED_PLAY = 6
    RESULTS = 7
    SPECTATING = 8


class MultiplayerRoom(Base):
    """Multiplayer room model."""

    __tablename__ = "multiplayer_rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    # Room info
    name: Mapped[str] = mapped_column(String(255))
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Type and status
    type: Mapped[str] = mapped_column(String(32), default=RoomType.HEAD_TO_HEAD)
    status: Mapped[str] = mapped_column(String(32), default=RoomStatus.IDLE)
    queue_mode: Mapped[str] = mapped_column(String(32), default=QueueMode.HOST_ONLY)

    # Limits
    max_participants: Mapped[int] = mapped_column(default=16)
    participant_count: Mapped[int] = mapped_column(default=0)

    # Settings
    auto_start_duration: Mapped[int] = mapped_column(default=0)  # seconds
    auto_skip: Mapped[bool] = mapped_column(Boolean, default=False)

    # Category (for playlists)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Timestamps
    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    # Chat channel
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Active playlist item
    current_playlist_item_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True,
    )

    # Relationships
    playlist_items: Mapped[list["MultiplayerPlaylistItem"]] = relationship(
        "MultiplayerPlaylistItem", back_populates="room", lazy="selectin",
    )
    scores: Mapped[list["MultiplayerScore"]] = relationship(
        "MultiplayerScore", back_populates="room", lazy="dynamic",
    )


class MultiplayerPlaylistItem(Base):
    """Playlist item in a multiplayer room."""

    __tablename__ = "multiplayer_playlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("multiplayer_rooms.id", ondelete="CASCADE"), index=True,
    )
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    # Beatmap
    beatmap_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("beatmaps.id", ondelete="CASCADE"),
    )
    ruleset_id: Mapped[int] = mapped_column(default=0)

    # Mods
    required_mods: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    allowed_mods: Mapped[str] = mapped_column(Text, default="[]")  # JSON

    # Order and status
    playlist_order: Mapped[int] = mapped_column(default=0)
    played_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    expired: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    # Relationships
    room: Mapped["MultiplayerRoom"] = relationship(
        "MultiplayerRoom", back_populates="playlist_items",
    )


class MultiplayerScore(Base):
    """Score in a multiplayer room."""

    __tablename__ = "multiplayer_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("multiplayer_rooms.id", ondelete="CASCADE"), index=True,
    )
    playlist_item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("multiplayer_playlist_items.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    score_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scores.id", ondelete="SET NULL"), nullable=True,
    )

    # Score summary
    total_score: Mapped[int] = mapped_column(BigInteger, default=0)
    accuracy: Mapped[float] = mapped_column(default=0.0)
    pp: Mapped[float | None] = mapped_column(nullable=True)
    max_combo: Mapped[int] = mapped_column(default=0)
    rank: Mapped[str] = mapped_column(String(4), default="D")

    # Passed
    passed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    # Relationships
    room: Mapped["MultiplayerRoom"] = relationship(
        "MultiplayerRoom", back_populates="scores",
    )
