#!/bin/bash
# 每周周报推送 — 周日晚 21:00 由 LaunchAgent 调用
cd /Users/wangyao/SportsBettingPro || exit 1

python3 src/report/periodic_report.py --weekly >> data/locks/weekly_report.log 2>&1
