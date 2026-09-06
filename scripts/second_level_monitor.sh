#!/bin/bash
# BB 秒级比价监控 启动/停止 (launchd 常驻, 收到 G04 赔率推送即时算 EV)
# 依赖: 独立 Chrome(port 9222) 开着 BB 页(供 CDP tap) + 主扫描周期刷新 comparison JSON

LABEL="com.sportsbettingpro.second_level_monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SRC="$(cd "$(dirname "$0")/.." && pwd)/scripts/com.sportsbettingpro.second_level_monitor.plist"

case "${1:-start}" in
  start)
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$SRC" "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST"
    echo "✅ 已启动 $LABEL (launchd 常驻, 崩溃自动重启)"
    echo "   日志: $(cd "$(dirname "$0")/.." && pwd)/data/logs/second_level_monitor.log"
    ;;
  stop)
    launchctl unload "$PLIST" 2>/dev/null
    echo "✅ 已停止 $LABEL"
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
  status)
    launchctl list | grep "$LABEL" && echo "运行中" || echo "未运行"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status}"
    ;;
esac
