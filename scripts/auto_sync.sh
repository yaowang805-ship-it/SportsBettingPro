#!/bin/bash
# 自动同步: 更新记忆库 + Git 提交 + 清除 pyc
# 每次系统优化后自动运行

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# 清除所有 .pyc 缓存
find . -name "*.pyc" -delete 2>/dev/null
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

# 更新记忆库时间戳
MEMORY_FILE="$HOME/.claude/projects/-Users-wangyao-SportsBettingPro/memory/current_state.md"
if [ -f "$MEMORY_FILE" ]; then
    TODAY=$(date +%Y-%m-%d)
    # 更新日期
    sed -i '' "s/description: .*更新/description: ${TODAY} 更新/" "$MEMORY_FILE" 2>/dev/null || true
fi

# Git 提交
if ! git diff --quiet || ! git diff --staged --quiet; then
    git add -A
    git commit -m "auto: system sync $(date '+%Y-%m-%d %H:%M')

Auto-commit by auto_sync.sh:
- Clear __pycache__
- Update memory bank timestamp
- Commit all pending changes

Co-Authored-By: Claude <noreply@anthropic.com>" 2>/dev/null
    echo "✅ Git committed"
else
    echo "✅ No changes to commit"
fi

echo "✅ Sync complete"
