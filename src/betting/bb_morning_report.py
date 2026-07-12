#!/usr/bin/env python3
"""明早8点钉钉推送 — BB体育 vs Pinnacle 虚拟投注结算报告。

会尝试：
  1. 从 BB体育 Chrome 提取最终比分
  2. 结算所有待结算投注
  3. 生成报告推送到钉钉

用法：
    python3 src/betting/bb_morning_report.py
"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.bb_settle import (
    extract_bb_scores, settle_bets, generate_report, send_dingtalk,
    PORTFOLIO_FILE
)
from config.logging_config import get_logger

logger = get_logger(__name__)


def _calc_cumulative_stats():
    """计算 BB vs Pinnacle 累计统计."""
    portfolio = json.loads(PORTFOLIO_FILE.read_text()) if PORTFOLIO_FILE.exists() else {}
    history = [h for h in portfolio.get("history", []) if h.get("source") == "bb_vs_pinnacle"]
    won = sum(1 for h in history if h.get("result") == "won")
    lost = sum(1 for h in history if h.get("result") == "lost")
    total_pnl = sum(h.get("profit", 0) for h in history)
    total_stake = sum(h.get("stake", 0) for h in history)
    return {
        "settled": len(history),
        "won": won,
        "lost": lost,
        "unknown": 0,
        "total_profit": total_pnl,
        "balance": portfolio.get("balance", 0),
        "roi_pct": round(total_pnl / (total_stake or 1) * 100, 2),
        "total_bets": len(history),
    }


def main():
    logger.info("=== BB体育 虚拟投注晨间报告 ===")

    # Step 1: Try to get BB体育 scores
    bb_scores = extract_bb_scores()
    if bb_scores:
        logger.info(f"从 BB体育 获取 {len(bb_scores)} 场比赛比分")
    else:
        logger.info("BB体育 Chrome 未打开或页面不对")

    # Step 2: Settle remaining bets
    settle_result = settle_bets(bb_scores)
    if settle_result["settled"] > 0:
        logger.info(
            f"新结算: {settle_result['settled']} 笔 | "
            f"赢 {settle_result['won']} / 输 {settle_result['lost']} / 待定 {settle_result['unknown']} | "
            f"盈亏 ${settle_result['total_profit']:+.2f}"
        )

    # Step 3: Build cumulative stats + report
    cumulative = _calc_cumulative_stats()
    report = generate_report(cumulative, bb_scores)
    print(report)

    # Step 4: Push to DingTalk
    success = send_dingtalk(report)
    if success:
        logger.info("钉钉推送成功 ✅")
    else:
        logger.warning("钉钉推送失败，请检查 DINGTALK_WEBHOOK")

    return cumulative


if __name__ == "__main__":
    main()
