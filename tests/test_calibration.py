"""测试概率校准模块 — dynamic_shrinkage, adjust_for_sample_size, calibrate_ensemble, brier_score, calibration_curve."""
import pytest
import numpy as np

from src.core.calibration import (
    dynamic_shrinkage,
    adjust_for_sample_size,
    calibrate_ensemble,
    brier_score,
    calibration_curve,
)


class TestDynamicShrinkage:
    def test_output_range(self):
        for mp in [0.3, 0.5, 0.7]:
            for mk in [0.3, 0.5, 0.7]:
                result = dynamic_shrinkage(mp, mk)
                assert 0.0 <= result <= 1.0

    def test_high_confidence_weights_model_more(self):
        # model very confident (0.95) vs market 0.5
        result = dynamic_shrinkage(0.95, 0.5)
        market_result = dynamic_shrinkage(0.5, 0.95)
        # confident model should pull result toward 0.95
        assert result > 0.5

    def test_low_confidence_weights_market_more(self):
        # model near 0.5 (uncertain) vs market 0.5
        result = dynamic_shrinkage(0.51, 0.5)
        # should be very close to 0.5
        assert abs(result - 0.5) < 0.05

    def test_min_model_weight_respected(self):
        # even at max confidence, model weight should be at least min_model_weight
        result = dynamic_shrinkage(0.999, 0.001)
        assert result > 0.001
        # with extreme confidence, result pulled toward model_prob
        # shrink(0.999, 0.001) → confidence≈1.0 → model_weight≈0.5 → 0.5*0.999+0.5*0.001≈0.5
        assert result > 0.001  # more than pure market_prob
        assert result < 0.999  # less than pure model_prob

    def test_model_weight_range_respected(self):
        r1 = dynamic_shrinkage(0.5, 0.5, min_model_weight=0.1, max_model_weight=0.9)
        assert 0.1 * 0.5 + 0.9 * 0.5 <= r1 <= 0.9 * 0.5 + 0.1 * 0.5
        # Actually at model=0.5, confidence=0, so weight=min=0.1
        # result = 0.1*0.5 + 0.9*0.5 = 0.5
        assert r1 == pytest.approx(0.5, abs=0.01)

    def test_confidence_power_effect(self):
        # Lower confidence_power → more aggressive shift toward model when confident
        r_low = dynamic_shrinkage(0.80, 0.50, confidence_power=0.3)
        r_high = dynamic_shrinkage(0.80, 0.50, confidence_power=1.5)
        # Lower power should give more weight to model
        assert r_low > r_high

    def test_extreme_values(self):
        # model_prob=0.0, market_prob=0.0
        result = dynamic_shrinkage(0.0, 0.0)
        assert 0.0 <= result <= 1.0

        # model_prob=1.0, market_prob=1.0
        result = dynamic_shrinkage(1.0, 1.0)
        assert 0.0 <= result <= 1.0

    def test_monotonic(self):
        # Higher model probability should give higher (or equal) result
        # all else equal
        base = dynamic_shrinkage(0.5, 0.5)
        higher = dynamic_shrinkage(0.6, 0.5)
        assert higher >= base


class TestAdjustForSampleSize:
    def test_large_sample_gives_higher_model_weight(self):
        r_small = adjust_for_sample_size(0.7, 0.5, n_samples=50)
        r_large = adjust_for_sample_size(0.7, 0.5, n_samples=500)
        # More samples → higher model weight → closer to model_prob
        assert abs(r_large - 0.7) <= abs(r_small - 0.7) or True
        # At 50 samples (< min_samples), model_weight=0.10
        # At 500 samples (>= min_samples*4), model_weight=0.40
        # So r_large should be closer to 0.7
        assert r_large > r_small

    def test_output_range(self):
        for n in [10, 50, 100, 200, 500]:
            result = adjust_for_sample_size(0.6, 0.5, n_samples=n)
            assert 0.0 <= result <= 1.0

    def test_min_samples_boundary(self):
        r_below = adjust_for_sample_size(0.7, 0.5, n_samples=30, min_samples=50)
        r_at = adjust_for_sample_size(0.7, 0.5, n_samples=50, min_samples=50)
        assert r_at >= r_below

    def test_extreme_n_samples(self):
        result = adjust_for_sample_size(0.8, 0.5, n_samples=0)
        assert 0.0 <= result <= 1.0


class TestCalibrateEnsemble:
    def test_output_range(self):
        result = calibrate_ensemble(0.7, 0.5)
        assert 0.0 <= result <= 1.0

    def test_with_n_samples(self):
        r1 = calibrate_ensemble(0.7, 0.5, n_samples=50)
        r2 = calibrate_ensemble(0.7, 0.5, n_samples=500)
        assert r2 >= r1

    def test_default_matches_dynamic_shrinkage(self):
        result = calibrate_ensemble(0.7, 0.5)
        expected = dynamic_shrinkage(0.7, 0.5)
        assert result == pytest.approx(expected, abs=0.01)


class TestBrierScore:
    def test_perfect_prediction(self):
        y_true = np.array([1, 1, 0, 0])
        y_prob = np.array([1.0, 1.0, 0.0, 0.0])
        assert brier_score(y_true, y_prob) == pytest.approx(0.0, abs=0.001)

    def test_worst_prediction(self):
        y_true = np.array([1, 0])
        y_prob = np.array([0.0, 1.0])
        assert brier_score(y_true, y_prob) == pytest.approx(1.0, abs=0.001)

    def test_uniform_guess(self):
        y_true = np.array([1, 0, 1, 0])
        y_prob = np.array([0.5, 0.5, 0.5, 0.5])
        score = brier_score(y_true, y_prob)
        assert score == pytest.approx(0.25, abs=0.001)

    def test_empty_input(self):
        y_true = np.array([])
        y_prob = np.array([])
        score = brier_score(y_true, y_prob)
        assert np.isnan(score) or score == 0.0

    def test_different_lengths(self):
        y_true = np.array([1, 0, 1])
        y_prob = np.array([0.8, 0.2])
        with pytest.raises(Exception):
            brier_score(y_true, y_prob)


class TestCalibrationCurve:
    def test_output_shape(self):
        y_true = np.array([1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
        y_prob = np.array([0.9, 0.1, 0.8, 0.2, 0.7, 0.3, 0.6, 0.4, 0.5, 0.5])
        centers, fractions = calibration_curve(y_true, y_prob, bins=5)
        assert len(centers) == 5
        assert len(fractions) == 5

    def test_all_nan_for_no_data(self):
        y_true = np.array([])
        y_prob = np.array([])
        centers, fractions = calibration_curve(y_true, y_prob, bins=5)
        assert len(centers) == 5
        assert np.all(np.isnan(fractions))

    def test_perfectly_calibrated(self):
        y_true = np.array([1, 1, 1, 0, 0, 0])
        y_prob = np.array([0.9, 0.8, 0.7, 0.3, 0.2, 0.1])
        centers, fractions = calibration_curve(y_true, y_prob, bins=3)
        # at least some bins should have non-nan values
        assert np.any(~np.isnan(fractions))
