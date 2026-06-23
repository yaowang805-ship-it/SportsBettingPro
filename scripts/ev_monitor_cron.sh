#!/bin/bash
# Line Shopping 定时运行器 — 每 30 分钟由 cron 调用
# 职责: 1) +EV 扫描 2) 自动结算 3) CLV 追踪
# 安装: crontab -e → 添加 */30 * * * * .../ev_monitor_cron.sh

cd ~/Desktop/SportsBettingPro || exit 1
source venv/bin/activate 2>/dev/null || true

# 1) +EV 机会扫描 + 钉钉推送
python3 -m src.monitor.ev_monitor >> data/storage/ev_monitor.log 2>&1

# 2) 自动结算（比赛结束后 30 分钟内完成）
python3 -c "
import sys; sys.path.insert(0, '.')
from config.logging_config import setup_logging; setup_logging()
from src.monitor.auto_settle import auto_settle
auto_settle()
" >> data/storage/auto_settle.log 2>&1
