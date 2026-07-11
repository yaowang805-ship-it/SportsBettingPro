#!/bin/bash
# 每日 9:00 推送 — 结算报告 + +EV 机会
cd /Users/wangyao/SportsBettingPro || exit 1

# 结算报告
python3 -m src.report.daily_settlement >> data/locks/daily_report.log 2>&1

# +EV 机会推送
python3 -m src.report.ev_push >> data/locks/ev_push.log 2>&1
