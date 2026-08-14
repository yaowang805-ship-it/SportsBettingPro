"""成交回填对比 — 真实成交(截图录入) vs 系统假设盈亏(tracked_bets)。

系统 tracked_bets 把每条推送默认当成"已下注"并按 Kelly 金额记账(假设值)。
本脚本读取 actual_bets.json(真实成交, 手动从 BB 结算截图录入), 对比真实 ROI vs 假设 ROI。

用法: python3 -m src.monitor.actual_bets_compare
"""
import json
from datetime import datetime
from pathlib import Path

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

ACTUAL_BETS = DATA_DIR / "actual_bets.json"
TRACKED_BETS = DATA_DIR / "tracked_bets.json"


def load_actual():
    if ACTUAL_BETS.exists():
        try:
            return json.loads(ACTUAL_BETS.read_text())
        except Exception:
            pass
    return []


def load_tracked():
    if TRACKED_BETS.exists():
        try:
            return json.loads(TRACKED_BETS.read_text()).get("bets", [])
        except Exception:
            pass
    return []


def compare() -> dict:
    actual = load_actual()
    tracked = load_tracked()

    # 真实成交统计
    actual_stake = sum(b.get("stake", 0) or 0 for b in actual)
    actual_profit = sum(b.get("profit", 0) or 0 for b in actual)
    actual_won = sum(1 for b in actual if b.get("result") == "won")
    actual_lost = sum(1 for b in actual if b.get("result") == "lost")
    actual_void = sum(1 for b in actual if b.get("result") == "void")

    # 系统假设统计
    sys_settled = [b for b in tracked if b.get("status") == "settled"]
    sys_stake = sum(b.get("stake", 0) or 0 for b in sys_settled)
    sys_profit = sum(b.get("profit", 0) or 0 for b in sys_settled)

    result = {
        "actual_bets_count": len(actual),
        "actual_stake": round(actual_stake, 2),
        "actual_profit": round(actual_profit, 2),
        "actual_roi": round(actual_profit / actual_stake * 100, 2) if actual_stake else 0,
        "actual_won": actual_won, "actual_lost": actual_lost, "actual_void": actual_void,
        "system_settled_count": len(sys_settled),
        "system_stake": round(sys_stake, 2),
        "system_profit": round(sys_profit, 2),
        "system_roi": round(sys_profit / sys_stake * 100, 2) if sys_stake else 0,
    }
    return result


if __name__ == "__main__":
    r = compare()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if r["actual_bets_count"] == 0:
        print("\n⚠️ actual_bets.json 还是空的 — 请从 BB 结算截图录入真实成交。")
