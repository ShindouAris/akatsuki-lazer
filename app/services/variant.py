"""Score variants: the separate boards for vanilla, Relax and Autopilot plays.

Like osuAkatsuki/score-service-rs (``GamemodeMod``), every play belongs to exactly one variant and each variant
has its own scores, statistics, pp and leaderboards:

* ``RELAX`` (RX) for osu!, osu!taiko and osu!catch,
* ``AUTOPILOT`` (AP) for osu! only,
* ``VANILLA`` for everything else.

A Relax mod on a ruleset that has no Relax board (mania) does not move the play anywhere, it stays vanilla.
"""

from collections.abc import Iterable
from enum import IntEnum
from typing import Any

from app.models.user import GameMode


class Variant(IntEnum):
    VANILLA = 0
    RELAX = 1
    AUTOPILOT = 2

    @property
    def label(self) -> str:
        return {Variant.VANILLA: "vanilla", Variant.RELAX: "relax", Variant.AUTOPILOT: "autopilot"}[self]


_RELAX_RULESETS = frozenset({GameMode.OSU, GameMode.TAIKO, GameMode.CATCH})
_AUTOPILOT_RULESETS = frozenset({GameMode.OSU})


def parse_variant(value: str | int | None) -> Variant:
    """Parse the ``variant`` query parameter (``vanilla``/``rx``/``relax``/``ap``/``autopilot`` or a number)."""
    if value is None or value == "":
        return Variant.VANILLA
    if isinstance(value, int):
        return Variant(value)

    text = value.strip().lower()
    names = {
        "vanilla": Variant.VANILLA,
        "nomod": Variant.VANILLA,
        "std": Variant.VANILLA,
        "rx": Variant.RELAX,
        "relax": Variant.RELAX,
        "ap": Variant.AUTOPILOT,
        "autopilot": Variant.AUTOPILOT,
    }
    if text in names:
        return names[text]
    return Variant(int(text))


def variant_for(acronyms: Iterable[str], ruleset_id: int) -> Variant:
    """The board a play with these mod acronyms goes to."""
    mods = {str(a).upper() for a in acronyms}
    try:
        mode = GameMode(ruleset_id)
    except ValueError:
        return Variant.VANILLA

    if "RX" in mods and mode in _RELAX_RULESETS:
        return Variant.RELAX
    if "AP" in mods and mode in _AUTOPILOT_RULESETS:
        return Variant.AUTOPILOT
    return Variant.VANILLA


def variant_for_mods(mods: Iterable[dict[str, Any]] | None, ruleset_id: int) -> Variant:
    """Same as :func:`variant_for` for lazer mod objects (``{"acronym": "RX", ...}``)."""
    return variant_for(
        (str(m.get("acronym", "")) for m in mods or [] if isinstance(m, dict)),
        ruleset_id,
    )


def board_for_request(
    variant: str | int | None,
    mods: str | None,
    default: Variant = Variant.VANILLA,
) -> Variant:
    """The board a leaderboard request asks for.

    An explicit ``variant`` wins. Otherwise the ``mods`` filter decides: asking for RX scores means the Relax
    board, AP the Autopilot board. A request that names no board gets ``default`` (the player's active board).
    """
    if variant is not None and variant != "":
        return parse_variant(variant)

    text = (mods or "").upper().replace(",", " ").replace("+", " ")
    tokens = set(text.split())
    # "HDRX" style strings: read them two letters at a time as well.
    tokens |= {text[i : i + 2] for i in range(0, len(text.replace(" ", "")), 2)}
    if "RX" in tokens:
        return Variant.RELAX
    if "AP" in tokens:
        return Variant.AUTOPILOT
    return default


def supports_variant(mode: GameMode, variant: Variant) -> bool:
    """Whether a board exists for this mode and variant."""
    if variant == Variant.RELAX:
        return mode in _RELAX_RULESETS
    if variant == Variant.AUTOPILOT:
        return mode in _AUTOPILOT_RULESETS
    return True
