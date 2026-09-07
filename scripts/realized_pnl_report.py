#!/usr/bin/env python3
"""实盘+观察库 变现(P&L/ROI) 全量分账报告。

按 时间段(滚球/临场/近场/远场) × 运动 × 盘口, 展示已结算投注的真实盈亏。
数据源:
  - tracked_bets.json  实盘真实投注(有 push_time, 可算时间窗)
  - paper_bets.json    观察库纸面(早盘, 无 push_time, 只有运动×盘口)
  - live_paper_bets.json 滚球观察库(足球)

用法: python3 scripts/realized_pnl_report.py [--all]
  --all  额外打印 paper_bets 观察库的运动×盘口全表
"""
import json
import sys
from datetime import datetime
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "data" / "storage"


def _win_label(epoch, push_time):
    """时间窗: 滚球(lead<0)/临场<6h/近场6-24h/远场24-72h。"""
    if not epoch or not push_time:
        return "未知"
    try:
        pt = datetime.fromisoformat(str(push_time).replace("Z", "+00:00"))
        lead = float(epoch) - pt.timestamp()
    except Exception:
        return "未知"
    if lead < 0:
        return "滚球"
    if lead < 6 * 3600:
        return "临场"
    if lead < 24 * 3600:
        return "近场"
    return "远场"


def _roi(bs):
    """盈亏/ROI。void(退款)/push(走盘) 不算真实风险, 从分母剔除(否则稀释 ROI)。"""
    real = [b for b in bs if b.get("result") not in ("void", "push", None)]
    pnl = sum(float(b.get("profit") or 0) for b in real)
    stk = sum(float(b.get("stake") or 0) for b in real)
    n = len(real)
    won = sum(1 for b in real if b.get("result") == "won")
    return pnl, stk, n, won


def _print_grid(grid, title, min_n=1, sort_by_n=True):
    print(f"\n=== {title} ===")
    rows = sorted(grid.items(), key=lambda x: -len(x[1]) if sort_by_n else x[0])
    for key, bs in rows:
        if len(bs) < min_n:
            continue
        pnl, stk, n, won = _roi(bs)
        r = pnl / stk * 100 if stk else 0
        label = "/".join(str(k) for k in key) if isinstance(key, tuple) else str(key)
        print(f"  {label}: n={n:4d} 盈亏{pnl:+9.1f} ROI{r:+7.1f}% 胜{won}")


def report_tracked():
    p = STORAGE / "tracked_bets.json"
    if not p.exists():
        return
    bets = json.loads(p.read_text())
    bets = bets.get("bets", bets) if isinstance(bets, dict) else bets
    settled = [b for b in bets if b.get("status") == "settled"]
    print(f"── 实盘 tracked_bets: 总 {len(bets)} 条, 已结算 {len(settled)} ──")

    # 按时间段汇总
    tw = defaultdict(list)
    for b in settled:
        tw[_win_label(b.get("match_epoch"), b.get("push_time"))].append(b)
    print("\n[按时间段]")
    for label in ["滚球", "临场", "近场", "远场", "未知"]:
        bs = tw.get(label, [])
        if not bs:
            continue
        pnl, stk, n, won = _roi(bs)
        print(f"  {label}: n={n:4d} 盈亏{pnl:+9.1f} ROI{pnl/stk*100:+7.1f}% 胜{won}")

    # 时间段 × 运动 × 盘口
    grid = defaultdict(list)
    for b in settled:
        grid[(_win_label(b.get("match_epoch"), b.get("push_time")),
              b.get("sport", "?"), b.get("sub_market", "?"))].append(b)
    _print_grid(grid, "实盘 时间段×运动×盘口 (n>=3)", min_n=3)


def report_paper():
    p = STORAGE / "paper_bets.json"
    if not p.exists():
        return
    bets = json.loads(p.read_text())
    bets = bets.get("bets", bets) if isinstance(bets, dict) else bets
    settled = [b for b in bets if b.get("result") in ("won", "lost", "void")]
    print(f"\n── 观察库 paper_bets: 总 {len(bets)} 条, 已结算 {len(settled)} ──")
    grid = defaultdict(list)
    for b in settled:
        grid[(b.get("sport", "?"), b.get("sub_market", "?"))].append(b)
    _print_grid(grid, "观察库(早盘纸面) 运动×盘口 (n>=5)", min_n=5)


def report_live():
    p = STORAGE / "live_paper_bets.json"
    if not p.exists():
        return
    bets = json.loads(p.read_text())
    bets = bets if isinstance(bets, list) else bets.get("bets", bets)
    settled = [b for b in bets if b.get("settled")]
    print(f"\n── 滚球观察库 live_paper_bets: 总 {len(bets)} 条, 已结算 {len(settled)} ──")
    grid = defaultdict(list)
    for b in settled:
        sp = b.get("sport", "?")
        grid[(sp, b.get("sub", "?"), b.get("designation", "?"))].append(b)
    _print_grid(grid, "滚球 运动×盘口×方向 (n>=5)", min_n=5)


def main():
    report_tracked()
    if "--all" in sys.argv:
        report_paper()
        report_live()


if __name__ == "__main__":
    main()
