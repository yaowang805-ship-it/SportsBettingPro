from datetime import datetime
from typing import List, Dict, Any
from .models import Match, Odds


def _find_best_h2h(bookmakers: List[Dict], home: str) -> tuple:
    """遍历所有博彩公司，找到最高的主胜赔率及公司名。"""
    best = None
    best_bm = None
    for bm in bookmakers:
        bm_name = bm.get("title", bm.get("key", "未知"))
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for out in market.get("outcomes", []):
                if out.get("name", "").strip().lower() == home.strip().lower():
                    price = out.get("price")
                    if price and (best is None or price > best):
                        best = price
                        best_bm = bm_name
    return best, best_bm


def _find_best_spread(bookmakers: List[Dict], home: str) -> tuple:
    """遍历所有博彩公司，找到最佳的让分盘赔率（兼顾盘口分和赔率）。"""
    best_odds = None
    best_point = None
    best_score = 0.0
    best_bm = None
    for bm in bookmakers:
        bm_name = bm.get("title", bm.get("key", "未知"))
        for market in bm.get("markets", []):
            if market.get("key") != "spreads":
                continue
            for out in market.get("outcomes", []):
                if out.get("name", "").strip().lower() != home.strip().lower():
                    continue
                pt = out.get("point")
                od = out.get("price")
                if pt is None or od is None:
                    continue
                score = od + abs(pt) * 0.3
                if score > best_score:
                    best_score = score
                    best_point = pt
                    best_odds = od
                    best_bm = bm_name
    return best_point, best_odds, best_bm


def _find_best_total(bookmakers: List[Dict]) -> tuple:
    """遍历所有博彩公司，找到最佳的大小球赔率。"""
    best_odds = None
    best_point = None
    best_score = 0.0
    best_bm = None
    for bm in bookmakers:
        bm_name = bm.get("title", bm.get("key", "未知"))
        for market in bm.get("markets", []):
            if market.get("key") != "totals":
                continue
            outcomes = market.get("outcomes", [])
            pt = market.get("point") or (outcomes[0].get("point") if outcomes else None)
            for out in outcomes:
                if out.get("name") != "Over":
                    continue
                od = out.get("price")
                if pt is None or od is None:
                    continue
                # 对大球，用赔率 × 0.7 + 总分 × 0.3 作综合评分
                score = od + float(pt) * 0.3
                if score > best_score:
                    best_score = score
                    best_point = pt
                    best_odds = od
                    best_bm = bm_name
    return best_point, best_odds, best_bm


class OddsNormalizer:
    @staticmethod
    def from_api_response(raw_json: List[Dict[str, Any]]) -> List[Match]:
        """标准化赔率数据，从多家博彩公司中选取最优赔率。"""
        matches = []
        for item in raw_json:
            home = item.get("home_team", "")
            away = item.get("away_team", "")
            commence = item.get("commence_time", "")
            date = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            bookmakers = item.get("bookmakers", [])

            if not bookmakers:
                matches.append(Match(date=date, home_team=home, away_team=away, odds=None))
                continue

            h2h_home, h2h_bm = _find_best_h2h(bookmakers, home)
            spread_point, spread_odds, spread_bm = _find_best_spread(bookmakers, home)
            total_point, over_odds, total_bm = _find_best_total(bookmakers)

            odds = None
            if h2h_home is not None:
                odds = Odds(
                    home_odds=float(h2h_home),
                    spread_point=float(spread_point or 0.0),
                    spread_odds=float(spread_odds or 0.0),
                    total_point=float(total_point or 0.0),
                    over_odds=float(over_odds or 0.0),
                    bookmaker=h2h_bm or "",
                    spread_bookmaker=spread_bm or "",
                    total_bookmaker=total_bm or "",
                )
            matches.append(Match(date=date, home_team=home, away_team=away, odds=odds))
        return matches


def find_best_odds(match_data: Dict, market_type: str = 'h2h') -> tuple:
    """为单场比赛查找指定市场的最优赔率和博彩公司。

    Args:
        match_data: Odds API 单场比赛的原始 JSON
        market_type: 'h2h' / 'spreads' / 'totals'

    Returns:
        (best_odds_value, bookmaker_name, point) 或 (None, None, None)
    """
    bookmakers = match_data.get("bookmakers", [])
    home = match_data.get("home_team", "")
    if not bookmakers or not home:
        return None, None, None

    best_price = None
    best_bm = None
    best_point = None

    for bm in bookmakers:
        bm_name = bm.get("title", bm.get("key", "未知"))
        for market in bm.get("markets", []):
            if market.get("key") != market_type:
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name", "").strip().lower()
                if market_type == 'h2h':
                    if name == home.strip().lower():
                        price = outcome.get("price")
                        if price and (best_price is None or price > best_price):
                            best_price = price
                            best_bm = bm_name
                elif market_type == 'spreads':
                    if name == home.strip().lower():
                        price = outcome.get("price")
                        pt = outcome.get("point")
                        if price and (best_price is None or price > best_price):
                            best_price = price
                            best_point = pt
                            best_bm = bm_name
                elif market_type == 'totals' and outcome.get("name") == "Over":
                    price = outcome.get("price")
                    pt = outcome.get("point")
                    if price and (best_price is None or price > best_price):
                        best_price = price
                        best_point = pt
                        best_bm = bm_name

    return best_price, best_bm, best_point


def get_bookmaker_list(match_data: Dict) -> list:
    """返回指定比赛的所有可用博彩公司列表。"""
    return [bm.get("title", bm.get("key", "未知"))
            for bm in match_data.get("bookmakers", [])]
