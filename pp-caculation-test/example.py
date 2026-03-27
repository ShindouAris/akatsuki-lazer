"""
Example usage of osu_pp Rust module from Python.

Build first:
    pip install maturin
    cd osu_pp
    maturin develop --release
"""

import osu_pp

# ─────────────────────────────────────────────
# osu!standard
# ─────────────────────────────────────────────
pp = osu_pp.calculate_osu(
    aim_difficulty=3.5,
    speed_difficulty=3.2,
    flashlight_difficulty=0.0,
    od=9.0,
    ar=10.0,
    slider_factor=0.95,
    speed_note_count=180.0,
    hit_circle_count=800,
    slider_count=200,
    spinner_count=2,
    max_combo=1200,
    count_great=980,
    count_ok=15,
    count_meh=3,
    count_miss=2,
    combo=1180,
    no_fail=False,
    spun_out=False,
    hidden=False,
    flashlight=False,
    blinds=False,
    relax=False,
    autopilot=False,
)
print(f"osu!standard PP: {pp:.2f}")

# ─────────────────────────────────────────────
# osu!mania
# ─────────────────────────────────────────────
pp = osu_pp.calculate_mania(
    star_rating=4.2,
    count_perfect=850,   # 320
    count_great=120,     # 300
    count_good=20,       # 200
    count_ok=8,          # 100
    count_meh=2,         # 50
    count_miss=0,
    no_fail=False,
    easy=False,
)
print(f"osu!mania PP:     {pp:.2f}")

# ─────────────────────────────────────────────
# osu!taiko
# ─────────────────────────────────────────────
pp = osu_pp.calculate_taiko(
    star_rating=5.1,
    great_hit_window=35.0,   # ms, depends on OD
    consistency_factor=0.85, # from DifficultyAttributes
    count_great=1100,
    count_ok=40,
    count_miss=3,
    hidden=False,
    flashlight=False,
    no_fail=False,
)
print(f"osu!taiko PP:     {pp:.2f}")
