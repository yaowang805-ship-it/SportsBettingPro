#!/usr/bin/env python3
"""SportsBettingPro 统一日常运行器。

本脚本负责启动每日赔率抓取、推荐生成、赛后监控与模型健康检查，
并确保使用本地配置环境变量。"""

import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config.settings import DATA_DIR, DINGTALK_WEBHOOK
from config.logging_config import setup_logging, get_logger

setup_logging()
logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent
DATA_DIR.mkdir(parents=True, exist_ok=True)

if not DINGTALK_WEBHOOK:
    logger.warning("未检测到有效钉钉Webhook，若需钉钉通知请在 .env 中设置 DINGTALK_WEBHOOK")

# ── 防退化检查 — 启动前验证数据完整性 ──
try:
    from src.health.regression import run_all_checks
    _passed, _issues = run_all_checks()
    if _passed:
        logger.info("✅ 防退化检查通过")
    else:
        for _i in _issues:
            logger.error("❌ 防退化: %s", _i)
        send_alert("系统防退化检查", "发现 " + str(len(_issues)) + " 个问题:\n" + "\n".join(_issues))
except Exception as _e:
    logger.warning("防退化检查异常: %s", _e)

SCRIPTS = [
    # (ROOT / "src" / "models" / "auto_retrain.py", "自动模型重训练（月度）", 900),  # ML 已暂停
    # (ROOT / "src" / "predict" / "run_all.py", "职业级每日预测（NBA+足球+NFL）", 600),
    (ROOT / "src" / "monitor" / "performance.py", "投注结算+盈亏监控"),
    # (ROOT / "src" / "monitor" / "clv_tracker.py", "CLV 收盘价追踪"),  # 模块不存在
    (ROOT / "src" / "monitor" / "health_check.py", "系统健康检查"),
]


def send_alert(title: str, message: str) -> None:
    from config.settings import send_dingtalk
    send_dingtalk(title, message)


def run_script(script_path: Path, description: str, max_retries: int = 2,
                timeout: int = 300) -> bool:
    """运行脚本，支持重试机制。"""
    logger.info("开始: %s", description)
    if not script_path.exists():
        logger.error("找不到脚本: %s", script_path)
        return False

    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run([sys.executable, str(script_path)],
                                  check=True,
                                  capture_output=True,
                                  text=True,
                                  timeout=timeout)
            logger.info("完成: %s", description)
            if result.stdout:
                tail = '\n'.join(result.stdout.strip().splitlines()[-8:])
                if tail:
                    logger.info("输出预览:\n%s", tail)
            return True
        except subprocess.TimeoutExpired:
            logger.warning("超时: %s (尝试 %d/%d)", description, attempt + 1, max_retries + 1)
            if attempt < max_retries:
                continue
        except subprocess.CalledProcessError as exc:
            logger.error("失败: %s, 退出码 %d", description, exc.returncode)
            if exc.stdout:
                logger.info("标准输出: %s", exc.stdout[-500:])
            if exc.stderr:
                logger.warning("错误输出: %s", exc.stderr[-500:])
            if attempt < max_retries:
                logger.info("重试中... (尝试 %d/%d)", attempt + 2, max_retries + 1)
                continue
            break

    send_alert(
        "SportsBettingPro 运行失败",
        f"{description} 在 {max_retries + 1} 次尝试后仍失败。请检查系统配置和网络连接。"
    )
    return False


if __name__ == "__main__":
    os.chdir(ROOT)
    logger.info("=" * 72)
    logger.info("SportsBettingPro 统一日常运行 - %s", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    logger.info("=" * 72)

    errors = []
    for item in SCRIPTS:
        path, name = item[0], item[1]
        timeout = item[2] if len(item) > 2 else 300
        if not run_script(path, name, timeout=timeout):
            errors.append(name)

    # ── ESPN 自动结算 — 比 CSV 更及时 ──
    try:
        from src.monitor.auto_settle import auto_settle
        n = auto_settle()
        if n:
            logger.info("  ✅ 自动结算: %d 笔比赛", n)
    except Exception as e:
        logger.warning("自动结算失败: %s", e)

    # Power Rating 报告
    try:
        from src.core.power_rating import print_ratings_report
        print_ratings_report()
    except Exception as e:
        logger.warning("Power Rating 报告失败: %s", e)

    # 盘口快照（模块当前不可用）
    # try:
    #     from src.monitor.line_movement import take_snapshot
    #     n = take_snapshot(force=False)
    #     logger.info("盘口快照: %d 场比赛", n)
    # except Exception as e:
    #     logger.warning("盘口快照失败: %s", e)

    # 套利扫描（模块当前不可用）
    # try:
    #     from src.monitor.arbitrage import scan_all_leagues, report_arbitrage
    #     arb_results = scan_all_leagues(force=False)
    #     report_arbitrage(arb_results, force=False)
    # except Exception as e:
    #     logger.warning("套利扫描失败: %s", e)

    # 模型衰减检测（ML 已暂停）
    # try:
    #     from src.monitor.model_decay import run_decay_check
    #     decay_report = run_decay_check()
    #     if decay_report.get("is_decaying"):
    #         logger.warning("模型衰减信号: %s", decay_report.get("decay_signal", ""))
    # except Exception as e:
    #     logger.warning("模型衰减检测失败: %s", e)

    # 数据质量检测（ML 已暂停）
    # try:
    #     from src.monitor.data_quality import run_data_quality_check
    #     dq_report = run_data_quality_check()
    #     if dq_report["overall_status"] == "error":
    #         logger.warning("数据质量检测发现严重问题")
    # except Exception as e:
    #     logger.warning("数据质量检测失败: %s", e)

    # # 特征漂移检测（ML 已暂停）
    # try:
    #     from src.core.interpretability import detect_feature_drift
    #     ...
    # except Exception as e:
    #     logger.warning("SHAP 特征漂移检测失败: %s", e)

    # Edge Attribution
    try:
        from src.monitor.edge_attribution import print_edge_attribution_report
        print_edge_attribution_report()
    except Exception as e:
        logger.warning("Edge attribution 失败: %s", e)

    # Team Edge Tracking
    try:
        from src.monitor.team_edge_tracker import print_team_edge_report
        print_team_edge_report()
    except Exception as e:
        logger.warning("Team edge tracking 失败: %s", e)

    # ── 历史回放引擎 — 模拟交易样本不足时自动补充 ──
    try:
        from src.betting.paper_trader import PaperTrader
        pt_state = PaperTrader().readiness_summary()
        if pt_state.get("ready"):
            logger.info("  ⏭️ 跳过回放: 模拟交易已达就绪状态")
        elif not pt_state.get("checks", {}).get("min_bets", {}).get("passed", False):
            from scripts.replay_engine import ReplayEngine
            engine = ReplayEngine(min_edge=0.03, kelly_fraction=0.07)
            engine.run()
            logger.info("  ✅ 回放引擎完成: %d 笔新增投注", len(engine.bet_records))
        else:
            logger.info("  ⏭️ 跳过回放: 样本量充足但其他检查未通过")
    except Exception as e:
        logger.warning("回放引擎失败: %s", e)

    # ── 组合风控状态概览（Task #160：组合风险摘要）──
    try:
        from src.risk.manager import RiskManager
        rm = RiskManager()
        cb = rm.circuit_breaker_status()
        n_active = len(rm.portfolio_optimizer.active_bets)

        logger.info("─" * 60)
        logger.info("  📊 组合风控状态")
        logger.info("─" * 60)
        logger.info("  %s", cb["message"])
        logger.info("  当前资金: ¥%.2f | ROI: %+.2f%% | 回撤: %.2f%%",
                   cb["balance"], rm.roi() * 100, cb["drawdown_pct"] * 100)
        logger.info("  胜率: %.1f%% | 连败: %d | 总下注: %d",
                   rm.win_rate() * 100, cb["consecutive_losses"], rm.total_bets)
        logger.info("  活跃投注: %d | VaR(95%%): ¥%.2f | CVaR(95%%): ¥%.2f",
                   n_active, rm.compute_var(0.95), rm.compute_cvar(0.95))
        if n_active > 0:
            ds = rm.portfolio_optimizer.diversification_score()
            logger.info("  组合分散度: %.2f", ds)
    except Exception as e:
        logger.warning("组合风控概览失败: %s", e)

    # ── 防退化终检 — 确认全流程未引入新问题 ──
    try:
        from src.health.regression import run_all_checks
        _p2, _i2 = run_all_checks()
        if _p2:
            logger.info("✅ 终检防退化通过")
        else:
            for _ii in _i2:
                logger.error("❌ 终检防退化: %s", _ii)
    except Exception:
        pass

    logger.info("=" * 72)
    if errors:
        logger.warning("部分任务失败: %s", ', '.join(errors))
        send_alert("SportsBettingPro 日常流程异常", "部分任务执行失败，请立即检查系统。")
        sys.exit(1)
    else:
        logger.info("统一日常流程全部完成")
        sys.exit(0)
