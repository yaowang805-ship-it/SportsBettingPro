"""投注校准引擎 — 概率校准 + 联赛级退化检测

功能:
  1. 记录每笔已结算投注的 {联赛, 市场, edge, 模型概率, 实际结果}
  2. 对比 Pinnacle 去水概率 vs 实际胜率，检测系统性偏差
  3. 按联赛/市场/edge区间分组分析
  4. 偏差 >10% 的联赛自动标记为"打折"（edge乘以折扣系数）

用法:
    from src.risk.calibration import BetCalibrator
    bc = BetCalibrator()
    bc.record(bet_id, league, market, edge_pct, model_prob, odds, result)
    report = bc.analyze()  # 返回校准报告
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict
from collections import defaultdict

from config.logging_config import get_logger
from config.settings import DATA_DIR

logger = get_logger(__name__)

CALIBRATION_FILE = DATA_DIR / "bet_calibration.json"
MIN_SAMPLES = 10           # 每组最少样本数才出结论
MAX_BIAS_PCT = 0.10        # 最大允许偏差 10%（超过则打折）


class BetCalibrator:
    """投注校准器 — 记录结算数据 + 交叉分析偏差。"""

    def __init__(self):
        self.data = self._load()

    # ── 公开接口 ──

    def record(self, bet_id: str, league: str, market: str,
               edge_pct: float, model_prob: float, odds: float,
               result: str):
        """记录一笔已结算投注。

        Args:
            bet_id: 投注ID
            league: 联赛名
            market: 市场类型（home/away/draw/yes/no/over_x/under_x）
            edge_pct: edge 百分比（如 5.2 表示 5.2%）
            model_prob: 模型概率（Pinnacle去水后）
            odds: 赔率
            result: "won" 或 "lost"
        """
        # 去重
        for entry in self.data:
            if entry["bet_id"] == bet_id:
                return

        self.data.append({
            "bet_id": bet_id,
            "league": league,
            "market": market,
            "edge_pct": round(edge_pct, 2),
            "model_prob": round(model_prob, 4),
            "odds": round(odds, 4),
            "result": result,
            "settled_at": datetime.now().isoformat(),
        })
        self._save()
        logger.info("  校准记录: %s → %s (prob=%.1f%%)", bet_id[:40], result, model_prob * 100)

    def analyze(self) -> Dict:
        """运行校准分析，返回报告 dict。"""
        if len(self.data) < MIN_SAMPLES:
            return {
                "status": "insufficient_data",
                "total_bets": len(self.data),
                "min_required": MIN_SAMPLES,
                "message": f"数据不足（{len(self.data)}/{MIN_SAMPLES}），无法分析",
            }

        total = len(self.data)
        wins = sum(1 for d in self.data if d["result"] == "won")
        avg_prob = sum(d["model_prob"] for d in self.data) / total if total > 0 else 0
        actual_wr = wins / total if total > 0 else 0

        # 总体偏差
        overall_bias = actual_wr - avg_prob

        # 按联赛分组
        league_stats = self._group_stats(lambda d: d["league"])
        # 按市场分组
        market_stats = self._group_stats(lambda d: self._market_group(d["market"]))
        # 按 edge 区间分组
        edge_stats = self._group_stats(lambda d: self._edge_bucket(d["edge_pct"]))

        # 找出有问题的组
        flagged_groups = []

        for name, stats in league_stats.items():
            if stats["count"] >= MIN_SAMPLES and abs(stats["bias"]) > MAX_BIAS_PCT:
                flagged_groups.append({
                    "type": "league",
                    "name": name,
                    **stats,
                    "action": "discount_edge" if stats["bias"] < 0 else "monitor",
                })

        for name, stats in market_stats.items():
            if stats["count"] >= MIN_SAMPLES and abs(stats["bias"]) > MAX_BIAS_PCT:
                flagged_groups.append({
                    "type": "market",
                    "name": name,
                    **stats,
                    "action": "discount_edge" if stats["bias"] < 0 else "monitor",
                })

        report = {
            "status": "ok",
            "total_bets": total,
            "wins": wins,
            "losses": total - wins,
            "avg_model_prob": round(avg_prob, 4),
            "actual_win_rate": round(actual_wr, 4),
            "overall_bias": round(overall_bias, 4),
            "overall_bias_pct": round(overall_bias * 100, 1),
            "league": league_stats,
            "market": market_stats,
            "edge_range": edge_stats,
            "flagged": flagged_groups,
        }

        logger.info("校准分析: %d 笔 | 模型概率 %.1f%% vs 实际 %.1f%% | 偏差 %+.1f%%",
                    total, avg_prob * 100, actual_wr * 100, overall_bias * 100)
        if flagged_groups:
            logger.warning("  标记 %d 个问题组", len(flagged_groups))
            for f in flagged_groups:
                logger.warning("    [%s] %s: 偏差 %+.1f%% → %s",
                               f["type"], f["name"], f["bias_pct"], f["action"])

        return report

    def get_edge_adjustment(self, league: str, market: str) -> float:
        """返回指定联赛+市场的 edge 折扣系数（1.0=不打折）。"""
        report = self.analyze()
        if report["status"] != "ok":
            return 1.0

        for f in report.get("flagged", []):
            if f["action"] != "discount_edge":
                continue
            if f["type"] == "league" and f["name"] == league:
                return max(0.3, 1.0 - abs(f["bias"]))
            if f["type"] == "market" and f["name"] == self._market_group(market):
                return max(0.3, 1.0 - abs(f["bias"]))

        return 1.0

    # ── 内部方法 ──

    def _group_stats(self, key_fn):
        """按 key_fn 分组，计算每组偏差。"""
        groups = defaultdict(list)
        for d in self.data:
            groups[key_fn(d)].append(d)

        result = {}
        for name, entries in sorted(groups.items()):
            n = len(entries)
            wins = sum(1 for e in entries if e["result"] == "won")
            avg_prob = sum(e["model_prob"] for e in entries) / n
            actual_wr = wins / n
            bias = actual_wr - avg_prob
            result[name] = {
                "count": n,
                "wins": wins,
                "losses": n - wins,
                "avg_prob": round(avg_prob, 4),
                "actual_wr": round(actual_wr, 4),
                "bias": round(bias, 4),
                "bias_pct": round(bias * 100, 1),
            }
        return result

    @staticmethod
    def _market_group(market: str) -> str:
        """将市场归类到大类。"""
        m = market.lower()
        if m in ("home", "away", "draw"):
            return "1x2"
        if m in ("yes", "no"):
            return "btts"
        if "over" in m or "under" in m or "大小" in m:
            return "over_under"
        if m == "total_corners":
            return "over_under"
        return "other"

    @staticmethod
    def _edge_bucket(edge_pct: float) -> str:
        """edge 区间分桶。"""
        if edge_pct < 3:
            return "0-3%"
        elif edge_pct < 5:
            return "3-5%"
        elif edge_pct < 10:
            return "5-10%"
        elif edge_pct < 20:
            return "10-20%"
        else:
            return "20%+"

    def _load(self) -> list:
        if CALIBRATION_FILE.exists():
            try:
                return json.loads(CALIBRATION_FILE.read_text())
            except Exception:
                return []
        return []

    def _save(self):
        CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
