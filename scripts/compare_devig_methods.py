#!/usr/bin/env python3
"""去水法对比 — 在宽样本(全市场 Pin 比赛)上, 四种去水的 log-loss + 分桶可靠性/ECE。

目的(2026-11-17): 诊断「去水 bias」vs「选择 bias」。若 Power 去水在宽样本上 log-loss 最低、
分桶偏差收敛到 ±2pp 内 → 去水本身无偏, 观察库/实盘样本的 +5.8~12pp 偏差是选择效应(我们专挑
BB 偏离 Pin 的注)。若 Power 在宽样本上也明显偏差 → 去水方法本身错, 换方法(而非校准去水)。

输入: calibration_wide.csv(由 build_calibration_wide.py 生成, 含 y 已回填的行)。
输出: 每去水法的 n/log-loss/ECE/整体偏差 + 分桶可靠性表。

用法: .venv312/bin/python scripts/compare_devig_methods.py
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WIDE_CSV = ROOT / "data" / "storage" / "calibration_wide.csv"

METHODS = ["p_power", "p_shin", "p_prop", "p_add"]
LABELS = {"p_power": "Power", "p_shin": "Shin", "p_prop": "比例", "p_add": "加法"}
# 概率桶边界(隐含概率分位): 冷门(<0.2)/中冷(0.2-0.35)/中间(0.35-0.65)/中热(0.65-0.8)/热门(>0.8)
BUCKETS = [0.0, 0.2, 0.35, 0.65, 0.8, 1.0]
BUCKET_LABELS = ["<20%", "20-35%", "35-65%", "65-80%", ">80%"]


def load_settled():
    rows = []
    with open(WIDE_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("y") in ("", None):
                continue
            rows.append(r)
    return rows


def logloss(p, y):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def main():
    rows = load_settled()
    if not rows:
        print("无已结算样本。先跑 build_calibration_wide.py 再 --settle 回填赛果。")
        return
    print(f"宽样本已结算方向级样本: {len(rows)} 行")

    for m in METHODS:
        ps = np.array([float(r[m]) for r in rows])
        ys = np.array([float(r["y"]) for r in rows])
        n = len(ps)
        ll = np.mean([logloss(p, y) for p, y in zip(ps, ys)])
        imp = ps.mean()
        wr = ys.mean()
        bias = (imp - wr) * 100  # 隐含 - 实际, 正=高估胜率

        # 分桶可靠性 + ECE
        ece = 0.0
        bucket_lines = []
        for i in range(len(BUCKETS) - 1):
            lo, hi = BUCKETS[i], BUCKETS[i + 1]
            mask = (ps >= lo) & (ps < hi)
            if mask.sum() < 5:
                continue
            bp = ps[mask].mean()
            by = ys[mask].mean()
            w = mask.sum() / n
            ece += w * abs(bp - by)
            bucket_lines.append(f"    {BUCKET_LABELS[i]:>7} n={mask.sum():>4} 预测{bp*100:4.1f}% 实际{by*100:4.1f}% 偏差{(bp-by)*100:+.1f}pp")

        print(f"\n【{LABELS[m]}】 n={n}")
        print(f"  log-loss={ll:.4f}  ECE={ece*100:.2f}pp")
        print(f"  整体: 隐含{imp*100:.1f}% vs 实际{wr*100:.1f}% = 偏差{bias:+.1f}pp")
        for line in bucket_lines:
            print(line)

    print("\n判读: log-loss 越低越好; ECE 越低越校准; 整体偏差接近 0 且各桶偏差 ≤2pp = 去水无偏。")


if __name__ == "__main__":
    main()
