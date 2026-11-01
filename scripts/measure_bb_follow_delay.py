#!/usr/bin/env python3
"""测 BB 价跟进 Pin drop 的延迟。

监听 pin_trigger.jsonl 的早盘 drop(stream != live), 对每个 drop 立即轮询该场 BB 价
(getMatchDetail), 检测 BB 何时跟进(任一盘口赔率变化 > 阈值), 算出 Pin drop → BB 跟进的延迟。

只读, 不下注不改状态。用于回答「我们的 2-4s 投注总耗时能否快于 BB 变化」。

用法: python3 scripts/measure_bb_follow_delay.py [--duration 1200] [--poll 2.0] [--threshold 2.0]
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PIN_TRIGGER = ROOT / "data" / "storage" / "pin_trigger.jsonl"


def _odds_snapshot(m):
    """提取一场比赛所有在售盘口的赔率, 键 (mty, pe, ty) → 赔率。"""
    snap = {}
    for mg in m.get("mg", []):
        mty, pe = mg.get("mty"), mg.get("pe")
        for mk in mg.get("mks", []):
            if mk.get("ss") != 1:
                continue
            for op in mk.get("op", []):
                od, ty = op.get("od"), op.get("ty")
                if od and ty is not None:
                    try:
                        snap[(mty, pe, int(ty))] = float(od)
                    except (TypeError, ValueError):
                        pass
    return snap


def _changed_keys(baseline, cur, threshold_pct):
    """返回 (key, 变化百分比) 中变化 > threshold 的那些。"""
    out = []
    for k, v in cur.items():
        b = baseline.get(k)
        if b and b > 1:
            delta = abs(v - b) / b * 100
            if delta >= threshold_pct:
                out.append((k, delta))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=1200, help="运行秒数(默认1200=20min)")
    ap.add_argument("--poll", type=float, default=2.0, help="每场轮询间隔秒(默认2s)")
    ap.add_argument("--threshold", type=float, default=2.0, help="BB 赔率变化阈值%(默认2%)")
    ap.add_argument("--timeout", type=float, default=120.0, help="单场跟踪超时秒(默认120s)")
    args = ap.parse_args()

    from src.scrapers.bb_api_fetcher import fetch_match_for_order

    offset = 0
    try:
        offset = sum(1 for _ in open(PIN_TRIGGER))
    except FileNotFoundError:
        offset = 0

    active = {}
    results = []
    t0 = time.time()
    print(f"[测] 监听早盘 drop | 时长{args.duration:.0f}s 轮询{args.poll}s 阈值{args.threshold}% | 初始offset={offset}", flush=True)

    while time.time() - t0 < args.duration:
        # 读新 drop
        try:
            lines = PIN_TRIGGER.read_text().splitlines()
        except OSError:
            lines = []
        if len(lines) < offset:  # 文件被轮转截断
            offset = 0
        for line in lines[offset:]:
            try:
                trig = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if trig.get("stream") == "live":
                continue
            mid = str(trig.get("bb_match_id") or "")
            if not mid or mid in active:
                continue
            m = fetch_match_for_order(mid)
            if m is None:
                continue
            active[mid] = {
                "drop_ts": float(trig.get("ts") or 0),
                "outcome": trig.get("outcome"),
                "from_price": trig.get("from_price"),
                "to_price": trig.get("to_price"),
                "baseline": _odds_snapshot(m),
                "n_polls": 0,
            }
            print(f"[测] drop {mid} outcome={trig.get('outcome')} Pin {trig.get('from_price')}→{trig.get('to_price')} 开始跟踪", flush=True)
        offset = len(lines)

        # 轮询 active
        now = time.time()
        for mid in list(active.keys()):
            a = active[mid]
            a["n_polls"] += 1
            m = fetch_match_for_order(mid)
            if m is None:
                continue
            changed = _changed_keys(a["baseline"], _odds_snapshot(m), args.threshold)
            if changed:
                delay = now - a["drop_ts"]
                # 只报变化最大的那个盘口
                k, delta = max(changed, key=lambda x: x[1])
                results.append((mid, a["outcome"], delay, delta, k))
                print(f"[测] ✅ BB跟进 {mid} 盘口{k} 延迟 {delay:.1f}s (变化{delta:.1f}%)", flush=True)
                del active[mid]
                continue
            if now - a["drop_ts"] > args.timeout:
                print(f"[测] ⏱️ {mid} {args.timeout:.0f}s 未跟进, 放弃", flush=True)
                del active[mid]

        time.sleep(args.poll)

    print(f"\n[测] 结束, 共捕获 {len(results)} 次 BB 跟进:", flush=True)
    if results:
        delays = sorted(r[2] for r in results)
        import statistics
        print(f"  延迟: 中位 {statistics.median(delays):.1f}s | 最小 {min(delays):.1f}s | 最大 {max(delays):.1f}s")
        b2 = sum(1 for d in delays if d < 2)
        b4 = sum(1 for d in delays if 2 <= d < 4)
        b10 = sum(1 for d in delays if 4 <= d < 10)
        b99 = sum(1 for d in delays if d >= 10)
        print(f"  <2s: {b2} | 2-4s: {b4} | 4-10s: {b10} | >10s: {b99}")
        for r in results:
            print(f"    {r[1]:8s} 延迟 {r[2]:6.1f}s 变化 {r[3]:5.1f}% 盘口 {r[4]}")


if __name__ == "__main__":
    main()
