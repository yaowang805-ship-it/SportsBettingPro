#!/usr/bin/env python3
"""观察库综合分析 — 方向级CLV + 时间窗CLV + 实盘ROI关联, 发现规律/新机会/假edge。

观察库(clv_tracking source=validate)记录所有 EV≥5% 机会(2026-08-30 起, 原2%是噪声)。
本脚本三个维度:
  1. 方向级: (sub_market, designation) 的 CLV, 发现被盘口级门槛掩盖的方向级 edge
  2. 时间窗: 早盘(24-72h)/近场(6-24h)/临场(<6h) 的 CLV, 发现哪个窗口 edge 干净
  3. ROI关联: join 实盘(tracked_bets), 标记「CLV正但ROI负」的假 edge 方向(如 ht 客胜)

用法: .venv312/bin/python scripts/observe_analysis.py
"""
import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "storage"


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _time_window(match_epoch, push_time):
    """距开赛时间 → 早盘/近场/临场。"""
    try:
        lead_h = (match_epoch - push_time) / 3600.0
    except Exception:
        return None
    if lead_h < 6:
        return "临场<6h"
    if lead_h <= 24:
        return "近6-24h"
    if lead_h <= 72:
        return "早24-72h"
    return ">72h"


def load_observe():
    """读观察库样本(validate), 返回 [(sub_market, designation, clv, time_window, league)]。"""
    out = []
    p = DATA / "clv_results.csv"
    if not p.exists():
        return out
    with open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("source") or "push").strip() != "validate":
                continue
            if (r.get("close_source") or "live").strip() == "archive_open":
                continue
            clv = _f(r.get("true_clv_pct"))
            ev = _f(r.get("push_ev_pct"))
            if clv is None or ev is None or ev < 5.0:
                continue
            out.append({
                "sm": r.get("sub_market", "?"),
                "des": r.get("designation", "?"),
                "clv": clv,
                "league": r.get("league", "?"),
                "window": _time_window(_f(r.get("match_epoch"), 0), _f(r.get("push_time"), 0)),
            })
    return out


def load_roi():
    """实盘 ROI, 按 (sub_market, designation)。"""
    p = DATA / "tracked_bets.json"
    roi = defaultdict(lambda: {"stake": 0.0, "profit": 0.0, "n": 0})
    if not p.exists():
        return roi
    d = json.loads(p.read_text())
    bets = d if isinstance(d, list) else (d.get("bets") or list(d.values()))
    for b in bets:
        if not isinstance(b, dict):
            continue
        if str(b.get("result")) not in ("won", "lost"):
            continue
        k = (b.get("sub_market", "?"), b.get("designation", "?"))
        s = _f(b.get("stake"), 0) or 0
        pr = _f(b.get("profit"), None)
        if pr is None:
            o = _f(b.get("bb_odds"), 0) or 0
            pr = s * (o - 1) if str(b.get("result")) == "won" else -s
        roi[k]["stake"] += s
        roi[k]["profit"] += pr
        roi[k]["n"] += 1
    return roi


def main():
    obs = load_observe()
    roi = load_roi()
    print(f"观察库样本(EV≥5%): {len(obs)} 条")

    # 1. 方向级 CLV
    by_dir = defaultdict(list)
    for o in obs:
        by_dir[(o["sm"], o["des"])].append(o["clv"])
    print("\n=== 1. 方向级(样本≥30, 按中位CLV降序) ===")
    print(f"{'盘口/方向':<36}{'样本':>5}{'中位CLV':>9}{'正率':>6}{'实盘ROI':>10}")
    rows = []
    for (sm, des), c in by_dir.items():
        if len(c) < 30:
            continue
        med = statistics.median(c)
        pos = sum(1 for x in c if x > 0) / len(c) * 100
        r = roi.get((sm, des))
        roi_val = r["profit"] / r["stake"] * 100 if r and r["stake"] > 0 else None
        rows.append((sm, des, len(c), med, pos, roi_val, r["n"] if r else 0))
    for sm, des, n, med, pos, roi_val, rn in sorted(rows, key=lambda x: -x[3]):
        roi_s = f"{roi_val:+.1f}%({rn})" if roi_val is not None else "—"
        flag = " ⚠️CLV正ROI负" if (med > 1 and roi_val is not None and roi_val < 0) else ""
        print(f"{sm}/{des:<30}{n:>5}{med:>+8.1f}%{pos:>5.0f}%{roi_s:>10}{flag}")

    # 2. 时间窗 CLV
    by_win = defaultdict(list)
    for o in obs:
        if o["window"]:
            by_win[o["window"]].append(o["clv"])
    print("\n=== 2. 时间窗 CLV ===")
    print(f"{'时间窗':<12}{'样本':>6}{'中位CLV':>9}{'正率':>7}")
    for w in ["早24-72h", "近6-24h", "临场<6h"]:
        c = by_win.get(w, [])
        if not c:
            continue
        med = statistics.median(c)
        pos = sum(1 for x in c if x > 0) / len(c) * 100
        flag = " ✅干净" if med > 1 else " ⚠️噪声" if med < 0 else ""
        print(f"{w:<12}{len(c):>6}{med:>+8.1f}%{pos:>6.0f}%{flag}")


if __name__ == "__main__":
    main()
