#!/usr/bin/env python3
"""实测 Betfair 赔率「瞬时抖动」持续时间分布 —— 用来科学定稳定期(PERSIST_MIN_AGE)。

背景: 稳定期(PERSIST_MIN_AGE=1.5s)的作用是过滤「瞬时抖动」——赔率变了很快又变(订单簿噪声),
不是真实市场移动。若稳定期设太短会漏过滤(假溢价下单), 设太长会拖慢端到端。

本脚本连 odds-api.io WS 收 updated(赔率变动), 统计「同一 (event_id, bookie) 连续两次
变动」的间隔分布。间隔的短尾就是瞬时抖动的持续时间: 若 <1s 的间隔占比很高, 说明稳定期
要 ≥ 那个短尾的分位数(如 P90/P95)才能滤干净。

⚠️ 注意: odds-api.io 一个 apiKey 只能 1 条连接, 本脚本会短暂顶掉 second_level_monitor
的 odds_ws 连接(跑完后 second_level_monitor 会自动重连)。所以只跑几分钟即可。

用法: .venv312/bin/python scripts/measure_price_jitter.py --minutes 5
"""
import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import ODDS_API_IO_KEY
import websockets.sync.client as wsc

WS_URL = "wss://api.odds-api.io/v3/ws"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--sport", default="football")
    args = ap.parse_args()

    url = WS_URL + "?" + urlencode({
        "apiKey": ODDS_API_IO_KEY,
        "markets": "ML,Spread,Totals",
        "sport": args.sport,
        "channels": "odds",  # 只赔率, 不 status
    })

    intervals = []          # 同一 (eid, bookie) 连续两次变动间隔
    last_ts = {}            # (eid, bookie) -> 上次 updated 时间
    n_updates = 0
    n_events = set()

    print(f"[jitter] 连 odds-api.io WS({args.sport}), 测 {args.minutes} 分钟...", flush=True)
    deadline = time.time() + args.minutes * 60
    try:
        with wsc.connect(url, open_timeout=15, ping_interval=30, ping_timeout=60) as ws:
            while time.time() < deadline:
                try:
                    raw = ws.recv(timeout=3)
                except Exception:
                    continue
                for line in raw.strip().split("\n"):
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "updated":
                        continue
                    eid = obj.get("id")
                    bookie = obj.get("bookie")
                    if not eid or not bookie:
                        continue
                    key = (eid, bookie)
                    now = time.time()
                    if key in last_ts:
                        intervals.append(now - last_ts[key])
                    last_ts[key] = now
                    n_updates += 1
                    n_events.add(eid)
    except Exception as e:
        print(f"[jitter] 连接异常: {e}", flush=True)

    if not intervals:
        print("[jitter] 没采到连续变动样本(比赛太少? 跑久一点)")
        return

    intervals.sort()
    n = len(intervals)

    def pct(p):
        return intervals[min(int(n * p), n - 1)]

    print()
    print("=== Betfair 赔率「连续变动间隔」分布(秒) ===")
    print(f"样本数: {n}  (updated {n_updates} 次, 覆盖 {len(n_events)} 个 event)")
    print(f"中位: {statistics.median(intervals):.2f}s")
    print(f"P10: {pct(0.10):.2f}s  P25: {pct(0.25):.2f}s  P50: {pct(0.50):.2f}s  "
          f"P75: {pct(0.75):.2f}s  P90: {pct(0.90):.2f}s  P95: {pct(0.95):.2f}s")
    print()
    for th in (0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0):
        print(f"间隔 < {th}s 占比: {sum(1 for x in intervals if x < th)/n*100:5.1f}%")
    print()
    print("解读: 若 <1s 占比很高(如 >30%), 说明瞬时抖动集中在 <1s, 稳定期设 ~P90 即可滤干净又不拖慢。")


if __name__ == "__main__":
    main()
