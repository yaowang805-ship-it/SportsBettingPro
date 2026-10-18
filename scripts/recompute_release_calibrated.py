#!/usr/bin/env python3
"""用「结果锚校准」口径重算释放清单 — 看哪些格子该留、哪些该撤。

问题(2026-10-18): 现有释放清单(compute_market_release.py)的观察库释放用「早盘收盘线 CLV」
当判据, 但实测收盘价仍有 +2.2pp 残留偏差(和局档收盘后仍高估), 正 CLV 的注实际亏钱
(Whelan: 3/5 正 CLV 档仍亏)。校准到「实际结果」比校准到「收盘」更准(Brier 0.209 vs 0.216)。

本脚本用结果锚 isotonic 校准(下注时公平价 → 实际胜率), 对每个格子算:
  - raw ROI: 该格子全部已结算注的实际 ROI(现状口径)
  - 校准后: ev_cal=(bb-fair_cal)/fair_cal > 0 的注有几笔、它们的实际 ROI
格子真正有 edge ⇔ 校准后 ev_cal>0 的注 ROI>0(公平价低估了它), 否则是假溢价(公平价高估)。

输出: 与 market_release.json 对比, 每格子给「保留/撤回/释放」建议。

用法: .venv312/bin/python scripts/recompute_release_calibrated.py
"""
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
PAPER = DATA / "paper_bets.json"
LIVE_PAPER = DATA / "live_paper_bets.json"
RELEASE = DATA / "market_release.json"

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
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from src.scrapers.odds_interval import prob_bucket
    if not fair or fair <= 1:
        return "?"
    return prob_bucket(1.0 / float(fair), sport)


def _load_rows():
    """返回 [(scope, sport, sub, direction, bucket, bb, fair, won, stake, profit), ...]"""
    rows = []
    # 早盘 paper_bets
    paper = json.load(open(PAPER))
    paper = paper.get("bets", paper) if isinstance(paper, dict) else paper
    for b in paper:
        if b.get("result") not in ("won", "lost") or not b.get("fair_price") or not b.get("bb_odds"):
            continue
        sport = b.get("sport") or "?"
        sub = b.get("sub_market") or "?"
        fair = b["fair_price"]
        rows.append(("early", sport, sub, _direction(b.get("designation"), sub),
                     _bucket(fair, sport), b["bb_odds"], fair,
                     1.0 if b["result"] == "won" else 0.0, b.get("stake") or 0, b.get("profit") or 0))
    # 滚球 live_paper_bets
    live = json.load(open(LIVE_PAPER))
    for b in live:
        if b.get("result") not in ("won", "lost") or not b.get("fair") or not b.get("bb_odds"):
            continue
        sport = BB_SPORT_MAP.get(b.get("sport"), b.get("sport", "?"))
        sub = BB_SUB_MAP.get(b.get("sub"), b.get("sub", "?"))
        fair = b["fair"]
        rows.append(("live", sport, sub, _direction(b.get("designation"), sub),
                     _bucket(fair, sport), b["bb_odds"], fair,
                     1.0 if b["result"] == "won" else 0.0, b.get("stake") or 0, b.get("profit") or 0))
    return rows


def _fit(rows, scope):
    X, Y = [], []
    for r in rows:
        if r[0] == scope:
            X.append(1.0 / r[6])
            Y.append(r[7])
    if len(X) < 10:
        return None
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(np.array(X), np.array(Y))
    return iso


def main():
    rows = _load_rows()
    iso = {"early": _fit(rows, "early"), "live": _fit(rows, "live")}

    # 组格 + 指标
    cell = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0,
                                "n_cal": 0, "stake_cal": 0.0, "profit_cal": 0.0})
    for r in rows:
        scope, sport, sub, dr, bk, bb, fair, won, stk, prof = r
        k = (sport, sub, dr, bk, scope)
        cell[k]["n"] += 1
        cell[k]["stake"] += stk
        cell[k]["profit"] += prof
        # 校准后 ev_cal
        m = iso.get(scope)
        if m is None:
            continue
        p_cal = max(float(m.predict(np.array([1.0 / fair]))[0]), 0.01)
        fair_cal = 1.0 / p_cal
        if (bb - fair_cal) / fair_cal > 0:
            cell[k]["n_cal"] += 1
            cell[k]["stake_cal"] += stk
            cell[k]["profit_cal"] += prof

    # 当前释放配置
    try:
        rel = json.load(open(RELEASE))
        released = {tuple(x) for x in rel.get("observe_released", [])}
        blocked = {tuple(x) for x in rel.get("observe_blocked", [])}
    except Exception:
        released, blocked = set(), set()

    def roi_of(c):
        return 100 * c["profit"] / c["stake"] if c["stake"] else 0.0

    def roi_cal(c):
        return 100 * c["profit_cal"] / c["stake_cal"] if c["stake_cal"] else 0.0

    print(f"总格子数(含 n>=1): {len(cell)}\n")
    print("=== 按 n 排序的格子清单 (raw vs 校准后) ===")
    print(f"{'格子(运动|盘口|方向|桶|阶段)':52s} {'n':>5s} {'rawROI':>7s} {'校准n':>6s} {'校准ROI':>8s} {'现状':>6s} {'建议':>6s}")
    for k in sorted(cell, key=lambda k: -cell[k]["n"]):
        c = cell[k]
        if c["n"] < 5:  # 样本太少跳过
            continue
        sport, sub, dr, bk, scope = k
        rroi = roi_of(c)
        croi = roi_cal(c)
        # 现状
        status = "释放" if k in released else ("封杀" if k in blocked else "未定")
        # 建议: 校准后有足够样本且 ROI>0 才留
        if c["n_cal"] >= 5 and croi > 0:
            rec = "保留"
        elif c["n_cal"] >= 5 and croi <= 0:
            rec = "撤回"
        else:
            rec = "观望"
        print(f"{(sport+'|'+sub+'|'+dr+'|'+bk+'|'+scope):52s} {c['n']:5d} {rroi:+7.1f}% {c['n_cal']:6d} {croi:+7.1f}% {status:>6s} {rec:>6s}")

    # 重点: 当前释放但校准后该撤的
    print("\n=== 当前「释放」但校准后建议「撤回」的格子 ===")
    for k in sorted(cell, key=lambda k: -cell[k]["n"]):
        if k not in released:
            continue
        c = cell[k]
        if c["n_cal"] >= 5 and roi_cal(c) <= 0:
            print(f"  {k}: n={c['n']} rawROI={roi_of(c):+.1f}% 校准n={c['n_cal']} 校准ROI={roi_cal(c):+.1f}%")
    print("\n=== 当前「封杀/未定」但校准后建议「保留」的格子 ===")
    for k in sorted(cell, key=lambda k: -cell[k]["n"]):
        if k in released:
            continue
        c = cell[k]
        if c["n_cal"] >= 5 and roi_cal(c) > 0:
            print(f"  {k}: n={c['n']} rawROI={roi_of(c):+.1f}% 校准n={c['n_cal']} 校准ROI={roi_cal(c):+.1f}%")


if __name__ == "__main__":
    main()
