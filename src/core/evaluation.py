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


def brier_score(actual, predicted):
    return float(brier_score_loss(actual, predicted))


def safe_log_loss(actual, predicted):
    try:
        return float(log_loss(actual, np.clip(predicted, 1e-6, 1 - 1e-6)))
    except Exception:
        return float('nan')
