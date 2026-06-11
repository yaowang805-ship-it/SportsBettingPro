"""测试模型退化追踪器。"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.risk.model_decay_tracker import ModelDecayTracker, DECAY_FILE, ACCURACY_BASELINES


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path):
    """每个测试用独立文件。"""
    test_file = tmp_path / "test_decay.json"
    with patch("src.risk.model_decay_tracker.DECAY_FILE", test_file):
        yield


class TestModelDecayTracker:
    def test_new_tracker_empty(self):
        tr = ModelDecayTracker()
        assert tr.history == {}

    def test_multiplier_without_data_is_1(self):
        tr = ModelDecayTracker()
        assert tr.get_confidence_multiplier("bb") == 1.0
        assert tr.get_confidence_multiplier("nonexistent") == 1.0

    def test_multiplier_with_few_records_is_1(self):
        tr = ModelDecayTracker()
        for _ in range(5):
            tr.record_prediction("bb", 0.6, True)
        assert tr.get_confidence_multiplier("bb") == 1.0  # < 20 records

    def test_multiplier_enough_records(self):
        tr = ModelDecayTracker()
        for _ in range(25):
            tr.record_prediction("bb", 0.6, True)
        m = tr.get_confidence_multiplier("bb")
        assert 0.3 <= m <= 1.0

    def test_high_accuracy_returns_1(self):
        tr = ModelDecayTracker()
        # 70% accuracy > bb baseline 55%+5%
        for _ in range(30):
            tr.record_prediction("bb", 0.6, True)
        assert tr.get_confidence_multiplier("bb") == 1.0

    def test_low_accuracy_reduces_multiplier(self):
        tr = ModelDecayTracker()
        # 30% accuracy << baseline
        for _ in range(50):
            tr.record_prediction("bb", 0.6, False)
        m = tr.get_confidence_multiplier("bb")
        assert m < 0.6

    def test_persistence(self):
        tr1 = ModelDecayTracker()
        for _ in range(30):
            tr1.record_prediction("bb", 0.6, True)
        tr2 = ModelDecayTracker()
        assert len(tr2.history.get("bb", [])) >= 30

    def test_all_health_report(self):
        tr = ModelDecayTracker()
        for _ in range(30):
            tr.record_prediction("bb", 0.6, True)
        for _ in range(30):
            tr.record_prediction("fb", 0.55, False)
        health = tr.get_all_health()
        assert "bb" in health
        assert "fb" in health
        assert "recent_50_accuracy" in health["bb"]
        assert health["bb"]["multiplier"] > health["fb"]["multiplier"]

    def test_clear_history(self):
        tr = ModelDecayTracker()
        tr.record_prediction("bb", 0.6, True)
        tr.record_prediction("fb", 0.55, False)
        tr.clear_history("bb")
        assert "bb" not in tr.history
        assert "fb" in tr.history
        tr.clear_history()
        assert tr.history == {}

    def test_multi_window_multiplier(self):
        tr = ModelDecayTracker()
        # 前30条全对，后20条全错 → 50窗口分数<30窗口分数
        for _ in range(30):
            tr.record_prediction("bb", 0.6, True)
        for _ in range(20):
            tr.record_prediction("bb", 0.6, False)
        mw = tr.get_multi_window_multiplier("bb")
        assert 0.3 <= mw <= 1.0

    def test_baselines_defined(self):
        """确保所有已知模型有基准准确率。"""
        for model in ["bb", "fb", "nfl", "ensemble", "dc", "poisson"]:
            assert model in ACCURACY_BASELINES
            assert 0.4 <= ACCURACY_BASELINES[model] <= 0.7
