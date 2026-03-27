"""Server-side PP calculation service."""

import importlib
import logging
import math
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.models.user import GameMode

try:
    from akatsuki_pp_py import Beatmap
    from akatsuki_pp_py import Calculator

    LEGACY_PP_ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional native dependency
    Beatmap = None
    Calculator = None
    LEGACY_PP_ENGINE_AVAILABLE = False

try:
    osu_pp = importlib.import_module("osu_pp")
    NEW_PP_ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on optional native dependency
    osu_pp = None
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
    combo: int | None = None
    accuracy: float | None = None
    n300: int | None = None
    n100: int | None = None
    n50: int | None = None
    ngeki: int | None = None
    nkatu: int | None = None
    nmiss: int | None = None


class PPService:
    """PP calculation service with optional hybrid engine migration support."""

    def __init__(self) -> None:
        if not LEGACY_PP_ENGINE_AVAILABLE:
            raise RuntimeError("akatsuki-pp-py is not installed")
        self._settings = get_settings()
        self._new_engine_modes = _parse_new_engine_modes(self._settings.pp_new_engine_modes)

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
        legacy_result = _build_legacy_result(result)
        legacy_pp = legacy_result.get("pp") or 0.0

        if self._should_use_new_engine(params.mode):
            if not _has_sufficient_score_stats_for_new_engine(params.mode, params):
                if not self._settings.pp_new_engine_fallback_legacy:
                    raise RuntimeError("New PP engine requires hit statistics for this mode")

                logger.warning(
                    "Insufficient hit statistics for new PP engine in mode %s, falling back to legacy",
                    params.mode,
                )
                return legacy_result

            new_pp = self._calculate_with_new_engine(result=result, params=params)
            if new_pp is not None:
                if _is_new_pp_unreasonable(new_pp=new_pp, legacy_pp=float(legacy_pp)):
                    if not self._settings.pp_new_engine_fallback_legacy:
                        raise RuntimeError("New PP engine output is outside safety bounds")

                    logger.warning(
                        "New PP engine produced suspicious value %.5f vs legacy %.5f in mode %s; fallback",
                        new_pp,
                        legacy_pp,
                        params.mode,
                    )
                    return legacy_result

                return _build_compatible_result(new_pp, result.difficulty)

            if not self._settings.pp_new_engine_fallback_legacy:
                raise RuntimeError("New PP engine selected but calculation failed")

            logger.warning(
                "New PP engine failed for mode %s, falling back to legacy engine",
                params.mode,
            )

        return legacy_result

    def _should_use_new_engine(self, mode: GameMode) -> bool:
        strategy = self._settings.pp_engine_strategy

        if strategy == "legacy":
            return False

        if strategy == "hybrid":
            return mode in self._new_engine_modes

        # "new" strategy still obeys explicit mode list, allowing controlled rollout.
        return mode in self._new_engine_modes

    def _calculate_with_new_engine(self, result: Any, params: PPCalculationParams) -> float | None:
        if not NEW_PP_ENGINE_AVAILABLE or osu_pp is None:
            return None

        try:
            if params.mode == GameMode.OSU:
                return _calculate_osu_pp_with_new_engine(result.difficulty, params)

            if params.mode == GameMode.TAIKO:
                return _calculate_taiko_pp_with_new_engine(result.difficulty, params)

            if params.mode == GameMode.MANIA:
                return _calculate_mania_pp_with_new_engine(result.difficulty, params)

            # Catch remains on legacy until dedicated adapter is implemented.
            return None
        except Exception as exc:  # pragma: no cover - defensive fallback path
            logger.warning("New PP engine exception: %s", exc)
            return None

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


def _build_legacy_result(result: Any) -> dict[str, float | None]:
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


def _build_compatible_result(pp: float, difficulty: Any) -> dict[str, float | None]:
    safe_pp = 0.0 if math.isnan(pp) or math.isinf(pp) else pp
    stars = _safe_float(getattr(difficulty, "stars", None)) or 0.0
    return {
        "pp": round(safe_pp, 5),
        "stars": round(stars, 5),
        "pp_aim": None,
        "pp_speed": None,
        "pp_acc": None,
        "pp_flashlight": None,
        "effective_miss_count": None,
        "pp_difficulty": None,
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
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, 5)


def _parse_new_engine_modes(raw: str) -> set[GameMode]:
    if not raw.strip():
        return {GameMode.OSU, GameMode.TAIKO, GameMode.MANIA}

    mode_map = {
        "osu": GameMode.OSU,
        "taiko": GameMode.TAIKO,
        "mania": GameMode.MANIA,
        "catch": GameMode.CATCH,
        "fruits": GameMode.CATCH,
    }

    result: set[GameMode] = set()
    for token in raw.split(","):
        normalized = token.strip().lower()
        mode = mode_map.get(normalized)
        if mode is not None:
            result.add(mode)

    return result


def _has_mod(mods: int, acronym: str) -> bool:
    bit = _MOD_BIT_VALUES.get(acronym, 0)
    return bool(mods & bit)


def _calculate_osu_pp_with_new_engine(difficulty: Any, params: PPCalculationParams) -> float:
    assert osu_pp is not None

    n300 = params.n300 or 0
    n100 = params.n100 or 0
    n50 = params.n50 or 0
    nmiss = params.nmiss or 0
    object_count = n300 + n100 + n50 + nmiss
    max_combo = _difficulty_int(difficulty, "max_combo")
    if max_combo <= 0:
        max_combo = params.combo or object_count

    return float(
        osu_pp.calculate_osu(
            aim_difficulty=_difficulty_float(difficulty, "aim", "aim_difficulty"),
            speed_difficulty=_difficulty_float(difficulty, "speed", "speed_difficulty"),
            flashlight_difficulty=_difficulty_float(difficulty, "flashlight", "flashlight_difficulty"),
            od=_difficulty_float(difficulty, "od", "overall_difficulty"),
            ar=_difficulty_float(difficulty, "ar", "approach_rate"),
            slider_factor=_difficulty_float(difficulty, "slider_factor", default=1.0),
            speed_note_count=_difficulty_float(difficulty, "speed_note_count"),
            hit_circle_count=_difficulty_int(difficulty, "n_circles", default=object_count),
            slider_count=_difficulty_int(difficulty, "n_sliders"),
            spinner_count=_difficulty_int(difficulty, "n_spinners"),
            max_combo=max_combo,
            count_great=n300,
            count_ok=n100,
            count_meh=n50,
            count_miss=nmiss,
            combo=params.combo or max_combo,
            no_fail=_has_mod(params.mods, "NF"),
            spun_out=_has_mod(params.mods, "SO"),
            hidden=_has_mod(params.mods, "HD"),
            flashlight=_has_mod(params.mods, "FL"),
            blinds=False,
            relax=_has_mod(params.mods, "RX"),
            autopilot=_has_mod(params.mods, "AP"),
        ),
    )


def _calculate_mania_pp_with_new_engine(difficulty: Any, params: PPCalculationParams) -> float:
    assert osu_pp is not None

    return float(
        osu_pp.calculate_mania(
            star_rating=_difficulty_float(difficulty, "stars", "star_rating"),
            count_perfect=params.ngeki or 0,
            count_great=params.n300 or 0,
            count_good=params.nkatu or 0,
            count_ok=params.n100 or 0,
            count_meh=params.n50 or 0,
            count_miss=params.nmiss or 0,
            no_fail=_has_mod(params.mods, "NF"),
            easy=_has_mod(params.mods, "EZ"),
        ),
    )


def _calculate_taiko_pp_with_new_engine(difficulty: Any, params: PPCalculationParams) -> float:
    assert osu_pp is not None

    od_value = _difficulty_float(difficulty, "od", "overall_difficulty", default=5.0)
    great_hit_window = _difficulty_float(
        difficulty,
        "great_hit_window",
        default=max(20.0, min(50.0, 50.0 - (3.0 * od_value))),
    )

    return float(
        osu_pp.calculate_taiko(
            star_rating=_difficulty_float(difficulty, "stars", "star_rating"),
            great_hit_window=great_hit_window,
            consistency_factor=_difficulty_float(difficulty, "consistency_factor", default=1.0),
            count_great=params.n300 or 0,
            count_ok=params.n100 or 0,
            count_miss=params.nmiss or 0,
            hidden=_has_mod(params.mods, "HD"),
            flashlight=_has_mod(params.mods, "FL"),
            no_fail=_has_mod(params.mods, "NF"),
        ),
    )


def _difficulty_float(difficulty: Any, *names: str, default: float = 0.0) -> float:
    for name in names:
        value = getattr(difficulty, name, None)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _difficulty_int(difficulty: Any, *names: str, default: int = 0) -> int:
    for name in names:
        value = getattr(difficulty, name, None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def _has_sufficient_score_stats_for_new_engine(mode: GameMode, params: PPCalculationParams) -> bool:
    if mode == GameMode.OSU:
        counts = [params.n300, params.n100, params.n50, params.nmiss]
    elif mode == GameMode.TAIKO:
        counts = [params.n300, params.n100, params.nmiss]
    elif mode == GameMode.MANIA:
        counts = [params.ngeki, params.n300, params.nkatu, params.n100, params.n50, params.nmiss]
    else:
        return False

    if all(value is None for value in counts):
        return False

    normalized_counts = [0 if value is None else int(value) for value in counts]
    return sum(normalized_counts) > 0


def _is_new_pp_unreasonable(new_pp: float, legacy_pp: float) -> bool:
    if math.isnan(new_pp) or math.isinf(new_pp) or new_pp <= 0:
        return True

    if legacy_pp <= 0:
        return False

    ratio = new_pp / legacy_pp
    return ratio < 0.2 or ratio > 5.0
