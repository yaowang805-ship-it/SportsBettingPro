"""备选盘口 EV 检查 — 从 Odds API alternate_spreads/alternate_totals 找更优 EV。

职业博彩中，备选盘口（alternative lines）提供比标准盘口更好的赔率。
例如模型预测主队覆盖 -5.5 (55%)，但 -4.5 的赔率可能产生更高 EV。

用法:
    finder = AltLineFinder(odds_data)
    best = finder.find_best_spread(
        home_team="Miami Heat", away_team="Boston Celtics",
        model_prob=0.55, market_line=-5.5, side="home"
    )
    if best:
        # best = {line, odds, ev, adj_prob, n_bookmakers}
"""

import numpy as np
from typing import Dict, List, Optional


# ── 概率调整系数（基于 NBA 历史数据） ──────────────────────────
# 让分盘：每偏离标准盘口 1 分 ≈ 4% 概率变化（50% 附近线性区）
# 总分盘：每偏离标准总分 1 分 ≈ 3% 概率变化
# 这些系数足够保守，防止过度自信的线调整
NBA_SPREAD_PROB_PER_POINT = 0.04
NBA_TOTAL_PROB_PER_POINT = 0.03


def _normalize_team(name: str) -> str:
    return name.strip().lower()


class AltLineFinder:
    """备选盘口 EV 搜索器。

    对给定的比赛+模型预测，在各博彩公司的备选盘口中搜索
    比标准盘口更优的 EV。
    """

    def __init__(self, odds_data: List[Dict]):
        self.odds_data = odds_data or []

    def _find_game(self, home_team: str, away_team: str) -> Optional[Dict]:
        h = _normalize_team(home_team)
        a = _normalize_team(away_team)
        for game in self.odds_data:
            if (_normalize_team(game.get("home_team", "")) == h
                    and _normalize_team(game.get("away_team", "")) == a):
                return game
        return None

    # ── 备选让分盘 ──────────────────────────────────────────

    def _get_alt_spreads(self, game: Dict, home_team: str, away_team: str):
        """提取所有博彩公司的备选让分盘口。

        Returns:
            {point: [(price_home, price_away), ...]}
            point = 主队让分值（负=主队让分，正=主队受让）
        """
        home_l = _normalize_team(home_team)
        away_l = _normalize_team(away_team)
        lines: Dict[float, list] = {}

        for bm in game.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") not in ("alternate_spreads",):
                    continue
                outcomes = market.get("outcomes", [])

                # 按 point 分组
                by_pt: Dict[float, dict] = {}
                for o in outcomes:
                    name = _normalize_team(o.get("name", ""))
                    price = o.get("price")
                    pt = o.get("point")
                    if price is None or pt is None:
                        continue
                    key = "home" if name == home_l else "away" if name == away_l else None
                    if key:
                        by_pt.setdefault(pt, {})[key] = price

                for pt, pair in by_pt.items():
                    if "home" in pair and "away" in pair:
                        lines.setdefault(pt, []).append((pair["home"], pair["away"]))

        return lines

    def _adjust_prob(self, model_prob: float, line_diff: float, per_point: float) -> float:
        """line_diff = alt_point - market_line（正 = 更易覆盖）"""
        return float(np.clip(model_prob + line_diff * per_point, 0.01, 0.99))

    def find_best_spread(self, home_team: str, away_team: str,
                         model_prob: float, market_line: float,
                         side: str = 'home',
                         prob_per_point: float = NBA_SPREAD_PROB_PER_POINT) -> Optional[Dict]:
        """查找最佳备选让分盘口。

        Args:
            home_team, away_team: 比赛双方
            model_prob: **主队**覆盖标准盘口的模型概率
            market_line: 标准盘口（如 -5.5 表示主队让 5.5 分）
            side: 'home' 或 'away'
            prob_per_point: 每偏离 1 分的概率调整

        Returns:
            {line, odds, ev, adj_prob, n_bookmakers} 或 None
        """
        game = self._find_game(home_team, away_team)
        if not game:
            return None

        alt_lines = self._get_alt_spreads(game, home_team, away_team)
        if not alt_lines:
            return None

        best = None
        best_ev = -1.0

        for alt_point, price_pairs in alt_lines.items():
            if side == 'home':
                # 主队覆盖 alt_point：正 diff = 更容易
                line_diff = -(alt_point - market_line)
                # 例如 market_line=-5.5 → alt_point=-4.5: diff=+1.0
                # 例如 market_line=-5.5 → alt_point=-6.5: diff=-1.0
                adj_prob = self._adjust_prob(model_prob, line_diff, prob_per_point)
                odds_list = [h for h, a in price_pairs]
            else:
                # 客队覆盖：从主队视角转换
                # 主队覆盖 alt_point 的概率
                # 如果 alt_point 更负（主队让更多），主队覆盖更难
                home_line_diff = -(alt_point - market_line)  # 主队视角的 diff
                adj_home_prob = self._adjust_prob(model_prob, home_line_diff, prob_per_point)
                adj_prob = 1.0 - adj_home_prob
                odds_list = [a for h, a in price_pairs]

            if not odds_list:
                continue

            # 用最佳赔率（最高价 = 最好回报）
            best_price = max(odds_list)
            ev = adj_prob * best_price - 1.0

            if ev > best_ev:
                best_ev = ev
                best = {
                    "line": alt_point,
                    "odds": best_price,
                    "ev": ev,
                    "adj_prob": adj_prob,
                    "n_bookmakers": len(price_pairs),
                }

        return best if best_ev > 0 else None

    # ── 备选总分盘 ──────────────────────────────────────────

    def _get_alt_totals(self, game: Dict):
        """提取所有博彩公司的备选总分盘口。

        Returns:
            {point: [(price_over, price_under), ...]}
        """
        lines: Dict[float, list] = {}

        for bm in game.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") not in ("alternate_totals",):
                    continue
                outcomes = market.get("outcomes", [])

                by_pt: Dict[float, dict] = {}
                for o in outcomes:
                    name = o.get("name", "")
                    price = o.get("price")
                    pt = o.get("point")
                    if price is None or pt is None:
                        continue
                    key = "over" if name == "Over" else "under" if name == "Under" else None
                    if key:
                        by_pt.setdefault(pt, {})[key] = price

                for pt, pair in by_pt.items():
                    if "over" in pair and "under" in pair:
                        lines.setdefault(pt, []).append((pair["over"], pair["under"]))

        return lines

    def find_best_total(self, home_team: str, away_team: str,
                        model_prob: float, market_line: float,
                        side: str = 'over',
                        prob_per_point: float = NBA_TOTAL_PROB_PER_POINT) -> Optional[Dict]:
        """查找最佳备选总分盘口。

        Args:
            home_team, away_team: 比赛双方
            model_prob: 大球（over）覆盖标准总分的模型概率
            market_line: 标准总分线（如 220.5）
            side: 'over' 或 'under'
            prob_per_point: 每偏离 1 分的概率调整

        Returns:
            {line, odds, ev, adj_prob, n_bookmakers} 或 None
        """
        game = self._find_game(home_team, away_team)
        if not game:
            return None

        alt_lines = self._get_alt_totals(game)
        if not alt_lines:
            return None

        best = None
        best_ev = -1.0

        for alt_point, price_pairs in alt_lines.items():
            if side == 'over':
                # 总分更低 = 更容易大球
                # 标准 220.5 → 备选 219.5: diff=+1.0（更容易）
                line_diff = market_line - alt_point
                adj_prob = self._adjust_prob(model_prob, line_diff, prob_per_point)
                odds_list = [o for o, u in price_pairs]
            else:
                # 小球：总分更高 = 更容易小球
                # 标准 220.5 → 备选 221.5: diff=+1.0（更容易）
                line_diff = alt_point - market_line
                adj_prob = self._adjust_prob(model_prob, line_diff, prob_per_point)
                odds_list = [u for o, u in price_pairs]

            if not odds_list:
                continue

            best_price = max(odds_list)
            ev = adj_prob * best_price - 1.0

            if ev > best_ev:
                best_ev = ev
                best = {
                    "line": alt_point,
                    "odds": best_price,
                    "ev": ev,
                    "adj_prob": adj_prob,
                    "n_bookmakers": len(price_pairs),
                }

        return best if best_ev > 0 else None
