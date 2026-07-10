#!/bin/bash
# 非足球体育扫描器 — 每 12 小时由 LaunchAgent 调用
# 职责: 扫描篮球+棒球+网球 → 统一推送

cd "$(dirname "$0")/.." || exit 1

LOG=data/locks/bb_line_shopping.log
echo "===== $(TZ=Asia/Shanghai date) =====" >> "$LOG"

# 篮球扫描
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.bb_line_shopping import scan_and_notify
scan_and_notify()
" >> "$LOG" 2>&1

# 棒球扫描
python3 -c "
import sys; sys.path.insert(0, '.')
from src.betting.mlb_line_shopping import scan_and_notify
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
from src.betting.line_shopping import push_cached_recommendations
push_cached_recommendations()
" >> "$LOG" 2>&1
