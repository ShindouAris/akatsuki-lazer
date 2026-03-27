"""Server-side PP calculation service."""

import logging
import math
from dataclasses import dataclass
from typing import Any

from app.models.user import GameMode

try:
    import rosu_pp_py as rosu_pp

    NEW_PP_ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional native dependency
    rosu_pp = None
    NEW_PP_ENGINE_AVAILABLE = False


logger = logging.getLogger(__name__)


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
    clock_rate: float | None = None
    combo: int | None = None
    accuracy: float | None = None
    n300: int | None = None
    n100: int | None = None
    n50: int | None = None
    ngeki: int | None = None
    nkatu: int | None = None
    nmiss: int | None = None


class PPService:
    """PP calculation service using the rosu PP engine."""

    def __init__(self) -> None:
        if not NEW_PP_ENGINE_AVAILABLE:
            raise RuntimeError("rosu-pp-py is not installed")

    def calculate_pp(self, osu_file_path: str, params: PPCalculationParams) -> dict[str, float | None]:
        """Calculate PP and core difficulty metrics from a local .osu file."""
        new_result = self._caculate(osu_file_path=osu_file_path, params=params)
        if new_result is None:
            raise RuntimeError("PP calculation failed with rosu engine")

        return new_result

    def _caculate(
        self,
        osu_file_path: str,
        params: PPCalculationParams,
    ) -> dict[str, float | None] | None:
        if not NEW_PP_ENGINE_AVAILABLE or rosu_pp is None:
            return None

        try:
            beatmap = rosu_pp.Beatmap(path=osu_file_path)
            if beatmap.is_suspicious():
                logger.warning("Beatmap %s flagged as suspicious, skipping new PP engine", osu_file_path)
                return None

            target_mode = _to_rosu_mode(params.mode)
            if target_mode is not None and beatmap.mode != target_mode:
                beatmap.convert(target_mode, params.mods)

            performance_kwargs: dict[str, Any] = {
                "mods": params.mods,
                "lazer": True,
                "hitresult_priority": rosu_pp.HitResultPriority.Fastest,
            }

            clock_rate = _normalize_clock_rate(params.clock_rate)
            if clock_rate is not None:
                performance_kwargs["clock_rate"] = clock_rate

            accuracy = _normalize_accuracy(params.accuracy)
            if accuracy is not None:
                performance_kwargs["accuracy"] = accuracy

            combo = _non_negative_int(params.combo)
            if combo is not None:
                performance_kwargs["combo"] = combo

            n300 = _non_negative_int(params.n300)
            if n300 is not None:
                performance_kwargs["n300"] = n300

            n100 = _non_negative_int(params.n100)
            if n100 is not None:
                performance_kwargs["n100"] = n100

            n50 = _non_negative_int(params.n50)
            if n50 is not None:
                performance_kwargs["n50"] = n50

            ngeki = _non_negative_int(params.ngeki)
            if ngeki is not None:
                performance_kwargs["n_geki"] = ngeki

            nkatu = _non_negative_int(params.nkatu)
            if nkatu is not None:
                performance_kwargs["n_katu"] = nkatu

            nmiss = _non_negative_int(params.nmiss)
            if nmiss is not None:
                performance_kwargs["misses"] = nmiss

            performance = rosu_pp.Performance(**performance_kwargs)
            result = performance.calculate(beatmap)
            return _build_result(result)
        except Exception as exc:  # pragma: no cover - defensive fallback path
            logger.warning("New PP engine exception: %s", exc)
            return None

    def calculate_for_score(self, osu_file_path: str, score_payload: dict[str, Any]) -> dict[str, float | None]:
        """Calculate PP from a score payload produced during score submission."""
        stats = score_payload.get("statistics", {})
        clock_rate = _extract_clock_rate_from_mods(score_payload.get("mods"))
        params = PPCalculationParams(
            mode=GameMode(score_payload.get("ruleset_id", 0)),
            mods=score_payload.get("mods_bitwise", 0),
            clock_rate=clock_rate,
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


def _build_result(result: Any) -> dict[str, float | None]:
    difficulty = getattr(result, "difficulty", None)
    pp = _coerce_finite_float(getattr(result, "pp", 0.0), default=0.0)
    stars = _coerce_finite_float(getattr(difficulty, "stars", 0.0), default=0.0)

    return {
        "pp": round(pp, 5),
        "stars": round(stars, 5),
        "pp_aim": _safe_float(getattr(result, "pp_aim", None)),
        "pp_speed": _safe_float(getattr(result, "pp_speed", None)),
        "pp_acc": _safe_float(getattr(result, "pp_accuracy", None)),
        "pp_flashlight": _safe_float(getattr(result, "pp_flashlight", None)),
        "effective_miss_count": _safe_float(getattr(result, "effective_miss_count", None)),
        "pp_difficulty": _safe_float(getattr(result, "pp_difficulty", None)),
        "aim": _safe_float(getattr(difficulty, "aim", None)),
        "speed": _safe_float(getattr(difficulty, "speed", None)),
        "flashlight": _safe_float(getattr(difficulty, "flashlight", None)),
    }


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
    number = _coerce_finite_float(value)
    if number is None:
        return None
    return round(number, 5)


def _coerce_finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default

    if math.isnan(number) or math.isinf(number):
        return default

    return number


def _normalize_accuracy(accuracy: float | None) -> float | None:
    if accuracy is None:
        return None

    normalized = _coerce_finite_float(accuracy)
    if normalized is None or normalized < 0:
        return None

    if normalized <= 1.0:
        normalized *= 100.0

    return min(normalized, 100.0)


def _normalize_clock_rate(clock_rate: float | None) -> float | None:
    if clock_rate is None:
        return None

    normalized = _coerce_finite_float(clock_rate)
    if normalized is None or normalized <= 0:
        return None

    return min(max(normalized, 0.01), 100.0)


def _extract_clock_rate_from_mods(mods: Any) -> float | None:
    if not isinstance(mods, list):
        return None

    speed_change_mods = {"HT", "DC", "NC", "DT"}

    for mod in mods:
        if not isinstance(mod, dict):
            continue

        acronym = str(mod.get("acronym", "")).upper()
        if acronym not in speed_change_mods:
            continue

        settings = mod.get("settings")
        if not isinstance(settings, dict):
            continue

        speed_change = _coerce_finite_float(settings.get("speed_change"))
        if speed_change is None or speed_change <= 0:
            continue

        return speed_change

    return None


def _non_negative_int(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 0:
        return None
    return value


def _to_rosu_mode(mode: GameMode) -> Any | None:
    if rosu_pp is None:
        return None

    if mode == GameMode.OSU:
        return rosu_pp.GameMode.Osu
    if mode == GameMode.TAIKO:
        return rosu_pp.GameMode.Taiko
    if mode == GameMode.CATCH:
        return rosu_pp.GameMode.Catch
    if mode == GameMode.MANIA:
        return rosu_pp.GameMode.Mania

    return None
