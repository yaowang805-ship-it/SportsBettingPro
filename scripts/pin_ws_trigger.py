"""Pin 三通道常驻进程 (2026-10-11): WS 全量赔率流 + SSE drop 触发, 维护实时 Pin 赔率缓存。

职责:
  WS  = 全量赔率流 → 维护 pin_odds_cache.json(实时公平价源), 每 10s 落盘
  SSE = drop 提醒(≥5% 骤降) → 触发回调(当前: 记日志 + 落盘 drop 文件, 供比价进程消费)

未来: SSE drop → 直接触发 BB 比价(lead-lag 套利), 替代 Betfair WS 触发。
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


def _on_drop(stream, drop):
    """SSE drop 回调: 落盘(供比价进程消费) + 打印。"""
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
    print(f"[pin-drop] {stream} {home} vs {away}: {fp} → {tp}", flush=True)


def main():
    cache = PinOddsCache()
    rest = PinRestClient()
    ws = PinWSClient(cache, rest)
    sse = PinSSEClient(on_event=_on_drop)

    ws.start()
    sse.start()
    print("[pin] Pin WS + SSE 常驻已启动", flush=True)

    # 每 10s 落盘缓存(供早盘/滚球进程读实时公平价)
    while True:
        time.sleep(10)
        try:
            cache.persist()
        except Exception as e:
            print(f"[pin] 缓存落盘失败: {type(e).__name__}", flush=True)


if __name__ == "__main__":
    main()
