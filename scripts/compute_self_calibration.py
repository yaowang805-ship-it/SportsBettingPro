#!/usr/bin/env python3
"""自有数据标定 — 用 Pin 收盘价 + 赛果 标定真实胜率, 供 V5 矩阵优先查询。

核心(用户 2026-08-25): 用我们自己的真实投注数据(Pin 收盘价 close_pin_odds + 赛果)
标定"某赔率区间的真实胜率", 替代/补充 V5 矩阵里没有外部数据覆盖的运动/联赛/盘口。

标定尺子 = Pin 收盘价(sharp), 不是 BB 价(软书, 自身有偏差)。

输出: data/storage/v5_self_calibration.json
  {"cells": {"sport|league|sub_market|bin": {"n":N, "wins":W, "win_rate":r}}, ...}
  只输出 n>=30 的格子; 不足的由 get_kelly_stake_pct 回退 V5 外部数据。
"""
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
RESULTS = DATA / "clv_results.csv"
TRACKED = DATA / "tracked_bets.json"
OUT = DATA / "v5_self_calibration.json"

# 复用 V5 的 ODDS_BINS (赔率区间, 30 桶)
ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.30, 2.50, 2.70, 2.90,
    3.10, 3.30, 3.50, 3.70, 3.90, 4.20, 4.50, 4.80,
    5.20, 5.60, 6.00, 6.50, 7.00, 7.50, 8.00, 9.00,
    10.00, 12.00, 15.00, 20.00, float('inf'),
]

N_DIRECTIONAL = 30   # 样本下限(与门槛矩阵一致)
N_CONFIRMED = 100    # 确认级


def bin_index(odds: float, bins: list) -> int:
    for i, t in enumerate(bins):
        if odds < t:
            return i
    return len(bins) - 1


def _key(sport, home_pin, away_pin, designation, sub_market, match_epoch):
    return (sport, home_pin, away_pin, designation, sub_market, str(match_epoch))


def main():
    # 1. 加载 Pin 收盘价 (clv_results push source)
    clv_map = {}
    if RESULTS.exists():
        with open(RESULTS, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r.get("source") != "push":
                    continue
                try:
                    co = float(r["close_pin_odds"])
                except (TypeError, ValueError):
                    continue
                if co <= 1.0:
                    continue
                clv_map[_key(r.get("sport"), r.get("home_pin"), r.get("away_pin"),
                            r.get("designation"), r.get("sub_market"), r.get("match_epoch"))] = co

    # 2. 加载赛果 (tracked_bets), 聚合 (sport, sub_market, bin) → [wins, total]
    #    联赛维度暂时太稀疏(4维 0 格), 先做 3 维(运动×盘口×收盘赔率桶), 联赛层待样本积累再下沉
    cells = defaultdict(lambda: [0, 0])
    if TRACKED.exists():
        tb = json.loads(TRACKED.read_text())
        for b in tb.get("bets", []):
            if b.get("result") not in ("won", "lost"):
                continue
            co = clv_map.get(_key(b.get("sport"), b.get("home_pin"), b.get("away_pin"),
                                b.get("designation"), b.get("sub_market"), b.get("match_epoch")))
            if co is None:
                continue
            cell = (b.get("sport"), b.get("sub_market"), bin_index(co, ODDS_BINS))
            cells[cell][1] += 1
            if b.get("result") == "won":
                cells[cell][0] += 1

    # 3. 输出 (n>=30 的格子才标定, 不足回退 V5 外部数据)
    out_cells = {}
    for (sport, sub, bin_i), (wins, total) in sorted(cells.items()):
        if total < N_DIRECTIONAL:
            continue
        out_cells[f"{sport}|{sub}|{bin_i}"] = {
            "n": total,
            "wins": wins,
            "win_rate": round(wins / total, 4),
            "odds_low": round(ODDS_BINS[bin_i - 1], 2) if bin_i > 0 else 0.0,
            "odds_high": round(ODDS_BINS[bin_i], 2),
            "grade": "确认" if total >= N_CONFIRMED else "方向性",
        }

    out = {
        "generated_at": datetime.now().isoformat(),
        "n_cells": len(out_cells),
        "n_samples": sum(v["n"] for v in out_cells.values()),
        "cells": out_cells,
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(OUT)

    print(f"✅ 自有标定生成 → {OUT.name}")
    print(f"  标定格子 {out['n_cells']} 个, 总样本 {out['n_samples']} 条")
    # 打印覆盖的格子(按样本量)
    for k in sorted(out_cells, key=lambda x: -out_cells[x]["n"])[:30]:
        v = out_cells[k]
        print(f"    {k:<50} n={v['n']:>4} 胜率={v['win_rate']*100:>5.1f}% 赔率{v['odds_low']}-{v['odds_high']}")


if __name__ == "__main__":
    main()
