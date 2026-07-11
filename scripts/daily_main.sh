#!/bin/bash
# 每日主流程 — 08:57 由 LaunchAgent 调用
# cd 到项目目录后执行 main.py

cd "$(dirname "$0")/.." || exit 1
python3 main.py >> data/storage/daily_run.log 2>&1
