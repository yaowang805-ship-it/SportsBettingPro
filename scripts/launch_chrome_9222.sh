#!/bin/bash
# 启动带远程调试端口(9222)的独立 Chrome, 供 second_level_monitor 的 CDP tap 抓 BB 页 WS 推送(G04)。
# 依赖: 独立 user-data-dir(否则普通 Chrome 已开时端口标志会被忽略)。
# 用法: bash scripts/launch_chrome_9222.sh
set -uo pipefail

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT=9222
USER_DATA_DIR="$HOME/SportsBettingPro/.chrome-bb"
CDP_URL="http://127.0.0.1:$PORT/json"

# 1. 已开着 9222 就不重复启动
if curl -s --max-time 2 "$CDP_URL" >/dev/null 2>&1; then
  echo "✅ Chrome 9222 已在运行"
  if curl -s --max-time 2 "$CDP_URL" 2>/dev/null | grep -qE "bbty|x14ff"; then
    echo "✅ BB 页已开着, WS 推送可直接用"
  else
    echo "⚠️ 9222 已开但没检测到 BB 页, 请在 Chrome 里打开 pc.x14ff.com 并登录"
  fi
  exit 0
fi

# 2. 校验 Chrome 二进制存在
if [ ! -x "$CHROME" ]; then
  echo "❌ 找不到 Chrome: $CHROME"
  exit 1
fi

# 3. 启动独立 Chrome(用 open -na 走 launchd, 进程持久不被 shell 回收; 独立配置目录)
echo "🚀 启动独立 Chrome (port $PORT)..."
open -na "Google Chrome" --args \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$USER_DATA_DIR"

# 4. 等端口就绪(最多 10s)
for _ in $(seq 1 20); do
  if curl -s --max-time 2 "$CDP_URL" >/dev/null 2>&1; then
    echo "✅ Chrome 9222 已启动"
    echo ""
    echo "   下一步:"
    echo "   1. 在新 Chrome 里打开 pc.x14ff.com 并登录 BB 账号(首次登录, 之后记住)"
    echo "   2. 验证 WS 推送生效:"
    echo "      tail -f data/logs/second_level_monitor.log | grep G04"
    echo "   3. 有 [G04 #N] 就是 WS 推送生效; 没有则回退 HTTP 轮询兜底"
    exit 0
  fi
  sleep 0.5
done

echo "❌ Chrome 9222 启动失败(10s 未就绪)"
exit 1
