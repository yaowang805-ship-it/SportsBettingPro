"""Pin 三通道常驻进程 (2026-10-11): WS 全量赔率流 + SSE drop 触发, 维护实时 Pin 赔率缓存。

职责:
  WS  = 全量赔率流 → 维护 pin_odds_cache.json(实时公平价源), 每 10s 落盘
  SSE = drop 提醒(≥5% 骤降) → drop → 映射 BB 比赛 → 写触发信号(供比价进程消费)

drop 是 lead-lag 命门: Pin 赔率骤降 = sharp money 进来, 要抢在 BB 跟上之前下单。
"""
import json
import os
import threading
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.pin_client import PinOddsCache, PinRestClient, PinWSClient, PinSSEClient

DROP_FILE = ROOT / "data" / "storage" / "pin_drops.jsonl"
TRIGGER_FILE = ROOT / "data" / "storage" / "pin_trigger.jsonl"
DROP_RETENTION_DAYS = 30  # pin_drops 归档保留天数(超期清理, 显式管磁盘)
LIVE_TICK_FILE = ROOT / "data" / "storage" / "pin_live_tick.ts"  # 滚球 WS live 帧触发信号时间戳
FRAME_GAP_FILE = ROOT / "data" / "storage" / "pin_frame_gaps.jsonl"  # 帧间隔埋点(2026-10-11 攒帧间隔分布, 定 suspension 阈值)
BB_SNAPSHOT = ROOT / "data" / "storage" / "bb_odds_extracted.json"

_bb_cache = {"ts": 0.0, "matches": []}


def _norm(name):
    # 与 odds_api_io._norm_team 同口径(去 FC/SC/CF 等后缀, 圣→st 等), 提升 drop→BB 匹配率
    from src.scrapers.odds_api_io import _norm_team
    return _norm_team(name)


def _load_bb_matches():
    """读 BB 早盘快照(带 60s 缓存), 建 (norm_home, norm_away) 反查表。"""
    now = time.time()
    if now - _bb_cache["ts"] < 60 and _bb_cache["matches"]:
        return _bb_cache["matches"]
    try:
        d = json.loads(BB_SNAPSHOT.read_text())
        matches = d.get("matches", []) if isinstance(d, dict) else d
    except Exception:
        matches = []
    _bb_cache["ts"] = now
    _bb_cache["matches"] = matches
    return matches


def _match_bb(home, away):
    """drop 的 (home, away) → BB 比赛(精确 + 主客互换)。返回 BB match dict 或 None。"""
    nh, na = _norm(home), _norm(away)
    for m in _load_bb_matches():
        eh, ea = _norm(m.get("home", "")), _norm(m.get("away", ""))
        if not eh or not ea:
            continue
        if (eh == nh and ea == na) or (eh == na and ea == nh):
            return m
    return None


def _append_rotating(path, line):
    """追加一行, 超过 2000 行截断到最近 100 行(防日志无界增长)。"""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if path.stat().st_size > 1_500_000:  # >1.5MB 截断
            lines = path.read_text().splitlines()
            path.write_text("\n".join(lines[-100:]) + "\n")
    except OSError:
        pass


# ── pin_drops 按天归档(2026-10-17) ──
# 旧 1.5MB 截断物理删除 drop 历史(只留 ~2 分钟), nvp/alerted/limit 已落盘但窗口太短,
# 口径对比/信号级回测/校准无样本。改按 UTC 日归档 pin_drops_YYYYMMDD.jsonl + 保留天数。
# pin_drops.jsonl 保留为 symlink 指向「今天」归档 → 旧读者(leadlag_measure.py)零修改。
# pin_trigger.jsonl 仍走 _append_rotating(触发信号热路径, offset 消费, 不归档)。


def _utc_date_str(ts=None):
    """UTC 日期字符串 YYYYMMDD(跨时区赛事对齐, 避免同场 drop 散落两个文件)。"""
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime("%Y%m%d")


def _drop_archive_file(date_str):
    return ROOT / "data" / "storage" / f"pin_drops_{date_str}.jsonl"


_drop_day = {"date": "", "path": None}  # 当日归档缓存: 日期变化才重建 symlink + 清理


def _ensure_drop_symlink():
    """确保 pin_drops.jsonl 是指向「今天」归档的 symlink(每日切分时重建)。

    兼容旧版: pin_drops.jsonl 若还是普通文件(非 symlink), 先 rename 成今天归档(内容保留)。
    """
    today = _utc_date_str()
    archive = _drop_archive_file(today)
    try:
        if DROP_FILE.is_symlink():
            if os.readlink(DROP_FILE) == archive.name:
                return archive  # 已指向今天
        elif DROP_FILE.exists():
            DROP_FILE.rename(archive)  # 旧普通文件 → 今天归档(原子, 不丢内容)
    except OSError:
        pass
    try:
        if DROP_FILE.is_symlink() or DROP_FILE.exists():
            DROP_FILE.unlink()
        DROP_FILE.symlink_to(archive.name)  # 相对 symlink(同目录)
    except OSError:
        pass
    return archive


def _cleanup_old_drops():
    """删除超过 DROP_RETENTION_DAYS 天的 pin_drops_*.jsonl 归档。"""
    import glob as _glob
    cutoff = _utc_date_str(time.time() - DROP_RETENTION_DAYS * 86400)
    for p in _glob.glob(str(ROOT / "data" / "storage" / "pin_drops_*.jsonl")):
        date_part = Path(p).name[len("pin_drops_"):-len(".jsonl")]
        if len(date_part) == 8 and date_part.isdigit() and date_part < cutoff:
            try:
                Path(p).unlink()
            except OSError:
                pass


def _append_drop(rec):
    """按 UTC 日归档追加一条 drop(append + 每行 flush, 崩一次只丢一行)。"""
    today = _utc_date_str()
    if _drop_day["date"] != today:
        _drop_day["date"] = today
        _drop_day["path"] = _ensure_drop_symlink()
        _cleanup_old_drops()
    try:
        with open(_drop_day["path"], "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()  # 每行刷盘
    except OSError:
        pass


_live_tick_state = {"last": 0.0}
_frame_ts = {}  # matchup_id -> 上一帧本地时刻(算帧间隔用)
_frame_gaps = []  # 攒帧间隔样本, 满 500 flush 一次


def _flush_frame_gaps():
    """帧间隔样本落盘(纯 append, 供离线算 p95/p99 定 suspension 阈值)。"""
    global _frame_gaps
    if not _frame_gaps:
        return
    try:
        with open(FRAME_GAP_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(f"{g:.3f}" for g in _frame_gaps) + "\n")
        _frame_gaps = []
    except OSError:
        pass


def _on_live(matchup_id, rec, frame_ts=None):
    """Pin WS live 帧 → 写滚球触发信号时间戳(节流 0.5s), 供 second_level_monitor 读。

    替代 odds-api.io(Betfair) WS 的 get_persisted_changes 触发(19 号 Betfair 停订后靠它)。
    2026-10-11 写 "{本地收到时刻},{Pin帧ts(秒)}" 两段: 拆「Pin产生→我收到」vs「我收到→下单完成」。
    埋点: 每个 matchup 的帧间隔 gap 攒到 FRAME_GAP_FILE, 供离线算 suspension 阈值(为精确版 A 铺路)。
    """
    now = time.time()
    # 帧间隔埋点(节流前, 尽量全量): 同 matchup 相邻帧的本地时间差
    _last = _frame_ts.get(matchup_id)
    if _last is not None:
        _gap = now - _last
        if 0.01 < _gap < 600:  # 过滤同帧重复 + 异常长间隔
            _frame_gaps.append(_gap)
            if len(_frame_gaps) >= 500:
                _flush_frame_gaps()
    _frame_ts[matchup_id] = now
    # 原有节流 + 写触发时间戳
    if now - _live_tick_state["last"] < 0.5:
        return
    _live_tick_state["last"] = now
    try:
        _pin_ts = (frame_ts / 1000.0) if frame_ts else ""
        LIVE_TICK_FILE.write_text(f"{now},{_pin_ts}")
    except OSError:
        pass


def _on_drop(stream, drop):
    """SSE drop 回调: 落盘 + 映射 BB 比赛 + 写触发信号。"""
    rec = {"ts": time.time(), "stream": stream, **drop}
    _append_drop(rec)
    home = drop.get("home", "?")
    away = drop.get("away", "?")
    fp = drop.get("from_price")
    tp = drop.get("to_price")
    m = _match_bb(home, away)
    outcome = drop.get("outcome", "")
    if m:
        trig = {"ts": time.time(), "stream": stream,
                "home": home, "away": away, "outcome": outcome,
                "from_price": fp, "to_price": tp,
                "bb_match_id": m.get("id"), "bb_home": m.get("home"),
                "bb_away": m.get("away"), "sport": m.get("sport")}
        _append_rotating(TRIGGER_FILE, json.dumps(trig, ensure_ascii=False))
        print(f"[pin-drop] {stream} {home} vs {away}: {fp} → {tp} [{outcome}] → 命中BB #{m.get('id')}", flush=True)
    else:
        print(f"[pin-drop] {stream} {home} vs {away}: {fp} → {tp} [{outcome}] (BB未匹配)", flush=True)


def _poll_rest_cache(cache, rest):
    """REST 低频轮询: 只拉 prematch 补 num_1(半场) 数据。

    2026-10-18 深度优化: WS 已实时推全量赔率(num_0 全场, snapshot+prematch_markets+live 帧),
    REST 周期轮询的 num_0 是冗余(120req/min 吃满 rate limit 触发 Cloudflare 封禁)。REST 只保留:
      - num_1(半场): WS 不推半场 spreads/totals, 只有 REST /kit/v1/markets 有, ht/ht_hc/ht_ou 用。
        半场赔率变化慢, 每 5min 拉一次足够(5 运动 × 1 prematch = 1 req/min)。
      - live 的 num_0 走 WS live 帧(实时), 不再用 REST 拉。
    供 pinnacle_fair_price 读(pin_odds_cache.json 落盘)。
    """
    while True:
        try:
            for sid in (1, 2, 3, 4, 6):
                data = rest.get_odds(sid, "prematch")
                if not data:
                    continue
                for ev in data.get("events", []):
                    eid = str(ev.get("event_id"))
                    if eid:
                        cache.update_event(eid, ev)
        except Exception as e:
            print(f"[pin] REST 轮询异常: {type(e).__name__} {str(e)[:80]}", flush=True)
        time.sleep(300)


# ── 早盘入库(2026-10-11): Pin 驱动观察库入库 ──
# 早盘 Betfair WS 触发(prematch_ws_trigger.py)目前 reconnect 循环 + BB 反查 0 命中(已坏),
# 且 19 号 Betfair 停订。改由 Pin 实时缓存驱动: 每 10s 扫缓存, 对 prematch 事件(赔率变了且
# 冷却期过)反查 BB → _build_oa_entry/_oa_add_markets → log_oa_opportunities 入库观察库。
# 替代 prematch_ws_trigger.py 的 Betfair WS 触发职责。
PREMATCH_COOLDOWN = 300.0     # 同一事件冷却(秒): 冷却内不重比, 过后赔率再变才重比
PREMATCH_FLUSH_INTERVAL = 30.0  # 入库批量写间隔(秒)


class PrematchIngestor:
    def __init__(self, cache):
        self.cache = cache
        self._pending = []     # 待入库 entry 队列
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self._pin_trigger_offset = 0  # 2026-10-17: 读 pin_trigger.jsonl 的 prematch drop offset(与实盘同源)

    def sweep(self):
        """读 pin_trigger.jsonl 的 prematch drop(骤降), 反查 BB, 比价, 入 pending。

        2026-10-17 改: 之前扫 cache.all_events()(全量)入库, 与实盘(_consume_pin_triggers 读骤降 drop)
        触发源不同 → 观察库(全量) vs 实盘(骤降)样本不同, 观察库 CLV 无参考性。改成与实盘同源:
        都读 pin_trigger.jsonl 的 prematch drop(骤降), 入库/实盘样本一致。
        """
        from src.scrapers.bb_vs_pinnacle import _build_oa_entry, _oa_add_markets
        if not TRIGGER_FILE.exists():
            return
        try:
            lines = TRIGGER_FILE.read_text().splitlines()
        except OSError:
            return
        last = self._pin_trigger_offset
        if last >= len(lines):
            self._pin_trigger_offset = 0  # 文件被截断, offset 重置
            last = 0
        new_lines = lines[last:]
        self._pin_trigger_offset = len(lines)
        # BB 快照(bb_match_id → match)
        bb_by_id = {}
        for m in _load_bb_matches():
            mid = m.get("id")
            if mid is not None:
                bb_by_id[str(mid)] = m
        if not bb_by_id:
            return
        for line in new_lines:
            try:
                trig = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if trig.get('stream') != 'prematch':
                continue
            mid = str(trig.get('bb_match_id') or '')
            m = bb_by_id.get(mid)
            if not m:
                continue
            sport = m.get('sport', 'football')
            try:
                e = _build_oa_entry(m, sport)
                _oa_add_markets(e, m, sport, use_rest=False, status='prematch')
            except Exception:
                continue
            if any(e.get(g) for g in ("opportunities", "handicap", "over_under",
                                      "double_chance", "draw_no_bet")):
                with self._lock:
                    self._pending.append(e)

    def flush(self):
        """批量写观察库(每 PREMATCH_FLUSH_INTERVAL 写一次, 降低 CSV 读写开销)。"""
        if time.time() - self._last_flush < PREMATCH_FLUSH_INTERVAL:
            return
        with self._lock:
            if not self._pending:
                self._last_flush = time.time()
                return
            batch = self._pending
            self._pending = []
        self._last_flush = time.time()
        try:
            from src.monitor.clv_collector import log_oa_opportunities
            n = log_oa_opportunities(batch, source="pin_drop") or 0  # 2026-10-18 双轨制: drop 来源
            if n:
                print(f"[pin-prematch] 入库 {n} 条机会", flush=True)
        except Exception as e:
            print(f"[pin-prematch] 入库失败: {type(e).__name__} {str(e)[:80]}", flush=True)


def main():
    cache = PinOddsCache()
    rest = PinRestClient()
    ws = PinWSClient(cache, rest, on_live=_on_live)
    sse = PinSSEClient(on_event=_on_drop)

    _ensure_drop_symlink()   # 启动时迁移旧文件 + 建当天 symlink
    _cleanup_old_drops()     # 启动时清理过期归档
    ws.start()
    sse.start()
    # 2026-10-18 REST 改低频(只补 num_1 半场, 每 5min): WS 已实时推全量 num_0 赔率, 原 REST 周期轮询的
    # num_0 是冗余(120req/min 吃满 rate limit 触发 Cloudflare 封禁)。REST 现在只:
    #   ① _poll_rest_cache 低频拉 prematch 补 num_1(半场, ht/ht_hc/ht_ou 用, 1 req/min)
    #   ② WS 断线重连快照(PinWSClient._run 里 get_snapshot)
    #   ③ 滚球 LEV(second_level_monitor._reanchor_pending 独立 force 路径)
    threading.Thread(target=_poll_rest_cache, args=(cache, rest), daemon=True, name="pin-rest").start()
    ingestor = PrematchIngestor(cache)
    print("[pin] Pin WS + SSE + REST低频补半场 + 早盘入库 常驻已启动", flush=True)

    # 每 10s: 落盘缓存(供早盘/滚球进程读实时公平价) + 扫缓存入库(早盘观察库)
    while True:
        time.sleep(10)
        try:
            cache.persist()
        except Exception as e:
            print(f"[pin] 缓存落盘失败: {type(e).__name__}", flush=True)
        try:
            ingestor.sweep()
            ingestor.flush()
        except Exception as e:
            print(f"[pin-prematch] 扫描异常: {type(e).__name__} {str(e)[:80]}", flush=True)


if __name__ == "__main__":
    main()
