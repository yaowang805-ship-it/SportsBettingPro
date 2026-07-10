"""Line Shopping 扫描器 — Pinnacle (sharp) vs 零售最佳赔率，发现 +EV 机会。

支持盘口:
  - 1x2（独赢）：已支持
  - Over/Under（大小球）：已支持（1.5/2.5/3.5）
  - Asian Handicap（让球盘）：BSD API 未提供

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR, DINGTALK_WEBHOOK, TRUSTED_LEAGUES
from src.core.team_names import cn_team
from fetchers.bsd_fetcher import fetch_upcoming_events, fetch_event_odds

logger = get_logger(__name__)

CACHE_DIR = DATA_DIR / "odds"
RAW_CACHE_DIR = CACHE_DIR / "bsd_raw"
RAW_CACHE_TTL = 1800  # 30 分钟

EDGE_HISTORY_FILE = DATA_DIR / "edge_history.json"
EDGE_HISTORY_MAX_AGE = 3600 * 6  # 保留 6 小时内的历史

PINNACLE_SLUG = "pinnacle"

RETAIL_SLUGS = [
    "bet365", "william-hill", "betvictor", "unibet", "sportingbet",
    "bwin", "ladbrokes", "betfair", "coral",
    "betsson", "marathon", "interwetten", "novibet",
    "888sport", "betano", "betway", "1xbet",
    "consensus", "oddssafari-consensus",
]

MIN_EV = 0.02
MAX_OPPORTUNITIES = 300  # 含1x2 + 大小球 + BTTS + 双边 + 无平 + 角球

# Over/Under 盘口配置：(市场 key, 盘口线, 显示名称)
OVER_UNDER_MARKETS = [
    ("over_under_25", 2.5, "O/U 2.5"),
    ("over_under_15", 1.5, "O/U 1.5"),
    ("over_under_35", 3.5, "O/U 3.5"),
]

# BTTS 双方进球配置：侧键名, 结果标签
BTTS_SIDES = {"yes": "双方进球", "no": "不进球"}

# 单场比赛最大暴露比例（避免同一个比赛多个盘口投注过多）
MAX_EXPOSURE_PER_MATCH = 0.30  # 日预算的 30%


def _remove_vig_2way(odds1: float, odds2: float) -> Tuple[float, float, float]:
    """2-way 市场去抽水（大小球、DNB 等）。返回 (真实概率1, 真实概率2, 抽水比例)。"""
    imp1, imp2 = 1.0 / odds1, 1.0 / odds2
    vig = imp1 + imp2 - 1.0
    if vig <= 0:
        return imp1, imp2, 0.0
    return imp1 / (1 + vig), imp2 / (1 + vig), vig


def _remove_vig(h_odds: float, d_odds: float, a_odds: float):
    """3-way 市场去抽水（1x2）。返回 (真实概率H, 真实概率D, 真实概率A, 抽水比例)。"""
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

    def _get_pinnacle_2way(self, market_data: dict, side1_key: str, side2_key: str) -> Optional[Dict[str, float]]:
        """从 BSD 2-way 市场（大小球等）提取 Pinnacle 赔率。"""
        s1 = (market_data.get(side1_key, {}).get("bookmakers", {})
              .get(PINNACLE_SLUG, {}).get("decimal_odds"))
        s2 = (market_data.get(side2_key, {}).get("bookmakers", {})
              .get(PINNACLE_SLUG, {}).get("decimal_odds"))
        if s1 and s2:
            return {side1_key: float(s1), side2_key: float(s2)}
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

    def _get_best_retail_2way(self, market_data: dict, side1_key: str, side2_key: str) -> dict:
        """从 BSD 2-way 市场获取最佳零售赔率及来源。"""
        result = {}
        for side_key in [side1_key, side2_key]:
            bm = market_data.get(side_key, {}).get("bookmakers", {})
            best_odds, best_bm = 0.0, ""
            for slug in RETAIL_SLUGS:
                odds_data = bm.get(slug, {}).get("decimal_odds")
                if odds_data:
                    odds = float(odds_data)
                    if odds > best_odds:
                        best_odds = odds
                        best_bm = slug
            result[side_key] = {"odds": best_odds, "bookmaker": best_bm}
        return result

    MAX_EDGE_PCT = 30.0  # 超过此值的 edge 视为数据错误
    MAX_PINNY_STALE_HOURS = 12  # Pinnacle 数据超过此小时数视为过期
    MAX_RETAIL_RATIO = 6.0  # 零售赔率 > Pinnacle赔率×此值 → 数据异常

    @staticmethod
    def _get_pinny_updated_at(market_data: dict, side_key: str) -> Optional[str]:
        """获取 Pinnacle 某个结果的最后更新时间。"""
        bks = market_data.get(side_key, {}).get("bookmakers", {})
        if isinstance(bks, dict):
            return bks.get("pinnacle", {}).get("updated_at")
        return None

    def _is_pinny_stale(self, market_data: dict, side_keys: list) -> bool:
        """检查 Pinnacle 数据是否过期。"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for sk in side_keys:
            updated = self._get_pinny_updated_at(market_data, sk)
            if updated:
                try:
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    age = (now - dt).total_seconds() / 3600
                    if age > self.MAX_PINNY_STALE_HOURS:
                        return True
                except Exception:
                    pass
        return False

    def _validate_opportunity(self, opp: dict, pinny: dict,
                               market_data: Optional[dict] = None) -> bool:
        """数据纠错校验，过滤明显错误的赔率数据。"""
        market = opp.get("market", "1x2")

        # 1. Edge 超过阈值 — 检查是否因 Pinnacle 过期导致
        if opp["edge_pct"] > self.MAX_EDGE_PCT:
            stale = False
            if market_data:
                if market == "1x2":
                    stale = self._is_pinny_stale(market_data, ["HOME", "AWAY", "DRAW"])
                elif market == "over_under":
                    line = opp.get("line", 2.5)
                    stale = self._is_pinny_stale(market_data, [f"over@{line}", f"under@{line}"])
                elif market == "btts":
                    stale = self._is_pinny_stale(market_data, ["yes", "no"])
            reason = "Pinnacle数据过期" if stale else "异常过高"
            logger.warning("  ⛔ 过滤 [%s vs %s %s]: edge=%.1f%% %s",
                          opp["home_team"], opp["away_team"],
                          opp.get("outcome_label", opp["outcome"]),
                          opp["edge_pct"], reason)
            return False

        # 2. 模型概率 > 90% 但零售赔率 > 2.0 → 数据不匹配
        if opp["model_prob"] > 0.90 and opp["retail_odds"] > 2.0:
            logger.warning("  ⛔ 过滤 [%s vs %s %s]: 概率=%.0f%% 但赔率=%.2f 不一致",
                          opp["home_team"], opp["away_team"],
                          opp.get("outcome_label", opp["outcome"]),
                          opp["model_prob"] * 100, opp["retail_odds"])
            return False

        # 3. 零售赔率异常高（对比 Pinnacle）
        pinny_ref = None
        if market == "1x2":
            pinny_ref = pinny.get(opp.get("outcome", "")) if isinstance(pinny, dict) else None
        elif market == "over_under":
            k = f"{opp['outcome']}@{opp['line']}"
            pinny_ref = pinny.get(k) if isinstance(pinny, dict) else None
        elif market == "btts":
            pinny_ref = pinny.get(opp.get("outcome", "")) if isinstance(pinny, dict) else None
        if pinny_ref and pinny_ref > 1.0 and opp["retail_odds"] / pinny_ref > self.MAX_RETAIL_RATIO:
            logger.warning("  ⛔ 过滤 [%s vs %s %s]: 零售赔率 %.2f / Pinnacle %.2f = %.1fx 过高",
                          opp["home_team"], opp["away_team"],
                          opp.get("outcome_label", opp["outcome"]),
                          opp["retail_odds"], pinny_ref,
                          opp["retail_odds"] / pinny_ref)
            return False

        # 4. 零售赔率 < 1.01
        if opp["retail_odds"] < 1.01:
            return False

        # 5. Pinnacle 赔率自身一致性
        if market == "1x2" and isinstance(pinny, dict):
            for k in ["home", "draw", "away"]:
                v = pinny.get(k)
                if v and v < 1.01:
                    return False
        # O/U / BTTS 的一致性在各自 evaluate 方法里已处理

        return True

    # ── Edge 衰退追踪 ───────────────────────────────────

    def _load_edge_history(self) -> dict:
        """加载历史 edge 快照。"""
        now = time.time()
        if EDGE_HISTORY_FILE.exists():
            try:
                data = json.loads(EDGE_HISTORY_FILE.read_text())
                # 清理过期条目
                cutoff = now - EDGE_HISTORY_MAX_AGE
                for k in list(data.get("snapshots", {}).keys()):
                    data["snapshots"][k] = [s for s in data["snapshots"][k]
                                             if s.get("t", 0) > cutoff]
                    if not data["snapshots"][k]:
                        del data["snapshots"][k]
                return data
            except Exception:
                pass
        return {"snapshots": {}, "last_scan": None}

    def _save_edge_history(self, opportunities: List[Dict]):
        """保存本次扫描的 edge 快照。"""
        data = self._load_edge_history()
        now_t = time.time()
        for o in opportunities:
            key = f"{o['home_team']}_{o['away_team']}_{o.get('market', '1x2')}_{o['outcome']}"
            key = key.replace(" ", "_")[:100]
            if key not in data["snapshots"]:
                data["snapshots"][key] = []
            data["snapshots"][key].append({
                "t": now_t,
                "edge": o["edge_pct"],
                "odds": o.get("retail_odds", 0),
                "pinny_prob": o.get("model_prob", 0),
            })
            # 只保留最近 5 条
            data["snapshots"][key] = data["snapshots"][key][-5:]
        data["last_scan"] = now_t
        EDGE_HISTORY_FILE.write_text(json.dumps(data, indent=2))

    def _annotate_decay(self, opportunities: List[Dict]) -> List[Dict]:
        """为每个机会添加 edge 衰退信息。"""
        data = self._load_edge_history()
        now_t = time.time()
        for o in opportunities:
            key = f"{o['home_team']}_{o['away_team']}_{o.get('market', '1x2')}_{o['outcome']}"
            key = key.replace(" ", "_")[:100]
            history = data.get("snapshots", {}).get(key, [])
            if len(history) >= 2:
                prev = history[-1]
                elapsed_min = (now_t - prev["t"]) / 60
                edge_change = o["edge_pct"] - prev["edge"]
                o["_edge_decay"] = {
                    "prev_edge": round(prev["edge"], 1),
                    "change": round(edge_change, 1),
                    "elapsed_min": int(elapsed_min),
                    "decaying": edge_change < -1.0,  # edge 下降超过 1pp → 标记为衰退
                }
            else:
                o["_edge_decay"] = None
        return opportunities

    def _evaluate_match(self, event_info: dict, raw_odds: dict) -> List[Dict]:
        """评估单场比赛的全部盘口（1x2 + 大小球 + 角球），返回 +EV 机会列表。"""
        opportunities = []
        markets = raw_odds.get("markets", {})

        # ── 1x2 独赢 ──
        h2h = markets.get("1x2")
        if h2h:
            opps = self._evaluate_1x2(event_info, h2h)
            opportunities.extend(opps)

        # ── Over/Under 大小球 ──
        for ou_key, line, _ in OVER_UNDER_MARKETS:
            ou_data = markets.get(ou_key)
            if not ou_data:
                continue
            opps = self._evaluate_ou(event_info, ou_data, line, ou_key)
            opportunities.extend(opps)

        # ── BTTS 双方进球 ──
        btts_data = markets.get("btts")
        if btts_data:
            opps = self._evaluate_btts(event_info, btts_data)
            opportunities.extend(opps)

        # ── Double Chance 双边机会 ──
        dc_data = markets.get("double_chance")
        if dc_data and h2h:  # 需要 1x2 数据做互补计算
            opps = self._evaluate_double_chance(event_info, dc_data, h2h)
            opportunities.extend(opps)

        # ── Draw No Bet 平局退款 ──
        dnb_data = markets.get("draw_no_bet")
        if dnb_data:
            opps = self._evaluate_draw_no_bet(event_info, dnb_data)
            opportunities.extend(opps)

        # ── Corners 1x2 角球独赢 ──
        corners_data = markets.get("corners_1x2")
        if corners_data:
            opps = self._evaluate_corners_1x2(event_info, corners_data)
            opportunities.extend(opps)

        return opportunities

    def _evaluate_1x2(self, event_info: dict, h2h: dict) -> List[Dict]:
        """评估 1x2 独赢盘口。"""
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

            opp = {
                "type": "line_shopping",
                "market": "1x2",
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
            }

            if self._validate_opportunity(opp, pinny, h2h):
                opportunities.append(opp)

        return opportunities

    def _evaluate_ou(self, event_info: dict, ou_data: dict, line: float, ou_key: str) -> List[Dict]:
        """评估 Over/Under 大小球盘口。"""
        side1 = f"over@{line}"
        side2 = f"under@{line}"

        pinny = self._get_pinnacle_2way(ou_data, side1, side2)
        if not pinny:
            return []

        prob_over, prob_under, vig = _remove_vig_2way(pinny[side1], pinny[side2])
        retail = self._get_best_retail_2way(ou_data, side1, side2)
        opportunities = []

        for outcome_name, outcome_label, prob in [
            ("over", "大", prob_over),
            ("under", "小", prob_under),
        ]:
            side_key = f"{outcome_name}@{line}"
            retail_info = retail.get(side_key, {})
            retail_odds = retail_info.get("odds", 0)
            if retail_odds <= 0:
                continue

            retail_implied = 1.0 / retail_odds
            ev = (prob - retail_implied) / retail_implied
            if ev <= self.min_ev:
                continue

            kelly = (prob * retail_odds - 1) / (retail_odds - 1) * 0.25
            if kelly <= 0:
                continue

            opp = {
                "type": "line_shopping",
                "market": "over_under",
                "line": line,
                "sport": "football",
                "league": event_info.get("league_name", ""),
                "home_team": event_info["home_team"],
                "away_team": event_info["away_team"],
                "outcome": f"{outcome_name}_{line}",
                "outcome_label": f"{outcome_label}{line}",
                "commence_time": event_info.get("commence_time", ""),
                "pinny_over_odds": round(pinny[side1], 4),
                "pinny_under_odds": round(pinny[side2], 4),
                "pinny_prob": round(prob, 4),
                "retail_odds": round(retail_odds, 4),
                "retail_bookmaker": retail_info.get("bookmaker", ""),
                "edge_pct": round(ev * 100, 2),
                "kelly_pct": round(kelly * 100, 2),
                "vig_pct": round(vig * 100, 2),
                "_ev": ev,
                "_kelly_frac": kelly,
                "odds": retail_odds,
                "model_prob": round(prob, 4),
                "mkt_prob": round(retail_implied, 4),
            }

            if not self._validate_opportunity(opp, pinny, ou_data):
                continue
            if pinny[side1] < 1.01 or pinny[side2] < 1.01:
                continue

            opportunities.append(opp)

        return opportunities

    def _evaluate_btts(self, event_info: dict, btts_data: dict) -> List[Dict]:
        """评估 BTTS 双方进球盘口。"""
        side1, side2 = "yes", "no"
        pinny = self._get_pinnacle_2way(btts_data, side1, side2)
        if not pinny:
            return []

        prob_yes, prob_no, vig = _remove_vig_2way(pinny[side1], pinny[side2])
        retail = self._get_best_retail_2way(btts_data, side1, side2)
        opportunities = []

        for outcome, outcome_label, prob in [
            ("yes", "双方进球", prob_yes),
            ("no", "一方不进", prob_no),
        ]:
            side_key = outcome
            retail_info = retail.get(side_key, {})
            retail_odds = retail_info.get("odds", 0)
            if retail_odds <= 0:
                continue

            retail_implied = 1.0 / retail_odds
            ev = (prob - retail_implied) / retail_implied
            if ev <= self.min_ev:
                continue

            kelly = (prob * retail_odds - 1) / (retail_odds - 1) * 0.25
            if kelly <= 0:
                continue

            opp = {
                "type": "line_shopping",
                "market": "btts",
                "sport": "football",
                "league": event_info.get("league_name", ""),
                "home_team": event_info["home_team"],
                "away_team": event_info["away_team"],
                "outcome": outcome,
                "outcome_label": outcome_label,
                "commence_time": event_info.get("commence_time", ""),
                "pinny_yes_odds": round(pinny["yes"], 4),
                "pinny_no_odds": round(pinny["no"], 4),
                "pinny_prob": round(prob, 4),
                "retail_odds": round(retail_odds, 4),
                "retail_bookmaker": retail_info.get("bookmaker", ""),
                "edge_pct": round(ev * 100, 2),
                "kelly_pct": round(kelly * 100, 2),
                "vig_pct": round(vig * 100, 2),
                "_ev": ev,
                "_kelly_frac": kelly,
                "odds": retail_odds,
                "model_prob": round(prob, 4),
                "mkt_prob": round(retail_implied, 4),
            }

            if not self._validate_opportunity(opp, pinny, btts_data):
                continue
            if pinny["yes"] < 1.01 or pinny["no"] < 1.01:
                continue

            opportunities.append(opp)

        return opportunities

    def _evaluate_double_chance(self, event_info: dict, dc_data: dict, h2h: dict) -> List[Dict]:
        """评估 Double Chance 双边机会（1X/12/X2）。"""
        # 从 1x2 市场取互补方向的 Pinnacle 赔率
        pinny_1x2 = self._get_pinnacle_odds(h2h)
        if not pinny_1x2:
            return []
        # DC 配对: (DC方向, 互补结果, 互补赔率key, 显示名)
        dc_config = [
            ("1X", "away", "主/平"),
            ("12", "draw", "主/客"),
            ("X2", "home", "客/平"),
        ]
        opportunities = []
        for dc_side, comp_key, label in dc_config:
            # Pinnacle DC 赔率
            pinny_dc_side = (dc_data.get(dc_side, {}).get("bookmakers", {})
                             .get(PINNACLE_SLUG, {}).get("decimal_odds"))
            if not pinny_dc_side:
                continue
            pinny_dc_side = float(pinny_dc_side)
            # 互补方向 Pinnacle 赔率
            comp_odds = pinny_1x2.get(comp_key)
            if not comp_odds or comp_odds <= 0:
                continue
            # 2-way 去抽水
            prob_dc, prob_comp, vig = _remove_vig_2way(pinny_dc_side, comp_odds)
            # 零售最佳
            retail_odds = 0.0
            retail_bm = ""
            bm = dc_data.get(dc_side, {}).get("bookmakers", {})
            for slug in RETAIL_SLUGS:
                rd = bm.get(slug, {}).get("decimal_odds")
                if rd:
                    r = float(rd)
                    if r > retail_odds:
                        retail_odds = r
                        retail_bm = slug
            if retail_odds <= 0:
                continue
            retail_implied = 1.0 / retail_odds
            ev = (prob_dc - retail_implied) / retail_implied
            if ev <= self.min_ev:
                continue
            kelly = (prob_dc * retail_odds - 1) / (retail_odds - 1) * 0.25
            if kelly <= 0:
                continue
            opp = {
                "type": "line_shopping", "market": "double_chance",
                "sport": "football", "league": event_info.get("league_name", ""),
                "home_team": event_info["home_team"], "away_team": event_info["away_team"],
                "outcome": dc_side, "outcome_label": label,
                "commence_time": event_info.get("commence_time", ""),
                "pinny_odds": round(pinny_dc_side, 4),
                "retail_odds": round(retail_odds, 4),
                "retail_bookmaker": retail_bm,
                "edge_pct": round(ev * 100, 2), "kelly_pct": round(kelly * 100, 2),
                "vig_pct": round(vig * 100, 2), "_ev": ev, "_kelly_frac": kelly,
                "odds": retail_odds, "model_prob": round(prob_dc, 4),
                "mkt_prob": round(retail_implied, 4),
            }
            if self._validate_opportunity(opp, pinny_1x2, None):
                opportunities.append(opp)
        return opportunities

    def _evaluate_draw_no_bet(self, event_info: dict, dnb_data: dict) -> List[Dict]:
        """评估 Draw No Bet 平局退款（HOME/AWAY）。"""
        sides = {"HOME": "主胜(无平)", "AWAY": "客胜(无平)"}
        opportunities = []
        for side, label in sides.items():
            pinny_odds = (dnb_data.get(side, {}).get("bookmakers", {})
                          .get(PINNACLE_SLUG, {}).get("decimal_odds"))
            if not pinny_odds:
                continue
            pinny_odds = float(pinny_odds)
            # 找互补方向做 2-way 去抽水
            comp_side = "AWAY" if side == "HOME" else "HOME"
            comp_pinny = (dnb_data.get(comp_side, {}).get("bookmakers", {})
                          .get(PINNACLE_SLUG, {}).get("decimal_odds"))
            if not comp_pinny:
                continue
            prob, prob_comp, vig = _remove_vig_2way(pinny_odds, float(comp_pinny))
            # 零售最佳
            retail_odds = 0.0
            retail_bm = ""
            bm = dnb_data.get(side, {}).get("bookmakers", {})
            for slug in RETAIL_SLUGS:
                rd = bm.get(slug, {}).get("decimal_odds")
                if rd:
                    r = float(rd)
                    if r > retail_odds:
                        retail_odds = r
                        retail_bm = slug
            if retail_odds <= 0:
                continue
            retail_implied = 1.0 / retail_odds
            ev = (prob - retail_implied) / retail_implied
            if ev <= self.min_ev:
                continue
            kelly = (prob * retail_odds - 1) / (retail_odds - 1) * 0.25
            if kelly <= 0:
                continue
            opp = {
                "type": "line_shopping", "market": "draw_no_bet",
                "sport": "football", "league": event_info.get("league_name", ""),
                "home_team": event_info["home_team"], "away_team": event_info["away_team"],
                "outcome": side.lower(), "outcome_label": label,
                "commence_time": event_info.get("commence_time", ""),
                "pinny_odds": round(pinny_odds, 4),
                "retail_odds": round(retail_odds, 4),
                "retail_bookmaker": retail_bm,
                "edge_pct": round(ev * 100, 2), "kelly_pct": round(kelly * 100, 2),
                "vig_pct": round(vig * 100, 2), "_ev": ev, "_kelly_frac": kelly,
                "odds": retail_odds, "model_prob": round(prob, 4),
                "mkt_prob": round(retail_implied, 4),
            }
            if self._validate_opportunity(opp, {side: pinny_odds, comp_side: float(comp_pinny)}, dnb_data):
                opportunities.append(opp)
        return opportunities

    def _evaluate_corners_1x2(self, event_info: dict, corners_data: dict) -> List[Dict]:
        """评估 Corners 1x2 角球独赢（复用 1x2 评估逻辑）。"""
        pinny = self._get_pinnacle_odds(corners_data)
        if not pinny:
            return []
        pinny_h, pinny_d, pinny_a, vig = _remove_vig(pinny["home"], pinny["draw"], pinny["away"])
        retail = self._get_best_retail(corners_data)
        opportunities = []
        for outcome, pinny_prob, retail_key, label in [
            ("home", pinny_h, "home", "角球主胜"),
            ("draw", pinny_d, "draw", "角球平局"),
            ("away", pinny_a, "away", "角球客胜"),
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
            opp = {
                "type": "line_shopping", "market": "corners_1x2",
                "sport": "football", "league": event_info.get("league_name", ""),
                "home_team": event_info["home_team"], "away_team": event_info["away_team"],
                "outcome": outcome, "outcome_label": label,
                "commence_time": event_info.get("commence_time", ""),
                "pinny_home_odds": pinny["home"], "pinny_draw_odds": pinny["draw"],
                "pinny_away_odds": pinny["away"],
                "pinny_prob_home": round(pinny_h, 4), "pinny_prob_draw": round(pinny_d, 4),
                "pinny_prob_away": round(pinny_a, 4),
                "retail_odds": round(retail_odds, 4),
                "retail_bookmaker": retail[f"{retail_key}_bm"],
                "edge_pct": round(ev * 100, 2), "kelly_pct": round(kelly * 100, 2),
                "vig_pct": round(vig * 100, 2), "_ev": ev, "_kelly_frac": kelly,
                "odds": retail_odds, "model_prob": round(pinny_prob, 4),
                "mkt_prob": round(retail_implied, 4),
            }
            if self._validate_opportunity(opp, pinny, corners_data):
                opportunities.append(opp)
        return opportunities


    def scan(self) -> List[Dict]:
        """扫描即将开始的比赛，返回 +EV 机会列表。"""
        logger.info("─" * 60)
        logger.info("🔍 Line Shopping 扫描 (Pinnacle vs 零售最佳)")
        logger.info("─" * 60)

        events = fetch_upcoming_events(hours_ahead=168)
        if not events:
            logger.info("  无即将开始的足球赛事")
            return []

        logger.info("  赛事: %d 场", len(events))

        opportunities = []
        n_with_1x2 = 0
        n_with_ou = 0
        n_with_btts = 0
        for ev in events:
            raw = self._load_raw_odds_cached(ev["id"])
            if not raw:
                continue

            markets = raw.get("markets", {})
            h2h = markets.get("1x2")
            if h2h and self._get_pinnacle_odds(h2h):
                n_with_1x2 += 1
            # 检查是否至少有一个 O/U 有 Pinnacle
            for ou_key, line, _ in OVER_UNDER_MARKETS:
                ou_data = markets.get(ou_key)
                if ou_data:
                    side1, side2 = f"over@{line}", f"under@{line}"
                    if self._get_pinnacle_2way(ou_data, side1, side2):
                        n_with_ou += 1
                        break
            # 检查 BTTS 是否有 Pinnacle
            btts_data = markets.get("btts")
            if btts_data and self._get_pinnacle_2way(btts_data, "yes", "no"):
                n_with_btts += 1
            # 检查 Total Corners 是否有 Pinnacle

            opps = self._evaluate_match(ev, raw)
            opportunities.extend(opps)

        opportunities.sort(key=lambda x: x["edge_pct"], reverse=True)
        opportunities = opportunities[:MAX_OPPORTUNITIES]

        # 统计 Pinnacle 数据新鲜度
        stale_1x2 = 0
        for ev in events:
            raw = self._load_raw_odds_cached(ev["id"])
            if not raw:
                continue
            mkts = raw.get("markets", {})
            h2h = mkts.get("1x2")
            if h2h:
                for sk in ["HOME", "AWAY", "DRAW"]:
                    if self._is_pinny_stale(h2h, [sk]):
                        stale_1x2 += 1
                        break
        if stale_1x2:
            logger.info("  ⚠ Pinnacle 过期数据: %d 场（>%d 小时未更新）",
                        stale_1x2, self.MAX_PINNY_STALE_HOURS)

        logger.info("  含 Pinnacle 1x2: %d 场 | O/U: %d 场 | BTTS: %d 场",
                    n_with_1x2, n_with_ou, n_with_btts)
        logger.info("  +EV 机会: %d 条（1x2 + 大小球 + 双方进球 + 角球）", len(opportunities))

        if opportunities:
            t = opportunities[0]
            # 判断市场类型
            if t.get("market") == "over_under":
                label = t.get("outcome_label", t["outcome"])
            else:
                outcome_cn = {"home": "主胜", "draw": "平局", "away": "客胜"}
                label = outcome_cn.get(t["outcome"], t["outcome"])
            h_cn = cn_team(t["home_team"], 'football')
            a_cn = cn_team(t["away_team"], 'football')
            logger.info("  最佳: %s vs %s [%s] edge=%.1f%% odds=%.2f (%s)",
                       h_cn, a_cn, label,
                       t["edge_pct"], t["retail_odds"], t["retail_bookmaker"])

            # Edge 衰退总结
            decaying = [o for o in opportunities if o.get("_edge_decay")]
            if decaying:
                fast_decay = [o for o in decaying if o["_edge_decay"].get("decaying")]
                if fast_decay:
                    worst = max(decaying, key=lambda x: abs(x["_edge_decay"]["change"]))
                    logger.info("  ⏳ Edge 衰退: %d 条在降（最快 %s vs %s: %.1fpp/%dmin）",
                               len(fast_decay),
                               worst["home_team"], worst["away_team"],
                               abs(worst["_edge_decay"]["change"]),
                               worst["_edge_decay"]["elapsed_min"])

        # Edge 衰退追踪
        self._annotate_decay(opportunities)
        self._save_edge_history(opportunities)

        self.opportunities = opportunities
        return opportunities

    def _opp_label(self, o: dict) -> str:
        """获取可读的结果标签。"""
        if o.get("market") == "over_under":
            return o.get("outcome_label", o["outcome"])
        if o.get("market") == "btts":
            return o.get("outcome_label", "双方进球" if o["outcome"] == "yes" else "不进球")
        outcome_cn = {"home": "主胜", "draw": "平局", "away": "客胜"}
        return outcome_cn.get(o["outcome"], o["outcome"])

    def _pinnacle_display(self, o: dict) -> str:
        """获取 Pinnacle 赔率显示字符串。"""
        if o.get("market") == "over_under":
            return f"大{o['line']} {o['pinny_over_odds']} / 小{o['line']} {o['pinny_under_odds']}"
        if o.get("market") == "btts":
            return f"是 {o['pinny_yes_odds']} / 否 {o['pinny_no_odds']}"
        if o.get("market") in ("double_chance", "draw_no_bet"):
            po = o.get("pinny_odds", "")
            return f"{po}" if po else "-"
        if o.get("pinny_home_odds") and o.get("pinny_away_odds"):
            return f"{o['pinny_home_odds']}/{o.get('pinny_draw_odds','-')}/{o['pinny_away_odds']}"
        # 兜底：用任何可用的 pinny 字段
        for k in ("pinny_odds", "pinny_prob"):
            v = o.get(k)
            if v:
                return f"{v}"
        return "-"

    def push_recommendations(self):
        """推送合格投注建议到钉钉（≥3% edge）。"""
        if not DINGTALK_WEBHOOK or not self.opportunities:
            return

        qualified = [o for o in self.opportunities if o['edge_pct'] >= 3 and o.get("league", "") in TRUSTED_LEAGUES]
        if not qualified:
            return

        # 保存结果给 push_cached_recommendations 消费
        self.save_results()

        # 复用缓存推送（格式统一在 ev_push.py）
        from src.betting.line_shopping import push_cached_recommendations
        push_cached_recommendations()
        return

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
    """便捷入口：扫描 + 保存 + 推送，返回机会列表。"""
    scanner = LineShoppingScanner()
    opps = scanner.scan()
    if opps:
        scanner.save_results()
        push_cached_recommendations()
    return opps


def push_cached_recommendations():
    """从已保存的结果文件推送新发现的 +EV 投注建议到钉钉。

    已推送过的机会会自动去重，不会重复发送。
    """
    if not DINGTALK_WEBHOOK:
        return
    try:
        from src.report.ev_push import build_ev_report, _load_seen, _save_seen, _validate_format, _check_body_chinese
    except Exception:
        return

    seen = _load_seen()
    body, new_fps = build_ev_report(seen)
    if body.startswith("no") or body.startswith("line"):
        logger.info("  ⏭️ %s", body)
        return

    # 格式验证 — 防止格式被意外修改
    try:
        if not _validate_format(body):
            logger.error("推送格式验证失败！阻止发送。body=%s...", body[:100])
            return
    except ImportError:
        pass

    # 中文自检 — 发现英文名残留时阻止发送
    en_issues = _check_body_chinese(body)
    if en_issues:
        for issue in en_issues:
            logger.warning("⚠️ %s", issue)
        logger.warning("推送内容有英文名残留，请检查 team_names.py 是否缺少映射")
        return

    title = f"+EV 投注推荐: {body.count('#####')} 条"
    try:
        from config.settings import send_dingtalk
        ok = send_dingtalk(title, body)
        if ok:
            seen.update(new_fps)
            _save_seen(seen)
            logger.info("  ✅ 推荐已推送钉钉（%d 条新机会）", len(new_fps))
    except Exception as e:
        logger.warning("  ⚠️ 推荐钉钉推送失败: %s", e)


if __name__ == "__main__":
    opps = run_line_shopping()
    if opps:
        print(f"发现 {len(opps)} 条 +EV 机会")
    else:
        print("未发现 +EV 机会")
