"""PP service safety guard tests."""

from app.models.user import GameMode
from app.services.pp import PPCalculationParams
from app.services.pp import _has_sufficient_score_stats_for_new_engine
from app.services.pp import _is_new_pp_unreasonable


def test_has_sufficient_stats_for_new_engine_with_partial_osu_counts() -> None:
    params = PPCalculationParams(
        mode=GameMode.OSU,
        n300=900,
        n100=30,
        n50=None,
        nmiss=2,
    )

    assert _has_sufficient_score_stats_for_new_engine(GameMode.OSU, params)


def test_has_sufficient_stats_for_new_engine_rejects_missing_counts() -> None:
    params = PPCalculationParams(mode=GameMode.OSU)

    assert not _has_sufficient_score_stats_for_new_engine(GameMode.OSU, params)


def test_new_pp_unreasonable_for_low_ratio() -> None:
    # Mirrors reported symptom: ~41 legacy vs ~0.257 new.
    assert _is_new_pp_unreasonable(new_pp=0.25745, legacy_pp=41.0)


def test_new_pp_unreasonable_accepts_normal_ratio() -> None:
    assert not _is_new_pp_unreasonable(new_pp=40.8, legacy_pp=41.0)
