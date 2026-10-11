"""Pin 三通道常驻进程 (2026-10-11): WS 全量赔率流 + SSE drop 触发, 维护实时 Pin 赔率缓存。

职责:
  WS  = 全量赔率流 → 维护 pin_odds_cache.json(实时公平价源), 每 10s 落盘
  SSE = drop 提醒(≥5% 骤降) → drop → 映射 BB 比赛 → 写触发信号(供比价进程消费)

drop 是 lead-lag 命门: Pin 赔率骤降 = sharp money 进来, 要抢在 BB 跟上之前下单。
"""
import json
import threading
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.pin_client import PinOddsCache, PinRestClient, PinWSClient, PinSSEClient

DROP_FILE = ROOT / "data" / "storage" / "pin_drops.jsonl"
TRIGGER_FILE = ROOT / "data" / "storage" / "pin_trigger.jsonl"
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


def _on_drop(stream, drop):
    """SSE drop 回调: 落盘 + 映射 BB 比赛 + 写触发信号。"""
    rec = {"ts": time.time(), "stream": stream, **drop}
    try:
        with open(DROP_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
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
        try:
            with open(TRIGGER_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(trig, ensure_ascii=False) + "\n")
        except OSError:
            pass
        print(f"[pin-drop] {stream} {home} vs {away}: {fp} → {tp} [{outcome}] → 命中BB #{m.get('id')}", flush=True)
    else:
        print(f"[pin-drop] {stream} {home} vs {away}: {fp} → {tp} [{outcome}] (BB未匹配)", flush=True)


def _poll_rest_cache(cache, rest):
    """REST since 轮询: 维护标准化 Pin 赔率缓存(WS 只有元数据没赔率, 赔率在 REST)。

    每 5s 拉 live+prematch 全量(10 次 REST/轮, ≤10 req/s), 存 {event_id: {event: 标准化事件}}。
    供 pinnacle_fair_price 读(pin_odds_cache.json 落盘)。
    """
    while True:
        try:
            for sid in (1, 2, 3, 4, 6):
                for et in ("live", "prematch"):
                    data = rest.get_odds(sid, et)
                    if not data:
                        continue
                    for ev in data.get("events", []):
                        eid = str(ev.get("event_id"))
                        if eid:
                            cache.update_event(eid, ev)
        except Exception as e:
            print(f"[pin] REST 轮询异常: {type(e).__name__} {str(e)[:80]}", flush=True)
        time.sleep(5)


def main():
    cache = PinOddsCache()
    rest = PinRestClient()
    ws = PinWSClient(cache, rest)
    sse = PinSSEClient(on_event=_on_drop)

    ws.start()
    sse.start()
    threading.Thread(target=_poll_rest_cache, args=(cache, rest), daemon=True, name="pin-rest").start()
    print("[pin] Pin WS + SSE + REST轮询 常驻已启动", flush=True)

    # 每 10s 落盘缓存(供早盘/滚球进程读实时公平价)
    while True:
        time.sleep(10)
        try:
            cache.persist()
        except Exception as e:
            print(f"[pin] 缓存落盘失败: {type(e).__name__}", flush=True)


if __name__ == "__main__":
    main()
