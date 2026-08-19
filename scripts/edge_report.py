#!/usr/bin/env python3
"""套利模式验证报告 — 按联赛/盘口/运动看 edge 在哪。

目标是回答三个问题:
    1. 比价套利这个模式到底成不成立
    2. 哪些联赛值得投
    3. 哪些盘口值得投

两个互补口径:
    CLV (领先指标)  — 有收盘价就能算, 不用等赛果, 样本积累快。
                      CLV>0 表示我们拿到的价好于最终市场共识, 是 edge 的直接证据。
    ROI (滞后指标)  — 要等赛果结算, 样本积累慢, 但它才是真金白银。

⚠️ 样本量纪律: 单元格样本 < MIN_N 一律标 "样本不足", 不给结论。
   这个系统历史上多次出现"数字异常好其实是 bug"(重复计数把 push CLV 从
   +1.88% 虚高到 +3.71%; 场次错配造出 +67% 假 edge), 所以小样本的漂亮数字
   默认当噪声, 不当发现。

用法: .venv312/bin/python scripts/edge_report.py [--min-n 10] [--by league|market|sport]
"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"

MIN_N = 10          # 低于这个样本量不给结论
GOOD_CLV = 1.0      # 中位 CLV 超过这个才算有 edge
GOOD_RATE = 55.0    # 正 CLV 率门槛


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def load_clv():
    """读 clv_results.csv, 只要真收盘价(排除归档开盘价)。"""
    p = DATA / "clv_results.csv"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("close_source") or "live").strip() == "archive_open":
                continue          # 开盘价不是收盘价, 不能算 CLV
            clv, ev = _f(r.get("true_clv_pct")), _f(r.get("push_ev_pct"))
            if clv is None or ev is None or ev < 2.0:
                continue
            out.append(r)
    return out


def load_bets():
    """读 tracked_bets.json, 只要真正定了胜负的(void/未结算不能算 ROI)。"""
    p = DATA / "tracked_bets.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text())
    except Exception:
        return []
    bets = d if isinstance(d, list) else (d.get("bets") or list(d.values()))
    return [b for b in bets if isinstance(b, dict) and str(b.get("result")) in ("won", "lost")]


def _clv_cell(rows):
    c = [_f(r.get("true_clv_pct"), 0) for r in rows]
    return {
        "n": len(c),
        "median": statistics.median(c),
        "pos_rate": sum(1 for x in c if x > 0) / len(c) * 100,
    }


def _roi_cell(bets):
    stake = sum(_f(b.get("stake"), 0) or 0 for b in bets)
    if stake <= 0:
        return None
    profit = 0.0
    for b in bets:
        s = _f(b.get("stake"), 0) or 0
        o = _f(b.get("bb_odds"), 0) or 0
        profit += s * (o - 1) if str(b.get("result")) == "won" else -s
    return {"n": len(bets), "stake": stake, "profit": profit, "roi": profit / stake * 100}


def report(dim, min_n):
    clv = load_clv()
    bets = load_bets()
    keyfn = {
        "league": lambda r: r.get("league", "?"),
        "market": lambda r: r.get("sub_market", "?"),
        "sport": lambda r: r.get("sport", "?"),
    }[dim]
    bkey = {
        "league": lambda b: b.get("league", "?"),
        "market": lambda b: b.get("sub_market", "?"),
        "sport": lambda b: b.get("sport", "?"),
    }[dim]

    g = defaultdict(list)
    for r in clv:
        g[keyfn(r)].append(r)
    gb = defaultdict(list)
    for b in bets:
        gb[bkey(b)].append(b)

    label = {"league": "联赛", "market": "盘口", "sport": "运动"}[dim]
    print("=" * 76)
    print(f"套利 edge 报告 · 按{label}")
    print(f"  CLV 样本 {len(clv)} 条 | 已定胜负投注 {len(bets)} 笔 | 样本门槛 n>={min_n}")
    print("=" * 76)
    print(f"  {label:<26}{'CLV样本':>7}{'中位CLV':>9}{'正率':>7}{'注数':>6}{'ROI':>9}  结论")
    print("  " + "-" * 72)

    rows = []
    for k in set(list(g.keys()) + list(gb.keys())):
        c = _clv_cell(g[k]) if g[k] else None
        r = _roi_cell(gb[k]) if gb[k] else None
        rows.append((k, c, r))
    rows.sort(key=lambda x: -(x[1]["n"] if x[1] else 0))

    solid = []
    for k, c, r in rows:
        n = c["n"] if c else 0
        med = f"{c['median']:+.1f}%" if c else "—"
        pos = f"{c['pos_rate']:.0f}%" if c else "—"
        bn = r["n"] if r else 0
        roi = f"{r['roi']:+.1f}%" if r else "—"
        if n < min_n:
            verdict = f"样本不足({n})"
        elif c["median"] > GOOD_CLV and c["pos_rate"] > GOOD_RATE:
            verdict = "✅ 有 edge"
            solid.append((k, c))
        elif c["median"] < -GOOD_CLV:
            verdict = "❌ 负 edge"
        else:
            verdict = "⚪ 无显著 edge"
        print(f"  {str(k)[:25]:<26}{n:>7}{med:>9}{pos:>7}{bn:>6}{roi:>9}  {verdict}")

    print()
    if solid:
        print(f"  达标{label}(中位CLV>{GOOD_CLV}% 且 正率>{GOOD_RATE}% 且 n>={min_n}):")
        for k, c in sorted(solid, key=lambda x: -x[1]["median"]):
            print(f"    {k} — 中位 {c['median']:+.1f}%, 正率 {c['pos_rate']:.0f}%, n={c['n']}")
    else:
        print(f"  ⚠️ 没有任何{label}达到判定标准 —— 要么确实没有 edge, 要么样本还不够。")
        print(f"     先看采集覆盖率(scripts/clv_stats.py), 覆盖率不达标时这张表没有意义。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--by", default="all", choices=["league", "market", "sport", "all"])
    a = ap.parse_args()
    dims = ["sport", "market", "league"] if a.by == "all" else [a.by]
    for i, d in enumerate(dims):
        if i:
            print()
        report(d, a.min_n)


if __name__ == "__main__":
    main()
