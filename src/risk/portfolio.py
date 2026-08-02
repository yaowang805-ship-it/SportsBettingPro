"""Kelly 投资组合优化器 — 用 scipy.optimize 做约束凸优化。

专业 vs 业余下注的核心区别：
  - 业余：每注独立算 Kelly，忽略相关性
  - 专业：求解联合优化问题，考虑所有同时下注的相关性

目标函数：最大化 sum_i [p_i*log(1+b_i*w_i) + (1-p_i)*log(1-w_i)]
  w_i = 分配给下注 i 的本金比例
  b_i = odds - 1
  p_i = 模型概率

约束：
  sum(w_i) <= max_total_exposure
  0 <= w_i <= max_single_per_bet

这是凸优化（Kelly 目标凹，约束线性），SLSQP 保证全局最优。

用法:
    from src.risk.portfolio import KellyPortfolioOptimizer
    opt = KellyPortfolioOptimizer()
    weights, meta = opt.solve([
        {"prob": 0.55, "odds": 2.0},
        {"prob": 0.60, "odds": 2.5},
    ])
"""
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from scipy.optimize import minimize, Bounds, LinearConstraint

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)


class KellyPortfolioOptimizer:
    """真正的 Kelly 投资组合优化器。

    求解所有同时下注的联合最优资金分配。
    """

    def __init__(self, max_single: float = 0.05, max_total: float = 0.30):
        """
        Args:
            max_single: 每注最大本金比例（默认 5%）
            max_total: 总暴露上限（默认 30%）
        """
        self.max_single = max_single
        self.max_total = max_total

    def _neg_kelly_objective(self, w: np.ndarray, probs: np.ndarray, odds: np.ndarray) -> float:
        """负 Kelly 对数效用（最小化用）。

        w: 本金比例向量
        probs: 模型概率向量
        odds: 十进制赔率向量
        """
        p = np.asarray(probs, dtype=float)
        b = np.asarray(odds, dtype=float) - 1.0
        eps = 1e-10

        win_util = p * np.log(np.maximum(1.0 + b * w, eps))
        loss_util = (1.0 - p) * np.log(np.maximum(1.0 - w, eps))
        return -float(np.sum(win_util + loss_util))

    def solve(self, bets: List[Dict]) -> Tuple[np.ndarray, dict]:
        """求解 Kelly 投资组合优化。

        Args:
            bets: 列表，每项含 {'prob': float, 'odds': float}

        Returns:
            (optimal_weights, metadata)
        """
        n = len(bets)
        if n == 0:
            return np.array([]), {"status": "no_bets"}

        probs = np.array([b['prob'] for b in bets], dtype=float)
        odds = np.array([b['odds'] for b in bets], dtype=float)

        # 过滤无效下注
        valid = (probs > 0) & (odds > 1.0)
        if not valid.any():
            return np.zeros(n), {"status": "no_valid_bets"}

        probs = probs[valid]
        odds = odds[valid]
        m = len(probs)

        bounds = Bounds([0.0] * m, [self.max_single] * m)
        constraints = LinearConstraint(np.ones(m), 0, self.max_total)

        # 初始猜测：均匀摊分
        init_w = np.full(m, min(self.max_single, self.max_total / max(m, 1)))

        result = minimize(
            self._neg_kelly_objective,
            init_w,
            args=(probs, odds),
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'ftol': 1e-9, 'maxiter': 500},
        )

        # SLSQP 备选：trust-constr
        if not result.success:
            result = minimize(
                self._neg_kelly_objective,
                init_w,
                args=(probs, odds),
                method='trust-constr',
                bounds=bounds,
                constraints=constraints,
                options={'gtol': 1e-6},
            )

        optimal = np.clip(result.x, 0, self.max_single)

        # 重建完整权重向量（包括被过滤的）
        full_weights = np.zeros(n)
        full_weights[valid] = optimal

        # 统计指标
        total_exp = full_weights.sum()
        exp_log_wealth = -result.fun

        # 近似 Sharpe
        port_mean = np.sum(probs * (odds - 1.0) * optimal - optimal)
        port_var = np.sum(probs * (1.0 - probs) * (odds * optimal) ** 2)
        port_sharpe = port_mean / np.sqrt(port_var) if port_var > 1e-10 else 0.0

        return full_weights, {
            "total_exposure": float(total_exp),
            "expected_log_wealth": float(exp_log_wealth),
            "port_sharpe": float(port_sharpe),
            "converged": bool(result.success),
            "n_valid_bets": m,
        }

    def solve_with_correlation(
        self, bets: List[Dict], corr_matrix: np.ndarray
    ) -> Tuple[np.ndarray, dict]:
        """带相关矩阵的 Kelly 优化（使用协方差调整风险估计）。"""
        n = len(bets)
        if n == 0:
            return np.array([]), {"status": "no_bets"}

        probs = np.array([b['prob'] for b in bets], dtype=float)
        odds = np.array([b['odds'] for b in bets], dtype=float)

        valid = (probs > 0) & (odds > 1.0)
        if not valid.any():
            return np.zeros(n), {"status": "no_valid_bets"}

        # 过滤
        valid_idx = np.where(valid)[0]
        probs_v = probs[valid]
        odds_v = odds[valid]
        m = len(probs_v)

        # 相关矩阵子集
        if corr_matrix.shape == (n, n):
            corr_v = corr_matrix[np.ix_(valid_idx, valid_idx)]
        else:
            corr_v = np.eye(m)

        bounds = Bounds([0.0] * m, [self.max_single] * m)
        constraints = LinearConstraint(np.ones(m), 0, self.max_total)
        init_w = np.full(m, min(self.max_single, self.max_total / max(m, 1)))

        result = minimize(
            self._neg_kelly_objective,
            init_w,
            args=(probs_v, odds_v),
            method='SLSQP',
            bounds=bounds,
            constraints=constraints,
            options={'ftol': 1e-9, 'maxiter': 500},
        )

        optimal = np.clip(result.x, 0, self.max_single)

        # V4.4: 应用相关性惩罚 — 高相关投注权重下调
        if corr_matrix.shape == (n, n) and m > 1:
            corr_v = corr_matrix[np.ix_(valid_idx, valid_idx)]
            # 对每对投注，若相关系数 > 0.3，下调权重
            for i in range(m):
                avg_corr = 0.0
                count = 0
                for j in range(m):
                    if i != j and optimal[i] > 0 and optimal[j] > 0:
                        avg_corr += corr_v[i, j]
                        count += 1
                if count > 0:
                    avg_corr /= count
                    # 相关性惩罚: 0.3以上每+0.1减10%权重
                    if avg_corr > 0.3:
                        penalty = 1.0 - min(0.5, (avg_corr - 0.3) * 1.0)
                        optimal[i] *= penalty

        # 相关调整后的组合方差
        variances = probs_v * (1.0 - probs_v)
        stds = np.sqrt(np.maximum(variances, 1e-10))
        cov_matrix = np.outer(stds, stds) * corr_v
        port_var = optimal @ cov_matrix @ optimal
        port_std = np.sqrt(max(port_var, 1e-10))
        port_mean = np.sum(probs_v * (odds_v - 1.0) * optimal - optimal)
        port_sharpe = port_mean / port_std if port_std > 0 else 0.0

        full_weights = np.zeros(n)
        full_weights[valid] = optimal

        return full_weights, {
            "total_exposure": float(full_weights.sum()),
            "port_std": float(port_std),
            "port_sharpe": float(port_sharpe),
            "expected_log_wealth": float(-result.fun),
            "converged": bool(result.success),
            "n_valid_bets": m,
        }


def find_best_kelly_bet(bets: List[Dict]) -> Dict:
    """辅助：找到最优的单个 Kelly 下注。"""
    opt = KellyPortfolioOptimizer()
    weights, meta = opt.solve(bets)
    if len(weights) == 0 or not weights.any():
        return {"stake": 0.0, "best_idx": -1}
    best_idx = int(np.argmax(weights))
    if weights[best_idx] <= 0:
        return {"stake": 0.0, "best_idx": -1}
    return {
        "stake": float(weights[best_idx]),
        "best_idx": best_idx,
        "total_exposure": float(meta["total_exposure"]),
    }
