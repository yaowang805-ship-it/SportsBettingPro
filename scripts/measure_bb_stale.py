#!/usr/bin/env python3
"""实测 BB 预取缓存(2s)的新鲜度 —— 2s 内 BB 赔率变化的概率(stale 率)。

背景: 讨论「去掉现拉 BB」时, 用户认为「每 2s 拉一次 BB 价已非常新鲜」, 我担心
「BB 赔率 4.1s 更新, 2s 缓存约一半概率 stale」。本脚本直接实测: 连续拉 getList
(模拟 2s 预取), 对比相邻两次每场比赛的 BB 赔率 signature, 统计「变了」的占比。

若 stale 率低(<10%) → 2s 缓存够新鲜, 去掉现拉成立;
若 stale 率高(>30%) → 去掉现拉会大量用假 edge 下单, 不宜去掉。

用法: .venv312/bin/python scripts/measure_bb_stale.py --rounds 20 --interval 2
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.pinnacle_live import fetch_bb_live_matches


def bb_signature(bb):
    """每场比赛的 BB 赔率 signature: 所有 markets 的 (sub, direction, line, odds)。"""
    sig = {}
    for mid, b in bb.items():
        markets = []
        for mk in b.get("markets", []):
            markets.append((mk.get("sub"), mk.get("direction"),
                            mk.get("line"), round(mk.get("odds", 0), 3)))
        sig[mid] = tuple(sorted(markets, key=lambda x: (str(x[0]), str(x[1]), str(x[2]), x[3])))
    return sig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    sport_ids = (1, 3, 5, 7, 13, 15)
    total_pairs = 0      # 相邻两次都有的比赛对数
    changed_pairs = 0    # 2s 内变了 的比赛对数

    print(f"[stale] 测 {args.rounds} 轮, 每轮间隔 {args.interval}s ...", flush=True)

    prev_sig = None
    for r in range(args.rounds):
        t0 = time.time()
        try:
            bb = fetch_bb_live_matches(sport_ids=sport_ids)
        except Exception as e:
            print(f"  第{r+1}轮拉取失败: {e}", flush=True)
            time.sleep(args.interval)
            continue
        sig = bb_signature(bb)
        if prev_sig is not None:
            common = set(prev_sig) & set(sig)
            changed = sum(1 for mid in common if prev_sig[mid] != sig[mid])
            total_pairs += len(common)
            changed_pairs += changed
            if r <= 5 or r % 5 == 0:
                print(f"  第{r+1}轮: {len(common)} 场共同, {changed} 场在 {args.interval}s 内变了 "
                      f"({changed/len(common)*100:.0f}%)", flush=True)
        prev_sig = sig
        # 等 interval（扣除拉取耗时）
        elapsed = time.time() - t0
        if elapsed < args.interval:
            time.sleep(args.interval - elapsed)

    print()
    print("=== BB 赔率 2s 内 stale 率 ===")
    if total_pairs:
        rate = changed_pairs / total_pairs * 100
        print(f"累计 {total_pairs} 场比赛对, {changed_pairs} 场在 {args.interval}s 内变了")
        print(f"stale 率: {rate:.1f}%")
        print()
        print("解读: 若 stale 率低(<10%), 2s 预取缓存够新鲜, 去掉现拉成立;")
        print("      若 stale 率高(>30%), 去掉现拉会大量用假 edge 下单, 不宜去掉。")
    else:
        print("没采到共同比赛对(比赛太少?)")


if __name__ == "__main__":
    main()
