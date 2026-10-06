"""Pin 三通道客户端骨架 (未来 Pin 锚点, 2026-10 起规划)。

三通道分工(见 [[observe-settle-release-20261006]] 会话讨论):
  WS   = 赔率 tick 流(原始数据流) — 抓「价变」触发 lead-lag 套利, 高频实时
  SSE  = 赛事事件流(开赛/盘口开闭/结束) — 管生命周期 + 自动重连断点续传, 可靠性
  REST = 按需精确查询(10 req/s) — 验价 / 收盘线 / WS断线快照重同步

设计原则:
  - WS 负责「快」, SSE 负责「稳」, REST 负责「准」, 三通道别越界。
  - WS 断线重连后先用 REST 拉全量快照重建缓存, 再继续吃 tick(WS 没有 SSE 的 Last-Event-ID)。
  - 下单前必过 REST 二次验价(WS/SSE 的 tick 可能已过时)。

⚠️ 本文件是骨架: 端点 URL / 鉴权 / 消息格式 是 TODO(等 Pin API 文档)。
   纯逻辑部分(PinRateLimiter / PinOddsCache / 公平价对接)已实现可立即用。
"""
import json
import threading
import time
from pathlib import Path

from src.scrapers.pin_fair import pin_fair_price

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_FILE = ROOT / "data" / "storage" / "pin_odds_cache.json"


class PinRateLimiter:
    """令牌桶限速器 — Pin REST 10 req/s。

    用法: limiter.acquire() 会在超速时阻塞到有额度, 保证整体 ≤ rate。
    """

    def __init__(self, rate=10.0):
        self.rate = rate
        self.tokens = rate
        self.last = time.time()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.time()
            # 补充令牌(上限 rate)
            self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            # 额度不足 → 睡到有额度
            wait = (1.0 - self.tokens) / self.rate
            self._lock.release()
            time.sleep(wait)
            self._lock.acquire()
            self.tokens = 0.0
            self.last = time.time()


class PinOddsCache:
    """Pin 实时赔率缓存(跨进程共享, 落盘供早盘/滚球进程各自读)。

    结构: {event_id: {"match": {...}, "markets": {market_name: {odds, ts}}}}
    与 odds_ws_cache.json 同构思想, 但换 Pin 源。
    """

    def __init__(self, cache_file=CACHE_FILE):
        self.cache_file = cache_file
        self._cache = {}
        self._lock = threading.Lock()

    def update_tick(self, event_id, market, odds, ts):
        with self._lock:
            e = self._cache.setdefault(event_id, {"markets": {}})
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
    """Pin REST 客户端(10 req/s, 带限速)。

    TODO(等文档): 端点 URL / 鉴权 header / 请求参数 / 响应 JSON 结构。
    实现后用于: 单场精确验价 / 收盘线采集 / WS 断线快照。
    """

    def __init__(self):
        self.limiter = PinRateLimiter(rate=10.0)

    def get_odds(self, event_id, markets=None):
        """拉单场(指定盘口)Pin 赔率。→ {market: {方向: 赔率}}"""
        self.limiter.acquire()
        # TODO: requests.get(f"{BASE}/v3/odds?eventId=...&markets=...", headers=AUTH)
        raise NotImplementedError("等 Pin REST API 文档填实现")

    def get_closing_line(self, event_id, sub_market, target_line=None):
        """拉临开赛收盘线(CLV 采集用)。"""
        self.limiter.acquire()
        # TODO: 拉该场该盘口临开赛赔率
        raise NotImplementedError("等 Pin REST API 文档填实现")

    def get_snapshot(self, sport=None):
        """拉全量快照(WS 断线重连后重建缓存)。"""
        self.limiter.acquire()
        # TODO: 拉全量赔率快照
        raise NotImplementedError("等 Pin REST API 文档填实现")


class PinWSClient:
    """Pin WS 原始数据流客户端(赔率 tick 流, lead-lag 触发主力)。

    TODO(等文档): WS 端点 / 订阅命令格式 / tick 消息结构。
    流程: 连 WS → 订阅全量比赛 → 每 tick 更新 PinOddsCache → 变动即触发 BB 比价。
    断线: 重连 → REST get_snapshot 重建缓存 → 继续吃 tick。
    """

    def __init__(self, cache: PinOddsCache, rest: PinRestClient):
        self.cache = cache
        self.rest = rest
        self._stop = False

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="pin-ws")
        t.start()
        return t

    def _run(self):
        while not self._stop:
            try:
                self._connect_and_consume()
            except Exception:
                time.sleep(1.0)
            # 断线 → REST 快照重同步, 再继续
            try:
                snap = self.rest.get_snapshot()
                self.cache.load_snapshot(snap)
            except Exception:
                pass

    def _connect_and_consume(self):
        # TODO: websockets.connect(WS_URL, ...)
        #   → 发送订阅命令 {cmd:"subscribe", sport:..., markets:...}
        #   → for tick in ws: 解析 {event_id, market, odds} → cache.update_tick(...)
        #   → 触发回调 on_tick(event_id, market)(供 lead-lag 比价消费)
        raise NotImplementedError("等 Pin WS API 文档填实现")


class PinSSEClient:
    """Pin SSE 赛事事件流客户端(生命周期事件, 自动重连 + Last-Event-ID)。

    TODO(等文档): SSE 端点 / 事件类型名 / data 格式。
    职责: 开赛/中场/结束/盘口上架/盘口关闭 等状态事件。
    用 Last-Event-ID 断点续传(SSE 王牌, WS 没有)。
    """

    def __init__(self, on_event=None):
        self.on_event = on_event or (lambda ev_type, data: None)
        self._stop = False
        self._last_event_id = ""

    def start(self):
        t = threading.Thread(target=self._run, daemon=True, name="pin-sse")
        t.start()
        return t

    def _run(self):
        # TODO: 用 requests 的 stream=True 或 sseclient 连 SSE 端点,
        #   header 带 "Last-Event-ID": self._last_event_id(断点续传)。
        #   for event in stream: 解析 event/event_type/data → on_event(...)
        #   记录 self._last_event_id = event.id(供下次重连续传)
        raise NotImplementedError("等 Pin SSE API 文档填实现")


# 便捷入口: 拿到 Pin 文档后, 组装三通道
def build_pin_stack():
    """组装 Pin 三通道: cache + rest + ws + sse。"""
    cache = PinOddsCache()
    rest = PinRestClient()
    ws = PinWSClient(cache, rest)
    sse = PinSSEClient()
    return cache, rest, ws, sse
