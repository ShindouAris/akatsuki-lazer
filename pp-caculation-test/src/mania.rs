/// osu!mania Performance Calculator
/// Based on ManiaPerformanceCalculator.cs
///
/// difficultyValue = 8 * max(starRating - 0.15, 0.05)^2.2
///                 * max(0, 5 * customAccuracy - 4)
///                 * (1 + 0.1 * min(1, totalHits / 1500))
/// total = difficultyValue * multiplier

pub struct ManiaScore {
    pub star_rating: f64,

    // Hit counts
    pub count_perfect: usize, // 320
    pub count_great: usize,   // 300
    pub count_good: usize,    // 200
    pub count_ok: usize,      // 100
    pub count_meh: usize,     // 50
    pub count_miss: usize,

    // Mods
    pub no_fail: bool,
    pub easy: bool,
}

impl ManiaScore {
    fn total_hits(&self) -> usize {
        self.count_perfect
            + self.count_great
            + self.count_good
            + self.count_ok
            + self.count_meh
            + self.count_miss
    }

    /// PP-weighted accuracy (not raw score accuracy)
    fn custom_accuracy(&self) -> f64 {
        let total = self.total_hits();
        if total == 0 {
            return 0.0;
        }
        let numerator = (self.count_perfect * 320
            + self.count_great * 300
            + self.count_good * 200
            + self.count_ok * 100
            + self.count_meh * 50) as f64;
        let denominator = (total * 320) as f64;
        numerator / denominator
    }

    fn compute_difficulty_value(&self) -> f64 {
        let custom_acc = self.custom_accuracy();
        let total_hits = self.total_hits() as f64;

        let star_component = (self.star_rating - 0.15_f64).max(0.05_f64).powf(2.2);
        let acc_component = (5.0 * custom_acc - 4.0).max(0.0);
        let length_component = 1.0 + 0.1 * (total_hits / 1500.0).min(1.0);

        8.0 * star_component * acc_component * length_component
    }

    pub fn calculate(&self) -> f64 {
        let diff_value = self.compute_difficulty_value();

        let mut multiplier = 1.0_f64;
        if self.no_fail {
            multiplier *= 0.75;
        }
        if self.easy {
            multiplier *= 0.5;
        }

        diff_value * multiplier
    }
}
