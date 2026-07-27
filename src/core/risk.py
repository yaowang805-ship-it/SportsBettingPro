from typing import Iterable, Optional
from config.settings import DEFAULT_BUDGET, MAX_SINGLE_BET_PCT
from config.constants import KELLY_FRACTION


def implied_probability(odds: float) -> float:
    if odds <= 1.0:
        return 0.0
    return 1.0 / odds


def normalize_market_probabilities(odds: Iterable[float], min_prob: float = 0.02) -> list[float]:
    probs = [implied_probability(float(o)) for o in odds]
    total = sum(probs)
    if total <= 0:
        return [1.0 / len(probs) for _ in probs]
    normalized = [p / total for p in probs]
    return [safe_probability(p, min_prob, 1.0 - min_prob) for p in normalized]


def market_probability_from_h2h(home_odds: float, draw_odds: float, away_odds: float) -> tuple[float, float, float]:
    home, draw, away = normalize_market_probabilities([home_odds, draw_odds, away_odds])
    return home, draw, away


def market_probability_from_two_way(odds_a: float, odds_b: float) -> tuple[float, float]:
    a, b = normalize_market_probabilities([odds_a, odds_b])
    return a, b


def safe_probability(prob: float, min_prob: float = 0.02, max_prob: float = 0.98) -> float:
    return max(min_prob, min(max_prob, prob))


def edge_ratio(model_prob: float, odds: Optional[float] = None, market_prob: Optional[float] = None) -> float:
    if market_prob is None:
        if odds is None:
            raise ValueError("必须提供 odds 或 market_prob")
        market_prob = implied_probability(odds)
    return safe_probability(model_prob) - market_prob


def kelly_stake(prob: float, odds: float, budget: float = DEFAULT_BUDGET,
                fraction: float = KELLY_FRACTION,
                max_pct: float = MAX_SINGLE_BET_PCT) -> float:
    prob = safe_probability(prob)
    if odds <= 1.0:
        return 0.0
    b = odds - 1.0
    kelly = (prob * b - (1.0 - prob)) / b
    if kelly <= 0:
        return 0.0
    stake_pct = max(0.0, min(kelly * fraction, max_pct))
    return budget * stake_pct


def blend_with_market(model_prob: float, market_odds: Optional[float] = None,
                      market_prob: Optional[float] = None,
                      market_weight: float = 0.7,
                      model_weight: float = 0.3) -> float:
    if market_prob is None:
        if market_odds is None:
            raise ValueError("必须提供 market_odds 或 market_prob")
        market_prob = implied_probability(market_odds)
    return safe_probability(model_prob * model_weight + market_prob * market_weight)


def format_probability(prob: float) -> str:
    return f"{safe_probability(prob):.1%}"


def expected_value(prob: float, odds: float) -> float:
    """计算单笔投注的期望值。"""
    if odds <= 1.0:
        return 0.0
    return prob * (odds - 1.0) - (1.0 - prob)


def remove_vig(probabilities: Iterable[float], min_prob: float = 0.02) -> list[float]:
    """去除盘口水位，得到净市场概率。"""
    probs = [max(0.0, float(p)) for p in probabilities]
    total = sum(probs)
    if total <= 0:
        return [1.0 / len(probs) for _ in probs]
    normalized = [p / total for p in probs]
    return [max(min_prob, min(1.0 - min_prob, p)) for p in normalized]


def remove_vig_two_way(odds_a: float, odds_b: float, min_prob: float = 0.02):
    probs = remove_vig([implied_probability(odds_a), implied_probability(odds_b)], min_prob)
    return tuple(probs)


def remove_vig_three_way(home_odds: float, draw_odds: float, away_odds: float, min_prob: float = 0.02):
    probs = remove_vig([implied_probability(home_odds), implied_probability(draw_odds), implied_probability(away_odds)], min_prob)
    return tuple(probs)
