#!/usr/bin/env python3
"""lead-lag 互相关测量（Pin 口径）：Pin SSE drop 赔率变动 → BB 赔率变动的时间差。

目的(2026-10-13 重写): 回答「Pin 先动到 BB 跟上，真实窗口是几秒」。
旧版(09-27)用 Betfair(odds_ws updatedAt) 当 sharp 侧 + BB G04 推送当 soft 侧，G04 已死、
Betfair 已停订。现在切 Pin 后改成:
- sharp 侧: pin_drops.jsonl（Pin SSE drop，精确时间戳 ts 到毫秒）
- soft 侧: fetch_bb_live_matches（HTTP 轮询 2s，检测 BB 赔率指纹变化）

匹配: (norm_home, norm_away, sub) 队名+盘口对齐，不管方向。sub 映射:
  drop.sect Moneyline→1x2, Spread→hc, Totals→ou；BB markets sub 直接是 1x2/hc/ou/dc。
  只测 1x2/hc/ou（dc 在 Pin drop 无对应）。

输出: lead-lag = t_soft - t_sharp 分布（中位/P25/P75/正占比）。正=soft 滞后 sharp。
正占比高 + 中位几秒 = BB 赔率变动慢有真实窗口可抢。

纯测量不下单，不影响风控。
用法: .venv312/bin/python scripts/leadlag_measure.py [--hours 1]
"""
import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.odds_api_io import _norm_team

DROP_FILE = ROOT / "data" / "storage" / "pin_drops.jsonl"

# Pin drop sect → sub（对齐 BB markets 的 sub 命名）
# 实测 drop sect: Moneyline/Spread/Total/TeamTotal（注意是 Total 不是 Totals）
SECT_TO_SUB = {"Moneyline": "1x2", "Spread": "hc", "Total": "ou", "Totals": "ou"}
# 匹配窗口: sharp 变动在 soft 之前 [0, LAG_MAX] 秒内才认为对应
LAG_MAX = 30.0


def _key(home, away, sub):
    return (_norm_team(home), _norm_team(away), sub)


class SharpReader:
    """读 pin_drops.jsonl，检测新增的 Pin drop → sharp 变动。"""

    def __init__(self):
        self._pos = 0
        self._init_done = False

    def read_new(self):
        events = []
        try:
            with open(DROP_FILE) as fh:
                if not self._init_done:
                    # 首次：跳到文件末尾，只读后续新增（历史 drop 不算）
                    fh.seek(0, 2)
                    self._pos = fh.tell()
                    self._init_done = True
                    return events
                fh.seek(0, 2)
                size = fh.tell()
                if size < self._pos:
                    self._pos = 0  # 文件被轮转
                fh.seek(self._pos)
                for line in fh:
                    try:
                        d = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    sub = SECT_TO_SUB.get(d.get("sect"))
                    if not sub or not d.get("ts"):
                        continue
                    events.append((d.get("home", ""), d.get("away", ""), sub, float(d.get("ts"))))
                self._pos = fh.tell()
        except (OSError, IOError):
            pass
        return events


async def soft_watcher(events, stop):
    """轮询 fetch_bb_live_matches，检测 BB 赔率指纹变化 → soft 变动。"""
    from src.scrapers.pinnacle_live import fetch_bb_live_matches
    last = {}  # (norm_home, norm_away, sub) -> 赔率指纹
    while not stop.is_set():
        try:
            bb = await asyncio.to_thread(fetch_bb_live_matches, (1, 3, 5, 7, 13, 15))
            now = time.time()
            for mid, b in bb.items():
                home = b.get("home_en", "")
                away = b.get("away_en", "")
                if not home or not away:
                    continue
                by_sub = defaultdict(list)
                for mk in b.get("markets", []):
                    sub = mk.get("sub")
                    odds = mk.get("odds")
                    if sub in ("1x2", "hc", "ou") and odds:
                        by_sub[sub].append(odds)
                for sub, odds_list in by_sub.items():
                    fp = tuple(sorted(odds_list))
                    key = _key(home, away, sub)
                    if key in last and last[key] != fp:
                        events.append(("soft", home, away, sub, now))
                    last[key] = fp
        except Exception:
            pass
        await asyncio.sleep(2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.0)
    args = ap.parse_args()

    events = []
    stop = asyncio.Event()
    sharp_reader = SharpReader()
    soft_task = asyncio.create_task(soft_watcher(events, stop))

    lags = []
    sharp_hist = defaultdict(deque)  # (norm_home, norm_away, sub) -> deque of ts

    print(f"[leadlag] Pin 口径测量开始（跑 {args.hours}h，Ctrl+C 停止）", flush=True)
    start = time.time()
    while time.time() - start < args.hours * 3600:
        # sharp 侧: 读新 drop
        for home, away, sub, ts in sharp_reader.read_new():
            sharp_hist[_key(home, away, sub)].append(ts)

        # soft 侧: 处理累积的 BB 变动（soft_watcher 异步产出）
        while events:
            ev = events.pop(0)
            if ev[0] == "soft":
                _, home, away, sub, ts = ev
                q = sharp_hist.get(_key(home, away, sub))
                if not q:
                    continue
                recent = None
                # 2026-10-13 修: 去掉 0<= 强制, 允许 BB 先动(找最近 sharp 不管前后, lag 可正可负)。
                # 之前 0<=ts-s 只找 soft 之前的 sharp → 强制"BB 永远滞后", "100% 正"是配对假象。
                for s in q:
                    if abs(ts - s) <= LAG_MAX and (recent is None or abs(ts - s) < abs(ts - recent)):
                        recent = s
                if recent:
                    lags.append(ts - recent)

        # 清理过旧 sharp 变动（>LAG_MAX 前的不再可能匹配）
        now = time.time()
        for k in list(sharp_hist.keys()):
            while sharp_hist[k] and sharp_hist[k][0] < now - LAG_MAX:
                sharp_hist[k].popleft()
            if not sharp_hist[k]:
                del sharp_hist[k]

        if len(lags) >= 3:
            s = sorted(lags)
            n = len(s)
            med = s[n // 2]
            p25 = s[n // 4]
            p75 = s[3 * n // 4]
            pos = sum(1 for x in lags if x > 0) / n * 100
            print(f"[leadlag] n={n} 中位={med:.1f}s P25={p25:.1f}s P75={p75:.1f}s "
                  f"正(soft滞后)占比={pos:.0f}%", flush=True)

        await asyncio.sleep(1)

    stop.set()
    await asyncio.gather(soft_task, return_exceptions=True)
    if lags:
        s = sorted(lags)
        n = len(s)
        print(f"\n[leadlag] 最终: n={n} 中位={s[n//2]:.1f}s P25={s[n//4]:.1f}s "
              f"P75={s[3*n//4]:.1f}s 正占比={sum(1 for x in lags if x > 0)/n*100:.0f}%", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
