"""组合业绩归因 — 按运动/联赛/市场类型拆解 ROI 和盈亏。

从 virtual_portfolio.json 提取已结算投注，按多个维度聚合：
  - 运动（NBA / 足球 / NFL / WNBA）
  - 市场类型（h2h / spread / total）
  - 联赛（英超 / 西甲 / NBA 等）
  - 交叉维度（运动×市场）

用法:
    attr = PerformanceAttribution()
    report = attr.compute()
    print(report["by_sport"])
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"


def _parse_settled_key(key: str) -> dict:
    """解析 virtual_portfolio.json 的 settled key。

    Key 格式: {sport}_{league}_{home_cn}_{away_cn}_{market_type}
    例: nba_NBA_洛杉矶湖人_金州勇士_主胜

    注意 home_cn/away_cn 之中也可能含下划线（如"波士顿_凯尔特人"），
    所以从前往后解析：前两段是 sport + league，最后一段是 market_type。
    """
    parts = key.split("_")
    if len(parts) < 4:
        return {"sport": "", "league": "", "market_type": "", "valid": False}
    sport = parts[0]
    league = parts[1]
    market_type = parts[-1]
    return {"sport": sport, "league": league, "market_type": market_type, "valid": True}


def _sport_group(sport: str) -> str:
    """将具体 sport 归组。"""
    s = sport.lower()
    if "nba" in s or "basketball" in s or "ncaa" in s:
        return "篮球"
    if "soccer" in s or "football" in s or s in ("epl", "laliga", "serie_a", "bundesliga", "ligue_one", "primera"):
        return "足球"
    if "nfl" in s:
        return "NFL"
    if "world_cup" in s:
        return "世界杯"
    if "wnba" in s:
        return "WNBA"
    return "其他"


def _calc_roi(total_profit: float, total_stake: float) -> float:
    return total_profit / total_stake if total_stake > 0 else 0.0


class PerformanceAttribution:
    """组合业绩归因计算。"""

    def __init__(self, portfolio_path: Optional[Path] = None):
        self.portfolio_path = portfolio_path or PORTFOLIO_FILE

    def compute(self) -> Dict:
        """计算全维度归因报告。"""
        portfolio = self._load_portfolio()
        if not portfolio:
            return self._empty_report()

        records = self._extract_records(portfolio)
        if not records:
            return self._empty_report()

        return {
            "by_sport": self._aggregate(records, "sport_group"),
            "by_league": self._aggregate(records, "league"),
            "by_market": self._aggregate(records, "market_type"),
            "by_sport_market": self._aggregate_cross(records, "sport_group", "market_type"),
            "by_result": self._aggregate(records, "result"),
            "overall": self._overall(records),
            "n_settled": len(records),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _empty_report(self) -> Dict:
        return {
            "by_sport": {}, "by_league": {}, "by_market": {},
            "by_sport_market": {}, "by_result": {},
            "overall": {"bets": 0, "wins": 0, "losses": 0, "win_rate": 0},
            "n_settled": 0, "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _load_portfolio(self) -> Optional[Dict]:
        if not self.portfolio_path.exists():
            return None
        try:
            return json.loads(self.portfolio_path.read_text())
        except Exception as e:
            logger.warning("组合文件加载失败: %s", e)
            return None

    def _extract_records(self, portfolio: Dict) -> List[Dict]:
        """从 settled 和 pending_bets 提取结构化记录。"""
        records = []

        # 已结算（含盈亏）
        settled = portfolio.get("settled", {})
        for key, result in settled.items():
            parsed = _parse_settled_key(key)
            if not parsed.get("valid"):
                continue
            records.append({
                "sport_group": _sport_group(parsed["sport"]),
                "league": parsed["league"],
                "market_type": parsed["market_type"],
                "result": result,
                "source": "settled",
            })

        # 待结算（含注额和赔率）
        for b in portfolio.get("pending_bets", []):
            sport = b.get("sport", "")
            league = b.get("league", "")
            market = b.get("market_type", "")
            stake = b.get("stake", 0)
            odds = b.get("odds", 1)
            prob = b.get("model_prob", 0.5)
            records.append({
                "sport_group": _sport_group(sport),
                "league": league,
                "market_type": market,
                "result": "pending",
                "stake": stake,
                "odds": odds,
                "model_prob": prob,
                "source": "pending",
            })

        return records

    def _aggregate(self, records: List[Dict], dimension: str) -> Dict:
        """按单个维度聚合。"""
        groups = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})

        for r in records:
            key = r.get(dimension, "未知") or "未知"
            res = r.get("result")
            if res == "won":
                groups[key]["wins"] += 1
                groups[key]["total"] += 1
            elif res == "lost":
                groups[key]["losses"] += 1
                groups[key]["total"] += 1

        result = {}
        for key, counts in sorted(groups.items()):
            total = counts["total"]
            wins = counts["wins"]
            losses = counts["losses"]
            result[key] = {
                "bets": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / total, 4) if total > 0 else 0,
            }
        return result

    def _aggregate_cross(self, records: List[Dict], dim1: str, dim2: str) -> Dict:
        """按两个维度交叉聚合。"""
        groups = defaultdict(lambda: defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0}))

        for r in records:
            k1 = r.get(dim1, "未知") or "未知"
            k2 = r.get(dim2, "未知") or "未知"
            res = r.get("result")
            if res == "won":
                groups[k1][k2]["wins"] += 1
                groups[k1][k2]["total"] += 1
            elif res == "lost":
                groups[k1][k2]["losses"] += 1
                groups[k1][k2]["total"] += 1

        result = {}
        for k1 in sorted(groups):
            result[k1] = {}
            for k2 in sorted(groups[k1]):
                c = groups[k1][k2]
                t = c["total"]
                result[k1][k2] = {
                    "bets": t,
                    "wins": c["wins"],
                    "losses": c["losses"],
                    "win_rate": round(c["wins"] / t, 4) if t > 0 else 0,
                }
        return result

    def _overall(self, records: List[Dict]) -> Dict:
        """全局汇总。"""
        settled = [r for r in records if r.get("result") in ("won", "lost")]
        wins = sum(1 for r in settled if r["result"] == "won")
        losses = sum(1 for r in settled if r["result"] == "lost")
        total = len(settled)
        return {
            "bets": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total, 4) if total > 0 else 0,
        }


def compute_and_save(output_path: Optional[Path] = None) -> Dict:
    """计算归因并保存到 JSON。"""
    attr = PerformanceAttribution()
    report = attr.compute()
    path = output_path or DATA_DIR / "performance_attribution.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("  📊 归因报告已保存: %s (%d 条已结算)", path, report["n_settled"])
    return report


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    report = compute_and_save()
    for sport, data in report.get("by_sport", {}).items():
        print(f"  {sport}: {data['bets']} 场, 胜率 {data['win_rate']:.1%}")
