#!/bin/bash
# 每日自动存档 — 由 LaunchAgent 调用
# 只提交已跟踪文件的修改，避免误提交数据/大文件
cd /Users/wangyao/SportsBettingPro || exit 1
git add -u
git diff --staged --quiet || git commit -m "日常自动存档 $(date +%Y年%m月%d日)"
