"""Ranked play / matchmaking enums and helpers.

The ``matchmaking_pools`` table is defined exactly once, by ``app.models.legacy.MatchmakingPool``, which
matches the alembic schema and osu-server-spectator. Declaring it a second time here made importing both
modules fail with "Table 'matchmaking_pools' is already defined", so this module only provides the pieces
that sit on top of that model.
"""

from enum import Enum
from enum import IntEnum

from app.models.legacy import MatchmakingPool


class MatchmakingPoolType(IntEnum):
    quick_play = 0
    ranked_play = 1


class RoundResult(Enum):
    win = 0
    loss = 1
    draw = 2


class Matchmaking_Pool:
    def __init__(
        self, id, ruleset_id, variant, name="", matchmaking_pool_type=MatchmakingPoolType.quick_play,
    ):
        self.id = id
        self.ruleset_id = ruleset_id
        self.variant = variant
        self.name = name
        self.matchmaking_pool_type = matchmaking_pool_type


def to_matchmaking_pool(pool: MatchmakingPool) -> Matchmaking_Pool:
    """Convert a pool row to the protocol-facing description."""
    try:
        pool_type = MatchmakingPoolType[pool.type]
    except KeyError:
        pool_type = MatchmakingPoolType.quick_play

    return Matchmaking_Pool(
        id=pool.id,
        ruleset_id=pool.ruleset_id,
        variant=pool.variant_id,
        name=pool.name,
        matchmaking_pool_type=pool_type,
    )


def pool_display_name(pool: MatchmakingPool) -> str:
    """Human readable pool name, e.g. ``osu!mania 4K (Season 1)``."""
    match pool.ruleset_id:
        case 0:
            return f"osu! ({pool.name})"
        case 1:
            return f"osu!taiko ({pool.name})"
        case 2:
            return f"osu!catch ({pool.name})"
        case 3:
            return f"osu!mania {pool.variant_id}K ({pool.name})"
        case _:
            return pool.name
