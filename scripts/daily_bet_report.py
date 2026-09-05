#!/usr/bin/env python3
"""每日已投注明细日报(2026-09-05 用户要求: 每天上午9点发已投注比赛明细)。

读 data/storage/bet_history.json 里当天的已投注记录, 发钉钉。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR, send_dingtalk
from config.logging_config import get_logger

logger = get_logger(__name__)


def _today_start_ts():
    from datetime import datetime
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_start.timestamp()


def main():
    hist_file = DATA_DIR / "bet_history.json"
    if not hist_file.exists():
        logger.info("无已投注记录, 不发日报")
        return

    try:
        data = json.loads(hist_file.read_text())
    except Exception:
        logger.warning("bet_history.json 解析失败")
        return

    bets = data.get("bets", [])
    today_start = _today_start_ts()
    today_bets = [b for b in bets if b.get("ts", 0) >= today_start]

    if not today_bets:
        logger.info("今天无已投注记录, 不发日报")
        return

    total = sum(b.get("stake", 0) for b in today_bets)
    lines = [f"**今日已投注 {len(today_bets)} 注 / ¥{total:.0f}**", ""]
    for b in today_bets:
        from datetime import datetime
        t = datetime.fromtimestamp(b.get("ts", 0)).strftime("%H:%M")
        lines.append(
            f"• {t} {b.get('home')} vs {b.get('away')} | "
            f"{b.get('designation')} @{b.get('odds')} | "
            f"¥{b.get('stake', 0):.0f} | 订单{b.get('order_id')}"
        )

    body = "\n".join(lines)
    ok = send_dingtalk(f"📊 今日投注明细 ({len(today_bets)}注)", body)
    logger.info("日报发送: %s", ok)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
