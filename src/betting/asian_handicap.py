"""亚洲盘口支持 — 从 Odds API 提取 AH 赔率 + 计算 EV。

亚洲盘口（Asian Handicap）是职业博彩的主流市场：
  - 消除平局选项，通过让球均衡双方
  - 盘口类型：整数(-1, +1)、半球(-0.5, +0.5)、四分之一球(-0.25, +0.75)
  - 四分之一盘口 = 拆分为两个相邻盘口各一半仓位

用法:
    from src.betting.asian_handicap import extract_ah_odds, compute_ah_ev
    ah_data = extract_ah_odds(odds_data)  # 从 Odds API 响应提取
    ev = compute_ah_ev(ah_data, model_probs)  # 计算期望值
"""
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np

from config.logging_config import get_logger

logger = get_logger(__name__)

# 常见亚洲盘口值
HANDICAP_VALUES = [-3.0, -2.75, -2.5, -2.25, -2.0, -1.75, -1.5, -1.25,
                   -1.0, -0.75, -0.5, -0.25, 0.0,
                   0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0]


def extract_ah_odds(odds_data: List[Dict]) -> List[Dict]:
    """从 Odds API 响应中提取亚洲盘口赔率。

    Odds API 的 spreads 市场对应于足球的亚洲盘口：
      - point = -0.5 → AH -0.5（主队让半球）
      - point = +0.5 → AH +0.5（客队受让半球）

    Args:
        odds_data: Odds API 原始响应

    Returns:
        [{match_key, home_team, away_team, handicap, home_odds, away_odds, n_bookmakers}]
    """
    results = []
    for game in odds_data:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        match_key = f"{home} @ {away}"
        commence = game.get("commence_time", "")
        bookmakers = game.get("bookmakers", [])

        # 跨所有博彩公司找最优 AH 赔率
        best_ah = {}  # {handicap: {home_odds: [], away_odds: []}}
        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market.get("key") != "spreads":
                    continue
                outcomes = market.get("outcomes", [])
                for out in outcomes:
                    oname = out.get("name", "").strip().lower()
                    point = out.get("point")
                    price = out.get("price")
                    if point is None or price is None:
                        continue

                    hcp = float(point)
                    if hcp not in best_ah:
                        best_ah[hcp] = {"home_odds": [], "away_odds": []}

                    if oname == home.strip().lower():
                        best_ah[hcp]["home_odds"].append(price)
                    elif oname == away.strip().lower():
                        best_ah[hcp]["away_odds"].append(price)

        for hcp, odds_dict in best_ah.items():
            home_odds_list = odds_dict["home_odds"]
            away_odds_list = odds_dict["away_odds"]
            if not home_odds_list or not away_odds_list:
                continue

            results.append({
                "match_key": match_key,
                "home_team": home,
                "away_team": away,
                "commence_time": commence,
                "handicap": hcp,
                "home_odds": max(home_odds_list),
                "away_odds": max(away_odds_list),
                "n_bookmakers": min(len(home_odds_list), len(away_odds_list)),
                # 市场隐含概率
                "implied_home_prob": 1.0 / max(home_odds_list) if max(home_odds_list) > 0 else 0.5,
                "implied_away_prob": 1.0 / max(away_odds_list) if max(away_odds_list) > 0 else 0.5,
            })

    return results


def compute_ah_ev(ah_odds: Dict, model_home_cover_prob: float) -> Dict:
    """计算亚洲盘口的期望值。

    Args:
        ah_odds: extract_ah_odds 返回的单条记录
        model_home_cover_prob: 模型预测的主队覆盖概率

    Returns:
        {market, home_ev, away_ev, kelly_stake_pct, edge, verdict}
    """
    home_odds = ah_odds.get("home_odds", 0)
    away_odds = ah_odds.get("away_odds", 0)
    hcp = ah_odds.get("handicap", 0)

    if home_odds <= 0 or away_odds <= 0:
        return {"error": "无效赔率"}

    market_home_prob = 1.0 / home_odds
    model_away_prob = 1.0 - model_home_cover_prob

    home_ev = model_home_cover_prob * home_odds - 1.0
    away_ev = model_away_prob * away_odds - 1.0

    # 凯利仓位
    b_home = home_odds - 1.0
    kelly_home = (model_home_cover_prob * b_home - (1 - model_home_cover_prob)) / b_home if b_home > 0 else 0

    b_away = away_odds - 1.0
    kelly_away = (model_away_prob * b_away - (1 - model_away_prob)) / b_away if b_away > 0 else 0

    return {
        "handicap": hcp,
        "home_odds": home_odds,
        "away_odds": away_odds,
        "market_home_prob": round(market_home_prob, 4),
        "model_home_prob": round(model_home_cover_prob, 4),
        "home_ev": round(home_ev, 4),
        "away_ev": round(away_ev, 4),
        "home_kelly": round(kelly_home, 4),
        "away_kelly": round(kelly_away, 4),
        "verdict": "home_value" if home_ev > 0.05 else ("away_value" if away_ev > 0.05 else "no_value"),
    }


def splits_quarter_handicap(handicap: float) -> List[Tuple[float, float]]:
    """拆分四分之一盘口为两个半盘口。

    例如: -0.75 → [(-0.5, 0.5), (-1.0, 0.5)]
    """
    if handicap % 0.5 != 0.25:
        return [(handicap, 1.0)]  # 不是四分之一盘口

    lower = np.floor(handicap * 2) / 2
    upper = lower + 0.5
    return [(lower, 0.5), (upper, 0.5)]
