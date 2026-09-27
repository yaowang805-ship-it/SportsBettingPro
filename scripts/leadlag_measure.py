#!/usr/bin/env python3
"""lead-lag 互相关测量：sharp(Betfair) 赔率变动 → soft(BB) 赔率变动的时间差。

目的(2026-09-27): 回答「sharp 动了到 BB 跟上，真实窗口是几秒」——之前记忆里 8.5s 是
HTTP 轮询口径(混了 2s 轮询 + 1.8s 拉取延迟), 不干净。现在:
- sharp 侧: odds_ws 文件缓存里 Betfair Exchange 各 market 的 updatedAt 变化(0.5s 轮询检测)
- soft 侧: BB G04 推送(CDP 挂 Chrome 9222 读帧, 实时)

匹配: BB matchId → (home_en,away_en) → match_event_orient → odds-api.io event_id
盘口对齐: Betfair market name → sub; G04 market(中文) → sub(同一套 1x2/hc/ou/dc/btts)
粒度: (event_id, sub) 不管方向——sharp 动了 vs soft 动了算时间差。

输出: lead-lag = t_soft - t_sharp 分布(中位/P25/P75/正占比)。正=soft 滞后 sharp。
纯测量不下单, 不影响风控。

用法: .venv312/bin/python scripts/leadlag_measure.py [--hours 1]
"""
import argparse
import asyncio
import json
import sys
import time
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.odds_ws import load_file_cache

# Betfair market name → sub（与 odds_api_io._SUB_TO_BETFAIR 反向）
SHARP_MARKET_TO_SUB = {"ML": "1x2", "Spread": "hc", "Totals": "ou",
                       "Double Chance": "dc", "Both Teams To Score": "btts"}
# G04 market(中文) → sub（与 second_level_monitor._sub_market_of 同口径）
G04_MARKET_KW = [
    ("dc", ("双重", "双胜彩")),
    ("ou", ("大/小", "大小", "进球数", "总进球")),
    ("hc", ("让球", "让分", "handicap")),
    ("1x2", ("独赢", "1x2", "胜平负", "输赢")),
    ("btts", ("双方进球", "BTTS")),
]
# 匹配窗口: sharp 变动在 soft 之前 [0, LAG_MAX] 秒内才认为对应(过滤无关变动)
LAG_MAX = 30.0


def g04_sub(market):
    mn = market or ""
    for sub, kws in G04_MARKET_KW:
        if any(k.lower() in mn.lower() for k in kws):
            return sub
    return None


def build_bb_map():
    """BB matchId → odds-api.io event_id（队名匹配，status='live' 只滚球）。"""
    from src.scrapers.pinnacle_live import fetch_bb_live_matches
    from src.scrapers.odds_api_io import match_event_orient
    bb = fetch_bb_live_matches(sport_ids=(1, 3, 5, 7, 13, 15))
    m = {}
    for mid, b in bb.items():
        eid, _ = match_event_orient(b.get("home_en", ""), b.get("away_en", ""), b.get("sport", 1), status="live")
        if eid:
            m[str(mid)] = eid
    return m


async def sharp_watcher(events, stop):
    """轮询 odds_ws 文件缓存，检测 Betfair market updatedAt 变化 → sharp 变动。"""
    last = {}  # (event_id, sub) -> updatedAt
    while not stop.is_set():
        try:
            cache = load_file_cache()
            for eid, bm in cache.items():
                bf = bm.get("Betfair Exchange")
                if not bf:
                    continue
                for m in bf:
                    sub = SHARP_MARKET_TO_SUB.get(m.get("name"))
                    if not sub:
                        continue
                    ua = m.get("updatedAt")
                    key = (eid, sub)
                    if key in last and last[key] != ua:
                        events.append(("sharp", eid, sub, time.time()))
                    last[key] = ua
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def soft_watcher(events, stop):
    """HTTP 轮询 fetch_bb_live_matches 检测 BB 赔率变化 → soft 变动。

    2026-09-27 实测: BB 的 push WS 只推「比赛信息(chId: 队名/比分)」+「联赛信息(L04)」+ 心跳，
    **不推赔率变动(G04)**——G04 推送已死(BB 前端赔率改 HTTP 轮询)。所以 soft 侧只能用
    fetch_bb_live_matches(2s 轮询)检测 BB 赔率变化, 精度受轮询周期限制(系统性偏大约 3-4s)。
    """
    from src.scrapers.pinnacle_live import fetch_bb_live_matches
    last = {}  # (matchId, sub) -> odds 值(检测变化)
    while not stop.is_set():
        try:
            bb = await asyncio.to_thread(fetch_bb_live_matches, (1, 3, 5, 7, 13, 15))
            now = time.time()
            for mid, b in bb.items():
                for mk in b.get("markets", []):
                    sub = mk.get("sub")
                    odds = mk.get("odds")
                    if not sub or not odds:
                        continue
                    key = (str(mid), sub)
                    if key in last and last[key] != odds:
                        events.append(("soft", str(mid), sub, now))
                    last[key] = odds
        except Exception:
            pass
        await asyncio.sleep(2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.0)
    args = ap.parse_args()

    events = []
    stop = asyncio.Event()
    sharp_task = asyncio.create_task(sharp_watcher(events, stop))
    soft_task = asyncio.create_task(soft_watcher(events, stop))

    bb_map = {}
    lags = []
    sharp_hist = defaultdict(deque)  # (eid, sub) -> deque of ts（最近 sharp 变动）

    print(f"[leadlag] 测量开始（跑 {args.hours}h，Ctrl+C 停止）", flush=True)
    start = time.time()
    last_map_refresh = 0.0
    while time.time() - start < args.hours * 3600:
        await asyncio.sleep(5)

        # 定期刷新 BB map（30s）
        if time.time() - last_map_refresh > 30:
            try:
                bb_map = build_bb_map()
                last_map_refresh = time.time()
            except Exception as e:
                print(f"[leadlag] BB map 刷新失败: {e}", flush=True)

        # 处理累积 events
        while events:
            ev = events.pop(0)
            if ev[0] == "sharp":
                _, eid, sub, ts = ev
                sharp_hist[(eid, sub)].append(ts)
            else:
                _, mid, sub, ts = ev
                eid = bb_map.get(mid)
                if not eid:
                    continue
                q = sharp_hist.get((eid, sub))
                if not q:
                    continue
                # 找 soft 之前 [LAG_MAX] 内最近的 sharp 变动
                recent = None
                for s in q:
                    if 0 <= ts - s <= LAG_MAX and (recent is None or s > recent):
                        recent = s
                if recent:
                    lags.append(ts - recent)

        # 清理过旧 sharp 变动（>LAG_MAX 前的，不再可能匹配）
        now = time.time()
        for k in list(sharp_hist.keys()):
            while sharp_hist[k] and sharp_hist[k][0] < now - LAG_MAX:
                sharp_hist[k].popleft()
            if not sharp_hist[k]:
                del sharp_hist[k]

        # 打印统计
        if len(lags) >= 3:
            s = sorted(lags)
            n = len(s)
            med = s[n // 2]
            p25 = s[n // 4]
            p75 = s[3 * n // 4]
            pos = sum(1 for x in lags if x > 0) / n * 100
            print(f"[leadlag] n={n} 中位={med:.1f}s P25={p25:.1f}s P75={p75:.1f}s "
                  f"正(soft滞后)占比={pos:.0f}%", flush=True)

    stop.set()
    await asyncio.gather(sharp_task, soft_task, return_exceptions=True)
    if lags:
        s = sorted(lags)
        n = len(s)
        print(f"\n[leadlag] 最终: n={n} 中位={s[n//2]:.1f}s P25={s[n//4]:.1f}s P75={s[3*n//4]:.1f}s "
              f"正占比={sum(1 for x in lags if x>0)/n*100:.0f}%", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
