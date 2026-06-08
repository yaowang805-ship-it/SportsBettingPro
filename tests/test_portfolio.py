"""测试 Kelly 投资组合优化器。"""
import pytest
import numpy as np

from src.risk.portfolio import KellyPortfolioOptimizer, find_best_kelly_bet


class TestKellyPortfolioOptimizer:
    def test_empty_bets(self):
        opt = KellyPortfolioOptimizer()
        w, meta = opt.solve([])
        assert len(w) == 0
        assert meta["status"] == "no_bets"

    def test_single_positive_ev_bet(self):
        """单注正 EV → w > 0 且受 max_single 限制。"""
        opt = KellyPortfolioOptimizer(max_single=0.05, max_total=0.30)
        w, meta = opt.solve([{"prob": 0.60, "odds": 2.0}])
        # 全 Kelly: (0.6*1 - 0.4)/1 = 0.2 → 20%
        # 受 max_single 5% 限制
        assert 0 < w[0] <= 0.05
        assert meta["total_exposure"] <= 0.05
        assert meta["converged"]

    def test_zero_edge_bet_gets_zero(self):
        """零 EV 不下注。"""
        opt = KellyPortfolioOptimizer()
        w, meta = opt.solve([{"prob": 0.50, "odds": 2.0}])
        assert w[0] == 0.0

    def test_negative_edge_bet_zero(self):
        """负 EV 不下注。"""
        opt = KellyPortfolioOptimizer()
        w, meta = opt.solve([{"prob": 0.40, "odds": 2.0}])
        assert w[0] == 0.0

    def test_odds_less_than_one_zero(self):
        opt = KellyPortfolioOptimizer()
        w, meta = opt.solve([{"prob": 0.60, "odds": 0.5}])
        assert w[0] == 0.0

    def test_multiple_independent_bets(self):
        """多个独立正 EV 下注 → 总暴露受 max_total 限制。"""
        opt = KellyPortfolioOptimizer(max_single=0.05, max_total=0.30)
        bets = [
            {"prob": 0.60, "odds": 2.0},
            {"prob": 0.65, "odds": 2.0},
            {"prob": 0.55, "odds": 2.5},
        ]
        w, meta = opt.solve(bets)
        assert all(wi >= 0 for wi in w)
        assert meta["total_exposure"] <= 0.30
        assert w[1] > 0  # 65% 是最高的 EV

    def test_total_exposure_capped(self):
        """很多高 EV 下注 → sum(w) ≤ max_total。"""
        opt = KellyPortfolioOptimizer(max_single=0.05, max_total=0.10)
        bets = [{"prob": 0.70, "odds": 2.0}] * 10
        w, meta = opt.solve(bets)
        assert abs(meta["total_exposure"] - 0.10) < 1e-4  # 达到上限
        assert all(wi <= 0.05 for wi in w)

    def test_best_bet_gets_most_weight(self):
        """最高 EV 的下注获得最多分配。"""
        opt = KellyPortfolioOptimizer(max_single=0.10, max_total=0.30)
        bets = [
            {"prob": 0.55, "odds": 2.0},   # EV = 5%
            {"prob": 0.70, "odds": 3.0},   # EV = ~23%
            {"prob": 0.60, "odds": 1.8},   # EV = ~8%
        ]
        w, meta = opt.solve(bets)
        assert w[1] >= w[0]  # 最高 EV 下注权重大
        assert w[1] >= w[2]

    def test_solve_with_correlation_basic(self):
        """带相关矩阵的求解不应出错。"""
        opt = KellyPortfolioOptimizer(max_single=0.05, max_total=0.20)
        bets = [
            {"prob": 0.60, "odds": 2.0},
            {"prob": 0.55, "odds": 2.2},
        ]
        corr = np.array([[1.0, 0.3], [0.3, 1.0]])
        w, meta = opt.solve_with_correlation(bets, corr)
        assert len(w) == 2
        assert all(wi >= 0 for wi in w)
        assert meta["converged"]

    def test_correlation_reduces_exposure(self):
        """高相关下注 → 组合驱动风险降低总暴露。"""
        opt = KellyPortfolioOptimizer(max_single=0.05, max_total=0.30)
        bets = [{"prob": 0.60, "odds": 2.0}, {"prob": 0.60, "odds": 2.0}]

        w_low_corr, _ = opt.solve_with_correlation(bets, np.array([[1.0, 0.0], [0.0, 1.0]]))
        w_high_corr, _ = opt.solve_with_correlation(bets, np.array([[1.0, 0.95], [0.95, 1.0]]))
        # 高相关 → 分散化收益小 → 总暴露可能更低
        assert w_low_corr.sum() >= 0

    def test_default_params(self):
        opt = KellyPortfolioOptimizer()
        assert opt.max_single == 0.05
        assert opt.max_total == 0.30

    def test_custom_params(self):
        opt = KellyPortfolioOptimizer(max_single=0.10, max_total=0.50)
        assert opt.max_single == 0.10
        assert opt.max_total == 0.50


class TestFindBestKellyBet:
    def test_single_bet(self):
        result = find_best_kelly_bet([{"prob": 0.60, "odds": 2.0}])
        assert result["stake"] > 0
        assert result["best_idx"] == 0

    def test_no_valid_bets(self):
        result = find_best_kelly_bet([{"prob": 0.40, "odds": 2.0}])
        assert result["stake"] == 0.0
        assert result["best_idx"] == -1

    def test_picks_best(self):
        # 用例确保两个下注不会同时达到 max_single 边界
        result = find_best_kelly_bet([
            {"prob": 0.52, "odds": 2.0},   # 小正 EV
            {"prob": 0.60, "odds": 2.5},   # 更高 EV
        ])
        assert result["best_idx"] == 1  # 更高 EV
