#!/usr/bin/env python3
"""真实成交 ROI 校准 —— 用已结算实盘样本量化 favorite-longshot 偏差, 重算真实 edge。

背景(2026-10-27): 系统连亏, 根因是公平价锚(Pin)对中热门(40-70%隐含)系统性高估 15-19pp,
导致「正 EV」是假信号(bb 价 vs 有偏 Pin 价)。本脚本用真实赛果做校准:
  1. 按隐含概率(1/fair)分桶, 算实际胜率 vs 隐含 = favorite-longshot 偏差曲线;
  2. 用偏差曲线把 fair 校准成「校准公平价」(1/实际胜率);
  3. 重算校准后 EV = (bb_odds - 校准公平价)/校准公平价;
  4. 对比 原始EV / 校准EV / 实际ROI, 揭示真 edge 在哪。

用法: .venv312/bin/python scripts/calibrate_real_roi.py [窗口天数, 默认7]
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REAL_BETS = ROOT / "data" / "storage" / "real_bets.json"

# 概率桶(足球8桶口径, 与 odds_interval 一致)
BUCKETS = [(0, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6),
           (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]


def main():
    window_days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    d = json.loads(REAL_BETS.read_text())
    bets = d.get("bets", {})
    now = time.time()
    cutoff = now - window_days * 86400

    rows = []
    for oid, b in bets.items():
        if b.get("ts", 0) < cutoff:
            continue
        if b.get("result") is None:
            continue
        fair = b.get("fair")
        if not fair:
            continue
        try:
            ip = 1.0 / float(fair)
            profit = float(b.get("profit", 0) or 0)
            stake = float(b.get("stake", 0) or 0)
            bb = float(b.get("bb_odds", 0) or 0)
        except (ValueError, TypeError, ZeroDivisionError):
            continue
        if ip <= 0 or bb <= 1:
            continue
        rows.append({"ip": ip, "won": profit > 0.01, "profit": profit,
                     "stake": stake, "bb": bb, "fair": float(fair)})

    if not rows:
        print("无已结算样本")
        return

    # 1. 偏差曲线: 桶中点 -> 实际胜率
    calib = {}
    print(f"=== favorite-longshot 偏差曲线(近{window_days}天, n={len(rows)}) ===")
    for lo, hi in BUCKETS:
        seg = [r for r in rows if lo <= r["ip"] < hi]
        if not seg:
            continue
        wr = sum(1 for r in seg if r["won"]) / len(seg)
        calib[(lo + hi) / 2] = wr
        print("  隐含 %4.0f-%4.0f%%  实际胜率 %5.1f%%  偏差%+5.1fpp  n=%d"
              % (lo * 100, hi * 100, wr * 100, (wr - (lo + hi) / 2) * 100, len(seg)))

    mids = sorted(calib)

    def cal(ip):
        if ip <= mids[0]:
            return calib[mids[0]]
        if ip >= mids[-1]:
            return calib[mids[-1]]
        for i in range(len(mids) - 1):
            if mids[i] <= ip < mids[i + 1]:
                a, b = mids[i], mids[i + 1]
                return calib[a] + (calib[b] - calib[a]) * (ip - a) / (b - a)
        return ip

    # 2. 分桶对比 原始EV / 校准EV / 实际ROI
    print("\n=== 分桶: 原始EV vs 校准EV vs 实际ROI ===")
    for lo, hi in BUCKETS:
        seg = [r for r in rows if lo <= r["ip"] < hi]
        if not seg:
            continue
        n = len(seg)
        raw_ev = sum((r["bb"] - r["fair"]) / r["fair"] * 100 for r in seg) / n
        cal_ev = sum((r["bb"] - 1.0 / cal(r["ip"])) / (1.0 / cal(r["ip"])) * 100 for r in seg) / n
        roi = sum(r["profit"] for r in seg) / sum(r["stake"] for r in seg) * 100
        flag = " ✅真edge" if (cal_ev > 2 and roi > 0) else (" ❌假edge" if (cal_ev < -2 and roi < 0) else "")
        print("  隐含 %4.0f-%4.0f%%  原始EV%+6.1f%%  校准EV%+6.1f%%  实际ROI%+6.1f%%  n=%d%s"
              % (lo * 100, hi * 100, raw_ev, cal_ev, roi, n, flag))

    # 3. 结论: 哪些桶是真 edge
    true_edge = [(lo, hi) for lo, hi in BUCKETS
                 if sum(1 for r in rows if lo <= r["ip"] < hi) >= 20
                 and sum(r["profit"] for r in rows if lo <= r["ip"] < hi)
                 / max(1e-9, sum(r["stake"] for r in rows if lo <= r["ip"] < hi)) > 0
                 and sum(1 for r in rows if lo <= r["ip"] < hi) / 1 > 0]
    print("\n=== 结论 ===")
    if not true_edge:
        print("  无 n>=20 且实际ROI>0 的桶 → 当前锚下没有经验证的真 edge。")
    else:
        print("  实际ROI>0 且 n>=20 的桶:", [(f"{lo*100:.0f}-{hi*100:.0f}%") for lo, hi in true_edge])


if __name__ == "__main__":
    main()
