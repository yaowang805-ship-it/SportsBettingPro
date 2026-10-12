#!/usr/bin/env python3
"""CLV 诊断工具 — 16 项指标落盘(9 核心 + 3 自检 + 4 分组维度)。

背景(2026-10-12):
    clv_results.csv 历史收盘价 close_fair_price = close_pin_odds × close_total_implied
    即「乘法(proportional)去水」, 会系统性压低冷门公平赔率 → 冷门档 bb_side 虚高(假 +EV)。
    Shin/Power 去水修正 favorite-longshot 偏差(Shin 抬冷门公平赔率 7%~30%)。

本脚本回答三个问题(不拍脑门, 每格带 n + CI):
    1. 冷门溢价是不是去水方法造成的假象?  → 三种去水重跑 bb_side, 看冷门档是否塌方。
    2. 哪些格子是真溢价?                  → 每格 n / mean_clv / se / ci95 / median / p25p75 / hit_rate / skew。
    3. 门槛从哪来?                        → placebo(validate 未下单组)的 bb_side 95 分位 = 噪声上限。

用法:
    .venv312/bin/python scripts/clv_diagnostic.py                 # 全量, 每格 n>=50
    .venv312/bin/python scripts/clv_diagnostic.py --sport football
    .venv312/bin/python scripts/clv_diagnostic.py --min-n 30 --group market
    .venv312/bin/python scripts/clv_diagnostic.py --dump-raw      # 导出每行中间量到 csv 供复查

字段口径:
    bb_side(决策) = bb_odds / push_fair_price - 1   (= push_ev_pct, 静态溢价)
    bb_side(收盘) = bb_odds / close_fair_price - 1  (= true_clv_pct, 扣线移动后溢价)
    line_move     = close_fair_price / push_fair_price - 1   (>0 = 收盘价高于决策价 = 线朝你反方向走)
    恒等式(对数): ln(1+clv) = ln(1+bb_side决策) - ln(1+line_move)  → 偏差 >1pp 说明口径不一致。
"""
import argparse
import csv
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.devig import devig_shin, devig_mult
from src.scrapers.odds_interval import prob_bucket_from_fair

RESULTS = ROOT / "data" / "storage" / "clv_results.csv"

# 2-way 盘(能重建完整赔率向量 → 能算 Shin/Power); 3-way/多结果盘只能精确算 mult。
TWOWAY_SUB = {"hc", "ou", "ht_hc", "ht_ou"}
TWOWAY_SPORT = {"basketball", "baseball", "tennis", "ice_hockey",
                "american_football", "volleyball", "boxing"}


def devig_power(odds, k=1.2):
    """Power 法: fair_prob_i ∝ implied_i^k (k>1 压低冷门概率, 修正 FLB, k 为经验值)。"""
    clean = [(i, float(o)) for i, o in enumerate(odds) if o and float(o) > 1.0]
    if len(clean) < 2:
        return [0.0] * len(odds)
    w = [(1.0 / o) ** k for _, o in clean]
    s = sum(w)
    out = [0.0] * len(odds)
    for (i, _), x in zip(clean, w):
        out[i] = x / s
    return out


def is_2way(sub_market, sport):
    return sub_market in TWOWAY_SUB or (sub_market == "1x2" and sport in TWOWAY_SPORT)


def _load():
    rows = []
    with open(RESULTS) as f:
        for r in csv.DictReader(f):
            try:
                bb = float(r["bb_odds"])
                pf = float(r["push_fair_price"])
                cf = float(r["close_fair_price"])
                cp = float(r["close_pin_odds"])
                ti = float(r["close_total_implied"])
            except (TypeError, ValueError):
                continue
            if bb <= 1 or pf <= 1:
                continue  # 决策侧必须有效
            rows.append({
                "sport": r["sport"], "league": r["league"], "sub": r["sub_market"],
                "source": r["source"] or "?", "designation": r["designation"],
                "bb": bb, "pf": pf, "cf": cf, "cp": cp, "ti": ti,
                "minutes_before": float(r["minutes_before_match"] or 0),
                "ev": float(r["push_ev_pct"] or 0),
                "tclv": float(r["true_clv_pct"] or 0),
            })
    return rows


def _metrics(vals):
    """对一个格子的一维指标列表, 算 9 项核心。返回 dict。"""
    n = len(vals)
    if n == 0:
        return None
    mean = st.mean(vals)
    sd = st.stdev(vals) if n >= 2 else 0.0
    se = sd / math.sqrt(n)
    ci95 = 1.96 * se
    s = sorted(vals)
    p25 = s[int(n * 0.25)]
    p75 = s[int(n * 0.75)]
    return {
        "n": n, "mean": mean, "se": se, "ci95": ci95,
        "median": st.median(vals), "p25": p25, "p75": p75,
        "hit_rate": sum(1 for x in vals if x > 0) / n * 100,
        "skew": (mean - st.median(vals)),  # 正=右偏(尾部脏点), 负=左偏
    }


def _pass(ci95, mean):
    """CI 下限是否 > 0(只读统计显著性, 不作经济判断)。"""
    return mean - ci95 > 0


def main():
    ap = argparse.ArgumentParser(description="CLV 16 项诊断")
    ap.add_argument("--sport", default=None)
    ap.add_argument("--min-n", type=int, default=50)
    ap.add_argument("--group", choices=["all", "market", "source", "bucket", "kickoff"],
                    default="all", help="分组维度(默认 market=联赛×盘口)")
    ap.add_argument("--dump-raw", action="store_true")
    ap.add_argument("--de-vig-only", action="store_true", help="只出三种去水对比")
    args = ap.parse_args()

    rows = _load()
    if args.sport:
        rows = [r for r in rows if r["sport"] == args.sport]

    # ── 每行中间量 ──
    for r in rows:
        bb, pf, cf = r["bb"], r["pf"], r["cf"]
        r["bb_side_dec"] = (bb / pf - 1) * 100          # 决策侧 bb_side = push_ev_pct
        r["bb_side_close_stored"] = (bb / cf - 1) * 100 if cf > 1 else None  # = true_clv
        r["line_move"] = (cf / pf - 1) * 100 if cf > 1 else None
        # 收盘侧三种去水 bb_side
        r["close_mult"] = (bb / (r["cp"] * r["ti"]) - 1) * 100 if r["cp"] > 1 and r["ti"] > 0 else None
        r["close_shin"] = None
        r["close_power"] = None
        if r["cf"] > 1 and r["cp"] > 1 and r["ti"] > 0 and is_2way(r["sub"], r["sport"]):
            other_implied = r["ti"] - 1.0 / r["cp"]
            if other_implied > 0:
                raw = [r["cp"], 1.0 / other_implied]
                ps = devig_shin(raw)
                pp = devig_power(raw)
                if ps[0] > 0:
                    r["close_shin"] = (bb / (1.0 / ps[0]) - 1) * 100
                if pp[0] > 0:
                    r["close_power"] = (bb / (1.0 / pp[0]) - 1) * 100

    if args.dump_raw:
        out = "data/storage/clv_diagnostic_raw.csv"
        keys = ["sport", "league", "sub", "source", "designation", "bb", "pf", "cf",
                "cp", "ti", "bb_side_dec", "bb_side_close_stored", "line_move",
                "close_mult", "close_shin", "close_power"]
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"原始中间量已导出 {out} ({len(rows)} 行)")

    # ── 自检 1: 恒等式 ln(1+clv) = ln(1+bb_side) - ln(1+line_move) ──
    gaps = []
    for r in rows:
        if r["bb_side_close_stored"] is None or r["line_move"] is None:
            continue
        lhs = math.log(1 + r["bb_side_close_stored"] / 100)
        rhs = math.log(1 + r["bb_side_dec"] / 100) - math.log(1 + r["line_move"] / 100)
        gaps.append(abs(lhs - rhs) * 100)  # 转 pp
    if gaps:
        print(f"\n[自检A 恒等式] ln(1+clv) vs ln(1+bb_side)-ln(1+line_move) 偏差: "
              f"median={st.median(gaps):.3f}pp  p90={sorted(gaps)[int(len(gaps)*0.9)]:.3f}pp  "
              f"({'✓ 口径一致' if st.median(gaps) < 1 else '✗ 超 1pp, 口径不一致, 整批重查'})")

    # ── 三种去水对比(冷门档是否塌方) ──
    print("\n[去水对比] 收盘侧 bb_side 三种去水(冷门档塌方 = 假象):")
    print("  注意: Shin/Power 只能对 2-way 盘重建原始赔率向量; 3-way/多结果盘(正确比分/半全场等)只能精确算 mult。")
    cold = [r for r in rows if r["cf"] > 1 and (1 / r["cf"]) < 0.15 and r["close_mult"] is not None]
    hot = [r for r in rows if r["cf"] > 1 and (1 / r["cf"]) >= 0.60 and r["close_mult"] is not None]
    for label, grp in (("冷门(收盘概率<15%)", cold), ("热门(收盘概率>=60%)", hot)):
        m = [r["close_mult"] for r in grp]
        s = [r["close_shin"] for r in grp if r["close_shin"] is not None]
        p = [r["close_power"] for r in grp if r["close_power"] is not None]
        def medn(x):
            return f"{st.median(x):+.2f}%(n={len(x)})" if x else "n/a(2-way不足)"
        print(f"  {label:>20}  mult={medn(m):>18}  Shin={medn(s):>18}  Power={medn(p):>18}")

    if args.de_vig_only:
        return

    # ── 自检 2/3: placebo(validate 未下单) ──
    placebo = [r for r in rows if r["source"] == "validate"]
    bet = [r for r in rows if r["source"] == "push"]
    if placebo:
        pv = [r["bb_side_dec"] for r in placebo]
        pv_sorted = sorted(pv)
        p95 = pv_sorted[int(len(pv_sorted) * 0.95)]
        print(f"\n[自检C placebo_bb_side 95分位] validate 未下单组(n={len(pv)}) bb_side 95分位 = {p95:+.2f}%"
              f"  ← 这是唯一该出现的阈值来源(真实样本须显著高于它)")
        if bet:
            print(f"  [自检B placebo_line_move] validate 线移动均={st.mean([r['line_move'] for r in placebo if r['line_move'] is not None]):+.2f}%  "
                  f"vs push 线移动均={st.mean([r['line_move'] for r in bet if r['line_move'] is not None]):+.2f}%")

    # ── 4 分组维度, 每格带 n+CI ──
    print(f"\n[分组] 每格 n={args.min_n} 起报, CI 下限>0 标 ✓(显著), 跨零标 ~, 负显著标 ✗:")
    def _bucket(r):
        return prob_bucket_from_fair(r["pf"], r["sport"])

    def _kickoff(r):
        mb = r["minutes_before"]
        if mb <= 0:
            return "?"
        for lo, hi, lab in ((0, 30, "0-30min"), (30, 120, "30m-2h"), (120, 360, "2-6h"),
                            (360, 1440, "6-24h"), (1440, 1e9, ">24h")):
            if lo <= mb < hi:
                return lab
        return "?"

    cells = defaultdict(list)
    for r in rows:
        if r["bb_side_close_stored"] is None:
            continue  # 未收盘的不进格子(无 CLV 可读)
        if args.group == "market":
            key = (r["league"] or "?", r["sub"])
        elif args.group == "source":
            key = (r["source"],)
        elif args.group == "bucket":
            key = (_bucket(r),)
        elif args.group == "kickoff":
            key = (_kickoff(r),)
        else:  # all
            key = (r["league"] or "?", r["sub"])
        cells[key].append(r)

    print(f"  {'格子':<40} {'n':>5} {'mean_clv':>8} {'ci95':>6} {'median':>7} {'p25':>6} {'p75':>6} {'hit%':>5} {'skew':>6} 判定")
    for key in sorted(cells, key=lambda k: -len(cells[k])):
        grp = cells[key]
        if len(grp) < args.min_n:
            continue
        clv = [r["bb_side_close_stored"] for r in grp]
        m = _metrics(clv)
        if m is None:
            continue
        flag = "✓" if _pass(m["ci95"], m["mean"]) else ("✗" if (m["mean"] + m["ci95"]) < 0 else "~")
        lab = " / ".join(str(x) for x in key)
        print(f"  {lab:<40} {m['n']:>5} {m['mean']:+7.2f}% {m['ci95']:>6.2f} {m['median']:+6.2f}% "
              f"{m['p25']:+5.1f}% {m['p75']:+5.1f}% {m['hit_rate']:>4.0f}% {m['skew']:+5.2f}  {flag}")


if __name__ == "__main__":
    main()
