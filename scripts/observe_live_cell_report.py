#!/usr/bin/env python3
"""滚球观察库各格子表现报表 — 样本数 / 纸面ROI / 实盘ROI / 结果锚(⚠️不可信)。

⚠️ 滚球 vs 早盘两个关键区别(2026-10-18):
  1) 滚球无收盘价: 没有「下注时 vs 收盘」的真 CLV, 只有下注后价格方向的 LEV(且采集稀疏,
     信噪比≈0, 见记忆「滚球CLV=latency不是edge」)。故本表不设 CLV 列。
  2) 结果锚校准过不了时间切分: 前半拟合→后半测试 ROI +11.6% → -10.7%, 说明滚球「热门高估」
     在 1 天内不稳定(可能跟联赛/时段相关), isotonic 拟合的是噪声。故「结果锚ROI」列仅供
     观察, 不能当决策尺(早盘才是)。

用法: .venv312/bin/python scripts/observe_live_cell_report.py [--min-n 20]
"""
import json
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
              "double_chance": "dc", "btts": "btts", "ht": "ht", "ht_ou": "ht_ou",
              "handicap_games": "hc_games"}


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
    args = sys.argv[1:]
    for a in args:
        if a.startswith("--min-n"):
            min_n = int(a.split("=")[1]) if "=" in a else int(args[args.index(a) + 1])

    cells = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0,
                                 "real_n": 0, "real_stake": 0.0, "real_profit": 0.0,
                                 "cal_n": 0, "cal_stake": 0.0, "cal_profit": 0.0})

    # ── 滚球观察库 (live_paper_bets) ──
    live = json.load(open(DATA / "live_paper_bets.json"))
    X, Y = [], []
    for b in live:
        if b.get("result") in ("won", "lost") and _f(b.get("fair")):
            X.append(1.0 / _f(b.get("fair")))
            Y.append(1.0 if b.get("result") == "won" else 0.0)
    iso = IsotonicRegression(out_of_bounds="clip") if len(X) >= 50 else None
    if iso:
        iso.fit(X, Y)

    for b in live:
        fair = _f(b.get("fair"))
        if b.get("result") not in ("won", "lost") or not fair:
            continue
        sport = BB_SPORT_MAP.get(b.get("sport"), b.get("sport", "?"))
        sm = BB_SUB_MAP.get(b.get("sub"), b.get("sub", "?"))
        k = (sport, sm, _direction(b.get("designation"), sm), _bucket(fair, sport))
        c = cells[k]
        c["n"] += 1
        c["stake"] += _f(b.get("stake")) or 0
        c["profit"] += _f(b.get("profit")) or 0
        if iso and _f(b.get("bb_odds")):
            p_cal = max(float(iso.predict([1.0 / fair])[0]), 0.01)
            bb = _f(b.get("bb_odds"))
            if (bb - 1.0 / p_cal) / (1.0 / p_cal) > 0:
                c["cal_n"] += 1
                c["cal_stake"] += _f(b.get("stake")) or 0
                c["cal_profit"] += _f(b.get("profit")) or 0

    # ── 滚球实盘 (real_bets) ──
    try:
        rb = json.load(open(DATA / "real_bets.json"))
        rb = rb.get("bets", rb) if isinstance(rb, dict) else rb
        for b in rb.values():
            fair = _f(b.get("fair"))
            if b.get("result") not in ("won", "lost") or not fair:
                continue
            sport = BB_SPORT_MAP.get(b.get("sport"), b.get("sport", "?"))
            sm = BB_SUB_MAP.get(b.get("sub"), b.get("sub", "?"))
            k = (sport, sm, _direction(b.get("desig") or b.get("designation"), sm), _bucket(fair, sport))
            c = cells[k]
            c["real_n"] += 1
            c["real_stake"] += _f(b.get("stake")) or 0
            c["real_profit"] += _f(b.get("profit")) or 0
    except Exception:
        pass

    def roi(p, s):
        return 100 * p / s if s else None

    rows = []
    for k, c in cells.items():
        if c["n"] < min_n:
            continue
        rows.append((k, c, roi(c["profit"], c["stake"]), roi(c["real_profit"], c["real_stake"]),
                     roi(c["cal_profit"], c["cal_stake"])))
    rows.sort(key=lambda x: -x[1]["n"])

    print("⚠️ 滚球无收盘价(无真 CLV), 且结果锚校准过不了时间切分(前半+11.6%→后半-10.7%), 结果锚列仅供观察不可信\n")
    print(f"滚球观察库格子报表 (纸面 n≥{min_n}, 共 {len(rows)} 格)\n")
    print(f"{'格子(运动|盘口|方向|桶)':44s} {'纸面n':>6s} {'纸面ROI':>8s} {'实盘n':>6s} {'实盘ROI':>8s} {'校准n':>6s} {'校准ROI(⚠)':>10s}")
    print("-" * 96)
    for k, c, proi, rroi, croi in rows:
        sport, sm, dr, bk = k
        label = f"{sport}|{sm}|{dr}|{bk}"
        def fmt(v):
            return f"{v:+.1f}%" if v is not None else "  --  "
        print(f"{label:44s} {c['n']:6d} {fmt(proi):>8s} {c['real_n']:6d} {fmt(rroi):>8s} "
              f"{c['cal_n']:6d} {fmt(croi):>10s}")


if __name__ == "__main__":
    main()
