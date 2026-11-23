"""Base classes / helpers for seeders."""

from __future__ import annotations

import logging
import random
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Seeder(ABC):
    """Abstract base class for all seeders.

    Subclasses implement :meth:`run` to populate the database.
    The runner is responsible for opening sessions / committing.
    """

    name: str = ""

    @abstractmethod
    async def run(self, session: AsyncSession) -> None:
        """Run the seeder against the given session."""

    def info(self, msg: str) -> None:
        logger.info("[%s] %s", self.name or self.__class__.__name__, msg)

    def warn(self, msg: str) -> None:
        logger.warning("[%s] %s", self.name or self.__class__.__name__, msg)

    def error(self, msg: str) -> None:
        logger.error("[%s] %s", self.name or self.__class__.__name__, msg)


def pick_random(items: Sequence[T]) -> T:
    """Pick a single random element. Mirrors PHP ``array_rand_val``."""
    return random.choice(list(items))


def random_int(low: int, high: int) -> int:
    """Inclusive random int (mirrors PHP ``rand``)."""
    return random.randint(low, high)


def biased_number_between(low: int, high: int) -> int:
    """Loosely emulate Faker's ``biasedNumberBetween`` (skewed toward low)."""
    # Triangular distribution skewed toward `low`
    return int(random.triangular(low, high, low))


def chance(percent: int) -> bool:
    """Return True with ``percent`` probability."""
    return random.randint(1, 100) <= percent
