"""测试 ML 动态仓位模型。"""
import pandas as pd
import numpy as np
from pathlib import Path
from unittest.mock import patch

import pytest

from src.risk.dynamic_staking import DynamicStakingModel, MODEL_FILE


class TestDynamicStakingModel:
    def test_default_not_trained(self):
        model = DynamicStakingModel()
        assert not model.is_trained
        assert not hasattr(model, '_sk_model') or model._sk_model is None

    def test_rule_based_fallback_high_edge(self):
        model = DynamicStakingModel()
        mult = model._rule_based_multiplier({"edge": 0.20, "drawdown_pct": 0, "consecutive_losses": 0})
        assert mult == pytest.approx(1.0, abs=0.01)

    def test_rule_based_fallback_low_edge_with_drawdown(self):
        model = DynamicStakingModel()
        mult = model._rule_based_multiplier({"edge": 0.05, "drawdown_pct": 0.12, "consecutive_losses": 3})
        # conf=0.3 * dd=0.4 * streak=0.7 = 0.084
        assert mult == pytest.approx(0.084, abs=0.01)

    def test_rule_based_fallback_stop(self):
        model = DynamicStakingModel()
        mult = model._rule_based_multiplier({"edge": 0.20, "drawdown_pct": 0.25, "consecutive_losses": 0})
        assert mult == 0.0  # drawdown > 20% → stop

    def test_predict_without_training_falls_back(self):
        model = DynamicStakingModel()
        mult = model.predict_multiplier({
            "edge": 0.12, "model_prob": 0.62, "odds": 2.0,
            "drawdown_pct": 0.0, "consecutive_losses": 0,
            "adaptive_kelly_frac": 0.25, "n_active_bets": 1,
            "win_rate": 0.5, "total_bets": 10,
        })
        assert 0.1 <= mult <= 1.0

    def test_collect_training_data_no_file(self):
        model = DynamicStakingModel()
        result = model.collect_training_data(Path("/tmp/nonexistent_bet_log.csv"))
        assert result is None

    def test_collect_training_data_sufficient(self, tmp_path):
        """模拟足量 bet_log 并验证特征提取。"""
        log_file = tmp_path / "test_bet_log.csv"
        records = []
        balance = 10000
        for i in range(60):
            win = 1 if np.random.random() < 0.5 else 0
            stake = 200
            odds = 2.0
            prob = 0.5
            if win:
                balance += stake * (odds - 1)
            else:
                balance -= stake
            records.append({
                "win": win, "stake": stake, "odds": odds,
                "model_prob": prob, "balance_after": balance,
            })
        pd.DataFrame(records).to_csv(log_file, index=False)

        model = DynamicStakingModel()
        data = model.collect_training_data(log_file)
        assert data is not None
        assert len(data) == 60
        for col in model.feature_cols:
            assert col in data.columns, f"缺少特征列: {col}"
        assert "target" in data.columns

    def test_train_with_synthetic_data(self, tmp_path):
        """用合成数据训练并验证预测合理性。"""
        log_file = tmp_path / "train_bet_log.csv"
        np.random.seed(42)
        records = []
        balance = 10000
        for i in range(200):
            prob = np.clip(np.random.beta(5, 4), 0.3, 0.8)
            odds = round(1.0 / prob + np.random.uniform(-0.1, 0.3), 2)
            edge = prob - 1.0 / odds
            win = 1 if np.random.random() < prob else 0
            stake = 200
            if win:
                balance += stake * (odds - 1)
            else:
                balance -= stake
            records.append({
                "win": win, "stake": stake, "odds": odds,
                "model_prob": prob, "balance_after": balance,
            })
        pd.DataFrame(records).to_csv(log_file, index=False)

        model = DynamicStakingModel()
        # 用隔离的 MODEL_FILE 路径
        test_model_file = tmp_path / "test_model.pkl"
        with patch("src.risk.dynamic_staking.MODEL_FILE", test_model_file.with_suffix(".json")):
            success = model.train(log_file)
        # sklearn 可能不可用（CI 环境），但不应报错
        if success:
            assert model.is_trained
            # 高 edge 投注预测乘数应 > 低 edge
            high = model.predict_multiplier({
                "edge": 0.20, "model_prob": 0.70, "odds": 1.8,
                "drawdown_pct": 0.0, "consecutive_losses": 0,
                "adaptive_kelly_frac": 0.25, "n_active_bets": 1,
                "win_rate": 0.6, "total_bets": 100,
            })
            low = model.predict_multiplier({
                "edge": 0.03, "model_prob": 0.45, "odds": 2.5,
                "drawdown_pct": 0.08, "consecutive_losses": 2,
                "adaptive_kelly_frac": 0.2, "n_active_bets": 5,
                "win_rate": 0.45, "total_bets": 100,
            })
            assert high >= low

    def test_get_feature_importance_without_model(self):
        model = DynamicStakingModel()
        assert model.get_feature_importance() is None

    def test_train_not_enough_data(self, tmp_path):
        model = DynamicStakingModel()
        log_file = tmp_path / "small_log.csv"
        pd.DataFrame([{"win": 1, "stake": 100, "odds": 2.0, "model_prob": 0.5}] * 10).to_csv(log_file, index=False)
        result = model.train(log_file)
        assert not result  # < 50 samples
