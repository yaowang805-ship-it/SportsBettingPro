"""Pin 三通道客户端 (2026-10-11 接入真实端点)。

三通道分工:
  WS   = 全量赔率 tick 流(每帧) — 维护实时 Pin 赔率缓存, 主源
  SSE  = drop 提醒流(≥5% 骤降) — sharp 触发信号, lead-lag 命门
  REST = 按需精确查询(10 req/s) — 验价 / 收盘线 / 断线快照重同步

端点(pinnapi.com, 实测 2026-10-11):
  REST: GET /kit/v1/markets?sport_id=X&event_type=live|prematch   (独赢/让球/大小球)
        GET /kit/v1/prematch/fixtures?sport_id=X                   (赛程)
        GET /kit/v1/details / /kit/v1/prematch/lines / /api/drops / /ping
  WS:   wss://pinnapi.com/ws/feed?key=$API_KEY
        → 5s 内发 {"type":"subscribe","streams":["live","prematch"],"sport_ids":[...]}
        → 收 snapshot(全量) + live(每帧) + prematch_matchups
  SSE:  GET /odds-drop?key=$KEY (live)  /odds-drop-prematch?key=$KEY (prematch)
        → data: {"home","away","league","from_price","to_price","outcome",...}

鉴权: x-portal-apikey 或 ?key= (SSE/WS 用 ?key=)。
"""
import json
import threading
import time
from pathlib import Path

from src.scrapers.pin_fair import pin_fair_price

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_FILE = ROOT / "data" / "storage" / "pin_odds_cache.json"

try:
    from config.settings import PINNAPI_KEY, PINNAPI_BASE
except Exception:
    PINNAPI_KEY = ""
    PINNAPI_BASE = "https://pinnapi.com"

# Pin sport_id(与 pinnapi 文档一致): 1=Soccer 2=Tennis 3=Basketball 4=Hockey 5=Football 6=Baseball
# 只订阅 BB∩Betfair 有 CLV 的运动(足球/网球/篮球/冰球/棒球)
PIN_SUBSCRIBE_SPORTS = [1, 2, 3, 4, 6]


def _am_to_dec(price):
    """美国盘 → 十进制。负(-133)=1+100/133≈1.752; 正(+123)=1+123/100=2.23。"""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p < 0:
        return round(1.0 + 100.0 / abs(p), 4)
    if p > 0:
        return round(1.0 + p / 100.0, 4)
    return None


class PinRateLimiter:
    """令牌桶限速器 — Pin REST 10 req/s。"""

    def __init__(self, rate=10.0):
        self.rate = rate
        self.tokens = rate
        self.last = time.time()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.time()
            self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            wait = (1.0 - self.tokens) / self.rate
            self._lock.release()
            time.sleep(wait)
            self._lock.acquire()
            self.tokens = 0.0
            self.last = time.time()


class PinOddsCache:
    """Pin 实时赔率缓存(跨进程共享, 落盘供早盘/滚球进程各自读)。

    结构: {event_id: {"event": {...full record...}, "markets": {market: {odds, ts}}}}
    """

    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self._cache = {}
        self._lock = threading.Lock()

    def update_event(self, event_id, event):
        """存整场 Pin 记录(元数据), 从 participants 提取 home/away 归一化。"""
        with self._lock:
            e = self._cache.setdefault(event_id, {"event": {}, "markets": {}})
            # participants → home/away(归一化, 供 _pin_cache_find_event 按队名查)
            for p in (event.get("participants") or []):
                name = p.get("name")
                align = p.get("alignment")
                if name and align == "home":
                    event["home"] = name
                elif name and align == "away":
                    event["away"] = name
            e["event"] = event
            e["ts"] = time.time()

    def update_markets(self, matchup_id, markets):
        """prematch_markets 赔率 → 归一化成 periods(供 pinnacle_fair_price 读)。"""
        with self._lock:
            e = self._cache.setdefault(matchup_id, {"event": {}, "markets": {}})
            e["markets"] = markets
            ev = e["event"]
            # 覆盖 periods(元数据的 periods 是 list 无赔率, 这里换成 dict)
            p0 = {}
            ml = markets.get("ml") or {}
            spreads = markets.get("spreads") or {}
            totals = markets.get("totals") or {}
            if ml:
                p0["money_line"] = ml
            if spreads:
                p0["spreads"] = spreads
            if totals:
                p0["totals"] = totals
            ev["periods"] = {"num_0": p0}

    def update_tick(self, event_id, market, odds, ts):
        with self._lock:
            e = self._cache.setdefault(event_id, {"event": {}, "markets": {}})
            e["markets"][market] = {"odds": odds, "ts": ts}

    def get(self, event_id):
        with self._lock:
            return self._cache.get(event_id)

    def snapshot(self):
        with self._lock:
            return json.dumps({"ts": time.time(), "cache": self._cache}, ensure_ascii=False)

    def load_snapshot(self, raw):
        """WS 断线重连后, 用 REST 拉的全量快照重建缓存。"""
        with self._lock:
            d = json.loads(raw) if isinstance(raw, str) else raw
            self._cache = d.get("cache", {})

    def persist(self):
        try:
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(self.snapshot())
            tmp.replace(self.cache_file)
        except OSError:
            pass


class PinRestClient:
    """Pin REST 客户端(10 req/s, 带限速)。用 requests 直接调 /kit/v1/ 端点。"""

    def __init__(self):
        self.limiter = PinRateLimiter(rate=10.0)
        self.base = PINNAPI_BASE
        self.key = PINNAPI_KEY

    def _get(self, path, params=None):
        self.limiter.acquire()
        import requests
        r = requests.get(f"{self.base}{path}", params=params,
                         headers={"x-portal-apikey": self.key}, timeout=15)
        if r.status_code != 200:
            return None
        return r.json()

    def get_odds(self, sport_id, event_type="prematch"):
        """拉全量市场赔率(该运动所有场)。→ API 原始 dict(sport_id/events)。"""
        return self._get("/kit/v1/markets", {"sport_id": sport_id, "event_type": event_type})

    def get_closing_line(self, event_id, sub_market, target_line=None):
        """拉临开赛收盘线(CLV 采集用)。/kit/v1/markets 全量里按 event_id 找。"""
        # 收盘线 = 该事件在 prematch 里的最新赔率; 这里按需拉全量再过滤(后续可换 /kit/v1/details)
        for sport_id in PIN_SUBSCRIBE_SPORTS:
            data = self._get("/kit/v1/markets", {"sport_id": sport_id, "event_type": "prematch"})
            if not data:
                continue
            for ev in data.get("events", []):
                if str(ev.get("event_id")) == str(event_id):
                    return ev
        return None

    def get_snapshot(self, sport=None):
        """拉全量快照(WS 断线重连后重建缓存)。返回 {event_id: event}。"""
        snaps = {}
        sports = [sport] if sport else PIN_SUBSCRIBE_SPORTS
        for sid in sports:
            for et in ("prematch", "live"):
                data = self._get("/kit/v1/markets", {"sport_id": sid, "event_type": et})
                if not data:
                    continue
                for ev in data.get("events", []):
                    snaps[str(ev.get("event_id"))] = ev
        return json.dumps({"ts": time.time(), "cache": {k: {"event": v} for k, v in snaps.items()}},
                          ensure_ascii=False)


class PinWSClient:
    """Pin WS 全量赔率流(每帧) — 维护 PinOddsCache 实时缓存, 主源。

    流程: 连 wss://pinnapi.com/ws/feed?key= → 5s 内发 subscribe →
         收 snapshot(全量) → 收 live/prematch_matchups(每帧更新) → cache.update_event。
    断线: 重连 → REST get_snapshot 重建缓存 → 继续吃 tick。
    """

    def __init__(self, cache: PinOddsCache, rest: PinRestClient, on_tick=None):
        self.cache = cache
        self.rest = rest
        self.on_tick = on_tick or (lambda event_id, event: None)
        self._stop = False

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="pin-ws")
        t.start()
        return t

    def _run(self):
        import websockets
        import asyncio
        while not self._stop:
            try:
                asyncio.run(self._connect_and_consume())
            except Exception as e:
                print(f"[pin-ws] 连接异常: {type(e).__name__} {str(e)[:80]}", flush=True)
            if self._stop:
                break
            # 断线 → REST 快照重同步
            try:
                snap = self.rest.get_snapshot()
                if snap:
                    self.cache.load_snapshot(snap)
            except Exception:
                pass
            time.sleep(2.0)

    async def _connect_and_consume(self):
        import websockets
        url = f"{PINNAPI_BASE.replace('https', 'wss')}/ws/feed?key={PINNAPI_KEY}"
        async with websockets.connect(url, open_timeout=15) as ws:
            # 5s 内发 subscribe
            sub = {"type": "subscribe", "streams": ["live", "prematch"],
                   "sport_ids": PIN_SUBSCRIBE_SPORTS}
            await ws.send(json.dumps(sub))
            print(f"[pin-ws] 已连接 + 订阅 {PIN_SUBSCRIBE_SPORTS}", flush=True)
            while not self._stop:
                try:
                    raw = await ws.recv()
                except Exception:
                    break
                self._handle_frame(raw)

    def _handle_frame(self, raw):
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        t = d.get("type")
        if t in ("ping", "pong"):
            return
        if t == "snapshot":
            # 全量快照: 存 events(元数据)
            for ev in d.get("events", []):
                eid = str(ev.get("event_id") or ev.get("id") or "")
                if eid and eid != "None":
                    self.cache.update_event(eid, ev)
                    self.on_tick(eid, ev)
            return
        if t == "prematch_markets":
            # 赔率帧: matchup_id + data(moneyline/spread/total, 美国盘) → 归一化存缓存
            mid = str(d.get("matchup_id") or "")
            markets = self._parse_markets(d.get("data") or [])
            if mid and markets:
                self.cache.update_markets(mid, markets)
            return
        if t in ("live", "prematch_matchups"):
            # 每帧更新: data 是 Pinnacle payload(元数据)
            data = d.get("data") or []
            if isinstance(data, dict):
                data = [data]
            for ev in data:
                if isinstance(ev, dict):
                    eid = str(ev.get("event_id") or ev.get("id") or "")
                    if eid and eid != "None":
                        self.cache.update_event(eid, ev)
                        self.on_tick(eid, ev)

    @staticmethod
    def _parse_markets(data):
        """prematch_markets 的 data → {matchup_id: {'ml': {home,draw,away}, 'spreads': {line:...}, 'totals': {...}}}。

        美国盘 → 十进制: 负(-133) = 1+100/133; 正(+123) = 1+123/100。
        """
        out = {"ml": {}, "spreads": {}, "totals": {}}
        for m in data or []:
            mtype = m.get("type")
            prices = m.get("prices") or []
            if mtype == "moneyline":
                for p in prices:
                    dg = p.get("designation")
                    out["ml"][dg] = _am_to_dec(p.get("price"))
            elif mtype == "spread":
                line = None
                for p in prices:
                    if p.get("points") is not None:
                        line = p["points"]
                if line is not None:
                    out["spreads"][str(line)] = {
                        p.get("designation"): _am_to_dec(p.get("price")) for p in prices}
            elif mtype == "total":
                line = None
                for p in prices:
                    if p.get("points") is not None:
                        line = p["points"]
                if line is not None:
                    out["totals"][str(line)] = {
                        p.get("designation"): _am_to_dec(p.get("price")) for p in prices}
        return out


class PinSSEClient:
    """Pin SSE drop 提醒流(≥5% 骤降) — sharp 触发信号, lead-lag 命门。

    /odds-drop(live) + /odds-drop-prematch(prematch)。data: {home,away,league,from_price,to_price,outcome}。
    自动重连(简单 sleep 重连; pinnapi 侧 EventSource 有 Last-Event-ID 断点续传, 这里简化)。
    """

    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda stream, drop: None)
        self._stop = False

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="pin-sse")
        t.start()
        return t

    def _run(self):
        import requests
        while not self._stop:
            for stream, path in (("live", "/odds-drop"), ("prematch", "/odds-drop-prematch")):
                try:
                    r = requests.get(f"{PINNAPI_BASE}{path}?key={PINNAPI_KEY}",
                                     stream=True, timeout=(15, 60))
                    for line in r.iter_lines(decode_unicode=True):
                        if self._stop:
                            return
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload.startswith('{"type"'):
                            continue
                        try:
                            drops = json.loads(payload)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        for drop in drops:
                            self.on_event(stream, drop)
                except Exception as e:
                    print(f"[pin-sse] {stream} 异常: {type(e).__name__} {str(e)[:80]}", flush=True)
                if self._stop:
                    return
                time.sleep(2.0)


# 便捷入口: 组装三通道
def build_pin_stack():
    """组装 Pin 三通道: cache + rest + ws + sse。"""
    cache = PinOddsCache()
    rest = PinRestClient()
    ws = PinWSClient(cache, rest)
    sse = PinSSEClient()
    return cache, rest, ws, sse
