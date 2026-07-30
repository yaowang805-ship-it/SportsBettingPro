#!/bin/bash
# =============================================================================
#  SportsBettingPro 标准化管道 — 统一 CLI 入口
#  用法: ./pipeline.sh <command> [options]
# =============================================================================
set -e

# ---- 项目根目录（基于脚本位置，无硬编码路径） ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# ---- 日志 ----
LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"
PIPELINE_LOG="$LOG_DIR/pipeline.log"
PIPELINE_DAEMON_LOG="$LOG_DIR/pipeline_daemon.log"

_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$PIPELINE_LOG"
}

# ---- 子命令 ----
usage() {
    cat <<EOF
用法: ./pipeline.sh <command> [options]

命令:
  scan [--no-bet]       全量扫描 + 对比 + 推送
  incremental           增量扫描（变动检测 → 定向对比）
  settle                结算已结束比赛
  tier-update [--dry-run]  ROI 自进化联赛分层更新
  report <type>         推送报告 (daily|weekly|monthly)
  daemon <action>       管理守护进程 (start|stop|restart|status)
  log [-n N|-f]         查看管道日志
  check                 健康检查
  clv-collect           采集收盘赔率计算 CLV
  clv-report [--no-push] 推送 CLV 日报
  git-commit            自动提交 git 变更

选项:
  --no-bet              只推送不投注（用于晚间扫描）
  -n N                  显示最后 N 行日志（默认 30）
  -f                    持续跟踪日志输出
EOF
    exit 0
}

# ---- 命令实现 ----

cmd_scan() {
    local no_bet=""
    local force=""
    [[ "$1" == "--no-bet" ]] && no_bet="--no-bet" && shift
    [[ "$1" == "--force" ]] && force="--force" && shift

    _log "====== SCAN START ======"
    _log "Step 1/3: BB/FB API 提取..."
    .venv312/bin/python -m src.scrapers.bb_api_fetcher --all-sports 2>&1 | tee -a "$PIPELINE_LOG"; rc=${PIPESTATUS[0]}
    if [ $rc -ne 0 ]; then
        _log "❌ Step 1/3 失败"
        _send_alert "全量扫描 Step 1/3 (bb_api_fetcher)" "$rc"
        return 1
    fi
    _log "Step 1/3: 完成"

    _log "Step 2/3: Pinnacle 对比 (BB+FB合并)..."
    .venv312/bin/python -m src.scrapers.bb_vs_pinnacle 2>&1 | tee -a "$PIPELINE_LOG"; rc=${PIPESTATUS[0]}
    if [ $rc -ne 0 ]; then
        _log "❌ Step 2/3 失败"
        _send_alert "全量扫描 Step 2/3 (bb_vs_pinnacle)" "$rc"
        return 1
    fi
    _log "Step 2/3: 完成"

    _log "Step 2b/3: Pinnacle 对比 (FB独立)..."
    .venv312/bin/python -m src.scrapers.bb_vs_pinnacle \
        --input=bb_odds_extracted_FB.json \
        --output=bb_vs_pinnacle_comparison_FB.json 2>&1 | tee -a "$PIPELINE_LOG"
    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        _log "⚠️ Step 2b/3 失败 (FB独立对比非关键，继续)"
    fi
    _log "Step 2b/3: 完成"

    _log "Step 3/3: +EV 推送 (合并双对比)..."
    if [ -n "$no_bet" ]; then
        .venv312/bin/python -m src.report.bb_ev_push --no-bet $force 2>&1 | tee -a "$PIPELINE_LOG"; rc=${PIPESTATUS[0]}
    else
        .venv312/bin/python -m src.report.bb_ev_push $force 2>&1 | tee -a "$PIPELINE_LOG"; rc=${PIPESTATUS[0]}
    fi
    if [ $rc -ne 0 ]; then
        _log "❌ Step 3/3 失败"
        _send_alert "全量扫描 Step 3/3 (bb_ev_push)" "$rc"
        return 1
    fi
    _log "Step 3/3: 完成"
    _log "====== SCAN DONE ======"
}

cmd_incremental() {
    _log "====== INCREMENTAL START ======"
    .venv312/bin/python -m src.scrapers.bb_incremental_scanner 2>&1 | tee -a "$PIPELINE_LOG"; rc=${PIPESTATUS[0]}
    if [ $rc -ne 0 ]; then
        _log "❌ 增量扫描失败 (rc=$rc)"
        _send_alert "增量扫描 (bb_incremental_scanner)" "$rc"
        return 1
    fi
    _log "====== INCREMENTAL DONE ======"
}

cmd_settle() {
    _log "====== SETTLE START ======"
    .venv312/bin/python -m src.monitor.auto_settle 2>&1 | tee -a "$PIPELINE_LOG"; rc=${PIPESTATUS[0]}
    if [ $rc -ne 0 ]; then
        _log "❌ 结算失败 (rc=$rc)"
        _send_alert "自动结算 (auto_settle)" "$rc"
        return 1
    fi
    _log "====== SETTLE DONE ======"
}

cmd_tier_update() {
    local dry_run=""
    [[ "$1" == "--dry-run" ]] && dry_run="--dry-run" && shift

    _log "====== TIER UPDATE START ======"
    .venv312/bin/python -m src.report.auto_tier_updater $dry_run 2>&1 | tee -a "$PIPELINE_LOG"; rc=${PIPESTATUS[0]}
    if [ $rc -ne 0 ]; then
        _log "❌ 联赛分层更新失败 (rc=$rc)"
        _send_alert "联赛分层更新 (auto_tier_updater)" "$rc"
        return 1
    fi
    _log "====== TIER UPDATE DONE ======"
}

cmd_report() {
    local type="$1"
    case "$type" in
        daily)
            _log "====== DAILY REPORT START ======"
            .venv312/bin/python -m src.report.daily_settlement 2>&1 | tee -a "$PIPELINE_LOG"
            ;;
        weekly)
            _log "====== WEEKLY REPORT START ======"
            .venv312/bin/python -m src.report.periodic_report --weekly 2>&1 | tee -a "$PIPELINE_LOG"
            ;;
        monthly)
            _log "====== MONTHLY REPORT START ======"
            .venv312/bin/python -m src.report.periodic_report --monthly 2>&1 | tee -a "$PIPELINE_LOG"
            ;;
        *)  _log "❌ 未知报告类型: $type (可用: daily|weekly|monthly)"; return 1 ;;
    esac
    local rc=${PIPESTATUS[0]}
    if [ $rc -ne 0 ]; then
        _log "❌ $type 报告失败 (rc=$rc)"
        _send_alert "${type}报告推送失败" "$rc"
        return 1
    fi
    _log "====== $type REPORT DONE ======"
}

cmd_daemon() {
    local action="$1"
    PLIST="com.sportsbettingpro.daemon"
    PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST.plist"

    case "$action" in
        start)
            launchctl load "$PLIST_PATH"
            _log "守护进程已加载 ($PLIST_PATH)"
            ;;
        stop)
            launchctl unload "$PLIST_PATH" 2>/dev/null || true
            _log "守护进程已卸载"
            ;;
        restart)
            launchctl unload "$PLIST_PATH" 2>/dev/null || true
            sleep 1
            launchctl load "$PLIST_PATH"
            _log "守护进程已重启"
            ;;
        status)
            if launchctl list "$PLIST" &>/dev/null; then
                local pid
                pid=$(launchctl list "$PLIST" | awk '{print $1}')
                if [ "$pid" = "-" ]; then
                    echo "守护进程: 已加载（等待调度）"
                elif [ -n "$pid" ] && [ "$pid" -gt 0 ] 2>/dev/null; then
                    echo "守护进程: 运行中 (PID $pid)"
                else
                    echo "守护进程: 已加载 (exit=$pid)"
                fi
            else
                echo "守护进程: ❌ 未加载"
            fi
            echo ""
            echo "最后 10 行日志 (pipeline_daemon.log):"
            tail -10 "$PIPELINE_DAEMON_LOG" 2>/dev/null || echo "  (日志为空)"
            ;;
        *)
            _log "❌ 用法: $0 daemon (start|stop|restart|status)"
            return 1
            ;;
    esac
}

cmd_log() {
    local lines=30
    local follow=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -n) lines="$2"; shift 2 ;;
            -f) follow="-f"; shift ;;
            *) shift ;;
        esac
    done
    if [ "$follow" = "-f" ]; then
        tail -f "$PIPELINE_LOG"
    else
        tail -n "$lines" "$PIPELINE_LOG"
    fi
}

cmd_check() {
    echo "===== 健康检查 ====="

    # 项目根目录
    echo "项目目录: $SCRIPT_DIR"
    echo ""

    # 守护进程状态
    echo -n "守护进程: "
    if launchctl list "com.sportsbettingpro.daemon" &>/dev/null; then
        local pid
        pid=$(launchctl list "com.sportsbettingpro.daemon" | awk '{print $1}')
        if [ "$pid" = "-" ]; then
            echo "已加载（未运行，等待调度）"
        elif [ -n "$pid" ] && [ "$pid" -gt 0 ] 2>/dev/null; then
            echo "运行中 (PID $pid)"
        else
            echo "已加载 (exit=$pid)"
        fi
    else
        echo "❌ 未加载"
    fi

    # 数据文件
    echo ""
    local files=(
        "data/storage/bb_odds_extracted.json"
        "data/storage/bb_vs_pinnacle_comparison.json"
        "data/storage/pinnacle_league_structure.json"
        "data/storage/team_name_map.json"
        "data/storage/virtual_portfolio.json"
    )
    for f in "${files[@]}"; do
        if [ -f "$f" ]; then
            local age=$(( ($(date +%s) - $(stat -f %m "$f")) / 60 ))
            echo "  ✅ $f (${age}分钟前)"
        else
            echo "  ❌ $f (不存在)"
        fi
    done

    # 最后扫描时间
    echo ""
    local last_scan
    last_scan=$(grep "====== SCAN DONE ======" "$PIPELINE_LOG" 2>/dev/null | tail -1 || true)
    if [ -n "$last_scan" ]; then
        echo "最后全量扫描: $last_scan"
    else
        echo "最后全量扫描: 无记录"
    fi

    local last_incr
    last_incr=$(grep "====== INCREMENTAL DONE ======" "$PIPELINE_LOG" 2>/dev/null | tail -1 || true)
    if [ -n "$last_incr" ]; then
        echo "最后增量扫描: $last_incr"
    else
        echo "最后增量扫描: 无记录"
    fi
}

cmd_git_commit() {
    _log "====== GIT COMMIT START ======"
    cd "$SCRIPT_DIR"
    git add -u
    if git diff --staged --quiet; then
        _log "无变更，跳过提交"
    else
        git commit -m "日常自动存档 $(date '+%Y-%m-%d')"
        _log "已提交变更"
    fi
    _log "====== GIT COMMIT DONE ======"
}

cmd_clv_collect() {
    _log "====== CLV COLLECT START ======"
    .venv312/bin/python -m src.monitor.clv_collector 2>&1 | tee -a "$PIPELINE_LOG"
    _log "====== CLV COLLECT DONE ======"
}

cmd_clv_report() {
    _log "====== CLV REPORT START ======"
    if [[ "$1" == "--no-push" ]]; then
        .venv312/bin/python -m src.report.clv_report --no-push 2>&1 | tee -a "$PIPELINE_LOG"
    else
        .venv312/bin/python -m src.report.clv_report 2>&1 | tee -a "$PIPELINE_LOG"
    fi
    _log "====== CLV REPORT DONE ======"
}

_send_alert() {
    local msg="$1"
    local exit_code="${2:-?}"
    local log_tail=""
    if [ -f "$PIPELINE_LOG" ]; then
        log_tail=$(tail -5 "$PIPELINE_LOG" 2>/dev/null | head -c 300)
    fi
    .venv312/bin/python -c "
from config.settings import send_dingtalk
msg = '''❌ $msg (exit=$exit_code)
时间: $(date '+%m/%d %H:%M')
最近日志:
$log_tail'''
send_dingtalk('Pipeline Alert', msg)
" 2>/dev/null || true
}

# ---- 主入口 ----
if [ $# -eq 0 ]; then usage; fi

CMD="$1"
shift

case "$CMD" in
    scan)         cmd_scan "$@" ;;
    incremental)  cmd_incremental "$@" ;;
    settle)       cmd_settle "$@" ;;
    tier-update)  cmd_tier_update "$@" ;;
    report)       cmd_report "$@" ;;
    daemon)       cmd_daemon "$@" ;;
    log)          cmd_log "$@" ;;
    check)        cmd_check "$@" ;;
    clv-collect)  cmd_clv_collect "$@" ;;
    clv-report)   cmd_clv_report "$@" ;;
    git-commit)   cmd_git_commit "$@" ;;
    help|--help)  usage ;;
    *)
        echo "❌ 未知命令: $CMD"
        echo "用法: ./pipeline.sh <command> [options]"
        echo "可用命令: scan, incremental, settle, report, daemon, log, check, git-commit"
        exit 1
        ;;
esac
