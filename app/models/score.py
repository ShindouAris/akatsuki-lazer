"""Score and score token models."""

from datetime import UTC
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from app.models.beatmap import Beatmap
    from app.models.user import User


class ScoreRank(StrEnum):
    """Score grade/rank (matches official enum values)."""

    XH = "XH"  # Silver SS (with hidden) - official uses X/XH
    X = "X"  # SS - official uses X
    SH = "SH"  # Silver S (with hidden)
    S = "S"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"  # Failed


class Score(Base):
    """Solo score model (matches official osu! solo_scores table).

    The `data` column stores a JSON object with:
    - mods: array of mod objects [{acronym, settings}, ...]
    - statistics: dict of hit results {great, ok, meh, miss, ...}
    - maximum_statistics: dict of max possible hits
    - total_score_without_mods: int
    """

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    beatmap_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("beatmaps.id", ondelete="CASCADE"), index=True,
    )
    ruleset_id: Mapped[int] = mapped_column(Integer, default=0, index=True)  # GameMode as int

    # Score data (combined into single JSON column like official)
    # Contains: mods, statistics, maximum_statistics, total_score_without_mods
    data: Mapped[str] = mapped_column(Text, default="{}")

    # Core score fields (kept separate for efficient queries)
    total_score: Mapped[int] = mapped_column(BigInteger, default=0)
    accuracy: Mapped[float] = mapped_column(default=0.0)
    pp: Mapped[float | None] = mapped_column(nullable=True)
    max_combo: Mapped[int] = mapped_column(default=0)
    rank: Mapped[str] = mapped_column(String(4), default="D")

    # Status flags
    passed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ranked: Mapped[bool] = mapped_column(Boolean, default=True)
    preserve: Mapped[bool] = mapped_column(Boolean, default=False)  # False like official
    has_replay: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    # Unix timestamp for efficient indexing (like official)
    unix_updated_at: Mapped[int] = mapped_column(
        Integer, default=lambda: int(datetime.now(UTC).timestamp()),
    )

    # Build and legacy info
    build_id: Mapped[int | None] = mapped_column(nullable=True)
    legacy_score_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    legacy_total_score: Mapped[int] = mapped_column(BigInteger, default=0)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="scores")
    beatmap: Mapped["Beatmap"] = relationship("Beatmap")


class ScoreToken(Base):
    """Score submission token (matches official solo_score_tokens table).

    Tokens don't expire in the official implementation.
    A token is considered "used" when score_id is not null.
    """

    __tablename__ = "score_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True,
    )
    beatmap_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("beatmaps.id", ondelete="CASCADE"), index=True,
    )
    ruleset_id: Mapped[int] = mapped_column(Integer, default=0)
    build_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    playlist_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("multiplayer_playlist_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Link to score once submitted (presence indicates token is used)
    score_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("scores.id", ondelete="SET NULL"), nullable=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    score: Mapped["Score | None"] = relationship("Score")
