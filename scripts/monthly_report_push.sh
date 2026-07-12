#!/bin/bash
# 每月月报推送 — 每月1日 10:00 由 LaunchAgent 调用
cd /Users/wangyao/SportsBettingPro || exit 1

python3 src/report/periodic_report.py --monthly >> data/locks/monthly_report.log 2>&1
