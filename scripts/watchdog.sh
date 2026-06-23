#!/usr/bin/env bash
# 进程守护 — 检查 live_monitor 是否存活，挂了自动重启。
# 用法:
#   ./scripts/watchdog.sh              # 单次检查（适合 cron）
#   ./scripts/watchdog.sh --loop       # 持续监控（每 5 分钟检查一次）
#
# 安装 crontab（每5分钟检查）:
#   crontab -e 添加:
#   */5 * * * * /bin/bash /path/to/scripts/watchdog.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$ROOT_DIR/data/storage/watchdog.log"
PID_FILE="$ROOT_DIR/data/storage/live_monitor.pid"

ensure_log_dir() {
    mkdir -p "$(dirname "$LOG_FILE")"
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

check_and_restart() {
    local running=false

    # 方式1: 检查 PID 文件
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            if ps -p "$pid" -o command= 2>/dev/null | grep -q "live_monitor.py"; then
                running=true
            fi
        fi
    fi

    # 方式2: ps 查找
    if ! $running; then
        local live_pid
        live_pid=$(ps aux | grep "live_monitor.py" | grep -v grep | awk '{print $2}' | head -1)
        if [ -n "$live_pid" ]; then
            running=true
            echo "$live_pid" > "$PID_FILE"
        fi
    fi

    if $running; then
        log "OK - live_monitor 运行中 (PID $(cat "$PID_FILE" 2>/dev/null || echo '?'))"
        return 0
    fi

    log "⚠ 进程未运行，正在重启..."
    cd "$ROOT_DIR"

    # 检查日志文件大小，超过100MB则清空
    local daemon_log="$ROOT_DIR/data/storage/live_monitor_daemon.log"
    if [ -f "$daemon_log" ]; then
        local size_mb
        size_mb=$(du -m "$daemon_log" 2>/dev/null | cut -f1)
        if [ "${size_mb:-0}" -gt 100 ]; then
            > "$daemon_log"
            log "  日志文件超过100MB，已清空"
        fi
    fi

    nohup python3 "$SCRIPT_DIR/live_monitor.py" --loop --dingtalk --betting >> "$daemon_log" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"
    log "✅ 已重启 (PID $new_pid)"

    # 钉钉通知
    if command -v curl &>/dev/null; then
        local webhook
        webhook=$(python3 -c "from config.settings import DINGTALK_WEBHOOK; print(DINGTALK_WEBHOOK)" 2>/dev/null || echo "")
        if [ -n "$webhook" ]; then
            local msg="{\"msgtype\":\"text\",\"text\":{\"content\":\"⚠ live_monitor 已自动重启 (PID $new_pid)\"}}"
            curl -s -X POST "$webhook" -H "Content-Type: application/json" -d "$msg" > /dev/null 2>&1 || true
        fi
    fi
}

ensure_log_dir

if [ "${1:-}" = "--loop" ]; then
    log "🔄 看门狗已启动（每300秒检查一次）"
    while true; do
        check_and_restart
        sleep 300
    done
else
    check_and_restart
fi
