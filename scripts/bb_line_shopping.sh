#!/bin/bash
# 篮球+网球扫描器 — 每天 08:00 由 LaunchAgent 调用
# 棒球已移入独立的 baseball_scan.sh（00:00 / 18:00 运行）

cd "$(dirname "$0")/.." || exit 1

LOG=data/locks/bb_line_shopping.log
echo "===== $(TZ=Asia/Shanghai date) =====" >> "$LOG"

# 篮球扫描
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.bb_line_shopping import scan_and_notify
scan_and_notify()
" >> "$LOG" 2>&1

# 网球扫描
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.tennis_line_shopping import scan_and_notify
scan_and_notify()
" >> "$LOG" 2>&1

# 统一推送（所有体育）
python3 -c "
import sys; sys.path.insert(0, '.')
from src.report.ev_push import push_ev_report
push_ev_report()
" >> "$LOG" 2>&1
