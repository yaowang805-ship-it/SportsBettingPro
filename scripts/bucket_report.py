#!/usr/bin/env python3
"""概率桶 × 执行价值/ROI 分析 — 滚球(LEV+ROI) + 早盘(CLV+ROI)。

数据源:
  滚球: data/storage/live_paper_bets.json  (LEV=clv字段(下注后3s复验), ROI=profit/stake, 桶=fair)
  早盘: data/storage/clv_results.csv       (CLV=true_clv_pct, 桶=push_fair_price)
        data/storage/tracked_bets.json     (实盘 ROI=profit/stake, 桶=fair_price)

用法: .venv312/bin/python scripts/bucket_report.py [--all]
  --all 打印所有桶(默认只打印 LEV/CLV>0 且 ROI>0 的桶 + 汇总)
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.scrapers.odds_interval import prob_bucket_from_fair

DATA = ROOT / "data" / "storage"

BB_SPORT = {1: "football", 2: "ice_hockey", 3: "basketball", 5: "tennis", 6: "american_football",
            7: "baseball", 13: "volleyball", 15: "pingpong", 18: "mma", 19: "boxing", 47: "badminton"}
SPORT_CN = {"football": "足球", "basketball": "篮球", "tennis": "网球", "baseball": "棒球",
            "ice_hockey": "冰球", "american_football": "美足", "volleyball": "排球",
            "pingpong": "乒乓", "mma": "MMA", "boxing": "拳击", "badminton": "羽毛球"}
SUB_CN = {"opportunities": "独赢", "over_under": "大小", "handicap": "让球", "double_chance": "双机会",
          "btts": "双边进球", "ht": "上半独赢", "ht_ou": "上半大小", "ht_hc": "上半让球",
          "dc": "双机会", "dnb": "平局退款", "ml": "独赢", "ou": "大小", "hc": "让球", "1x2": "独赢",
          # 早盘 clv_results.csv 的 sub_market 命名
          "ht_dc": "上半双机会", "htft": "半全场", "correct_score": "正确比分",
          "correct_score_ht": "正确比分上半", "total_goals_range": "总进球区间",
          "first_to_score": "先进球", "last_to_score": "最后进球", "both_teams_score": "双方进球",
          "half_with_most_goals": "进球最多半场", "both_teams_to_score": "双方进球"}


def _cn(orig):
    return SUB_CN.get(orig, orig)


def _sport_name(sp):
    if isinstance(sp, str):
        return sp
    return BB_SPORT.get(sp, f"sp{sp}")


def _bucket(fair, sp):
    return prob_bucket_from_fair(fair, _sport_name(sp))


def _fmt_roi(pnl, stk):
    return pnl / stk * 100 if stk else 0.0


def _print_rows(rows, quality_label):
    """rows: [(sport, sub, bucket, n, quality, roi, pnl, won)]"""
    print(f"{'':2s}{'运动':6s}{'盘口':8s}{'概率桶':10s}{'n':>5s}  {quality_label:>8s}  {'ROI':>8s}  {'盈亏':>9s}  胜")
    print("-" * 74)
    for sp, sub, bk, n, q, roi, pnl, won in rows:
        print(f"{'★' if (q > 0 and roi > 0) else ' ':2s}"
              f"{SPORT_CN.get(sp, sp):6s}{_cn(sub):8s}{bk:10s}{n:5d}  {q:+7.2f}%  {roi:+7.1f}%  {pnl:+9.1f}  {won}")


def live_report(show_all):
    p = DATA / "live_paper_bets.json"
    if not p.exists():
        print("滚球观察库不存在:", p)
        return
    bets = json.loads(p.read_text())
    settled = [b for b in bets if b.get("settled")]
    grid = defaultdict(list)
    for b in settled:
        grid[(_sport_name(b.get("sport")), b.get("sub", "?"), _bucket(b.get("fair"), b.get("sport")))].append(b)

    rows = []
    for (sp, sub, bk), bs in grid.items():
        n = len(bs)
        lev = sum(b.get("clv") or 0 for b in bs) / n  # LEV = clv 字段(下注后3s复验)
        pnl = sum(b.get("profit") or 0 for b in bs)
        stk = sum(b.get("stake") or 0 for b in bs)
        roi = _fmt_roi(pnl, stk)
        won = sum(1 for b in bs if b.get("result") == "won")
        rows.append((sp, sub, bk, n, lev, roi, pnl, won))

    rows.sort(key=lambda r: (-(r[4] > 0 and r[5] > 0), -(r[5]), -r[3]))
    good = [r for r in rows if r[4] > 0 and r[5] > 0]
    print("\n" + "=" * 74)
    print(f"滚球概率桶 — LEV>0 且 ROI>0 的桶（共 {len(good)} 个）")
    print("=" * 74)
    if good:
        _print_rows(good, "LEV")
    else:
        print("  (无 LEV>0 且 ROI>0 的桶)")
    if show_all:
        print("\n--- 全部滚球桶 ---")
        _print_rows(rows, "LEV")


def prematch_clv_report(show_all):
    p = DATA / "clv_results.csv"
    if not p.exists():
        print("clv_results.csv 不存在")
        return
    rows = []
    grid = defaultdict(list)
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("source") != "validate":
                continue
            sp = r.get("sport", "?")
            sub = r.get("sub_market", "?")
            fair = r.get("push_fair_price")
            clv = r.get("true_clv_pct")
            try:
                clv = float(clv)
            except (TypeError, ValueError):
                clv = None
            if clv is None:
                continue
            bk = _bucket(fair, sp)
            grid[(sp, sub, bk)].append(clv)

    rows = []
    for (sp, sub, bk), clvs in grid.items():
        n = len(clvs)
        mean_clv = sum(clvs) / n
        rows.append((sp, sub, bk, n, mean_clv, None, None, None))
    rows.sort(key=lambda r: (-(r[4] > 0), -r[4], -r[3]))
    good = [r for r in rows if r[4] > 0]
    print("\n" + "=" * 74)
    print(f"早盘概率桶 — CLV>0 的桶（共 {len(good)} 个，按运动×盘口×概率桶）")
    print("=" * 74)
    print(f"{'':2s}{'运动':6s}{'盘口':10s}{'概率桶':10s}{'n':>5s}  {'CLV':>8s}")
    print("-" * 56)
    show = good if not show_all else rows
    for sp, sub, bk, n, mean_clv, *_ in show:
        print(f"{'★' if mean_clv > 0 else ' ':2s}{SPORT_CN.get(sp, sp):6s}{_cn(sub):10s}{bk:10s}{n:5d}  {mean_clv:+7.2f}%")


def prematch_roi_report(show_all):
    p = DATA / "tracked_bets.json"
    if not p.exists():
        print("tracked_bets.json 不存在")
        return
    bets = json.loads(p.read_text())
    bets = bets if isinstance(bets, list) else list(bets.values())
    settled = [b for b in bets if isinstance(b, dict) and b.get("status") == "settled"]
    grid = defaultdict(list)
    for b in settled:
        sp = b.get("sport", "?")
        sub = b.get("sub_market", "?")
        bk = _bucket(b.get("fair_price"), sp)
        grid[(sp, sub, bk)].append(b)

    rows = []
    for (sp, sub, bk), bs in grid.items():
        n = len(bs)
        pnl = sum(b.get("profit") or 0 for b in bs)
        stk = sum(b.get("stake") or 0 for b in bs)
        roi = _fmt_roi(pnl, stk)
        won = sum(1 for b in bs if b.get("result") == "won")
        rows.append((sp, sub, bk, n, roi, pnl, won))
    rows.sort(key=lambda r: (-(r[4] > 0), -r[4], -r[3]))
    good = [r for r in rows if r[4] > 0]
    print("\n" + "=" * 74)
    print(f"早盘实盘概率桶 — ROI>0 的桶（共 {len(good)} 个）")
    print("=" * 74)
    print(f"{'':2s}{'运动':6s}{'盘口':10s}{'概率桶':10s}{'n':>5s}  {'ROI':>8s}  {'盈亏':>9s}  胜")
    print("-" * 74)
    show = good if not show_all else rows
    for sp, sub, bk, n, roi, pnl, won in show:
        print(f"{'★' if roi > 0 else ' ':2s}{SPORT_CN.get(sp, sp):6s}{_cn(sub):10s}{bk:10s}{n:5d}  {roi:+7.1f}%  {pnl:+9.1f}  {won}")


def main():
    show_all = "--all" in sys.argv
    live_report(show_all)
    prematch_clv_report(show_all)
    prematch_roi_report(show_all)


if __name__ == "__main__":
    main()
