#!/usr/bin/env python3
"""
SportsBettingPro 统一每日预测入口
用法：
  python src/predict/run_all.py
  python src/predict/run_all.py --sport nba
  python src/predict/run_all.py --sport football
  python src/predict/run_all.py --sport nfl
  python src/predict/run_all.py --skip-monitor
"""
import subprocess, sys, argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

def run_script(script_path, description):
    logger.info("\n%s\n▶  %s\n%s", "─" * 60, description, "─" * 60)
    if not script_path.exists():
        logger.error("❌ 脚本不存在：%s", script_path)
        return False
    try:
        subprocess.run([sys.executable, str(script_path)], check=True, text=True, timeout=300)
        logger.info("✅ 完成：%s", description)
        return True
    except subprocess.TimeoutExpired:
        logger.warning("⏰ 超时（5分钟）：%s", description)
    except subprocess.CalledProcessError as e:
        logger.error("❌ 失败（退出码 %s）：%s", e.returncode, description)
    return False

def run_monitor():
    logger.info("\n%s\n▶  赛后监控 & 健康度检查\n%s", "─" * 60, "─" * 60)
    try:
        from src.monitor.performance import update_performance
        update_performance()
        logger.info("✅ 赛后盈亏监控完成")
    except Exception as e:
        logger.warning("⚠️  赛后监控失败：%s", e)
    # 数据质量检查
    try:
        from src.monitor.data_quality import run_data_quality_check
        dq = run_data_quality_check()
        if dq.get("healthy", True):
            logger.info("✅ 数据质量: 健康")
        else:
            issues = dq.get("issues", [])
            logger.warning("⚠️  数据质量问题: %d 项", len(issues))
            for iss in issues[:5]:
                logger.warning("   - %s", iss)
    except Exception as e:
        logger.warning("⚠️  数据质量检查跳过: %s", e)
    try:
        from src.monitor.health_monitor import check_model_health
        health = check_model_health()
        status = health.get('status', 'ok')
        if status == 'ok':
            logger.info("   Brier(30d): %s  胜率(15d): %s", health.get('brier_30d','N/A'), health.get('winrate_15d','N/A'))
        else:
            logger.info("   模型健康度: %s", status)
        logger.info("✅ 模型健康度检查完成")
    except Exception as e:
        logger.warning("⚠️  健康度检查失败：%s", e)

def _run_ranking():
    """跨运动统一排名（非阻塞，失败不影响主流程）。"""
    try:
        from src.predict.rank_recommendations import main as rank_main
        rank_main()
    except Exception as e:
        logger.warning("⚠️  统一排名跳过: %s", e)


def main():
    parser = argparse.ArgumentParser(description="SportsBettingPro 每日预测入口")
    parser.add_argument("--sport", choices=["nba","football","nfl","all"], default="all")
    parser.add_argument("--skip-monitor", action="store_true")
    args = parser.parse_args()
    predict_dir = ROOT / "src" / "predict"
    logger.info("="*60)
    logger.info("🏆 SportsBettingPro 每日预测  %s", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    logger.info("="*60)
    errors = []
    if args.sport in ("nba","all"):
        if not run_script(predict_dir/"daily_bb.py", "🏀 篮球预测引擎"):
            errors.append("篮球预测")
    if args.sport in ("football","all"):
        if not run_script(predict_dir/"daily_fb.py", "⚽ 足球预测引擎"):
            errors.append("足球预测")
    if args.sport in ("nfl","all"):
        if not run_script(predict_dir/"daily_nfl.py", "🏈 NFL 预测引擎"):
            errors.append("NFL预测")
    # 跨运动统一排名（有推荐数据就跑）
    _run_ranking()
    if not args.skip_monitor:
        run_monitor()
    logger.info("\n%s", "="*60)
    if errors:
        logger.warning("⚠️  失败模块：%s", ', '.join(errors))
        sys.exit(1)
    else:
        logger.info("✅ 全部完成  %s", datetime.now().strftime('%H:%M:%S'))

if __name__ == "__main__":
    main()
