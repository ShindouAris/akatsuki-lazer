"""Legacy support tables required by the C# hub layer."""

from datetime import UTC
from datetime import datetime

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import LargeBinary
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class OsuBuild(Base):
    """Client build metadata used by the version checker."""

    __tablename__ = "osu_builds"

    build_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str | None] = mapped_column(Text, nullable=True)
    hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    users: Mapped[int] = mapped_column(Integer, default=0)
    allow_bancho: Mapped[bool] = mapped_column(Boolean, default=False)


class ChatFilter(Base):
    """Chat filtering rules used by the hub layer."""

    __tablename__ = "chat_filters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match: Mapped[str] = mapped_column(Text)
    replacement: Mapped[str] = mapped_column(Text)
    block: Mapped[bool] = mapped_column(Boolean, default=False)
    whitespace_delimited: Mapped[bool] = mapped_column(Boolean, default=False)


class MatchmakingPool(Base):
    """Matchmaking pool definition."""

    __tablename__ = "matchmaking_pools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ruleset_id: Mapped[int] = mapped_column(Integer)
    variant_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32), default="quick_play")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    lobby_size: Mapped[int] = mapped_column(Integer, default=2)
    rating_search_radius: Mapped[int] = mapped_column(Integer, default=0)
    rating_search_radius_max: Mapped[int] = mapped_column(Integer, default=9999)
    rating_search_radius_exp: Mapped[int] = mapped_column(Integer, default=0)


class MatchmakingPoolBeatmap(Base):
    """Beatmap assignment inside a matchmaking pool."""

    __tablename__ = "matchmaking_pool_beatmaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("matchmaking_pools.id", ondelete="CASCADE"),
        index=True,
    )
    beatmap_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("beatmaps.id", ondelete="CASCADE"),
        index=True,
    )
    mods: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selection_count: Mapped[int] = mapped_column(Integer, default=0)


class MatchmakingUserStats(Base):
    """Per-pool matchmaking statistics for a user."""

    __tablename__ = "matchmaking_user_stats"
    __table_args__ = (UniqueConstraint("user_id", "pool_id", name="uq_matchmaking_user_stats_user_pool"),)

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    pool_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("matchmaking_pools.id", ondelete="CASCADE"),
        primary_key=True,
    )
    first_placements: Mapped[int] = mapped_column(Integer, default=0)
    total_points: Mapped[int] = mapped_column(Integer, default=0)
    elo_data: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class MatchmakingUserEloHistory(Base):
    """Elo history entries for matchmaking results."""

    __tablename__ = "matchmaking_user_elo_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger)
    pool_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("matchmaking_pools.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    opponent_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    result: Mapped[str] = mapped_column(String(32))
    elo_before: Mapped[int] = mapped_column(Integer)
    elo_after: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ScoreProcessHistory(Base):
    """Marks score submissions that have been fully processed."""

    __tablename__ = "score_process_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    score_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("scores.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )