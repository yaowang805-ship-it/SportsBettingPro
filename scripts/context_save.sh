#!/bin/bash
# 上下文耗尽前自动保存 (PreCompact/SessionEnd hook 调用)
# 作用: 记忆库快照 + git commit + git push, 防止重启 Claude 后信息脱节。
# 幂等、非阻塞、失败静默(不阻断 Claude 退出)。

set -u
PROJECT_DIR="/Users/wangyao/SportsBettingPro"
MEM_DIR="$HOME/.claude/projects/-Users-wangyao/memory"
SNAP="$MEM_DIR/session-handoff.md"
export GIT_TERMINAL_PROMPT=0   # 防 push 卡在凭证提示

cd "$PROJECT_DIR" || exit 0

# 1) 清除缓存
find . -name "*.pyc" -delete 2>/dev/null

# 2) 记忆库快照(机械交接: 时间戳 + git HEAD + 抓取进度)
mkdir -p "$MEM_DIR"
{
  echo "---"
  echo "name: session-handoff"
  echo "description: 上下文耗尽自动交接快照(由 context_save.sh hook 生成)"
  echo "metadata:"
  echo "  type: project"
  echo "  updated: $(date +%Y-%m-%d)"
  echo "---"
  echo ""
  echo "# 会话交接快照 $(date '+%Y-%m-%d %H:%M:%S')"
  echo ""
  echo "触发: PreCompact/SessionEnd hook (上下文即将耗尽)"
  echo ""
  echo "## git 状态"
  echo "- HEAD: $(git log --oneline -1 2>/dev/null)"
  echo "- 分支: $(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo ""
  echo "## 数据抓取进度 (op_batch.log 尾部)"
  tail -12 data/logs/op_batch.log 2>/dev/null | sed 's/^/  /'
  echo ""
  echo "## 各运动 CSV 数量"
  for d in data/oddsportal/*/; do
    s=$(basename "$d")
    n=$(ls "$d" 2>/dev/null | wc -l | tr -d ' ')
    echo "  - $s: $n"
  done
  echo ""
  echo "## 恢复指引"
  echo "- 读 data/logs/op_batch.log 看抓取到哪了"
  echo "- <100行的足球/篮球/冰球 CSV 是翻页截断, 需删掉重抓 (见 [[oddsportal-download-20260813]])"
} > "$SNAP"

# 3) git 提交 + 推送
git add -A 2>/dev/null
if ! git diff --staged --quiet 2>/dev/null; then
  git commit -m "auto: context-save $(date '+%Y-%m-%d %H:%M')" -q 2>/dev/null
fi
git push -q origin main 2>/dev/null

echo "✅ context_save done"
exit 0
