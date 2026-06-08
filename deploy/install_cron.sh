#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# SportsBettingPro 自动部署 / crontab 一键安装脚本
# ═══════════════════════════════════════════════════════════
# 用法:
#   bash deploy/install_cron.sh          # 安装定时任务
#   bash deploy/install_cron.sh --dry    # 只查看不安装
#   bash deploy/install_cron.sh --remove # 移除定时任务
# ═══════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
CRON_LOG="$ROOT/logs/cron.log"
VENV_ACTIVATE="$ROOT/venv/bin/activate"

# 确保日志目录存在
mkdir -p "$ROOT/logs"

# crontab 条目
# 每天 09:30 运行全量预测 + 排名 + 监控
CRON_JOB="30 9 * * * cd $ROOT && (source $VENV_ACTIVATE 2>/dev/null || true) && python3 src/predict/run_all.py --sport all >> $CRON_LOG 2>&1"

install() {
    # 检查是否有 venv
    if [ ! -f "$VENV_ACTIVATE" ]; then
        echo "⚠️  未检测到 venv，尝试使用系统 python3"
    fi

    # 添加到 crontab（去重）
    if crontab -l 2>/dev/null | grep -q "run_all.py"; then
        echo "✅ crontab 已存在，跳过安装"
        crontab -l 2>/dev/null | grep "run_all.py"
        exit 0
    fi

    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "✅ crontab 已安装:"
    echo "   每天 09:30 → $ROOT/src/predict/run_all.py --sport all"
    echo "   日志: $CRON_LOG"
    echo ""
    echo "手动测试:"
    echo "   python3 src/predict/run_all.py --sport all"
}

remove() {
    if crontab -l 2>/dev/null | grep -q "run_all.py"; then
        crontab -l 2>/dev/null | grep -v "run_all.py" | crontab -
        echo "✅ SportsBettingPro crontab 已移除"
    else
        echo "ℹ️  未安装 crontab"
    fi
}

case "${1:-}" in
    --remove) remove ;;
    --dry)
        echo "将安装以下 crontab:"
        echo "$CRON_JOB"
        echo ""
        echo "日志: $CRON_LOG"
        ;;
    *)
        install
        ;;
esac
