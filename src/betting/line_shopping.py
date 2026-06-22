"""Line Shopping 扫描器 — Pinnacle (sharp) vs 零售最佳赔率，发现 +EV 机会。

核心理念：Pinnacle 是最 sharp 的博彩公司，其去 vig 概率 ≈ 真实概率。
当零售博彩公司（Bet365、WH 等）提供比 Pinnacle 更优的赔率时，存在 +EV 机会。

用法:
    from src.betting.line_shopping import LineShoppingScanner
    scanner = LineShoppingScanner()
    opps = scanner.scan()
    scanner.save_results()

与现有管线的集成:
    run_all.py → fetch_football_odds() 已拉取 BSD 赔率
    line_shopping.py 复用 BSD 原始赔率数据做 Pinnacle vs Retail 比较
    结果输出到 line_shopping_results.json 并同步到 arbitrage_log.json
    rank_recommendations.py 自动消费 arbitrage_log.json 中的 line_shopping 条目
"""
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR
from fetchers.bsd_fetcher import fetch_upcoming_events, fetch_event_odds

logger = get_logger(__name__)

CACHE_DIR = DATA_DIR / "odds"
RAW_CACHE_DIR = CACHE_DIR / "bsd_raw"
RAW_CACHE_TTL = 1800  # 30 分钟

PINNACLE_SLUG = "pinnacle"

RETAIL_SLUGS = [
    "bet365", "william-hill", "betvictor", "unibet", "sportingbet",
    "bwin", "ladbrokes", "betfair", "coral",
]

MIN_EV = 0.02
MAX_OPPORTUNITIES = 20


def _remove_vig(h_odds: float, d_odds: float, a_odds: float):
    """去除抽水 (vig)，返回真实概率和抽水比例。"""
    imp_h, imp_d, imp_a = 1.0 / h_odds, 1.0 / d_odds, 1.0 / a_odds
    vig = imp_h + imp_d + imp_a - 1.0
    if vig <= 0:
        return imp_h, imp_d, imp_a, 0.0
    return imp_h / (1 + vig), imp_d / (1 + vig), imp_a / (1 + vig), vig


class LineShoppingScanner:
    """Line Shopping 扫描器 — 比较 Pinnacle vs 零售最佳赔率。"""

    def __init__(self, min_ev: float = MIN_EV):
        self.min_ev = min_ev
        self.opportunities: List[Dict] = []

    def _load_raw_odds_cached(self, event_id: int) -> Optional[dict]:
        """带文件缓存的 BSD 原始赔率读取，避免重复 API 请求。"""
        RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = RAW_CACHE_DIR / f"event_{event_id}.json"

        now = time.time()
        if cache_file.exists() and now - cache_file.stat().st_mtime < RAW_CACHE_TTL:
            try:
                return json.loads(cache_file.read_text())
            except Exception:
                pass

        odds = fetch_event_odds(event_id)
        if odds:
            cache_file.write_text(json.dumps(odds, indent=2))
        return odds

    def _get_pinnacle_odds(self, h2h_market: dict) -> Optional[Dict[str, float]]:
        """从 BSD 1x2 市场数据中提取 Pinnacle 赔率。"""
        h = (h2h_market.get("HOME", {}).get("bookmakers", {})
             .get(PINNACLE_SLUG, {}).get("decimal_odds"))
        a = (h2h_market.get("AWAY", {}).get("bookmakers", {})
             .get(PINNACLE_SLUG, {}).get("decimal_odds"))
        d = (h2h_market.get("DRAW", {}).get("bookmakers", {})
             .get(PINNACLE_SLUG, {}).get("decimal_odds"))

        if h and a and d:
            return {"home": float(h), "away": float(a), "draw": float(d)}
        return None

    def _get_best_retail(self, h2h_market: dict) -> Dict[str, float]:
        """从 BSD 1x2 市场数据中获取每个结果的最佳零售赔率及来源。"""
        best = {"home": 0.0, "away": 0.0, "draw": 0.0,
                "home_bm": "", "away_bm": "", "draw_bm": ""}

        for side_key, outcome in [("HOME", "home"), ("AWAY", "away"), ("DRAW", "draw")]:
            bm = h2h_market.get(side_key, {}).get("bookmakers", {})
            best_odds, best_bm = 0.0, ""
            for slug in RETAIL_SLUGS:
                odds_data = bm.get(slug, {}).get("decimal_odds")
                if odds_data:
                    odds = float(odds_data)
                    if odds > best_odds:
                        best_odds = odds
                        best_bm = slug
            if best_odds > 0:
                best[outcome] = best_odds
                best[f"{outcome}_bm"] = best_bm

        return best

    def _evaluate_match(self, event_info: dict, raw_odds: dict) -> List[Dict]:
        """评估单场比赛，返回 +EV 机会列表。"""
        markets = raw_odds.get("markets", {})
        h2h = markets.get("1x2")
        if not h2h:
            return []

        pinny = self._get_pinnacle_odds(h2h)
        if not pinny:
            return []

        pinny_h, pinny_d, pinny_a, vig = _remove_vig(
            pinny["home"], pinny["draw"], pinny["away"]
        )

        retail = self._get_best_retail(h2h)
        opportunities = []

        for outcome, pinny_prob, retail_key in [
            ("home", pinny_h, "home"),
            ("draw", pinny_d, "draw"),
            ("away", pinny_a, "away"),
        ]:
            retail_odds = retail[retail_key]
            if retail_odds <= 0:
                continue

            retail_implied = 1.0 / retail_odds
            ev = (pinny_prob - retail_implied) / retail_implied

            if ev <= self.min_ev:
                continue

            kelly = (pinny_prob * retail_odds - 1) / (retail_odds - 1) * 0.25
            if kelly <= 0:
                continue

            opportunities.append({
                "type": "line_shopping",
                "sport": "football",
                "league": event_info.get("league_name", ""),
                "home_team": event_info["home_team"],
                "away_team": event_info["away_team"],
                "outcome": outcome,
                "commence_time": event_info.get("commence_time", ""),
                "pinny_home_odds": pinny["home"],
                "pinny_draw_odds": pinny["draw"],
                "pinny_away_odds": pinny["away"],
                "pinny_prob_home": round(pinny_h, 4),
                "pinny_prob_draw": round(pinny_d, 4),
                "pinny_prob_away": round(pinny_a, 4),
                "retail_odds": round(retail_odds, 4),
                "retail_bookmaker": retail[f"{retail_key}_bm"],
                "edge_pct": round(ev * 100, 2),
                "kelly_pct": round(kelly * 100, 2),
                "vig_pct": round(vig * 100, 2),
                "_ev": ev,
                "_kelly_frac": kelly,
                "odds": retail_odds,
                "model_prob": round(pinny_prob, 4),
                "mkt_prob": round(retail_implied, 4),
            })

        return opportunities

    def scan(self) -> List[Dict]:
        """扫描即将开始的比赛，返回 +EV 机会列表。"""
        logger.info("─" * 60)
        logger.info("🔍 Line Shopping 扫描 (Pinnacle vs 零售最佳)")
        logger.info("─" * 60)

        events = fetch_upcoming_events(hours_ahead=72)
        if not events:
            logger.info("  无即将开始的足球赛事")
            return []

        logger.info("  赛事: %d 场", len(events))

        opportunities = []
        n_with_pinnacle = 0
        for ev in events:
            raw = self._load_raw_odds_cached(ev["id"])
            if not raw:
                continue

            h2h = raw.get("markets", {}).get("1x2")
            if not h2h:
                continue

            if self._get_pinnacle_odds(h2h):
                n_with_pinnacle += 1

            opps = self._evaluate_match(ev, raw)
            opportunities.extend(opps)

        opportunities.sort(key=lambda x: x["edge_pct"], reverse=True)
        opportunities = opportunities[:MAX_OPPORTUNITIES]

        logger.info("  含 Pinnacle 赔率: %d 场", n_with_pinnacle)
        logger.info("  +EV 机会: %d 条", len(opportunities))

        if opportunities:
            t = opportunities[0]
            outcome_cn = {"home": "主胜", "draw": "平局", "away": "客胜"}
            logger.info("  最佳: %s vs %s [%s] edge=%.1f%% odds=%.2f (%s)",
                       t["home_team"], t["away_team"],
                       outcome_cn.get(t["outcome"], t["outcome"]),
                       t["edge_pct"], t["retail_odds"], t["retail_bookmaker"])

        self.opportunities = opportunities
        return opportunities

    def save_results(self):
        """保存并同步到 arbitrage_log.json 供排名系统消费。"""
        path = DATA_DIR / "line_shopping_results.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "updated": pd.Timestamp.now().isoformat(),
            "total": len(self.opportunities),
            "opportunities": self.opportunities,
        }, ensure_ascii=False, indent=2))
        logger.info("  ✅ 结果已保存: %s (%d 条)", path.name, len(self.opportunities))

        self._sync_to_arbitrage_log()

    def _sync_to_arbitrage_log(self):
        """同步 line shopping 条目到 arbitrage_log.json。"""
        arb_path = DATA_DIR / "arbitrage_log.json"
        existing = {"opportunities": []}
        if arb_path.exists():
            try:
                existing = json.loads(arb_path.read_text())
            except Exception:
                pass

        existing["opportunities"] = [
            o for o in existing.get("opportunities", [])
            if o.get("type") != "line_shopping"
        ]

        for opp in self.opportunities:
            existing["opportunities"].append({
                "type": "line_shopping",
                "_sport": "football",
                "home_team": opp["home_team"],
                "away_team": opp["away_team"],
                "outcome": opp["outcome"],
                "best_price": opp["retail_odds"],
                "price_gap": opp["edge_pct"] / 100,
                "edge": opp["_ev"],
                "bookmaker": opp["retail_bookmaker"],
                "commence_time": opp.get("commence_time", ""),
            })

        arb_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        logger.info("  ✅ 已同步到 arbitrage_log.json (%d 条 line_shopping)",
                   len(self.opportunities))


def run_line_shopping() -> List[Dict]:
    """便捷入口：扫描 + 保存，返回机会列表。"""
    scanner = LineShoppingScanner()
    opps = scanner.scan()
    if opps:
        scanner.save_results()
    return opps


if __name__ == "__main__":
    opps = run_line_shopping()
    if opps:
        print(f"发现 {len(opps)} 条 +EV 机会")
    else:
        print("未发现 +EV 机会")
