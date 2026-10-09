#!/usr/bin/env python3
"""早盘 WS 触发(2026-10-07)—— Betfair 早盘赔率一变就触发单场比价 + 入库观察库, 常驻运行。

架构: 独立进程(与滚球 second_level_monitor 分离), 单独 WS 订阅 status=prematch。
  - WS 事件只有 event_id → 用精确索引反查 BB 快照建 {event_id: BB match}。
  - 触发后: 反查 BB → _build_oa_entry + _oa_add_markets(比价) → log_oa_opportunities(入库 EV>=2%)。
  - 常驻: WS 断线重连 + 周期刷新 BB 快照(管线增量扫描更新) + 有界去重。

用法:
  手动测: .venv312/bin/python scripts/prematch_ws_trigger.py --sport football --duration 60
  常驻:   .venv312/bin/python scripts/prematch_ws_trigger.py --sport football  (launchd KeepAlive 托管)
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
BB_SNAPSHOT = ROOT / "data" / "storage" / "bb_odds_extracted.json"
BB_RELOAD_INTERVAL = 600.0  # BB 快照刷新间隔(秒), 对齐管线增量扫描
COOLDOWN = 300.0  # 同一 event_id 冷却窗口(秒): 冷却内不重复比价, 过后赔率再变动重新比(2026-10-07 取代永久去重)


class PrematchTrigger:
    def __init__(self, sport="football"):
        self.sport = sport
        self.cnt = Counter()
        self._last_compared = {}   # {event_id: 上次比价时间戳}(冷却窗口去重, 取代永久去重)
        self.bb_fetched = 0
        self.entries_logged = 0
        self.start = time.time()
        self._bb_by_event = {}
        self._bb_mtime = 0.0
        self._bb_reload_ts = 0.0
        # 输出层(入库/未来实盘分叉点): 待写队列(异步批量写文件, 不阻塞触发-比价)
        self._pending = []
        self._pending_lock = threading.Lock()
        self._stop = False
        self._reload_bb_lookup(force=True)

    def _load_bb_lookup(self):
        """加载 BB 早盘快照, 建 {event_id: BB match} 反查表(精确索引 O(1))。"""
        try:
            from src.scrapers.bb_data import load_bb_odds
            from src.scrapers.odds_api_io import _get_events_indexed, _norm_team
            matches = load_bb_odds()
        except Exception as e:
            print(f"[prematch_ws] BB 快照加载失败: {type(e).__name__} {str(e)[:80]}", flush=True)
            return {}
        by_sport = {}
        for m in matches:
            by_sport.setdefault(m.get("sport", "football"), []).append(m)
        lookup = {}
        for slug, ms in by_sport.items():
            try:
                _, idx = _get_events_indexed(slug, None)
            except Exception:
                continue
            for m in ms:
                hit = idx.get((_norm_team(m.get("home", "")), _norm_team(m.get("away", ""))))
                if hit:
                    eid, _ = hit
                    lookup[str(eid)] = m
        return lookup

    def _reload_bb_lookup(self, force=False):
        """周期刷新 BB 快照反查表(快照 mtime 变了才重载)。"""
        now = time.time()
        if not force and now - self._bb_reload_ts < BB_RELOAD_INTERVAL:
            return
        try:
            mt = BB_SNAPSHOT.stat().st_mtime
        except OSError:
            return
        if force or mt != self._bb_mtime:
            self._bb_reload_ts = now
            lookup = self._load_bb_lookup()
            self._bb_by_event = lookup
            self._bb_mtime = mt
            print(f"[prematch_ws] BB 快照刷新: 反查命中 {len(lookup)} 场", flush=True)

    def _compare(self, event_id):
        """触发-比价(统一流程, 入库和未来实盘共用): 反查 BB → 比价 → 返回 entry 或 None。"""
        m = self._bb_by_event.get(str(event_id))
        if not m:
            return None
        sport = m.get("sport", "football")
        try:
            from src.scrapers.bb_vs_pinnacle import _build_oa_entry, _oa_add_markets
            entry = _build_oa_entry(m, sport)
            _oa_add_markets(entry, m, sport)
            _grps = ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet")
            if any(entry.get(g) for g in _grps):
                return entry
        except Exception as e:
            print(f"[prematch_ws] 比价异常 {event_id}: {type(e).__name__} {str(e)[:80]}", flush=True)
        return None

    def _enqueue(self, entry):
        """输出层(入库): 加入待写队列, 后台异步批量写文件(不阻塞触发-比价)。

        未来实盘: 这里换成 _place_bet(entry)(下单), 触发-比价那段 _compare 完全不变,
        保证入库和实盘流程一致, 只是最后一步输出不同。
        """
        with self._pending_lock:
            self._pending.append(entry)

    def _flush_loop(self):
        """后台: 批量写观察库(异步, 每 60s 写一次)。

        60s 而非 5s: 入库是给 clv_collector 在赛前 1-45min 采 CLV 用, 早盘机会是 1-72h 后的比赛,
        晚写 1 分钟零影响; 且队列绝大多数时间空, 60s 醒一次几乎不耗电(省 CPU/发热)。
        代价: 崩溃最多丢 60s 内未写的 pending 机会。
        """
        while not self._stop:
            time.sleep(60)
            with self._pending_lock:
                if not self._pending:
                    continue
                batch = self._pending
                self._pending = []
            try:
                from src.monitor.clv_collector import log_oa_opportunities
                n = log_oa_opportunities(batch) or 0
                self.entries_logged += n
            except Exception as e:
                print(f"[prematch_ws] 批量入库失败: {type(e).__name__} {str(e)[:80]}", flush=True)

    def _on_message(self, obj):
        t = obj.get("type", "?")
        self.cnt[t] += 1
        if t != "updated":
            return
        eid = str(obj.get("event_id") or obj.get("id") or "")
        if not eid:
            return
        # 冷却窗口去重(2026-10-07 取代永久去重): 同一 event_id 冷却(COOLDOWN)内不重复比价,
        # 冷却过后赔率再变动(updated)就重新比价, 捕获后来才出现的 edge(永久去重会漏掉)。
        now = time.time()
        if now - self._last_compared.get(eid, 0) < COOLDOWN:
            return
        self._last_compared[eid] = now
        self.bb_fetched += 1
        entry = self._compare(eid)
        if entry:
            self._enqueue(entry)

    def _url(self):
        params = {"apiKey": ODDS_API_IO_KEY, "sport": self.sport,
                  "markets": MARKETS, "channels": "odds", "status": "prematch"}
        return WS_URL + "?" + urlencode(params)

    async def run(self, duration=60):
        """一次性跑 duration 秒(测试用)。"""
        print(f"[prematch_ws] 连 {self.sport} 早盘 WS, 跑 {duration}s ...", flush=True)
        end = time.time() + duration
        try:
            async with websockets.connect(self._url(), max_size=2**26, open_timeout=15) as ws:
                while time.time() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    self._drain(raw)
        except Exception as e:
            print(f"[prematch_ws] 连接异常: {type(e).__name__} {str(e)[:100]}", flush=True)
        self.report()

    async def run_forever(self):
        """常驻: WS 断线重连(指数退避, 防网络抖动时每5s疯狂重连烧CPU) + 周期刷新 BB 快照。"""
        print(f"[prematch_ws] 早盘 WS 触发常驻启动(sport={self.sport})", flush=True)
        # 后台写文件线程(异步, 不阻塞触发-比价)
        threading.Thread(target=self._flush_loop, daemon=True, name="prematch-flush").start()
        backoff = 1  # 重连退避(秒), 连接成功即重置
        while True:
            try:
                async with websockets.connect(self._url(), max_size=2**26, open_timeout=15) as ws:
                    print("[prematch_ws] WS 已连接", flush=True)
                    backoff = 1  # 连接成功, 重置退避
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            self._reload_bb_lookup()
                            continue
                        self._drain(raw)
            except Exception as e:
                print(f"[prematch_ws] 连接断开: {type(e).__name__} {str(e)[:80]}, {backoff}s 后重连", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)  # 指数退避 1→2→4→...→60s, 连接成功后重置

    def _drain(self, raw):
        for line in raw.strip().split("\n"):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._on_message(obj)

    def report(self):
        dur = time.time() - self.start
        if dur <= 0:
            return
        upd = self.cnt.get("updated", 0)
        uniq = len(self._uniq_set)
        print()
        print("=" * 62)
        print(f"早盘 WS 触发实测 ({self.sport}, {dur:.0f}s)")
        print(f"  事件分布: {dict(self.cnt)}")
        print(f"  updated 原始: {upd} 条 = {upd/dur*60:.0f} 条/min")
        print(f"  去重后唯一比赛: {uniq} 场 = {uniq/dur*60:.0f} 场/min")
        print(f"  比价次数: {self.bb_fetched} 次 = {self.bb_fetched/dur*60:.0f} 次/min")
        print(f"  入库观察库: {self.entries_logged} 条机会")
        print("=" * 62)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="早盘 WS 触发(单场比价+入库观察库, 常驻)")
    ap.add_argument("--sport", default="football", help="运动 slug, 逗号分隔可多个")
    ap.add_argument("--duration", type=int, default=0, help=">0 只跑 N 秒(测试), 0=常驻")
    ap.add_argument("--bb-limit", type=float, default=20.0, help="已废弃(2026-10-07 取消限流, 参数保留兼容 launchd)")
    a = ap.parse_args()
    trig = PrematchTrigger(sport=a.sport)
    if a.duration > 0:
        asyncio.run(trig.run(duration=a.duration))
    else:
        asyncio.run(trig.run_forever())


if __name__ == "__main__":
    main()
