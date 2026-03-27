mod osu;
mod mania;
mod taiko;

use pyo3::prelude::*;

// ─────────────────────────────────────────────
//  osu!standard
// ─────────────────────────────────────────────

/// Calculate osu!standard total PP.
///
/// Args:
///   aim_difficulty (float): Aim difficulty from DifficultyAttributes
///   speed_difficulty (float): Speed difficulty
///   flashlight_difficulty (float): Flashlight difficulty
///   od (float): Overall Difficulty (0–11)
///   ar (float): Approach Rate (0–11)
///   slider_factor (float): Slider factor (0–1)
///   speed_note_count (float): Effective speed note count
///   hit_circle_count (int): Number of hit circles
///   slider_count (int): Number of sliders
///   spinner_count (int): Number of spinners
///   max_combo (int): Maximum possible combo
///   count_great (int): Number of 300s
///   count_ok (int): Number of 100s
///   count_meh (int): Number of 50s
///   count_miss (int): Number of misses
///   combo (int): Score combo achieved
///   no_fail (bool): NoFail mod active
///   spun_out (bool): SpunOut mod active
///   hidden (bool): Hidden mod active
///   flashlight (bool): Flashlight mod active
///   blinds (bool): Blinds mod active
///   relax (bool): Relax mod active
///   autopilot (bool): Autopilot mod active
///
/// Returns:
///   float: Total PP
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn calculate_osu(
    aim_difficulty: f64,
    speed_difficulty: f64,
    flashlight_difficulty: f64,
    od: f64,
    ar: f64,
    slider_factor: f64,
    speed_note_count: f64,
    hit_circle_count: usize,
    slider_count: usize,
    spinner_count: usize,
    max_combo: usize,
    count_great: usize,
    count_ok: usize,
    count_meh: usize,
    count_miss: usize,
    combo: usize,
    no_fail: bool,
    spun_out: bool,
    hidden: bool,
    flashlight: bool,
    blinds: bool,
    relax: bool,
    autopilot: bool,
) -> f64 {
    let score = osu::OsuScore {
        aim_difficulty,
        speed_difficulty,
        flashlight_difficulty,
        od,
        ar,
        slider_factor,
        speed_note_count,
        hit_circle_count,
        slider_count,
        spinner_count,
        max_combo,
        count_great,
        count_ok,
        count_meh,
        count_miss,
        combo,
        no_fail,
        spun_out,
        hidden,
        flashlight,
        blinds,
        relax,
        autopilot,
    };
    score.calculate()
}

// ─────────────────────────────────────────────
//  osu!mania
// ─────────────────────────────────────────────

/// Calculate osu!mania total PP.
///
/// Args:
///   star_rating (float): Beatmap star rating
///   count_perfect (int): 320s (MAX)
///   count_great (int): 300s
///   count_good (int): 200s
///   count_ok (int): 100s
///   count_meh (int): 50s
///   count_miss (int): Misses
///   no_fail (bool): NoFail mod active
///   easy (bool): Easy mod active
///
/// Returns:
///   float: Total PP
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn calculate_mania(
    star_rating: f64,
    count_perfect: usize,
    count_great: usize,
    count_good: usize,
    count_ok: usize,
    count_meh: usize,
    count_miss: usize,
    no_fail: bool,
    easy: bool,
) -> f64 {
    let score = mania::ManiaScore {
        star_rating,
        count_perfect,
        count_great,
        count_good,
        count_ok,
        count_meh,
        count_miss,
        no_fail,
        easy,
    };
    score.calculate()
}

// ─────────────────────────────────────────────
//  osu!taiko
// ─────────────────────────────────────────────

/// Calculate osu!taiko total PP.
///
/// Args:
///   star_rating (float): Beatmap star rating
///   great_hit_window (float): Hit window for GREAT in ms (OD-based)
///   consistency_factor (float): ConsistencyFactor from DifficultyAttributes
///   count_great (int): Number of GREATs
///   count_ok (int): Number of OKs
///   count_miss (int): Number of misses
///   hidden (bool): Hidden mod active
///   flashlight (bool): Flashlight mod active
///   no_fail (bool): NoFail mod active
///
/// Returns:
///   float: Total PP
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn calculate_taiko(
    star_rating: f64,
    great_hit_window: f64,
    consistency_factor: f64,
    count_great: usize,
    count_ok: usize,
    count_miss: usize,
    hidden: bool,
    flashlight: bool,
    no_fail: bool,
) -> f64 {
    let score = taiko::TaikoScore {
        star_rating,
        great_hit_window,
        consistency_factor,
        count_great,
        count_ok,
        count_miss,
        hidden,
        flashlight,
        no_fail,
    };
    score.calculate()
}

// ─────────────────────────────────────────────
//  Module registration
// ─────────────────────────────────────────────

/// osu! PP calculator (osu!standard, osu!mania, osu!taiko)
/// Built with Rust + PyO3 for maximum performance.
#[pymodule]
fn osu_pp(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(calculate_osu, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_mania, m)?)?;
    m.add_function(wrap_pyfunction!(calculate_taiko, m)?)?;
    Ok(())
}
