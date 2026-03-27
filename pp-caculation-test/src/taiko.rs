/// osu!taiko Performance Calculator
/// Based on TaikoPerformanceCalculator.cs
///
/// total = difficultyValue * 1.08 + accuracyValue * 1.1

pub struct TaikoScore {
    pub star_rating: f64,
    pub great_hit_window: f64, // OD-based hit window in ms
    pub consistency_factor: f64, // From DifficultyAttributes

    // Hit counts
    pub count_great: usize,
    pub count_ok: usize,
    pub count_miss: usize,

    // Mods
    pub hidden: bool,
    pub flashlight: bool,
    pub no_fail: bool,
}

impl TaikoScore {
    fn total_hits(&self) -> usize {
        self.count_great + self.count_ok + self.count_miss
    }

    fn accuracy(&self) -> f64 {
        let total = self.total_hits();
        if total == 0 {
            return 0.0;
        }
        let numerator = (self.count_great * 2 + self.count_ok) as f64;
        let denominator = (total * 2) as f64;
        numerator / denominator
    }

    /// Estimate unstable rate from great ratio and OD hit window
    /// Lower UR = more consistent = lower penalty
    fn estimated_unstable_rate(&self) -> f64 {
        let total = self.total_hits();
        if total == 0 {
            return f64::INFINITY;
        }
        let great_ratio = self.count_great as f64 / total as f64;
        // Approximation: UR scales inversely with great ratio and hit window
        // Full greats => UR near 0; all OKs => UR near great_hit_window
        (1.0 - great_ratio) * self.great_hit_window * 10.0
    }

    fn compute_difficulty_value(&self) -> f64 {
        let total_hits = self.total_hits() as f64;
        let total_difficult_hits = total_hits * self.consistency_factor;

        // Nonlinear star rating -> PP conversion
        let mut diff_value = (self.star_rating.powf(2.0)) * 5.0;

        // Length bonus from difficult hits
        let length_bonus = (total_difficult_hits / 1500.0).tanh() + 1.0;
        diff_value *= length_bonus;

        // Miss penalty
        let miss_count = self.count_miss as f64;
        if miss_count > 0.0 {
            diff_value *= 0.97_f64.powf(miss_count);
        }

        // Hidden / Flashlight bonuses
        if self.hidden {
            diff_value *= 1.025;
        }
        if self.flashlight {
            diff_value *= 1.05 * length_bonus;
        }

        // Rhythm penalty from estimated unstable rate
        let ur = self.estimated_unstable_rate();
        // Expected UR ~100ms -> no penalty, higher UR increases penalty
        let rhythm_factor = if ur < 100.0 {
            1.0
        } else {
            (1.0 - (ur - 100.0) / 500.0).max(0.5)
        };
        diff_value *= rhythm_factor;

        // Accuracy scaling (mono-color stamina component)
        diff_value *= self.accuracy().powf(2.0);

        diff_value
    }

    fn compute_accuracy_value(&self) -> f64 {
        let ur = self.estimated_unstable_rate();

        // Base term: lower UR => higher accuracy PP
        let mut acc_value = if ur < f64::INFINITY {
            // Sigmoid-like: peaks when UR ~ 0
            150.0 / (1.0 + (ur / 30.0).exp())
        } else {
            0.0
        };

        // Hidden bonus
        if self.hidden {
            acc_value *= 1.1;
        }

        acc_value
    }

    pub fn calculate(&self) -> f64 {
        let diff = self.compute_difficulty_value() * 1.08;
        let acc = self.compute_accuracy_value() * 1.1;

        let mut total = diff + acc;

        // NoFail nerf
        if self.no_fail {
            total *= 0.9;
        }

        total
    }
}
