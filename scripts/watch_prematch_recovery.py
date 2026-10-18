#!/usr/bin/env python3
"""监控早盘投注恢复(2026-10-18): 新早盘实盘注 / 限速复发 / 新+EV机会。

每 20s 轮询一次, 只在有信号时输出一行(供 Monitor 工具流式通知)。
心跳每 15 分钟一条(证明还活着)。
"""
import json
import os
import time

TRACKED = "data/storage/tracked_bets.json"
LOG = "data/logs/pipeline_daemon.log"


def load_bets():
    try:
        d = json.load(open(TRACKED))
        bets = d.get("bets", d) if isinstance(d, dict) else d
        return bets if isinstance(bets, list) else list(bets)
    except Exception:
        return []


def main():
    prev_count = len(load_bets())
    log_offset = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    last_heartbeat = time.time()
    last_rate_warn = 0.0  # 限速告警冷却(600s 内不重复报)
    print(f"[监控] 启动: 已有 {prev_count} 笔早盘实盘注", flush=True)
    while True:
        try:
            # 1) 新早盘实盘注
            bets = load_bets()
            if len(bets) > prev_count:
                new_bets = sorted(bets, key=lambda b: b.get("push_time") or "")[prev_count:]
                for b in new_bets:
                    print(f"✅ 新早盘实盘注: {b.get('home')} vs {b.get('away')} | "
                          f"{b.get('designation')} @{b.get('bb_odds')} ¥{b.get('stake')}", flush=True)
                prev_count = len(bets)
            # 2) 新日志 (只报限速, 10min 冷却防刷屏; +EV机会不转发)
            cur_size = os.path.getsize(LOG) if os.path.exists(LOG) else 0
            if cur_size > log_offset:
                with open(LOG, errors="ignore") as f:
                    f.seek(log_offset)
                    new = f.read()
                log_offset = cur_size
                if "全局限速" in new and time.time() - last_rate_warn > 600:
                    last_rate_warn = time.time()
                    print(f"⚠️ 限速仍在复发 (10min 内不再重复报)", flush=True)
            # 3) 心跳
            if time.time() - last_heartbeat > 900:
                print(f"[监控] 心跳: 早盘实盘 {prev_count} 笔, 无新注", flush=True)
                last_heartbeat = time.time()
        except Exception:
            pass
        time.sleep(20)


if __name__ == "__main__":
    main()
