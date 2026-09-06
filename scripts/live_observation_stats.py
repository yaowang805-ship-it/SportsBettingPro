#!/usr/bin/env python3
"""滚球观察库分账统计 — 按运动×盘口(×方向)打印已结算样本的 ROI。

用法: python3 scripts/live_observation_stats.py [--all]

数据源: data/storage/live_paper_bets.json (second_level_monitor 写入的滚球虚拟投注观察库)。
sport 字段: BB 运动 id(1足球/3篮球/5网球/7棒球/6美式足球)。
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_PAPER_FILE = ROOT / "data" / "storage" / "live_paper_bets.json"
BB_SPORT_CN = {1: "足球", 3: "篮球", 5: "网球", 7: "棒球", 6: "美式足球"}


def _roi(bs):
    pnl = sum(b.get("profit", 0) or 0 for b in bs)
    stk = sum(b.get("stake", 0) or 0 for b in bs)
    n = len(bs)
    won = sum(1 for b in bs if b.get("result") == "won")
    return pnl, stk, n, won


def main():
    show_all = "--all" in sys.argv
    if not LIVE_PAPER_FILE.exists():
        print("观察库不存在:", LIVE_PAPER_FILE)
        return
    bets = json.loads(LIVE_PAPER_FILE.read_text())
    settled = [b for b in bets if b.get("settled")]
    unsettled = [b for b in bets if not b.get("settled")]
    print(f"总 {len(bets)} 条 | 已结算 {len(settled)} | 未结算 {len(unsettled)}")
    if not settled:
        print("(暂无已结算样本)")
        return

    pnl, stk, n, won = _roi(settled)
    print(f"整体: 盈亏 {pnl:+.1f} | 注额 {stk:.0f} | ROI {pnl/stk*100:+.1f}% | 胜 {won}/{n}\n")

    def print_grid(key_fn, title):
        grid = defaultdict(list)
        for b in settled:
            grid[key_fn(b)].append(b)
        print(f"=== {title} ===")
        for k, bs in sorted(grid.items(), key=lambda x: -len(x[1])):
            p, s, nn, w = _roi(bs)
            roi = p / s * 100 if s else 0
            print(f"  {k}: n={nn:3d} 盈亏{p:+8.1f} ROI{roi:+7.1f}% 胜{w}/{nn}")
        print()

    def sport_cn(b):
        sp = b.get("sport")
        return BB_SPORT_CN.get(sp) if isinstance(sp, int) else (f"sp{sp}" if sp else "未标")

    print_grid(lambda b: f"{sport_cn(b)}/{b.get('sub','?')}", "按 运动×盘口")
    if show_all:
        print_grid(lambda b: f"{sport_cn(b)}/{b.get('sub','?')}/{b.get('designation','?')}", "按 运动×盘口×方向")
        # 按 EV 分桶
        def ev_bucket(b):
            ev = b.get("ev", 0)
            for lo, hi in [(0, 3), (3, 5), (5, 8), (8, 12), (12, 999)]:
                if lo <= ev < hi:
                    return f"EV {lo}-{hi}%"
            return "EV ?"
        print_grid(ev_bucket, "按 EV 分桶")


if __name__ == "__main__":
    main()
