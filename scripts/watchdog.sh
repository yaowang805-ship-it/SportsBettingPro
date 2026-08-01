#!/bin/bash
# =============================================================================
#  Pipeline 守护进程看门狗 — 独立于 launchd 的健康检查 + 自动恢复
#  用法: ./scripts/watchdog.sh
#  建议: crontab 每 5 分钟运行一次
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

LOG="data/logs/watchdog.log"
PLIST="$HOME/Library/LaunchAgents/com.sportsbettingpro.daemon.plist"
LABEL="com.sportsbettingpro.daemon"

_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

# 1. 检查进程是否活着
PID=$(launchctl list "$LABEL" 2>/dev/null | awk 'NR>1{print $1}')
if [ -z "$PID" ] || [ "$PID" = "-" ]; then
    _log "❌ 守护进程未运行，重新加载..."
    launchctl unload "$PLIST" 2>/dev/null || true
    sleep 2
    launchctl load "$PLIST"
    _log "✅ 已重新加载守护进程"
    exit 0
fi

# 2. 检查进程是否在响应 (最近 2 分钟内有日志)
LAST_LOG=$(tail -1 data/logs/pipeline_daemon.log 2>/dev/null | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}' || echo "")
if [ -n "$LAST_LOG" ]; then
    LAST_EPOCH=$(date -j -f "%Y-%m-%d %H:%M" "$LAST_LOG" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    AGE=$((NOW_EPOCH - LAST_EPOCH))
    if [ "$AGE" -gt 300 ]; then  # 5 分钟无日志
        _log "⚠️ 守护进程无响应 (${AGE}s 无日志, PID=$PID)，强制重启..."
        launchctl unload "$PLIST" 2>/dev/null || true
        sleep 3
        launchctl load "$PLIST"
        _log "✅ 已强制重启守护进程"
        exit 0
    fi
fi

# 3. 检查增量扫描是否在运行 (最近 30 分钟内有增量扫描)
LAST_INCR=$(grep "incremental.*DONE" data/logs/pipeline_daemon.log 2>/dev/null | tail -1 | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}' || echo "")
if [ -n "$LAST_INCR" ]; then
    LAST_INCR_EPOCH=$(date -j -f "%Y-%m-%d %H:%M" "$LAST_INCR" +%s 2>/dev/null || echo 0)
    INCR_AGE=$((NOW_EPOCH - LAST_INCR_EPOCH))
    if [ "$INCR_AGE" -gt 2700 ]; then  # 45 分钟无增量扫描
        _log "⚠️ 增量扫描停滞 (${INCR_AGE}s)，但进程仍在，可能是 Pinnacle 故障"
    fi
fi

# 4. 日志轮转 (防止 50MB+ 大文件)
LOG_SIZE=$(stat -f%z data/logs/pipeline_daemon.log 2>/dev/null || echo 0)
if [ "$LOG_SIZE" -gt 52428800 ]; then  # 50MB
    _log "📦 日志轮转 (${LOG_SIZE} bytes)"
    mv data/logs/pipeline_daemon.log "data/logs/pipeline_daemon.log.$(date +%Y%m%d_%H%M)"
fi

_log "✅ 守护进程正常 (PID=$PID, 最后日志: ${AGE}s前)"
