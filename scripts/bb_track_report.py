#!/usr/bin/env python3
"""BB 价轨迹追踪报告: 读 bb_price_track.jsonl, 汇总 lead-lag 窗口是否真的拿到。

判据: 下注后 BB 价是否追上 Pin(校准公平价 fair_cal)。追上 = 拿到 lead-lag 窗口。
输出: 总笔数 / 追价率 / 平均追价秒数 / BB 价变动幅度分布。

用法: python3 scripts/bb_track_report.py
"""
import json
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACK = ROOT / "data" / "storage" / "bb_price_track.jsonl"


def main():
    if not TRACK.exists():
        print("bb_price_track.jsonl 尚无数据(等第一笔追踪完成: 下单后 BB 追上 Pin 或 10min 超时)")
        return
    rows = []
    for line in TRACK.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    if not rows:
        print("无追踪记录")
        return

    caught = 0
    catchup_secs = []
    bb_moves = []  # BB 价变动幅度(相对下注时)
    for r in rows:
        bb0 = float(r.get("bb_at_bet") or 0)
        fair_cal = float(r.get("fair_cal_at_bet") or 0)
        final_bb = r.get("final_bb")
        if final_bb is None:
            continue
        final_bb = float(final_bb)
        # 是否追上 Pin(校准公平价)
        if final_bb <= fair_cal:
            caught += 1
            if r.get("catchup_sec") is not None:
                catchup_secs.append(float(r["catchup_sec"]))
        # BB 价变动(负=朝 Pin 方向跌, 正=反向)
        if bb0 > 1:
            bb_moves.append((final_bb - bb0) / bb0 * 100)

    n = len(rows)
    print(f"=== BB 价轨迹追踪汇总 ({n} 笔已结算轨迹) ===")
    if n == 0:
        return
    print(f"追价率(BB 追上 Pin): {caught}/{n} = {caught/n*100:.0f}%")
    if catchup_secs:
        import statistics
        print(f"平均追价耗时: {statistics.mean(catchup_secs):.0f}s (中位 {statistics.median(catchup_secs):.0f}s, 最大 {max(catchup_secs):.0f}s)")
    if bb_moves:
        import statistics
        print(f"BB 价变动幅度: 均值 {statistics.mean(bb_moves):+.1f}% (负=朝 Pin 跌=追价, 正=反向)")
        pos = sum(1 for m in bb_moves if m < 0)
        print(f"BB 朝 Pin 方向移动: {pos}/{len(bb_moves)} = {pos/len(bb_moves)*100:.0f}%")

    # 按盘口分
    print("\n=== 按盘口追价率 ===")
    by_sub = {}
    for r in rows:
        sub = r.get("sub", "?")
        by_sub.setdefault(sub, [0, 0])  # [caught, total]
        bb0 = float(r.get("bb_at_bet") or 0)
        fair_cal = float(r.get("fair_cal_at_bet") or 0)
        final_bb = r.get("final_bb")
        if final_bb is not None and float(final_bb) <= fair_cal:
            by_sub[sub][0] += 1
        by_sub[sub][1] += 1
    for sub, (c, t) in sorted(by_sub.items(), key=lambda x: -x[1][1]):
        print(f"  {sub:12s} {c}/{t} = {c/t*100:.0f}%")


if __name__ == "__main__":
    main()
