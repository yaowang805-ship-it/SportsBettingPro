#!/usr/bin/env python3
"""早盘观察库各格子表现报表 — 样本数 / 纸面ROI / 实盘ROI / 收盘价CLV / 结果锚 并排。

目的(2026-10-18): 一个格子「该不该投」看五把尺, 别只看一把:
  - 样本数: 纸面已结算注数(够不够下结论)
  - 纸面ROI: 观察库全量注的实际 ROI(可能被下注时公平价高估污染 → 假正)
  - 实盘ROI: 真实投注的 ROI(真金白银, 但样本通常薄)
  - 收盘价CLV: 中位 CLV(bb vs Pin收盘价, 事后验证尺, 但继承收盘价 +2.2pp 残留偏差)
  - 结果锚: 校准后 ev_cal>0 的注的实际 ROI(校准到真实胜率, 决策尺)

五把尺对不齐的格子(如纸面ROI正但结果锚负)就是「假溢价」重灾区。

用法: .venv312/bin/python scripts/observe_cell_report.py [--all 显示全部] [--min-n 10]
"""
import json
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"

BB_SPORT_MAP = {1: "football", 3: "basketball", 5: "tennis", 7: "baseball",
                6: "american_football", 2: "ice_hockey", 13: "volleyball", 15: "pingpong",
                18: "mma", 19: "boxing", 47: "badminton"}
BB_SUB_MAP = {"over_under": "ou", "handicap": "hc", "opportunities": "1x2",
              "double_chance": "dc", "btts": "btts", "ht": "ht", "ht_ou": "ht_ou"}


def _direction(desig, sub_market):
    d = (desig or "")
    if sub_market == "dc":
        if "主" in d and "和" in d:
            return "1X"
        if "主" in d and "客" in d:
            return "12"
        if "和" in d and "客" in d:
            return "X2"
        return "其他"
    dl = d.lower()
    if "大" in d or "over" in dl:
        return "大"
    if "小" in d or "under" in dl:
        return "小"
    if ("和" in d or "平" in d or "draw" in dl) and "客" not in d and "主" not in d:
        return "平"
    if "客" in d or "away" in dl:
        return "客"
    if "主" in d or "home" in dl:
        return "主"
    return "其他"


def _bucket(fair, sport):
    import sys as _s
    if str(ROOT) not in _s.path:
        _s.path.insert(0, str(ROOT))
    from src.scrapers.odds_interval import prob_bucket
    if not fair or fair <= 1:
        return "?"
    return prob_bucket(1.0 / float(fair), sport)


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def main():
    min_n = 5
    show_all = False
    args = sys.argv[1:]
    for a in args:
        if a.startswith("--min-n"):
            min_n = int(a.split("=")[1]) if "=" in a else int(args[args.index(a) + 1])
        if a == "--all":
            show_all = True

    cells = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0,  # 纸面
                                 "real_n": 0, "real_stake": 0.0, "real_profit": 0.0,  # 实盘
                                 "clv": [],  # CLV 列表
                                 "cal_n": 0, "cal_stake": 0.0, "cal_profit": 0.0})  # 结果锚

    # ── 纸面 (paper_bets) ──
    paper = json.load(open(DATA / "paper_bets.json"))
    paper = paper.get("bets", paper) if isinstance(paper, dict) else paper
    # 拟合结果锚 isotonic(下注时隐含胜率 → 实际)
    X, Y = [], []
    for b in paper:
        if b.get("result") in ("won", "lost") and _f(b.get("fair_price")):
            X.append(1.0 / _f(b.get("fair_price")))
            Y.append(1.0 if b.get("result") == "won" else 0.0)
    iso = IsotonicRegression(out_of_bounds="clip") if len(X) >= 50 else None
    if iso:
        iso.fit(X, Y)

    for b in paper:
        fair = _f(b.get("fair_price"))
        if b.get("result") not in ("won", "lost") or not fair:
            continue
        sport = b.get("sport") or "?"
        sm = b.get("sub_market") or "?"
        k = (sport, sm, _direction(b.get("designation"), sm), _bucket(fair, sport))
        c = cells[k]
        c["n"] += 1
        c["stake"] += _f(b.get("stake")) or 0
        c["profit"] += _f(b.get("profit")) or 0
        # 结果锚
        if iso and _f(b.get("bb_odds")):
            p_cal = max(float(iso.predict([1.0 / fair])[0]), 0.01)
            fair_cal = 1.0 / p_cal
            bb = _f(b.get("bb_odds"))
            if (bb - fair_cal) / fair_cal > 0:
                c["cal_n"] += 1
                c["cal_stake"] += _f(b.get("stake")) or 0
                c["cal_profit"] += _f(b.get("profit")) or 0

    # ── 实盘 (tracked_bets) ──
    try:
        tb = json.load(open(DATA / "tracked_bets.json"))
        tb = tb.get("bets", tb) if isinstance(tb, dict) else tb
        for b in tb:
            fair = _f(b.get("fair_price")) or _f(b.get("pin_odds"))
            if b.get("result") not in ("won", "lost") or not fair:
                continue
            sport = b.get("sport") or "?"
            sm = b.get("sub_market") or "?"
            k = (sport, sm, _direction(b.get("designation"), sm), _bucket(fair, sport))
            c = cells[k]
            c["real_n"] += 1
            c["real_stake"] += _f(b.get("stake")) or 0
            c["real_profit"] += _f(b.get("profit")) or 0
    except Exception:
        pass

    # ── 收盘价 CLV (clv_results) ──
    try:
        for r in csv.DictReader(open(DATA / "clv_results.csv", encoding="utf-8-sig")):
            if r.get("close_source") != "live" or r.get("source") != "validate":
                continue
            clv = _f(r.get("true_clv_pct"))
            fair = _f(r.get("push_fair_price"))
            if clv is None or not fair:
                continue
            sport = r.get("sport") or "?"
            sm = r.get("sub_market") or "?"
            k = (sport, sm, _direction(r.get("designation"), sm), _bucket(fair, sport))
            cells[k]["clv"].append(clv)
    except Exception:
        pass

    # ── 展示 ──
    def roi(p, s):
        return 100 * p / s if s else None

    rows = []
    for k, c in cells.items():
        if c["n"] < min_n and not show_all:
            continue
        sport, sm, dr, bk = k
        clv_med = np.median(c["clv"]) if c["clv"] else None
        rows.append((k, c, roi(c["profit"], c["stake"]), roi(c["real_profit"], c["real_stake"]),
                     clv_med, roi(c["cal_profit"], c["cal_stake"])))

    # 按样本数降序
    rows.sort(key=lambda x: -x[1]["n"])

    print(f"早盘观察库格子报表 (纸面 n≥{min_n}, 共 {len(rows)} 格)\n")
    print(f"{'格子(运动|盘口|方向|桶)':44s} {'纸面n':>6s} {'纸面ROI':>8s} {'实盘n':>6s} {'实盘ROI':>8s} {'CLV中位':>8s} {'校准n':>6s} {'校准ROI':>8s}")
    print("-" * 100)
    for k, c, proi, rroi, clv_med, croi in rows:
        sport, sm, dr, bk = k
        label = f"{sport}|{sm}|{dr}|{bk}"
        def fmt(v):
            return f"{v:+.1f}%" if v is not None else "  --  "
        # 结果锚负但纸面正 = 假溢价重灾区, 标 *
        flag = "*" if (proi is not None and croi is not None and proi > 0 and croi <= 0) else " "
        print(f"{label:44s} {c['n']:6d} {fmt(proi):>8s} {c['real_n']:6d} {fmt(rroi):>8s} "
              f"{fmt(clv_med):>8s} {c['cal_n']:6d} {fmt(croi):>8s}{flag}")

    print("\n* = 纸面ROI正但结果锚负(假溢价重灾区, 下注时公平价高估了它)")
    print("\n=== 假溢价重灾区(纸面正/结果锚负, 该撤) ===")
    for k, c, proi, rroi, clv_med, croi in rows:
        if proi is not None and croi is not None and proi > 0 and croi <= 0:
            print(f"  {k}: 纸面ROI {proi:+.1f}% → 结果锚 {croi:+.1f}% (校准n={c['cal_n']})")


if __name__ == "__main__":
    main()
