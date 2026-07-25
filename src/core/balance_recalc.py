"""余额重新计算工具 — 从第一性原理重算组合余额。

用法:
    python3 -m src.core.balance_recalc           # 检查并修复漂移
    python3 -m src.core.balance_recalc --check   # 仅检查不修复
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR

logger = get_logger(__name__)

PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"


def recalculate_balance(state: dict) -> float:
    """从第一性原理重算余额。

    balance = initial_bankroll + sum(history settled profit) - sum(pending stakes)

    Args:
        state: virtual_portfolio.json 内容

    Returns:
        重算后的 balance 值
    """
    initial = state.get("initial_bankroll", 50000.0)
    history_profit = sum(
        h.get("profit", 0) or 0
        for h in state.get("history", [])
    )
    pending_exposure = sum(
        b.get("stake", 0)
        for b in state.get("pending_bets", [])
    )
    calculated = round(initial + history_profit - pending_exposure, 2)
    return calculated


def check_and_fix(dry_run: bool = False, auto_fix: bool = True):
    """检查余额是否有漂移，可选自动修复。

    Args:
        dry_run: True 时只打印不保存
        auto_fix: True 时自动修复漂移
    """
    if not PORTFOLIO_FILE.exists():
        logger.warning("组合文件不存在: %s", PORTFOLIO_FILE)
        return

    state = json.loads(PORTFOLIO_FILE.read_text())
    stored = state.get("balance", 0)
    calculated = recalculate_balance(state)

    initial = state.get("initial_bankroll", 50000)
    history_profit = sum(h.get("profit", 0) or 0 for h in state.get("history", []))
    pending_exposure = sum(b.get("stake", 0) for b in state.get("pending_bets", []))
    pending_count = len(state.get("pending_bets", []))
    history_count = len(state.get("history", []))

    print(f"{'='*60}")
    print(f"余额重新计算")
    print(f"{'='*60}")
    print(f"  初始资金:       ¥{initial:>10,.0f}")
    print(f"  已结算盈亏:     ¥{history_profit:>+10,.0f}  ({history_count} 笔)")
    print(f"  待结算敞口:     ¥{pending_exposure:>10,.0f}  ({pending_count} 笔)")
    print(f"  {'':-<30}")
    print(f"  重算余额:       ¥{calculated:>10,.0f}")
    print(f"  存储余额:       ¥{stored:>10,.0f}")
    drift = stored - calculated
    print(f"  漂移:           ¥{drift:>+10,.2f}")

    if abs(drift) > 0.01:
        print(f"\n  ⚠️ 检测到余额漂移 ¥{drift:+.2f}！")
        if auto_fix and not dry_run:
            state["balance"] = calculated
            PORTFOLIO_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            print(f"  ✅ 已修复，balance = ¥{calculated:,.0f}")
        elif dry_run:
            print(f"  (dry-run，未保存)")
    else:
        print(f"  ✅ 余额一致，无漂移")

    return calculated, drift


if __name__ == "__main__":
    import argparse
    from config.logging_config import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(description="余额重新计算工具")
    parser.add_argument("--check", action="store_true", help="仅检查不修复")
    args = parser.parse_args()

    if args.check:
        check_and_fix(dry_run=False, auto_fix=False)
    else:
        check_and_fix(dry_run=False, auto_fix=True)
