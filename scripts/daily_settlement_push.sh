#!/bin/bash
# 每日晨间 9:00 结算报告 — 由 LaunchAgent 调用
cd /Users/wangyao/Desktop/SportsBettingPro || exit 1

python3 src/report/daily_settlement.py >> data/locks/settlement_push.log 2>&1
