"""PP service safety guard tests."""

from types import SimpleNamespace

from app.services.pp import _build_result
from app.services.pp import _extract_clock_rate_from_mods
from app.services.pp import _normalize_clock_rate
from app.services.pp import _normalize_accuracy


def test_normalize_accuracy_fraction_to_percent() -> None:
    assert _normalize_accuracy(0.985) == 98.5


def test_normalize_accuracy_keeps_percent_input() -> None:
    assert _normalize_accuracy(98.5) == 98.5


def test_normalize_accuracy_rejects_invalid_values() -> None:
    assert _normalize_accuracy(-1.0) is None
    assert _normalize_accuracy(None) is None


def test_extract_clock_rate_from_mod_settings() -> None:
    mods = [
        {"acronym": "HD", "settings": {}},
        {"acronym": "DT", "settings": {"speed_change": 1.42}},
    ]

    assert _extract_clock_rate_from_mods(mods) == 1.42


def test_extract_clock_rate_ignores_invalid_settings() -> None:
    mods = [
        {"acronym": "NC", "settings": {"speed_change": -1.0}},
        {"acronym": "HT", "settings": {"speed_change": 0}},
    ]

    assert _extract_clock_rate_from_mods(mods) is None


def test_normalize_clock_rate_bounds() -> None:
    assert _normalize_clock_rate(None) is None
    assert _normalize_clock_rate(-1.0) is None
    assert _normalize_clock_rate(0.001) == 0.01
    assert _normalize_clock_rate(200.0) == 100.0


def test_build_new_engine_result_maps_accuracy_key() -> None:
    difficulty = SimpleNamespace(stars=6.789, aim=3.123, speed=2.456, flashlight=0.789)
    result = SimpleNamespace(
        pp=123.4567,
        difficulty=difficulty,
        pp_aim=50.1234,
        pp_speed=40.5678,
        pp_accuracy=20.9999,
        pp_flashlight=5.1111,
        effective_miss_count=1.2345,
        pp_difficulty=80.5432,
    )

    mapped = _build_result(result)

    assert mapped["pp"] == 123.4567
    assert mapped["stars"] == 6.789
    assert mapped["pp_aim"] == 50.1234
    assert mapped["pp_speed"] == 40.5678
    assert mapped["pp_acc"] == 20.9999
    assert mapped["pp_flashlight"] == 5.1111
    assert mapped["effective_miss_count"] == 1.2345
    assert mapped["pp_difficulty"] == 80.5432
    assert mapped["aim"] == 3.123
    assert mapped["speed"] == 2.456
    assert mapped["flashlight"] == 0.789
