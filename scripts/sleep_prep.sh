#!/bin/bash
# 合盖前运行: 关闭非必要应用，降低能耗
# 保活: Shadowrocket + pipeline daemon + DingTalk

echo "[$(date)] Sleep prep: killing non-essential apps..."

# Chrome (最大耗电)
pkill -f "Google Chrome" 2>/dev/null && echo "  ✅ Chrome closed"

# WPS Office
pkill -f "wpsoffice\|WPS" 2>/dev/null && echo "  ✅ WPS closed"

# deepseek-proxy (不需要在合盖时运行)
pkill -f "deepseek-proxy" 2>/dev/null && echo "  ✅ deepseek-proxy closed"

echo ""
echo "Still running:"
ps aux | grep -E "pipeline_orchestrator|Shadowrocket|DingTalk|钉钉" | grep -v grep | awk '{print "  " $11, $12, $13}'

echo ""
echo "✅ Ready for low-power lid-closed mode"
