"""odds-api.io WebSocket 实时赔率订阅 — 替代 2s REST 轮询的实时推送(lead-lag 套利命门)。

端点: wss://api.odds-api.io/v3/ws?apiKey=...&markets=ML,Spread,Totals&sport=football&status=live
- 一个 apiKey 只能 1 条连接(新连接自动顶掉旧连接)。
- 一帧可能含多条 JSON(按 \\n 分隔)。
- 每条消息带全局递增 seq, 断线带 lastSeq 重连可补发漏消息(server 保留 24h)。
- resync_required = 服务端无法续传, 需清缓存重来(下次消息重建)。

消息类型: welcome / created(新场) / updated(盘口变动) / deleted(场次移除) / no_markets / resync_required。
缓存结构 {event_id: {bookmaker: [markets]}} 与 odds_api_io.get_odds 返回同构, get_odds 直接读。
"""
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from config.settings import ODDS_API_IO_KEY

WS_URL = "wss://api.odds-api.io/v3/ws"

# 落盘文件: 供跨进程共享(早盘 bb_vs_pinnacle 是独立进程, 读不到滚球进程的内存缓存)
CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "storage" / "odds_ws_cache.json"
_PERSIST_INTERVAL = 5.0  # 落盘间隔(秒)

# 实时缓存: {event_id: {bookmaker: [markets]}} — 与 get_odds 返回同构
_ws_odds_cache = {}
_ws_odds_ts = {}   # {event_id: 最近更新 ts}
# 收盘价缓存: {event_id: {bookmaker: [markets]}} — 开赛(→live)那一刻的赔率快照, 供早盘 CLV
_closing_cache = {}
_closing_ts = {}
_lock = threading.Lock()
_change_queue = []  # 变动事件队列 [(event_id, bookie, ts)], 供 WS 触发实时比价消费者轮询(2026-09-19)


def get_recent_changes():
    """取并清空变动事件队列(WS 触发实时比价消费者用)。返回 [(event_id, bookie, ts)]。"""
    with _lock:
        out = list(_change_queue)
        _change_queue.clear()
        return out


def _merge_markets(old_markets, new_markets):
    """按 name 合并: 同名替换, 新名追加。WS updated 可能只推变化的盘口(不推全量)。"""
    out = list(old_markets or [])
    names = {m.get("name") for m in out}
    for m in new_markets:
        name = m.get("name")
        if name in names:
            out = [x for x in out if x.get("name") != name]
        out.append(m)
        names.add(name)
    return out


def _on_message(obj):
    """处理一条 WS JSON 消息, 更新缓存。"""
    t = obj.get("type")
    if t in ("created", "updated"):
        eid = obj.get("id")
        bookie = obj.get("bookie", "")
        markets = obj.get("markets") or []
        if eid is None or not bookie:
            return
        try:
            eid = int(eid)
        except (TypeError, ValueError):
            return
        with _lock:
            bm = _ws_odds_cache.setdefault(eid, {})
            bm[bookie] = _merge_markets(bm.get(bookie), markets)
            _ws_odds_ts[eid] = time.time()
            if t == "updated":  # 2026-09-19: 赔率变动入队, 供 WS 触发实时比价
                _change_queue.append((eid, bookie, time.time()))
    elif t == "deleted":
        eid = obj.get("id")
        if eid is None:
            return
        try:
            eid = int(eid)
        except (TypeError, ValueError):
            return
        with _lock:
            _ws_odds_cache.pop(eid, None)
            _ws_odds_ts.pop(eid, None)
    elif t == "status":
        # 事件状态变化(pending→live→settled): 转 live 那一刻把当前 odds 存成「收盘价」(供早盘 CLV)
        eid = obj.get("id")
        st = obj.get("status")
        if eid is None or st not in ("live", "settled"):
            return
        try:
            eid = int(eid)
        except (TypeError, ValueError):
            return
        with _lock:
            if eid in _ws_odds_cache:
                _closing_cache[eid] = {k: list(v) for k, v in _ws_odds_cache[eid].items()}
                _closing_ts[eid] = time.time()


def snapshot(event_id=None):
    """读实时缓存。event_id=None 返回全量 {eid:{bookie:markets}}, 否则返回 {bookie:markets}。"""
    with _lock:
        if event_id is None:
            return {k: {bk: list(mk) for bk, mk in v.items()} for k, v in _ws_odds_cache.items()}
        return {k: list(v) for k, v in (_ws_odds_cache.get(event_id) or {}).items()}


def closing_snapshot(event_id=None):
    """读收盘价缓存(开赛那一刻的赔率快照)。event_id=None 返回全量, 否则返回 {bookie:markets}。"""
    with _lock:
        if event_id is None:
            return {k: {bk: list(mk) for bk, mk in v.items()} for k, v in _closing_cache.items()}
        return {k: list(v) for k, v in (_closing_cache.get(event_id) or {}).items()}


def closing_fair_price(event_id, sub_market):
    """从收盘价快照算某盘口的 Betfair 公平价(中间价)。复用 odds_api_io 的公平价提取。"""
    from src.scrapers.odds_api_io import _SUB_TO_BETFAIR, _fair_three_way, mid_price, _select_line
    closing = closing_snapshot(event_id)
    bf = closing.get("Betfair Exchange")
    if not bf:
        return None
    market_name = _SUB_TO_BETFAIR.get(sub_market)
    if not market_name:
        return None
    m = next((x for x in bf if x.get("name") == market_name), None)
    if not m:
        return None
    o = _select_line(m, None)  # 收盘价, 取主线(最平衡那条)
    if not o:
        return None
    if sub_market in ("1x2", "ht"):
        return _fair_three_way(o)
    if sub_market in ("ou", "ht_ou"):
        line = o.get("hdp")
        over = mid_price(o.get("over"), o.get("layOver"))
        under = mid_price(o.get("under"), o.get("layUnder"))
        if line is None or not over or not under:
            return None
        return {"over": over, "under": under, "line": line}
    if sub_market == "hc":
        line = o.get("hdp")
        home = mid_price(o.get("home"), o.get("layHome"))
        away = mid_price(o.get("away"), o.get("layAway"))
        if line is None or not home or not away:
            return None
        return {"home": home, "away": away, "line": line}
    return None


_file_cache_ts = 0.0
_file_cache_data = {}


def load_file_cache():
    """从落盘文件读 WS 缓存(供其他进程: 早盘 bb_vs_pinnacle)。返回 {event_id:{bookie:markets}} 或 {}。

    带 2s TTL 缓存: 早盘对比每轮 get_odds 几百次, 不能每次都 parse 几 MB 的 JSON 文件。
    """
    global _file_cache_ts, _file_cache_data
    now = time.time()
    if now - _file_cache_ts < 2.0:
        return _file_cache_data
    try:
        if CACHE_FILE.exists():
            d = json.loads(CACHE_FILE.read_text())
            cache = d.get("cache", {}) if isinstance(d, dict) else {}
            # JSON key 变字符串, 转回 int
            _file_cache_data = {int(k): v for k, v in cache.items() if str(k).isdigit()}
        else:
            _file_cache_data = {}
    except (json.JSONDecodeError, OSError, ValueError):
        _file_cache_data = {}
    _file_cache_ts = now
    return _file_cache_data


_file_closing_ts = 0.0
_file_closing_data = {}


def load_closing_cache():
    """从落盘文件读收盘价缓存(供其他进程: clv_collector 早盘 CLV)。带 2s TTL。"""
    global _file_closing_ts, _file_closing_data
    now = time.time()
    if now - _file_closing_ts < 2.0:
        return _file_closing_data
    try:
        if CACHE_FILE.exists():
            d = json.loads(CACHE_FILE.read_text())
            closing = d.get("closing", {}) if isinstance(d, dict) else {}
            _file_closing_data = {int(k): v for k, v in closing.items() if str(k).isdigit()}
        else:
            _file_closing_data = {}
    except (json.JSONDecodeError, OSError, ValueError):
        _file_closing_data = {}
    _file_closing_ts = now
    return _file_closing_data


def _persist():
    """把内存缓存落盘(跨进程共享)。原子写: tmp → rename。"""
    try:
        with _lock:
            data = {"ts": time.time(), "cache": _ws_odds_cache, "closing": _closing_cache}
        _tmp = CACHE_FILE.with_suffix(".tmp")
        _tmp.write_text(json.dumps(data, ensure_ascii=False))
        _tmp.replace(CACHE_FILE)
    except OSError:
        pass


class OddsWSClient:
    """后台线程连 WebSocket, 更新 odds_ws 模块级缓存。get_odds 直接读该缓存。"""

    def __init__(self, sport="football", status=None,
                 markets=("ML", "Spread", "Totals"), channels="odds,status"):
        # status=None 表示不限状态(live+prematch 一起推); 'live'/'prematch' 只推一种。
        # channels=odds,status: 订阅赔率 + 状态变化(状态变化时抓「收盘价」供早盘 CLV)
        self._params = {
            "apiKey": ODDS_API_IO_KEY,
            "markets": ",".join(markets),
            "sport": sport,
            "channels": channels,
        }
        if status:
            self._params["status"] = status
        self._last_seq = None
        self._stop = False

    def start(self):
        """启动后台线程(收消息 + 定期落盘)。返回收消息的 Thread。"""
        t = threading.Thread(target=self._run, daemon=True, name="odds-ws")
        t.start()
        p = threading.Thread(target=self._persist_loop, daemon=True, name="odds-ws-persist")
        p.start()
        return t

    def _persist_loop(self):
        while not self._stop:
            time.sleep(_PERSIST_INTERVAL)
            _persist()

    def stop(self):
        self._stop = True

    def _run(self):
        from websockets.sync.client import connect
        backoff = 1.0
        while not self._stop:
            try:
                params = dict(self._params)
                if self._last_seq is not None:
                    params["lastSeq"] = self._last_seq
                url = f"{WS_URL}?{urlencode(params)}"
                with connect(url, open_timeout=15, close_timeout=5) as ws:
                    backoff = 1.0  # 连上后重置退避
                    for raw in ws:
                        if self._stop:
                            return
                        for line in raw.strip().split("\n"):
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            seq = obj.get("seq")
                            if seq is not None:
                                self._last_seq = seq
                            if obj.get("type") == "resync_required":
                                # 服务端无法续传 → 清缓存, 靠后续消息重建(再兜底 REST 快照)
                                with _lock:
                                    _ws_odds_cache.clear()
                                    _ws_odds_ts.clear()
                                continue
                            _on_message(obj)
            except Exception:
                # 断线重连: 指数退避(1→2→4→...→30s 封顶)
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))
    client = OddsWSClient()
    client.start()
    # 自测: 连 20s, 打印缓存增长
    t0 = time.time()
    while time.time() - t0 < 20:
        snap = snapshot()
        print(f"\r[odds_ws] 缓存 {len(snap)} 场, 命中事件数 {len(snap)}", end="")
        time.sleep(2)
    print()
    # 打印 2 场样例
    for i, (eid, bm) in enumerate(snapshot().items()):
        if i >= 2:
            break
        print(f"  event {eid}: {list(bm.keys())}")
