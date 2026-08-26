#!/usr/bin/env python3
"""CLV 格子覆盖进度 — 「什么时候能把门槛精确到运动×联赛×盘口」的进度条。

背景:
    门槛矩阵目前只能做到「盘口层 + 运动微调 + 联赛微调(n≥30)」, 三维格
    (运动×联赛×盘口)绝大多数样本不足。要往下沉必须先攒够数据。本脚本把
    「还差多少、按当前速率还要几天」算成数字, 免得靠感觉判断何时解锁。

样本纪律(与 compute_ev_thresholds.py 完全一致, 直接复用其过滤器避免口径漂移):
    n≥100 确认 / 30≤n<100 方向性 / n<30 保守回退。

两个库分开看:
    validate = 观察库(所有≥2%EV机会, 不受推送门槛影响, 积累最快, 决定何时能解锁细分格)
    push     = 真实投注库(实际下注的样本, 口径最真, 决定门槛最终能否切到"只用真实投注")

⚠️ 一个诚实的限制: 格子样本够 ≠ 该格门槛可算。门槛是在 EV 分档上找"负转正"边界,
   需要**目标档内**有 ≥30 条(如 EV≥5% 档)。所以本报告同时给 n_all 和 n_ev5。

用法:
    .venv312/bin/python scripts/clv_cell_coverage.py            # 打印 + 写 json
    .venv312/bin/python scripts/clv_cell_coverage.py --top 40   # 多列几行
"""
import argparse
import collections
import csv
import importlib.util
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
RESULTS = DATA / "clv_results.csv"
OUT = DATA / "clv_cell_coverage.json"

# 复用门槛脚本的过滤器/常量 —— 两边口径必须一致, 否则"进度条"和"实际解锁"对不上
_spec = importlib.util.spec_from_file_location(
    "compute_ev_thresholds", Path(__file__).resolve().parent / "compute_ev_thresholds.py")
_cet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cet)

N_DIRECTIONAL = _cet.N_DIRECTIONAL     # 30
N_CONFIRMED = _cet.N_CONFIRMED         # 100
RATE_WINDOW_DAYS = 7                   # 用最近 7 天的产出速率外推(比全期均值更贴近现状)
EV_BAND = 5.0                          # 门槛搜索常落在 5% 档, 单独统计该档样本量


def load_rows():
    """带 (盘口, 运动, 联赛, clv, ev, source, 日期) 的干净样本。过滤器与门槛脚本一致。"""
    if not RESULTS.exists():
        return []
    out = []
    with open(RESULTS, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if (r.get("close_source") or "live").strip() == "archive_open":
                continue
            clv = _cet.f(r.get("true_clv_pct"))
            ev = _cet.f(r.get("push_ev_pct"))
            od = _cet.f(r.get("bb_odds"))
            if clv is None or ev is None or ev < 2:
                continue
            if od and ev > max(12.0, (od - 1) * 20):
                continue
            if od and od > _cet.MAX_ODDS:
                continue
            if _cet.BANNED_LEAGUE_RE.search(r.get("league") or ""):
                continue
            day = (r.get("push_time") or r.get("collect_time") or "")[:10]
            out.append({
                "market": r.get("sub_market", "?"), "sport": r.get("sport", "?"),
                "league": r.get("league", ""), "clv": clv, "ev": ev,
                "source": (r.get("source") or "push").strip() or "push", "day": day,
            })
    return out


def _grade(n):
    if n >= N_CONFIRMED:
        return "确认"
    if n >= N_DIRECTIONAL:
        return "方向性"
    return "不足"


def summarize(rows, keyfn, recent_days):
    """按 keyfn 分组, 算 n / 近期速率 / 解锁 ETA。"""
    cells = collections.defaultdict(lambda: {"n": 0, "n_push": 0, "n_ev5": 0,
                                             "recent": 0, "clvs": []})
    for r in rows:
        c = cells[keyfn(r)]
        c["n"] += 1
        c["clvs"].append(r["clv"])
        if r["source"] == "push":
            c["n_push"] += 1
        if r["ev"] >= EV_BAND:
            c["n_ev5"] += 1
        if r["day"] in recent_days:
            c["recent"] += 1
    out = {}
    for k, c in cells.items():
        rate = c["recent"] / RATE_WINDOW_DAYS
        need30 = max(0, N_DIRECTIONAL - c["n"])
        need100 = max(0, N_CONFIRMED - c["n"])
        out[k] = {
            "n": c["n"], "n_push": c["n_push"], "n_ev5": c["n_ev5"],
            "rate_per_day": round(rate, 2),
            "median_clv": round(statistics.median(c["clvs"]), 2),
            "pos_rate": round(sum(1 for x in c["clvs"] if x > 0) / c["n"] * 100),
            "grade": _grade(c["n"]),
            "eta_30_days": None if need30 == 0 else (round(need30 / rate, 1) if rate > 0 else None),
            "eta_100_days": None if need100 == 0 else (round(need100 / rate, 1) if rate > 0 else None),
        }
    return out


def _fmt_eta(d):
    if d is None:
        return "—"
    if d > 365:
        return ">1年"
    return f"{d:.0f}天"


def main():
    ap = argparse.ArgumentParser(description="CLV 格子覆盖进度")
    ap.add_argument("--top", type=int, default=20, help="每层最多列几行")
    args = ap.parse_args()

    rows = load_rows()
    if not rows:
        print("无干净样本")
        return

    today = datetime.now().date()
    recent_days = {str(today - timedelta(days=i)) for i in range(RATE_WINDOW_DAYS)}
    daily = collections.Counter(r["day"] for r in rows)
    rate_total = sum(v for d, v in daily.items() if d in recent_days) / RATE_WINDOW_DAYS

    levels = [
        ("盘口层", lambda r: r["market"]),
        ("运动×盘口", lambda r: f"{r['sport']}|{r['market']}"),
        ("运动×联赛", lambda r: f"{r['sport']}|{r['league']}"),
        ("运动×联赛×盘口", lambda r: f"{r['sport']}|{r['league']}|{r['market']}"),
    ]

    n_push = sum(1 for r in rows if r["source"] == "push")
    print(f"CLV 格子覆盖进度  样本 {len(rows)} 条 "
          f"(观察库 validate {len(rows)-n_push} / 真实投注库 push {n_push}), "
          f"近 {RATE_WINDOW_DAYS} 天速率 {rate_total:.0f} 条/天")
    print(f"判定: n≥{N_CONFIRMED} 确认 / {N_DIRECTIONAL}-{N_CONFIRMED} 方向性 / <{N_DIRECTIONAL} 保守回退")
    print("⚠️ ETA 假设该格按近 7 天速率持续产出 —— 赛季结束/联赛轮空会让实际更慢, "
          f"且门槛搜索真正吃的是 EV≥{EV_BAND:.0f}% 档样本(见括号内), 不是总数\n")

    report = {"generated_at": datetime.now().isoformat(), "n_samples": len(rows),
              "n_push": n_push, "rate_per_day": round(rate_total, 1), "levels": {}}

    for label, keyfn in levels:
        cells = summarize(rows, keyfn, recent_days)
        ge30 = [k for k, v in cells.items() if v["n"] >= N_DIRECTIONAL]
        ge100 = [k for k, v in cells.items() if v["n"] >= N_CONFIRMED]
        # 还没解锁但有产出的格子: 按 ETA 排序, 最快解锁的排前面
        pending = sorted(((k, v) for k, v in cells.items()
                          if v["n"] < N_DIRECTIONAL and v["eta_30_days"] is not None),
                         key=lambda kv: kv[1]["eta_30_days"])
        stalled = [k for k, v in cells.items()
                   if v["n"] < N_DIRECTIONAL and v["eta_30_days"] is None]
        print(f"── {label}: {len(cells)} 格 | 确认 {len(ge100)} / 方向性 {len(ge30)-len(ge100)} "
              f"/ 不足 {len(cells)-len(ge30)}(其中 {len(stalled)} 格近7天零产出)")
        for k, v in sorted(cells.items(), key=lambda kv: -kv[1]["n"])[:args.top]:
            print(f"   {k:<52} n={v['n']:>4} (真投{v['n_push']:>3}, EV≥{EV_BAND:.0f}%档{v['n_ev5']:>3}) "
                  f"{v['grade']:<3} 中位{v['median_clv']:+6.2f}% 正率{v['pos_rate']:>3}% "
                  f"速率{v['rate_per_day']:>5.1f}/天 →100条 {_fmt_eta(v['eta_100_days'])}")
        if pending:
            nxt = ", ".join(f"{k}({_fmt_eta(v['eta_30_days'])})" for k, v in pending[:5])
            print(f"   下一批解锁(→30条): {nxt}")
        print()
        report["levels"][label] = {
            "cells": len(cells), "confirmed": len(ge100),
            "directional": len(ge30) - len(ge100), "insufficient": len(cells) - len(ge30),
            "stalled": len(stalled), "detail": cells,
        }

    # 真实投注库单独进度: 门槛何时能从 validate+push 切到"只用真实投注"
    push_by_market = collections.Counter(r["market"] for r in rows if r["source"] == "push")
    push_recent = sum(1 for r in rows if r["source"] == "push" and r["day"] in recent_days)
    ready = [m for m, n in push_by_market.items() if n >= N_DIRECTIONAL]
    print(f"── 真实投注库(push)口径进度: {n_push} 条, 近{RATE_WINDOW_DAYS}天 "
          f"{push_recent/RATE_WINDOW_DAYS:.0f} 条/天")
    print(f"   已达 n≥{N_DIRECTIONAL} 的盘口: {', '.join(ready) if ready else '无'}")
    print("   " + ", ".join(f"{m}={n}" for m, n in push_by_market.most_common(10)))
    report["push_by_market"] = dict(push_by_market)
    report["push_ready_markets"] = ready

    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    tmp.replace(OUT)
    print(f"\n→ {OUT.name}")


if __name__ == "__main__":
    main()
