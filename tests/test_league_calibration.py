"""测试每联赛校准器。"""
import pytest
import numpy as np

from src.core.league_calibration import LeagueCalibrator


class TestLeagueCalibrator:
    def test_initial_state(self, tmp_path):
        c = LeagueCalibrator(storage_path=str(tmp_path / "cal.json"))
        assert c.get_league_stats() == {}

    def test_update_no_calibration_below_threshold(self, tmp_path):
        c = LeagueCalibrator(storage_path=str(tmp_path / "cal.json"))
        for _ in range(30):
            c.update("EPL", 0.6, 1)
        assert "EPL" not in c.calibrators  # < 50 samples

    def test_update_triggers_calibration_above_threshold(self, tmp_path):
        c = LeagueCalibrator(storage_path=str(tmp_path / "cal.json"))
        rng = np.random.RandomState(42)
        for _ in range(60):
            actual = 1 if rng.random() < 0.55 else 0  # ~55% win rate
            c.update("EPL", 0.6, actual)
        assert "EPL" in c.calibrators

    def test_calibrate_returns_raw_when_no_calibrator(self, tmp_path):
        c = LeagueCalibrator(storage_path=str(tmp_path / "cal.json"))
        assert c.calibrate("UNKNOWN", 0.7) == 0.7

    def test_calibrate_adjusts_systematic_bias(self, tmp_path):
        c = LeagueCalibrator(storage_path=str(tmp_path / "cal.json"))
        # 系统偏差：预测 0.7，实际只有 50% 胜率
        rng = np.random.RandomState(42)
        for _ in range(100):
            actual = 1 if rng.random() < 0.5 else 0
            c.update("EPL", 0.7, actual)
        # 校准后概率应低于 0.7
        calibrated = c.calibrate("EPL", 0.7)
        assert calibrated < 0.7
        assert 0.02 <= calibrated <= 0.98

    def test_calibrate_under_confident(self, tmp_path):
        c = LeagueCalibrator(storage_path=str(tmp_path / "cal.json"))
        # 反向偏差：预测 0.3，实际 50% 胜率（低估了）
        rng = np.random.RandomState(42)
        for _ in range(100):
            actual = 1 if rng.random() < 0.5 else 0
            c.update("Bundesliga", 0.3, actual)
        calibrated = c.calibrate("Bundesliga", 0.3)
        assert calibrated > 0.3

    def test_multiple_leagues_independent(self, tmp_path):
        c = LeagueCalibrator(storage_path=str(tmp_path / "cal.json"))
        rng = np.random.RandomState(42)
        for _ in range(60):
            c.update("EPL", 0.7, 1)  # 高估
            c.update("Bundesliga", 0.3, 0)  # 准确
        stats = c.get_league_stats()
        assert "EPL" in stats
        assert "Bundesliga" in stats
        assert stats["EPL"]["n_samples"] >= 60
        assert stats["Bundesliga"]["n_samples"] >= 60

    def test_save_and_load(self, tmp_path):
        p = tmp_path / "cal.json"
        c1 = LeagueCalibrator(storage_path=str(p))
        for _ in range(60):
            c1.update("EPL", 0.6, 1)
        c1.save()

        c2 = LeagueCalibrator(storage_path=str(p))
        c2.load()
        stats = c2.get_league_stats()
        assert "EPL" in stats

    def test_clipped_output(self, tmp_path):
        """校准概率应始终在 [0.02, 0.98] 范围内。"""
        c = LeagueCalibrator(storage_path=str(tmp_path / "cal.json"))
        rng = np.random.RandomState(42)
        for _ in range(60):
            c.update("EPL", 0.999, rng.randint(0, 2))
        cal = c.calibrate("EPL", 0.999)
        assert 0.02 <= cal <= 0.98
