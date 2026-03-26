"""Server-side PP calculation service."""

import math
from dataclasses import dataclass
from typing import Any

from app.models.user import GameMode

try:
    from akatsuki_pp_py import Beatmap
    from akatsuki_pp_py import Calculator

    PP_ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional native dependency
    Beatmap = None
    Calculator = None
    PP_ENGINE_AVAILABLE = False


_MOD_BIT_VALUES: dict[str, int] = {
    "NF": 1 << 0,
    "EZ": 1 << 1,
    "TD": 1 << 2,
    "HD": 1 << 3,
    "HR": 1 << 4,
    "SD": 1 << 5,
    "DT": 1 << 6,
    "RX": 1 << 7,
    "HT": 1 << 8,
    "NC": 1 << 9,
    "FL": 1 << 10,
    "AT": 1 << 11,
    "SO": 1 << 12,
    "AP": 1 << 13,
    "PF": 1 << 14,
    "4K": 1 << 15,
    "5K": 1 << 16,
    "6K": 1 << 17,
    "7K": 1 << 18,
    "8K": 1 << 19,
    "FI": 1 << 20,
    "RD": 1 << 21,
    "CN": 1 << 22,
    "TP": 1 << 23,
    "9K": 1 << 24,
    "CO": 1 << 25,
    "1K": 1 << 26,
    "3K": 1 << 27,
    "2K": 1 << 28,
    "V2": 1 << 29,
    "MR": 1 << 30,
}


@dataclass(slots=True)
class PPCalculationParams:
    """Parameters accepted by PP calculator."""

    mode: GameMode
    mods: int = 0
    combo: int | None = None
    accuracy: float | None = None
    n300: int | None = None
    n100: int | None = None
    n50: int | None = None
    ngeki: int | None = None
    nkatu: int | None = None
    nmiss: int | None = None


class PPService:
    """Wrapper around akatsuki-pp-py for score and ad-hoc PP calculation."""

    def __init__(self) -> None:
        if not PP_ENGINE_AVAILABLE:
            raise RuntimeError("akatsuki-pp-py is not installed")

    def calculate_pp(self, osu_file_path: str, params: PPCalculationParams) -> dict[str, float | None]:
        """Calculate PP and core difficulty metrics from a local .osu file."""
        if Beatmap is None or Calculator is None:
            raise RuntimeError("PP engine unavailable")

        beatmap = Beatmap(path=osu_file_path)
        mods = params.mods

        # NC implies DT in rosu-pp bitflags.
        if mods & _MOD_BIT_VALUES["NC"]:
            mods |= _MOD_BIT_VALUES["DT"]

        calculator = Calculator(
            mode=int(params.mode),
            mods=mods,
            combo=params.combo,
            acc=params.accuracy,
            n300=params.n300,
            n100=params.n100,
            n50=params.n50,
            n_geki=params.ngeki,
            n_katu=params.nkatu,
            n_misses=params.nmiss,
        )

        result = calculator.performance(beatmap)
        pp = float(result.pp)
        if math.isnan(pp) or math.isinf(pp):
            pp = 0.0

        stars = float(result.difficulty.stars)
        return {
            "pp": round(pp, 5),
            "stars": round(stars, 5),
            "pp_aim": _safe_float(getattr(result, "pp_aim", None)),
            "pp_speed": _safe_float(getattr(result, "pp_speed", None)),
            "pp_acc": _safe_float(getattr(result, "pp_acc", None)),
            "pp_flashlight": _safe_float(getattr(result, "pp_flashlight", None)),
            "effective_miss_count": _safe_float(getattr(result, "effective_miss_count", None)),
            "pp_difficulty": _safe_float(getattr(result, "pp_difficulty", None)),
            "aim": _safe_float(getattr(result.difficulty, "aim", None)),
            "speed": _safe_float(getattr(result.difficulty, "speed", None)),
            "flashlight": _safe_float(getattr(result.difficulty, "flashlight", None)),
        }

    def calculate_for_score(self, osu_file_path: str, score_payload: dict[str, Any]) -> dict[str, float | None]:
        """Calculate PP from a score payload produced during score submission."""
        stats = score_payload.get("statistics", {})
        params = PPCalculationParams(
            mode=GameMode(score_payload.get("ruleset_id", 0)),
            mods=score_payload.get("mods_bitwise", 0),
            combo=score_payload.get("max_combo"),
            accuracy=score_payload.get("accuracy"),
            n300=_pick_stat(stats, "count_300", "great"),
            n100=_pick_stat(stats, "count_100", "ok"),
            n50=_pick_stat(stats, "count_50", "meh"),
            ngeki=_pick_stat(stats, "count_geki", "perfect"),
            nkatu=_pick_stat(stats, "count_katu", "good"),
            nmiss=_pick_stat(stats, "count_miss", "miss"),
        )
        return self.calculate_pp(osu_file_path, params)


def mods_to_bitwise(mods: list[dict[str, Any]]) -> int:
    """Convert lazer mod objects to legacy bitwise representation."""
    bitwise = 0
    for mod in mods:
        acronym = str(mod.get("acronym", "")).upper()
        bitwise |= _MOD_BIT_VALUES.get(acronym, 0)
    return bitwise


def _pick_stat(stats: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = stats.get(key)
        if isinstance(value, int):
            return value
    return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, 5)
