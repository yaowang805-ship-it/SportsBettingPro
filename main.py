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

SCRIPTS = [
    (ROOT / "src" / "models" / "auto_retrain.py", "自动模型重训练（月度）"),
    (ROOT / "src" / "predict" / "run_all.py", "职业级每日预测（NBA+足球）"),
    (ROOT / "src" / "scripts" / "auto_settle_loop.py", "虚拟投注自动结算"),
    (ROOT / "src" / "monitor" / "performance.py", "赛后盈亏监控"),
    (ROOT / "src" / "monitor" / "clv_tracker.py", "CLV 收盘价追踪"),
    (ROOT / "src" / "monitor" / "health_check.py", "系统健康检查"),
]


def send_alert(title: str, message: str) -> None:
    if not DINGTALK_WEBHOOK:
        return
    try:
        import requests
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"**{title}**\n\n{message}"}
        }
        requests.post(DINGTALK_WEBHOOK, json=payload, timeout=10)
    except Exception:
        pass


def run_script(script_path: Path, description: str, max_retries: int = 2) -> bool:
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
                                  timeout=300)
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
    for path, name in SCRIPTS:
        if not run_script(path, name):
            errors.append(name)

    # Power Rating 报告
    try:
        from src.core.power_rating import print_ratings_report
        print_ratings_report()
    except Exception as e:
        logger.warning("Power Rating 报告失败: %s", e)

    # 盘口快照（使用缓存，预测脚本刚拉完）
    try:
        from src.monitor.line_movement import take_snapshot
        n = take_snapshot(force=False)
        logger.info("盘口快照: %d 场比赛", n)
    except Exception as e:
        logger.warning("盘口快照失败: %s", e)

    # 套利扫描（使用缓存，避免重复消耗 API 配额）
    try:
        from src.monitor.arbitrage import scan_all_leagues, report_arbitrage
        arb_results = scan_all_leagues(force=False)
        report_arbitrage(arb_results, force=False)
    except Exception as e:
        logger.warning("套利扫描失败: %s", e)

    # 模型衰减检测
    try:
        from src.monitor.model_decay import run_decay_check
        decay_report = run_decay_check()
        if decay_report.get("is_decaying"):
            logger.warning("模型衰减信号: %s", decay_report.get("decay_signal", ""))
    except Exception as e:
        logger.warning("模型衰减检测失败: %s", e)

    # 数据质量检测
    try:
        from src.monitor.data_quality import run_data_quality_check
        dq_report = run_data_quality_check()
        if dq_report["overall_status"] == "error":
            logger.warning("数据质量检测发现严重问题")
    except Exception as e:
        logger.warning("数据质量检测失败: %s", e)

    # SHAP 特征漂移检测
    try:
        from src.core.interpretability import detect_feature_drift
        shap_dir = ROOT / "models" / "shap"
        baseline_csv = shap_dir / "feature_importance.csv"
        if baseline_csv.exists():
            import pandas as pd
            baseline = pd.read_csv(baseline_csv)
            if len(baseline) > 0:
                drifted = detect_feature_drift(str(baseline_csv), baseline)
                if drifted:
                    logger.warning("特征漂移: %d 个特征发生变化", len(drifted))
    except Exception as e:
        logger.warning("SHAP 特征漂移检测失败: %s", e)

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

    logger.info("=" * 72)
    if errors:
        logger.warning("部分任务失败: %s", ', '.join(errors))
        send_alert("SportsBettingPro 日常流程异常", "部分任务执行失败，请立即检查系统。")
        sys.exit(1)
    else:
        logger.info("统一日常流程全部完成")
        sys.exit(0)
