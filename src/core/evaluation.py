import numpy as np
from sklearn.metrics import brier_score_loss, log_loss


def max_drawdown(equity_curve):
    equity = np.asarray(equity_curve, dtype=float)
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    return float(np.max(drawdown))


def sharpe_ratio(returns, risk_free_rate: float = 0.0, annual_factor: float = 252) -> float:
    returns = np.asarray(returns, dtype=float)
    if returns.size == 0 or np.std(returns) == 0:
        return 0.0
    excess = returns - risk_free_rate / annual_factor
    return float(np.mean(excess) / np.std(excess) * np.sqrt(annual_factor))


def calmar_ratio(returns, equity_curve, annual_factor: float = 252) -> float:
    """Calmar 比率 = 年化收益率 / 最大回撤比率。

    Args:
        returns: 每期收益率序列
        equity_curve: 资金曲线序列
        annual_factor: 年化因子 (日=252, 周=52, 月=12)

    Returns:
        Calmar 比率（分母为 0 时返回 0.0）
    """
    returns = np.asarray(returns, dtype=float)
    equity = np.asarray(equity_curve, dtype=float)
    if returns.size == 0 or equity.size == 0:
        return 0.0
    total_return = equity[-1] / equity[0] - 1 if equity[0] > 0 else 0.0
    n_periods = len(returns)
    cagr = (1 + total_return) ** (annual_factor / n_periods) - 1 if n_periods > 0 else 0.0
    peak = np.maximum.accumulate(equity)
    dd_pct = np.max(1 - equity / peak) if peak[-1] > 0 else 0.0
    if dd_pct <= 0:
        return 0.0
    return float(cagr / dd_pct)


def brier_score(actual, predicted):
    return float(brier_score_loss(actual, predicted))


def safe_log_loss(actual, predicted):
    try:
        return float(log_loss(actual, np.clip(predicted, 1e-6, 1 - 1e-6)))
    except Exception:
        return float('nan')
