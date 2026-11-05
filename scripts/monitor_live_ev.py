#!/usr/bin/env python3
"""监控新滚球实盘注的 EV 是否校准生效(line 1422 修复验证)。

判定:
  - ev != ev_raw  → 校准生效(修复 OK)
  - ev == ev_raw  → 校准被冲掉(修复没生效, 报警)
  - ev > 8%       → 溢价异常高(可能是匹配错, 报警)
每条新滚球注 emit 一行, 供 Monitor 流式通知。

用法: .venv312/bin/python scripts/monitor_live_ev.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "data" / "storage" / "real_bets.json"

START = time.time()  # 只盯脚本启动后的新注
seen = set()

while True:
    try:
        d = json.loads(REAL.read_text())
        bets = d.get("bets", {})
        for oid, v in bets.items():
            ts = v.get("ts") or 0
            if ts < START:
                continue
            if v.get("trigger_source") in ("pin_drop", "ws_scan"):
                continue  # 只看滚球
            if oid in seen:
                continue
            seen.add(oid)
            ev = v.get("ev")
            ev_raw = v.get("ev_raw")
            fair_cal = v.get("fair_cal")
            # 校准是否生效: ev 与 ev_raw 差异 > 0.01pp(校准会拉低热门 EV)
            calibrated = (ev_raw is not None and ev is not None
                          and abs(float(ev) - float(ev_raw)) > 0.01)
            high = (ev or 0) > 8.0
            if not calibrated and high:
                flag = "❌ 校准未生效(ev==ev_raw)且溢价>8%"
            elif high:
                flag = "⚠️ 溢价>8%(需人工核是否匹配错)"
            elif not calibrated:
                flag = "⚠️ 校准未生效(ev==ev_raw)"
            else:
                flag = "✅ 校准生效"
            print("[新滚球注] %s vs %s | %s %s | ev=%.1f%% ev_raw=%.1f%% fair_cal=%s | %s"
                  % (v.get("home"), v.get("away"), v.get("sub"), v.get("desig"),
                     ev or 0, ev_raw or 0, fair_cal, flag), flush=True)
    except Exception:
        pass
    time.sleep(60)
