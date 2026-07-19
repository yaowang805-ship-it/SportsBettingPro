#!/bin/bash
# BB体育 增量扫描（每15-30分钟）
# 依赖: 已有 BB API 数据 (bb_api_fetcher)
# 检测赔率变动 → 只扫变动联赛的 Pinnacle → 合并结果 → 推送

cd "$(dirname "$0")/.." || exit 1

LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/incremental_scan.log"

echo "===== $(date) =====" >> "$LOG_FILE"

python3 -m src.scrapers.bb_incremental_scanner >> "$LOG_FILE" 2>&1

echo "===== 完成 =====" >> "$LOG_FILE"
