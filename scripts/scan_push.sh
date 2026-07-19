#!/bin/bash
# BB体育 扫描+推送 — LaunchAgent 定时任务
# 参数: --no-bet 不投注（20:00用）, --bet 投注（08:00用）
# 使用 BB API 直连（无需 Chrome）

cd "$(dirname "$0")/.." || exit 1

LOG_DIR="data/locks"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/scan_push.log"

# 随机延迟 1~5 分钟，避免准点被风控
JITTER=$((60 + RANDOM % 240))
echo "⏳ 等待 ${JITTER}s（随机延迟）..."
python3 -c "import time; time.sleep($JITTER)" 2>/dev/null

echo "===== $(date) =====" >> "$LOG_FILE"

# Step 1: 提取 BB/FB 赔率
echo "[1/3] 提取 BB/FB 赔率..." >> "$LOG_FILE"
python3 -m src.scrapers.bb_api_fetcher --all-sports >> "$LOG_FILE" 2>&1
if [ $? -ne 0 ]; then
    echo "❌ 提取失败" >> "$LOG_FILE"
    exit 1
fi

# Step 2: 对比 Pinnacle
echo "[2/3] 对比 Pinnacle..." >> "$LOG_FILE"
python3 src/scrapers/bb_vs_pinnacle.py >> "$LOG_FILE" 2>&1
if [ $? -ne 0 ]; then
    echo "❌ 对比失败" >> "$LOG_FILE"
    exit 1
fi

# Step 3: 推送
echo "[3/3] 钉钉推送..." >> "$LOG_FILE"
if [[ "$1" == "--no-bet" ]]; then
    python3 src/report/bb_ev_push.py --no-bet >> "$LOG_FILE" 2>&1
else
    python3 src/report/bb_ev_push.py >> "$LOG_FILE" 2>&1
fi

echo "===== 完成 =====" >> "$LOG_FILE"
