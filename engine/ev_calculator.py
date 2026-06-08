import numpy as np
from config.settings import SHRINK_BB, SHRINK_FB

def calculate_ev(model_prob, market_odds, shrink_coef=SHRINK_BB):
    implied_prob = 1.0 / market_odds
    shrunk_prob = (1 - shrink_coef) * model_prob + shrink_coef * implied_prob
    ev = (shrunk_prob * (market_odds - 1)) - (1 - shrunk_prob)
    return ev, shrunk_prob

def kelly_criterion(prob, odds, fraction=0.5):
    b = odds - 1
    q = 1 - prob
    f = (b * prob - q) / b
    return max(0, f * fraction)
