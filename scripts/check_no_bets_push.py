#!/usr/bin/env python3
"""2小时没实盘投注独立提醒(2026-09-24 从自愈看门狗彻底分离)。

独立推送, 标题「⏰ 滚球长时间未投注」, 与自愈看门狗(self_heal)无关。
launchd 每 30min 跑一次, 带 2 小时冷却(超2小时没投注后每2小时才推一次, 避免刷屏)。
"""
import sys, json, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data" / "storage"
COOLDOWN_FILE = DATA_DIR / "no_bet_push_cooldown.json"
COOLDOWN = 2 * 3600  # 2小时冷却


def main():
    from src.monitor.self_heal import check_no_bets
    ok, detail = check_no_bets()
    if ok:
        return  # 正常(2小时内投注过或无比赛), 不推

    # 2小时冷却: 避免每30min刷屏
    now = time.time()
    try:
        last = float(json.loads(COOLDOWN_FILE.read_text()).get("ts", 0) or 0)
    except Exception:
        last = 0.0
    if now - last < COOLDOWN:
        print(f"冷却中(距上次 {(now - last) / 60:.0f}min), 跳过推送")
        return

    from config.settings import send_dingtalk
    body = f"⏰ 滚球长时间未投注\n\n{detail}"
    try:
        send_dingtalk("⏰ 滚球长时间未投注", body)
        COOLDOWN_FILE.write_text(json.dumps({"ts": now}))
        print(f"已推送: {detail}")
    except Exception as e:
        print(f"推送失败: {e}")


if __name__ == "__main__":
    main()
