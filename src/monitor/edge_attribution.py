"""Edge Attribution Framework — 分解每条推荐的 edge 来源。

职业博彩的第一原则: 知道盈利来自哪里。
本模块将总 edge 分解为三个来源:

  总 Edge = 模型 Edge + 选线 Edge + 时机 Edge

  模型 Edge (model_edge):     model_prob - sharp_consensus_prob
    你的模型是否看到了 sharp 市场没看到的东西?

  选线 Edge (line_shopping):  sharp_consensus_prob - best_market_prob
    你是否通过选到更好的赔率获得了 edge?
    (在软公司下注时为正，因为软公司定价偏高)

  时机 Edge (timing):         best_market_prob - closing_market_prob
    你是否在开盘价优于收盘价时下注了?
    只有已结算且有收盘价记录的预测可计算。
    等同于 CLV (Closing Line Value)。

用法:
    from src.monitor.edge_attribution import print_edge_attribution_report
    print_edge_attribution_report()
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

LOG_FILE = ROOT / "data" / "storage" / "prediction_log.csv"
ATTRIBUTION_FILE = ROOT / "data" / "storage" / "edge_attribution_report.json"


def compute_edge_attribution() -> Dict:
    """计算所有已结算预测的 edge 归因。

    读取 prediction_log.csv，对每条已结算的预测分解 edge 来源。

    Returns:
        {
            "summary": {
                "total_settled": int,
                "avg_total_edge": float,
                "avg_model_edge": float,
                "avg_line_shopping_edge": float,
                "avg_timing_edge": float,
                "attribution_pct": {
                    "model_pct": float,
                    "line_shopping_pct": float,
                    "timing_pct": float
                },
                "by_sport": {...},
                "by_market": {...},
            },
            "recent_attributions": [...]
        }
    """
    if not LOG_FILE.exists():
        return {"error": "prediction_log.csv 不存在"}

    df = pd.read_csv(LOG_FILE)
    settled = df[df["status"].isin(["won", "lost"])].copy()
    if settled.empty:
        return {"error": "暂无已结算预测"}

    attributions = []

    for _, row in settled.iterrows():
        try:
            model_prob = float(row["model_prob"])
            market_prob = float(row["market_prob"])
            odds = float(row["odds"])
            ev = float(row["ev"])
        except (ValueError, TypeError):
            continue

        # 1. 总 Edge: 存储的 ev 已经是 model_prob - market_prob
        total_edge = ev

        # 2. 模型 Edge: model_prob - sharp_consensus_prob
        sharp_prob = float(row["sharp_prob"]) if row.get("sharp_prob") and str(row["sharp_prob"]).strip() else market_prob
        if sharp_prob <= 0 or sharp_prob >= 1:
            sharp_prob = market_prob  # fallback
        model_edge = model_prob - sharp_prob

        # 3. 选线 Edge: sharp_prob - (1/odds 扣除水钱后)
        implied_prob = 1.0 / odds if odds > 0 else market_prob
        line_shopping_edge = sharp_prob - implied_prob

        # 4. 时机 Edge: 需要收盘价 (CLV)
        result_odds = row.get("result_odds")
        timing_edge = 0.0
        has_timing = False
        if result_odds and str(result_odds).strip():
            try:
                close_odds = float(result_odds)
                if close_odds > 0:
                    close_prob = 1.0 / close_odds
                    timing_edge = implied_prob - close_prob
                    has_timing = True
            except (ValueError, TypeError):
                pass

        # 重新合成总 edge 用于验证
        composed = model_edge + line_shopping_edge + (timing_edge if has_timing else 0)

        # Edge 归因百分比
        abs_total = abs(composed) if composed != 0 else 1e-10
        model_pct = model_edge / abs_total
        line_pct = line_shopping_edge / abs_total
        timing_pct = timing_edge / abs_total if has_timing else 0.0

        attributions.append({
            "match": f"{row.get('home_team_cn', '')} vs {row.get('away_team_cn', '')}",
            "sport": str(row.get("sport", "")),
            "league": str(row.get("league", "")),
            "market_type": str(row.get("market_type", "")),
            "result": str(row.get("status", "")),
            "odds": round(odds, 4),
            "model_prob": round(model_prob, 4),
            "market_prob": round(market_prob, 4),
            "sharp_prob": round(sharp_prob, 4),
            "total_edge": round(total_edge, 4),
            "model_edge": round(float(model_edge), 4),
            "line_shopping_edge": round(float(line_shopping_edge), 4),
            "timing_edge": round(float(timing_edge), 4),
            "edge_decomposition": {
                "model_pct": round(float(model_pct), 4),
                "line_shopping_pct": round(float(line_pct), 4),
                "timing_pct": round(float(timing_pct), 4),
            },
            "has_timing_data": has_timing,
        })

    if not attributions:
        return {"error": "无法计算 edge 归因"}

    attr_df = pd.DataFrame(attributions)

    # 聚合统计
    with_timing = attr_df[attr_df["has_timing_data"]]

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_settled": len(attr_df),
        "total_with_timing_data": len(with_timing),
        "avg_total_edge": round(float(attr_df["total_edge"].mean()), 4),
        "avg_model_edge": round(float(attr_df["model_edge"].mean()), 4),
        "avg_line_shopping_edge": round(float(attr_df["line_shopping_edge"].mean()), 4),
        "avg_timing_edge": round(float(with_timing["timing_edge"].mean()), 4) if not with_timing.empty else 0.0,
        "attribution_pct": {
            "model_pct": round(float(attr_df["model_edge"].sum() / attr_df["total_edge"].sum()), 4) if abs(attr_df["total_edge"].sum()) > 1e-10 else 0,
            "line_shopping_pct": round(float(attr_df["line_shopping_edge"].sum() / attr_df["total_edge"].sum()), 4) if abs(attr_df["total_edge"].sum()) > 1e-10 else 0,
            "timing_pct": round(float(with_timing["timing_edge"].sum() / with_timing["total_edge"].sum()), 4) if not with_timing.empty and abs(with_timing["total_edge"].sum()) > 1e-10 else 0,
        },
        "by_sport": {},
        "by_market": {},
    }

    # 按运动分解
    for sport in attr_df["sport"].unique():
        sub = attr_df[attr_df["sport"] == sport]
        summary["by_sport"][sport] = {
            "count": len(sub),
            "avg_total_edge": round(float(sub["total_edge"].mean()), 4),
            "avg_model_edge": round(float(sub["model_edge"].mean()), 4),
            "avg_line_shopping_edge": round(float(sub["line_shopping_edge"].mean()), 4),
        }

    # 按市场分解
    for mkt in attr_df["market_type"].unique():
        sub = attr_df[attr_df["market_type"] == mkt]
        summary["by_market"][mkt] = {
            "count": len(sub),
            "avg_total_edge": round(float(sub["total_edge"].mean()), 4),
        }

    # 保存报告
    report = {
        "summary": summary,
        "recent_attributions": sorted(attributions, key=lambda x: x.get("match", ""), reverse=True)[:100],
    }
    ATTRIBUTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    ATTRIBUTION_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("Edge attribution report saved to %s", ATTRIBUTION_FILE)

    return report


def print_edge_attribution_report():
    """打印可读的 edge 归因汇总。"""
    report = compute_edge_attribution()
    if "error" in report:
        logger.warning("Edge attribution not available: %s", report["error"])
        return

    s = report["summary"]
    logger.info("\n" + "=" * 60)
    logger.info("  Edge Attribution Report")
    logger.info("=" * 60)
    logger.info("  Settled predictions: %d", s["total_settled"])
    logger.info("  With timing (CLV) data: %d", s["total_with_timing_data"])
    logger.info("")
    logger.info("  Average edge decomposition:")
    logger.info("    Total edge:       %+.4f", s["avg_total_edge"])
    logger.info("    Model edge:       %+.4f", s["avg_model_edge"])
    logger.info("    Line shopping:    %+.4f", s["avg_line_shopping_edge"])
    logger.info("    Timing (CLV):     %+.4f", s["avg_timing_edge"])
    logger.info("")
    logger.info("  Attribution percentages:")
    logger.info("    From model:       %.1f%%", s["attribution_pct"]["model_pct"] * 100)
    logger.info("    From line shop:   %.1f%%", s["attribution_pct"]["line_shopping_pct"] * 100)
    logger.info("    From timing:      %.1f%%", s["attribution_pct"]["timing_pct"] * 100)
    logger.info("")
    if s.get("by_sport"):
        logger.info("  By sport:")
        for sport, stats in s["by_sport"].items():
            logger.info("    %s: %d bets, model edge %+.4f, line edge %+.4f",
                       sport, stats["count"], stats["avg_model_edge"], stats["avg_line_shopping_edge"])
    logger.info("=" * 60)


if __name__ == "__main__":
    print_edge_attribution_report()
