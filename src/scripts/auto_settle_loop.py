#!/usr/bin/env python3
"""自动结算循环 — 定时检查已完成比赛并结算待处理投注。

用法:
  python3 src/scripts/auto_settle_loop.py          # 单次运行
  python3 src/scripts/auto_settle_loop.py --loop   # 持续监控（每2小时检查一次）
  python3 src/scripts/auto_settle_loop.py --dry-run  # 试运行
"""
import sys, time, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger(__name__)

from src.monitor.auto_settle import auto_settle
from src.dashboard.components.virtual_portfolio import load_portfolio_state, _load_state


def print_summary():
    """打印当前组合摘要。"""
    state = load_portfolio_state()
    pending = state.get("pending_bets", [])
    history = state.get("history", [])
    balance = state.get("balance", 10000.0)

    total_stake = sum(h.get("stake", 0) for h in history)
    total_profit = sum(h.get("profit", 0) for h in history)
    wins = sum(1 for h in history if h.get("status") == "won")
    losses = sum(1 for h in history if h.get("status") == "lost")
    total = wins + losses

    logger.info("=" * 50)
    logger.info("  虚拟组合状态")
    logger.info("  余额: ¥%.2f", balance)
    logger.info("  已结算: %d 笔 (胜 %d / 负 %d)", total, wins, losses)
    if total > 0:
        logger.info("  胜率: %.1f%% | 总盈亏: ¥%.2f | ROI: %.2f%%",
                     wins / total * 100, total_profit, total_profit / total_stake * 100 if total_stake > 0 else 0)
    logger.info("  待结算: %d 笔", len(pending))
    logger.info("=" * 50)


def main():
    loop_mode = "--loop" in sys.argv
    dry_run = "--dry-run" in sys.argv

    if loop_mode:
        logger.info("🔄 启动自动结算监控循环（每2小时检查一次）")
        interval = 2 * 3600  # 2小时

        while True:
            try:
                n = auto_settle(dry_run=dry_run)
                if n > 0:
                    print_summary()
                else:
                    state = _load_state()
                    pending = state.get("pending_bets", [])
                    if not pending:
                        logger.info("✅ 所有投注已结算，监控结束")
                        print_summary()
                        break
                    logger.info("⏳ 仍有 %d 笔待结算，%d 分钟后重新检查...",
                                len(pending), interval // 60)
            except Exception as e:
                logger.exception("结算循环异常: %s", e)

            time.sleep(interval)
    else:
        n = auto_settle(dry_run=dry_run)
        print_summary()
        return n


if __name__ == "__main__":
    main()
