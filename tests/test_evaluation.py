"""测试评估工具 — sharpe_ratio, max_drawdown, calmar_ratio, brier_score。"""
import numpy as np
import pytest

from src.core.evaluation import sharpe_ratio, max_drawdown, calmar_ratio, brier_score


class TestSharpeRatio:
    def test_constant_returns_zero(self):
        assert sharpe_ratio([1, 1, 1]) == 0.0

    def test_positive_returns(self):
        sr = sharpe_ratio([0.01, 0.02, 0.01, 0.015], annual_factor=1)
        assert sr > 0

    def test_empty_returns_zero(self):
        assert sharpe_ratio([]) == 0.0


class TestMaxDrawdown:
    def test_straight_line_zero(self):
        assert max_drawdown([100, 200, 300]) == 0.0

    def test_single_dip(self):
        dd = max_drawdown([100, 90, 80, 70, 85, 95])
        assert dd == pytest.approx(30.0)

    def test_empty_returns_zero(self):
        assert max_drawdown([]) == 0.0


class TestCalmarRatio:
    def test_positive_returns_no_drawdown(self):
        # No drawdown → 0 (avoid division by zero)
        cr = calmar_ratio([0.01, 0.01], [100, 102], annual_factor=1)
        assert cr == 0.0

    def test_increasing_then_dip(self):
        cr = calmar_ratio([0.1, -0.05, 0.05], [100, 110, 104.5], annual_factor=1)
        assert cr > 0

    def test_empty_returns_zero(self):
        assert calmar_ratio([], []) == 0.0


class TestBrierScore:
    def test_perfect_prediction(self):
        assert brier_score([1, 1, 0], [1, 1, 0]) == 0.0

    def test_worst_prediction(self):
        assert brier_score([1, 0], [0.5, 0.5]) == pytest.approx(0.25)
