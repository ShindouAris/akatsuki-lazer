# osu_pp — Rust PP calculator for Python

Local Performance Point calculator for **osu!standard**, **osu!mania**, and **osu!taiko**,
written in Rust and exposed to Python via **PyO3 + Maturin**.

## Project structure

```
osu_pp/
├── Cargo.toml          # Rust deps
├── pyproject.toml      # Maturin build config
├── example.py          # Python usage example
└── src/
    ├── lib.rs           # PyO3 bindings
    ├── osu.rs           # osu!standard calculator
    ├── mania.rs         # osu!mania calculator
    └── taiko.rs         # osu!taiko calculator
```

## Build & install

```bash
# 1. Install maturin (once)
pip install maturin

# 2. Build + install into current Python env (dev mode)
cd osu_pp
maturin develop --release

# 3. Or build a wheel to distribute
maturin build --release
pip install target/wheels/osu_pp-*.whl
```

> Requires: Rust toolchain (`rustup`) and Python 3.8+

## Usage

```python
import osu_pp

# osu!standard
pp = osu_pp.calculate_osu(
    aim_difficulty=3.5,
    speed_difficulty=3.2,
    flashlight_difficulty=0.0,
    od=9.0, ar=10.0,
    slider_factor=0.95,
    speed_note_count=180.0,
    hit_circle_count=800, slider_count=200, spinner_count=2,
    max_combo=1200,
    count_great=980, count_ok=15, count_meh=3, count_miss=2,
    combo=1180,
    no_fail=False, spun_out=False, hidden=False,
    flashlight=False, blinds=False, relax=False, autopilot=False,
)

# osu!mania
pp = osu_pp.calculate_mania(
    star_rating=4.2,
    count_perfect=850, count_great=120, count_good=20,
    count_ok=8, count_meh=2, count_miss=0,
    no_fail=False, easy=False,
)

# osu!taiko
pp = osu_pp.calculate_taiko(
    star_rating=5.1,
    great_hit_window=35.0,    # ms, from OD
    consistency_factor=0.85,  # from DifficultyAttributes
    count_great=1100, count_ok=40, count_miss=3,
    hidden=False, flashlight=False, no_fail=False,
)
```

## Notes

- **DifficultyAttributes** (star rating, aim/speed difficulty, consistency factor, etc.)
  must be obtained from the game client or your own difficulty calculator.
- Formula constants follow the osu! source as of March 2026.
- Taiko's `estimated_unstable_rate` is an approximation from hit counts and hit window;
  for production use, prefer computing UR from actual hit offsets if available.
