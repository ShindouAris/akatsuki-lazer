"""Medals (achievements): the catalogue and who earned what."""

from datetime import UTC
from datetime import datetime

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class Medal(Base):
    """A medal that can be earned. Ids follow osu-web's ``osu_achievements`` where the medal exists there."""

    __tablename__ = "medals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(255), default="")
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    grouping: Mapped[str] = mapped_column(String(50), default="")
    ordering: Mapped[int] = mapped_column(Integer, default=0)
    # Ruleset the medal belongs to; ``None`` when it can be earned in any of them.
    mode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class UserMedal(Base):
    """A medal a user earned (once)."""

    __tablename__ = "user_medals"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    medal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("medals.id", ondelete="CASCADE"), primary_key=True,
    )
    # The beatmap of the play that earned it, when there was one.
    beatmap_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("beatmaps.id", ondelete="SET NULL"), nullable=True,
    )
    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )
