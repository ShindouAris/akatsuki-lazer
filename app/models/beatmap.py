"""Beatmap and beatmapset models."""

from datetime import UTC
from datetime import datetime
from enum import IntEnum

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
from app.models.user import GameMode


class BeatmapStatus(IntEnum):
    """Beatmap approval/ranked status."""

    GRAVEYARD = -2
    WIP = -1
    PENDING = 0
    RANKED = 1
    APPROVED = 2
    QUALIFIED = 3
    LOVED = 4


# The statuses of maps that count as ranked (they have a leaderboard and award pp).
RANKED_STATUSES = (BeatmapStatus.RANKED, BeatmapStatus.APPROVED)


class BeatmapSet(Base):
    """Beatmap set (collection of difficulties)."""

    __tablename__ = "beatmapsets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    # Metadata
    artist: Mapped[str] = mapped_column(String(255))
    artist_unicode: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    title_unicode: Mapped[str | None] = mapped_column(String(255), nullable=True)
    creator: Mapped[str] = mapped_column(String(255))
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Status
    status: Mapped[BeatmapStatus] = mapped_column(
        Enum(BeatmapStatus), default=BeatmapStatus.PENDING, index=True,
    )
    ranked_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    submitted_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    # Stats
    play_count: Mapped[int] = mapped_column(default=0)
    favourite_count: Mapped[int] = mapped_column(default=0)

    # Media
    preview_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    covers: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    has_video: Mapped[bool] = mapped_column(Boolean, default=False)
    has_storyboard: Mapped[bool] = mapped_column(Boolean, default=False)

    # Settings
    nsfw: Mapped[bool] = mapped_column(Boolean, default=False)
    spotlight: Mapped[bool] = mapped_column(Boolean, default=False)
    discussion_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    download_disabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # BPM
    bpm: Mapped[float] = mapped_column(default=0.0)

    # Classification (ported from osu-web: ``genre_id``/``language_id`` on
    # ``osu_beatmapsets``). FKs intentionally omitted until the Genre/Language
    # models are ported (MiscSeeder).
    genre_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    language_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Aggregate counts over child beatmaps (maintained by BeatmapSeeder).
    versions_available: Mapped[int] = mapped_column(Integer, default=0)
    difficulty_names: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    beatmaps: Mapped[list["Beatmap"]] = relationship(
        "Beatmap", back_populates="beatmapset", lazy="selectin",
    )


class Beatmap(Base):
    """Individual beatmap (single difficulty)."""

    __tablename__ = "beatmaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    beatmapset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("beatmapsets.id", ondelete="CASCADE"), index=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    # Metadata
    version: Mapped[str] = mapped_column(String(255))  # Difficulty name
    checksum: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    # Game mode
    mode: Mapped[GameMode] = mapped_column(Enum(GameMode), index=True)

    # Status (mirrors beatmapset but can differ for loved maps)
    status: Mapped[BeatmapStatus] = mapped_column(
        Enum(BeatmapStatus), default=BeatmapStatus.PENDING, index=True,
    )

    # Difficulty settings
    cs: Mapped[float] = mapped_column(default=5.0)  # Circle Size
    ar: Mapped[float] = mapped_column(default=5.0)  # Approach Rate
    od: Mapped[float] = mapped_column(default=5.0)  # Overall Difficulty
    hp: Mapped[float] = mapped_column(default=5.0)  # HP Drain

    # Calculated difficulty
    difficulty_rating: Mapped[float] = mapped_column(default=0.0, index=True)

    # Map info
    total_length: Mapped[int] = mapped_column(default=0)  # seconds
    hit_length: Mapped[int] = mapped_column(default=0)  # drain time in seconds
    bpm: Mapped[float] = mapped_column(default=0.0)
    count_circles: Mapped[int] = mapped_column(default=0)
    count_sliders: Mapped[int] = mapped_column(default=0)
    count_spinners: Mapped[int] = mapped_column(default=0)

    # Stats
    play_count: Mapped[int] = mapped_column(default=0)
    pass_count: Mapped[int] = mapped_column(default=0)
    max_combo: Mapped[int | None] = mapped_column(nullable=True)

    # Timestamps
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    # Relationships
    beatmapset: Mapped["BeatmapSet"] = relationship(
        "BeatmapSet", back_populates="beatmaps",
    )
    difficulties: Mapped[list["BeatmapDifficulty"]] = relationship(
        "BeatmapDifficulty",
        back_populates="beatmap",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    failtimes: Mapped[list["BeatmapFailtimes"]] = relationship(
        "BeatmapFailtimes",
        back_populates="beatmap",
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class BeatmapDifficulty(Base):
    """Per-ruleset/mod star-rating cache (``osu_beatmap_difficulty``).

    The PHP model uses a composite primary key ``(beatmap_id, mode, mods)``.
    ``mode`` stores the ruleset id (including converts, e.g. an osu! map with
    ``mode=1`` is the taiko convert entry) and ``mods`` is a legacy mod bitmask.
    """

    __tablename__ = "beatmap_difficulties"

    beatmap_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("beatmaps.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mode: Mapped[int] = mapped_column(Integer, primary_key=True)
    mods: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    diff_unified: Mapped[float] = mapped_column(default=0.0)
    last_update: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    beatmap: Mapped["Beatmap"] = relationship("Beatmap", back_populates="difficulties")


class BeatmapFailtimes(Base):
    """Fail/retry histogram for a beatmap (``osu_beatmap_failtimes``).

    Stored as 100 bucketed counters ``p1``..``p100`` keyed by
    ``(beatmap_id, type)`` where ``type`` is either ``fail`` or ``exit``.
    """

    __tablename__ = "beatmap_failtimes"

    beatmap_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("beatmaps.id", ondelete="CASCADE"),
        primary_key=True,
    )
    type: Mapped[str] = mapped_column(String(8), primary_key=True)

    beatmap: Mapped["Beatmap"] = relationship("Beatmap", back_populates="failtimes")

    # 100 fail-time buckets (p1..p100). Declared via locals() to avoid a
    # 100-line copy-paste; equivalent to ``p1: Mapped[int] = mapped_column(...)``.
    for _i in range(1, 101):
        locals()[f"p{_i}"] = mapped_column(Integer, default=0, nullable=False)
    del _i
