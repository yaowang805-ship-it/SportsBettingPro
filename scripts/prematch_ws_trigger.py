#!/usr/bin/env python3
"""早盘 WS 触发（2026-10-07 搭）—— 复用 odds_ws 的 WS 触发骨架，先跑观察库不投实盘。

目的: 把早盘从「管线 5-10 分钟轮询」升级到「分钟级 steam-chasing」。Betfair 早盘赔率
一变(WS 推 updated)就触发, 拉 BB 该场当前价比价, EV>=2% 入库观察库。

⚠️ 架构要点(与滚球差异):
  - 滚球: WS 触发 → 直接下单(不验价, 抢 2-3s 窗口)。
  - 早盘: WS 触发 → 先验价(分钟窗口验得起) → 入库(不投实盘)。
  - BB 拉取要限流(早盘变动频次 ~40-100/min, 全拉会 2-3 倍 BB 负载)。

当前版本: 只做「计数 + 去重 + 限流」的骨架, 实际 BB 拉取+比价+入库是 TODO(下一步接
compare_bb_vs_oa 的单场版 + log_oa_opportunities)。

用法: .venv312/bin/python scripts/prematch_ws_trigger.py [--sport football] [--duration 60]
"""
import asyncio
import json
import sys
import time
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import websockets
from config.settings import ODDS_API_IO_KEY

WS_URL = "wss://api.odds-api.io/v3/ws"
MARKETS = "ML,Spread,Totals,Double Chance,Both Teams To Score"


class RateLimiter:
    """令牌桶限流(BB 拉取次数)。默认 20 次/min, 防 BB 过载。"""

    def __init__(self, rate_per_min=20.0):
        self.rate = rate_per_min / 60.0  # 转每秒
        self.tokens = rate_per_min / 60.0
        self.last = time.time()
        self._lock = threading.Lock()

    def try_acquire(self):
        with self._lock:
            now = time.time()
            self.tokens = min(self.rate * 5, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class PrematchTrigger:
    def __init__(self, sport="football", bb_fetch_limit=20.0):
        self.sport = sport
        self.bb_limiter = RateLimiter(bb_fetch_limit)
        self.cnt = Counter()
        self.uniq_events = set()          # 去重(event_id)
        self.bb_fetched = 0               # 实际触发 BB 拉取的次数
        self.start = time.time()

    def _url(self):
        params = {"apiKey": ODDS_API_IO_KEY, "sport": self.sport,
                  "markets": MARKETS, "channels": "odds", "status": "prematch"}
        return WS_URL + "?" + urlencode(params)

    async def run(self, duration=60):
        url = self._url()
        print(f"[prematch_ws] 连 {self.sport} 早盘 WS, 跑 {duration}s ...")
        try:
            async with websockets.connect(url, max_size=2**26, open_timeout=15) as ws:
                end = time.time() + duration
                while time.time() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    for line in raw.strip().split("\n"):
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self._on_message(obj)
        except Exception as e:
            print(f"[prematch_ws] 连接异常: {type(e).__name__} {str(e)[:100]}")
        self.report()

    def _on_message(self, obj):
        t = obj.get("type", "?")
        self.cnt[t] += 1
        if t != "updated":
            return
        eid = str(obj.get("event_id") or obj.get("id") or "")
        # 去重: 同一场在短窗口内只触发一次(避免 BB 重复拉取)
        if eid in self.uniq_events:
            return
        self.uniq_events.add(eid)
        # 限流: 只有拿到额度才真正去拉 BB(TODO: 接单场比价+入库)
        if self.bb_limiter.try_acquire():
            self.bb_fetched += 1
            # TODO(下一步): 这里接 compare_bb_vs_oa 单场版 + log_oa_opportunities
            #   sig = compare_single_match(eid)  # 拉 BB 该场当前价 + Betfair 公平价比价
            #   if sig and sig.ev >= 2.0: log_oa_opportunities([sig])

    def report(self):
        dur = time.time() - self.start
        if dur <= 0:
            return
        upd = self.cnt.get("updated", 0)
        uniq = len(self.uniq_events)
        print()
        print("=" * 60)
        print(f"早盘 WS 触发频次实测 ({self.sport}, {dur:.0f}s)")
        print(f"  事件分布: {dict(self.cnt)}")
        print(f"  updated(赔率变动) 原始: {upd} 条 = {upd/dur*60:.0f} 条/min")
        print(f"  去重后唯一比赛: {uniq} 场 = {uniq/dur*60:.0f} 场/min")
        print(f"  实际触发 BB 拉取(限流后): {self.bb_fetched} 次 = {self.bb_fetched/dur*60:.0f} 次/min")
        print("=" * 60)


def main():
    sport = "football"
    duration = 60
    for a in sys.argv[1:]:
        if a.startswith("--sport"):
            sport = a.split("=", 1)[1]
        elif a.startswith("--duration"):
            duration = int(a.split("=", 1)[1])
    trig = PrematchTrigger(sport=sport, bb_fetch_limit=20.0)
    asyncio.run(trig.run(duration=duration))


if __name__ == "__main__":
    main()
