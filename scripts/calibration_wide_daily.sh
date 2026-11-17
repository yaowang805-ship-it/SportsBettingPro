#!/bin/bash
# 宽样本校准库每日任务: 先 build 落盘新 BB∩Pin 比赛, 再 settle 回填已结束赛果。
# launchd com.sportsbettingpro.calibration_wide 每天 03:40 调用。
cd /Users/wangyao/SportsBettingPro || exit 1
LOG=data/logs/calibration_wide.log
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') build ====="
  .venv312/bin/python scripts/build_calibration_wide.py
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') settle ====="
  .venv312/bin/python scripts/build_calibration_wide.py --settle
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') orchestrate ====="
  .venv312/bin/python scripts/calibration_orchestrate.py
} >> "$LOG" 2>&1
