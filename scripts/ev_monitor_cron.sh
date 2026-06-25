#!/bin/bash
# Line Shopping 定时运行器 — 每 30 分钟由 LaunchAgent 调用
# 职责: 1) +EV 扫描 2) 虚拟投注 3) 自动结算 4) 就绪评估

cd "$(dirname "$0")/.." || exit 1

# 1) +EV 机会扫描 + 钉钉推送
python3 -m src.monitor.ev_monitor >> data/storage/ev_monitor.log 2>&1

# 2) 虚拟投注（两段式：仅投距离开赛 30 小时内的比赛）
python3 -c "
import sys; sys.path.insert(0, '.')
from config.logging_config import setup_logging; setup_logging()
from src.betting.place_line_shops import place_line_shops as _pls
_pls()
" >> data/storage/place_line_shops.log 2>&1

# 3) 自动结算
python3 -c "
import sys; sys.path.insert(0, '.')
from config.logging_config import setup_logging; setup_logging()
from src.monitor.auto_settle import auto_settle
auto_settle()
" >> data/storage/auto_settle.log 2>&1

# 4) 就绪评估（静默刷新）
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.paper_trader import PaperTrader
PaperTrader().refresh()
" > /dev/null 2>&1
