/// osu!standard Performance Calculator
/// Based on OsuPerformanceCalculator.cs
///
/// total = (aim^1.1 + speed^1.1 + accuracy^1.1 + flashlight^1.1)^(1/1.1) * multiplier

pub struct OsuScore {
    pub aim_difficulty: f64,
    pub speed_difficulty: f64,
    pub flashlight_difficulty: f64,
    pub od: f64,
    pub ar: f64,
    pub slider_factor: f64,
    pub speed_note_count: f64,
    pub hit_circle_count: usize,
    pub slider_count: usize,
    pub spinner_count: usize,
    pub max_combo: usize,

    // Score stats
    pub count_great: usize,
    pub count_ok: usize,
    pub count_meh: usize,
    pub count_miss: usize,
    pub combo: usize,

    // Mods (flags)
    pub no_fail: bool,
    pub spun_out: bool,
    pub hidden: bool,
    pub flashlight: bool,
    pub blinds: bool,
    pub relax: bool,
    pub autopilot: bool,
}

impl OsuScore {
    fn total_hits(&self) -> usize {
        self.count_great + self.count_ok + self.count_meh + self.count_miss
    }

    fn accuracy(&self) -> f64 {
        let total = self.total_hits();
        if total == 0 {
            return 0.0;
        }
        let numerator = (self.count_great * 6 + self.count_ok * 2 + self.count_meh) as f64;
        let denominator = (total * 6) as f64;
        numerator / denominator
    }

    /// Effective miss count: max of actual misses and combo-based estimate
    fn effective_miss_count(&self) -> f64 {
        let miss_count = self.count_miss as f64;
        let combo_based = self.combo_based_miss_estimate();
        miss_count.max(combo_based)
    }

    /// Simple combo-based miss estimate
    fn combo_based_miss_estimate(&self) -> f64 {
        if self.max_combo == 0 {
            return 0.0;
        }
        let combo_ratio = self.combo as f64 / self.max_combo as f64;
        // If combo is less than max, estimate some misses
        if combo_ratio >= 1.0 {
            0.0
        } else {
            ((1.0 - combo_ratio) * self.total_hits() as f64).max(self.count_miss as f64)
        }
    }

    fn length_bonus(&self) -> f64 {
        let total = self.total_hits() as f64;
        0.95 + 0.4 * (total / 2000.0).min(1.0)
            + if total > 2000.0 {
                (total / 2000.0).log10() * 0.5
            } else {
                0.0
            }
    }

    fn miss_penalty(miss_count: f64, difficulty: f64) -> f64 {
        0.96 / ((miss_count / (2.0 * difficulty.sqrt())) + 1.0)
    }

    fn difficulty_to_performance(d: f64) -> f64 {
        let base = (5.0 * (d / 0.0675).max(1.0) - 4.0).powi(3);
        base / 100_000.0
    }

    fn compute_aim(&self) -> f64 {
        if self.autopilot {
            return 0.0;
        }

        let effective_miss = self.effective_miss_count();
        let mut aim_value = Self::difficulty_to_performance(self.aim_difficulty);

        // Slider nerf
        let estimated_slider_ends_dropped = (self.slider_factor
            * (1.0 - self.accuracy())
            * self.slider_count as f64)
            .min(effective_miss);
        let slider_nerf_factor = if self.slider_count > 0 {
            1.0 - (1.0 - self.slider_factor)
                * (estimated_slider_ends_dropped / self.slider_count as f64).powf(0.75)
        } else {
            1.0
        };
        aim_value *= slider_nerf_factor;

        // Length bonus
        aim_value *= self.length_bonus();

        // Miss penalty
        if effective_miss > 0.0 {
            aim_value *= Self::miss_penalty(effective_miss, self.aim_difficulty);
        }

        // Combo scaling
        if self.max_combo > 0 {
            let combo_ratio = (self.combo as f64 / self.max_combo as f64).powf(0.8);
            aim_value *= combo_ratio;
        }

        // AR bonus
        let ar_factor = if self.ar > 10.33 {
            0.3 * (self.ar - 10.33)
        } else if self.ar < 8.0 {
            0.05 * (8.0 - self.ar)
        } else {
            0.0
        };
        aim_value *= 1.0 + ar_factor;

        // Hidden bonus
        if self.hidden {
            aim_value *= 1.0 + 0.04 * (12.0 - self.ar);
        }

        // Blinds/Traceable visibility mod
        if self.blinds {
            aim_value *= 1.3 + self.total_hits() as f64 * 0.00022;
        }

        // Scale by accuracy
        aim_value *= self.accuracy();

        aim_value
    }

    fn compute_speed(&self) -> f64 {
        if self.relax {
            return 0.0;
        }

        let effective_miss = self.effective_miss_count();
        let mut speed_value = Self::difficulty_to_performance(self.speed_difficulty);

        // Length bonus
        speed_value *= self.length_bonus();

        // Miss penalty
        if effective_miss > 0.0 {
            speed_value *= Self::miss_penalty(effective_miss, self.speed_difficulty);
        }

        // Combo scaling
        if self.max_combo > 0 {
            let combo_ratio = (self.combo as f64 / self.max_combo as f64).powf(0.8);
            speed_value *= combo_ratio;
        }

        // AR bonus
        let ar_factor = if self.ar > 10.33 {
            0.3 * (self.ar - 10.33)
        } else {
            0.0
        };
        speed_value *= 1.0 + ar_factor;

        // Hidden bonus
        if self.hidden {
            speed_value *= 1.0 + 0.04 * (12.0 - self.ar);
        }

        // OD scaling + speed-note accuracy
        let od_factor = 0.95 + 0.4 * (self.od / 11.0).powi(2);
        let speed_accuracy = if self.speed_note_count > 0.0 {
            let relevant_acc_hits = (self.count_great as f64 + self.count_ok as f64 * 0.3
                - self.count_miss as f64)
                .min(self.speed_note_count);
            (relevant_acc_hits / self.speed_note_count).max(0.0)
        } else {
            self.accuracy()
        };

        speed_value *= od_factor;
        speed_value *= (speed_accuracy * 1.1).powf(8.0).max(0.0);

        speed_value
    }

    fn compute_accuracy(&self) -> f64 {
        if self.relax {
            return 0.0;
        }

        let better_acc_objects = self.hit_circle_count as f64;
        if better_acc_objects == 0.0 {
            return 0.0;
        }

        // "Better accuracy" over hit circles
        let better_acc_hits =
            (self.count_great as f64 + self.count_ok as f64 * 0.3333).min(better_acc_objects);
        let better_accuracy = (better_acc_hits / better_acc_objects).max(0.0);

        let mut acc_value =
            1.52163_f64.powf(self.od) * better_accuracy.powf(24.0) * 2.83;

        // Length bonus based on circle count
        acc_value *= (1.0 + (better_acc_objects / 1000.0).min(1.0) * 0.3).min(1.15);

        // Hidden bonus
        if self.hidden {
            acc_value *= 1.08;
        }
        // Flashlight bonus
        if self.flashlight {
            acc_value *= 1.02;
        }

        acc_value
    }

    fn compute_flashlight(&self) -> f64 {
        if !self.flashlight {
            return 0.0;
        }

        let effective_miss = self.effective_miss_count();
        // Flashlight: 25 * d^2
        let mut fl_value = 25.0 * self.flashlight_difficulty.powi(2);

        // Miss penalty
        if effective_miss > 0.0 {
            fl_value *= Self::miss_penalty(effective_miss, self.flashlight_difficulty);
        }

        // Combo scaling
        if self.max_combo > 0 {
            let combo_ratio = (self.combo as f64 / self.max_combo as f64).powf(0.8);
            fl_value *= combo_ratio;
        }

        // Scale by accuracy
        fl_value *= 0.7 + 0.1 * self.accuracy().min(1.0)
            + if self.accuracy() > 0.8 {
                (self.accuracy() - 0.8) * 2.0
            } else {
                0.0
            };

        fl_value
    }

    pub fn calculate(&self) -> f64 {
        let aim = self.compute_aim();
        let speed = self.compute_speed();
        let accuracy = self.compute_accuracy();
        let flashlight = self.compute_flashlight();

        let mut multiplier = 1.14_f64;

        // NoFail nerf
        if self.no_fail {
            let effective_miss = self.effective_miss_count();
            multiplier *= (1.0 - 0.02 * effective_miss).max(0.9);
        }

        // SpunOut nerf
        if self.spun_out && self.total_hits() > 0 {
            let spinner_ratio = self.spinner_count as f64 / self.total_hits() as f64;
            multiplier *= 1.0 - 0.02_f64.powf(spinner_ratio);
        }

        let base = (aim.powf(1.1) + speed.powf(1.1) + accuracy.powf(1.1) + flashlight.powf(1.1))
            .powf(1.0 / 1.1);

        base * multiplier
    }
}
