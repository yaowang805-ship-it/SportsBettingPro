#!/bin/bash
# 棒球定时扫描器 — 每天 00:00 和 18:00 由 LaunchAgent 调用
# 00:00 → MLB 夜场（7h 后开赛，+EV 窗口期）+ NPB 日场（12h 后）
# 18:00 → MLB 日场（7h 后开赛）+ NPB 夜场（1h 后开赛）

cd "$(dirname "$0")/.." || exit 1

LOG=data/locks/baseball_scan.log
echo "===== $(TZ=Asia/Shanghai date) =====" >> "$LOG"

python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.mlb_line_shopping import scan_and_notify
scan_and_notify()
" >> "$LOG" 2>&1

# 扫描完统一推送
python3 -c "
import sys; sys.path.insert(0, '.')
from src.report.ev_push import push_ev_report
push_ev_report()
" >> "$LOG" 2>&1
