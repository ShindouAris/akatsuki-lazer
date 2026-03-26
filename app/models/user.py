"""User and authentication models."""

from datetime import UTC
from datetime import datetime
from enum import IntEnum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.score import Score


class GameMode(IntEnum):
    """osu! game modes."""

    OSU = 0
    TAIKO = 1
    CATCH = 2
    MANIA = 3


class UserStatus(IntEnum):
    """User online status."""

    OFFLINE = 0
    ONLINE = 1
    DO_NOT_DISTURB = 2


class User(Base):
    """User account model."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))

    # Profile
    country_acronym: Mapped[str] = mapped_column(String(2), default="XX")
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cover_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Preferences
    playmode: Mapped[GameMode] = mapped_column(
        Enum(GameMode), default=GameMode.OSU,
    )
    playstyle: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_supporter: Mapped[bool] = mapped_column(Boolean, default=False)
    is_restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )
    last_visit: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Relationships
    statistics: Mapped[list["UserStatistics"]] = relationship(
        "UserStatistics", back_populates="user", lazy="selectin",
    )
    scores: Mapped[list["Score"]] = relationship(
        "Score", back_populates="user", lazy="dynamic",
    )
    oauth_tokens: Mapped[list["OAuthToken"]] = relationship(
        "OAuthToken", back_populates="user", lazy="dynamic",
    )
    relations: Mapped[list["UserRelation"]] = relationship(
        "UserRelation",
        foreign_keys="UserRelation.user_id",
        back_populates="user",
        lazy="dynamic",
    )
    related_by: Mapped[list["UserRelation"]] = relationship(
        "UserRelation",
        foreign_keys="UserRelation.target_id",
        back_populates="target",
        lazy="dynamic",
    )

    @property
    def max_friends(self) -> int:
        """Maximum number of friends allowed."""
        return 500 if self.is_supporter else 250

    @property
    def max_blocks(self) -> int:
        """Maximum number of blocks allowed."""
        return (self.max_friends + 4) // 5  # ceil division


class UserStatistics(Base):
    """User statistics per game mode."""

    __tablename__ = "user_statistics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    mode: Mapped[GameMode] = mapped_column(Enum(GameMode), index=True)

    # Ranking
    ranked_score: Mapped[int] = mapped_column(BigInteger, default=0)
    total_score: Mapped[int] = mapped_column(BigInteger, default=0)
    pp: Mapped[float] = mapped_column(default=0.0)
    global_rank: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    country_rank: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Accuracy
    accuracy: Mapped[float] = mapped_column(default=100.0)
    hit_accuracy: Mapped[float] = mapped_column(default=100.0)

    # Play counts
    play_count: Mapped[int] = mapped_column(default=0)
    play_time: Mapped[int] = mapped_column(default=0)  # seconds
    total_hits: Mapped[int] = mapped_column(BigInteger, default=0)
    maximum_combo: Mapped[int] = mapped_column(default=0)
    replays_watched: Mapped[int] = mapped_column(default=0)

    # Grade counts
    grade_ss: Mapped[int] = mapped_column(default=0)
    grade_ssh: Mapped[int] = mapped_column(default=0)
    grade_s: Mapped[int] = mapped_column(default=0)
    grade_sh: Mapped[int] = mapped_column(default=0)
    grade_a: Mapped[int] = mapped_column(default=0)

    # Level
    level: Mapped[int] = mapped_column(default=1)
    level_progress: Mapped[int] = mapped_column(default=0)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="statistics")


class UserRelation(Base):
    """User relationship model (friends and blocks)."""

    __tablename__ = "user_relations"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    target_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    friend: Mapped[bool] = mapped_column(Boolean, default=False)
    foe: Mapped[bool] = mapped_column(Boolean, default=False)  # block

    # Relationships
    user: Mapped["User"] = relationship(
        "User", foreign_keys=[user_id], back_populates="relations",
    )
    target: Mapped["User"] = relationship(
        "User", foreign_keys=[target_id], back_populates="related_by",
    )


class OAuthClient(Base):
    """OAuth2 client application."""

    __tablename__ = "oauth_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    secret: Mapped[str] = mapped_column(String(255))
    redirect: Mapped[str] = mapped_column(Text)  # Comma-separated URIs

    personal_access_client: Mapped[bool] = mapped_column(Boolean, default=False)
    password_client: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    tokens: Mapped[list["OAuthToken"]] = relationship(
        "OAuthToken", back_populates="client", lazy="dynamic",
    )


class OAuthToken(Base):
    """OAuth2 access token."""

    __tablename__ = "oauth_tokens"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True,
    )
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("oauth_clients.id", ondelete="CASCADE"),
    )

    scopes: Mapped[str] = mapped_column(Text, default="")  # Space-separated scopes
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Relationships
    user: Mapped["User | None"] = relationship("User", back_populates="oauth_tokens")
    client: Mapped["OAuthClient"] = relationship("OAuthClient", back_populates="tokens")

    @property
    def scope_list(self) -> list[str]:
        """Get scopes as a list."""
        return self.scopes.split() if self.scopes else []


class UserBeatmapFavorite(Base):
    """User favorite beatmapsets."""

    __tablename__ = "user_beatmap_favorites"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    beatmapset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("beatmapsets.id", ondelete="CASCADE"), primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )


class UserActivity(Base):
    """User activity log for tracking rank changes and achievements."""

    __tablename__ = "user_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    type: Mapped[str] = mapped_column(String(32), index=True)  # "rank", "achievement", etc.
    beatmap_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("beatmaps.id", ondelete="SET NULL"), nullable=True,
    )
    mode: Mapped[GameMode] = mapped_column(Enum(GameMode), default=GameMode.OSU)
    data: Mapped[str] = mapped_column(Text, default="{}")  # JSON for additional info
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, default=lambda: datetime.now(UTC),
    )


class KudosuHistory(Base):
    """Kudosu voting history."""

    __tablename__ = "kudosu_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    beatmap_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("beatmaps.id", ondelete="SET NULL"), nullable=True,
    )
    action: Mapped[str] = mapped_column(String(16))  # "give", "reset", "deny"
    amount: Mapped[int] = mapped_column(Integer)  # Can be negative for reset/deny
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Discussion post ID
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, default=lambda: datetime.now(UTC),
    )
