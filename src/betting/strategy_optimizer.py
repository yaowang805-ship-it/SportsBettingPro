"""策略优化器 — 根据实际结算数据动态调整投注参数。

工作流：
  1. 每笔投注结算后记录（edge, 联赛, 市场类型, 结果, 盈亏）
  2. 定期分析数据，推荐最优参数
  3. 自动调整 MIN_EDGE、Kelly 分数、联赛白名单
"""
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.logging_config import get_logger
from config.settings import DATA_DIR

logger = get_logger(__name__)

_LOG_FILE = DATA_DIR / "settlement_log.csv"
_OPT_STATE_FILE = DATA_DIR / "strategy_optimizer.json"


class SettlementLogger:
    """记录每笔已结算投注的实际表现。"""

    def __init__(self):
        self.log_file = _LOG_FILE

    def record(self, bet_id: str, league: str, market: str,
               edge_pct: float, odds: float, stake: float,
               profit: float, outcome: str):
        """记录一笔结算结果。"""
        is_new = not self.log_file.exists()
        with open(self.log_file, "a", newline="") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["date", "bet_id", "league", "market",
                           "edge_pct", "odds", "stake", "profit", "outcome"])
            w.writerow([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                bet_id, league, market,
                round(edge_pct, 2), round(odds, 4), round(stake, 2),
                round(profit, 2), outcome,
            ])

    def load(self) -> list:
        """加载所有结算记录。"""
        if not self.log_file.exists():
            return []
        with open(self.log_file) as f:
            return list(csv.DictReader(f))


class StrategyOptimizer:
    """分析结算数据，推荐最优策略参数。"""

    def __init__(self):
        self.logger = SettlementLogger()
        self.state_file = _OPT_STATE_FILE
        self._load_state()

    def _load_state(self):
        if self.state_file.exists():
            try:
                self.state = json.loads(self.state_file.read_text())
            except Exception:
                self.state = {}
        else:
            self.state = {}

    def _save_state(self):
        self.state_file.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))

    def analyze(self) -> dict:
        """分析历史数据，输出优化建议。"""
        records = self.logger.load()
        if len(records) < 20:
            return {"status": "insufficient_data", "count": len(records),
                    "message": f"还需 {20 - len(records)} 笔才能分析"}

        # 按 edge 区间分组
        buckets = defaultdict(lambda: {"bets": 0, "wins": 0, "profit": 0.0, "stake": 0.0})
        for r in records:
            edge = float(r["edge_pct"])
            bucket = f"{int(edge // 5) * 5}-{int(edge // 5) * 5 + 5}%"
            b = buckets[bucket]
            b["bets"] += 1
            b["stake"] += float(r["stake"])
            b["profit"] += float(r["profit"])
            if r["outcome"] == "won":
                b["wins"] += 1

        # 按联赛分组
        by_league = defaultdict(lambda: {"bets": 0, "wins": 0, "profit": 0.0})
        for r in records:
            league = r.get("league", "unknown")
            b = by_league[league]
            b["bets"] += 1
            b["profit"] += float(r["profit"])
            if r["outcome"] == "won":
                b["wins"] += 1

        # 推荐最小 edge 阈值
        best_edge = None
        best_roi = -999
        for bucket, data in sorted(buckets.items()):
            if data["bets"] < 5:
                continue
            roi = data["profit"] / max(data["stake"], 1)
            if roi > best_roi:
                best_roi = roi
                best_edge = bucket

        # 负 ROI 联赛（需要屏蔽）
        bad_leagues = []
        for league, data in sorted(by_league.items()):
            if data["bets"] >= 10 and data["profit"] < 0:
                bad_leagues.append(league)

        recommendation = {}
        if best_edge:
            min_edge = int(best_edge.split("-")[0])
            recommendation["min_edge_pct"] = min_edge

        recommendation["blocked_leagues"] = bad_leagues
        recommendation["total_analyzed"] = len(records)

        result = {
            "status": "ready",
            "count": len(records),
            "by_edge_bucket": dict(buckets),
            "by_league": dict(by_league),
            "recommendation": recommendation,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        self.state["last_analysis"] = result
        self._save_state()
        return result

    def get_optimal_min_edge(self) -> float:
        """获取推荐的 edge 阈值。"""
        analysis = self.state.get("last_analysis", {})
        rec = analysis.get("recommendation", {})
        return rec.get("min_edge_pct", 3.0) / 100.0

    def get_blocked_leagues(self) -> list:
        """获取应屏蔽的联赛列表。"""
        analysis = self.state.get("last_analysis", {})
        return analysis.get("recommendation", {}).get("blocked_leagues", [])

    def print_summary(self):
        """打印优化建议摘要。"""
        analysis = self.state.get("last_analysis", {})
        if not analysis:
            print("  尚无分析数据（需至少 20 笔已结算投注）")
            return

        print(f"\n  策略优化分析（{analysis.get('count', 0)} 笔已结算）")
        print("  " + "-" * 50)

        buckets = analysis.get("by_edge_bucket", {})
        if buckets:
            print("  按 Edge 区间：")
            for bucket in sorted(buckets.keys()):
                d = buckets[bucket]
                wr = d["wins"] / d["bets"] * 100 if d["bets"] > 0 else 0
                roi = d["profit"] / max(d["stake"], 1) * 100
                print(f"    {bucket:>10s}: {d['bets']:>3} 笔, 胜率 {wr:>5.1f}%, ROI {roi:>+6.1f}%")

        rec = analysis.get("recommendation", {})
        if rec.get("min_edge_pct"):
            print(f"  推荐最低 Edge: {rec['min_edge_pct']}%")
        if rec.get("blocked_leagues"):
            print(f"  建议屏蔽联赛: {', '.join(rec['blocked_leagues'])}")
