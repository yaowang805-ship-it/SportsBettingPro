#!/usr/bin/env python3
"""数据盘口周报 — 每周日 21:00 推运动×盘口全表(门槛/CLV/实盘ROI)。

数据源:
  ev_threshold_matrix.json — 数据驱动门槛(盘口层+运动层)
  clv_results.csv          — CLV(validate+push 合并, 与门槛同口径)
  tracked_bets.json        — 实盘 ROI(已定胜负)

口径: CLV = 观察库+实盘库合并(门槛就是这么算的); ROI = 实盘真金白银。
用法: .venv312/bin/python scripts/weekly_market_report.py
"""
import json
import csv
import statistics
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "storage"

SPORT_CN = {
    "football": "⚽足球", "basketball": "🏀篮球", "tennis": "🎾网球",
    "baseball": "⚾棒球", "american_football": "🏈美足", "ice_hockey": "🏒冰球",
    "mma": "🥋MMA", "boxing": "🥊拳击", "volleyball": "🏐排球",
    "pingpong": "🏓乒乓", "badminton": "🏸羽毛球",
}


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def load_clv():
    """分 (sport, sub_market) 的 CLV(validate+push 合并)。"""
    clv = defaultdict(list)
    p = DATA / "clv_results.csv"
    if not p.exists():
        return clv
    with open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("close_source") or "live").strip() == "archive_open":
                continue
            c = _f(r.get("true_clv_pct"))
            ev = _f(r.get("push_ev_pct"))
            if c is None or ev is None or ev < 2.0:
                continue
            clv[(r.get("sport", "?"), r.get("sub_market", "?"))].append(c)
    return clv


def load_roi():
    """分 (sport, sub_market) 的实盘 ROI。"""
    p = DATA / "tracked_bets.json"
    roi = defaultdict(lambda: {"stake": 0.0, "profit": 0.0, "n": 0})
    if not p.exists():
        return roi
    try:
        d = json.loads(p.read_text())
    except Exception:
        return roi
    bets = d if isinstance(d, list) else (d.get("bets") or list(d.values()))
    for b in bets:
        if not isinstance(b, dict):
            continue
        if str(b.get("result")) not in ("won", "lost"):
            continue
        k = (b.get("sport", "?"), b.get("sub_market", "?"))
        s = _f(b.get("stake"), 0) or 0
        pr = _f(b.get("profit"), None)
        if pr is None:
            o = _f(b.get("bb_odds"), 0) or 0
            pr = s * (o - 1) if str(b.get("result")) == "won" else -s
        roi[k]["stake"] += s
        roi[k]["profit"] += pr
        roi[k]["n"] += 1
    return roi


def load_mtx():
    p = DATA / "ev_threshold_matrix.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _threshold(mtx, sport, sub_market):
    """实际门槛 = 运动×盘口独立值(非足球) / 盘口层+运动调整。"""
    markets = mtx.get("markets", {})
    default = mtx.get("default_threshold", 8.0)
    if sport != "football":
        sm = mtx.get("sport_market", {}).get(sport, {}).get(sub_market)
        if sm is not None:
            return sm
        return markets.get(sub_market, default) + mtx.get("sport_adjust", {}).get(sport, 0.0)
    return markets.get(sub_market, default)


def _concl(med, pos, roi_val):
    """结论: 真edge / CLV正ROI负 / 负edge / 样本不足。"""
    if roi_val is not None:
        if roi_val > 0 and med is not None and med > 0:
            return "✅真edge"
        if roi_val < 0 and med is not None and med > 0:
            return "⚠️CLV正ROI负"
        if roi_val < 0:
            return "❌负"
        if roi_val > 0:
            return "❓CLV负ROI正"
        return "⚪"
    if med is not None and med > 0:
        return "🟢CLV正待结算"
    if med is not None and med < 0:
        return "🔴负"
    return "⚪样本不足"


def build_report():
    mtx = load_mtx()
    clv = load_clv()
    roi = load_roi()

    # 汇总所有 (sport, sub_market) 组合
    keys = set(clv.keys()) | set(roi.keys())
    by_sport = defaultdict(list)
    for sp, sm in keys:
        by_sport[sp].append(sm)

    lines = ["📊 数据盘口周报（运动×盘口全表）", ""]
    lines.append("CLV=观察库+实盘库合并 | ROI=实盘已结算 | 门槛=数据驱动")
    lines.append("")

    for sport in sorted(by_sport.keys(), key=lambda s: -sum(1 for _ in by_sport[s])):
        markets = by_sport[sport]
        label = SPORT_CN.get(sport, sport)
        lines.append(f"▸ {label} {sport}")

        rows = []
        for sm in sorted(markets):
            c = clv.get((sport, sm), [])
            r = roi.get((sport, sm))
            med = statistics.median(c) if c else None
            pos = sum(1 for x in c if x > 0) / len(c) * 100 if c else None
            thr = _threshold(mtx, sport, sm)
            roi_val = r["profit"] / r["stake"] * 100 if r and r["stake"] > 0 else None
            rows.append((sm, thr, med, pos, len(c), roi_val, r["n"] if r else 0))

        # 按 CLV 样本量降序
        rows.sort(key=lambda x: -x[4])
        for sm, thr, med, pos, n, roi_val, rn in rows:
            med_s = f"{med:+.1f}%" if med is not None else "—"
            pos_s = f"{pos:.0f}%" if pos is not None else "—"
            roi_s = f"{roi_val:+.1f}%({rn}注)" if roi_val is not None else "无结算"
            concl = _concl(med, pos, roi_val)
            lines.append(f"  • {sm}: 门槛{thr:.0f}% | CLV {med_s}(正率{pos_s}) n={n} | ROI {roi_s} {concl}")
        lines.append("")

    if not by_sport:
        lines.append("（暂无数据）")
    return "\n".join(lines)


def main():
    body = build_report()
    try:
        from config.settings import send_dingtalk
        ok = send_dingtalk("数据盘口周报", body, timeout=10)
        print("✅ 已推送" if ok else "⚠️ 推送失败")
    except Exception as e:
        print(f"❌ 推送异常: {e}")
        print(body)


if __name__ == "__main__":
    main()
