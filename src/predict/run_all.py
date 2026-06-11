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
import subprocess
import sys
import argparse
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
    timeout = 600 if "football" in description else 300
    try:
        subprocess.run([sys.executable, str(script_path)], check=True, text=True, timeout=timeout)
        logger.info("✅ 完成：%s", description)
        return True
    except subprocess.TimeoutExpired:
        logger.warning("⏰ 超时（%d秒）：%s", timeout, description)
    except subprocess.CalledProcessError as e:
        logger.error("❌ 失败（退出码 %s）：%s", e.returncode, description)
    return False

def run_monitor():
    logger.info("\n%s\n▶  赛后监控 & 健康度检查\n%s", "─" * 60, "─" * 60)
    # 自动结算（virtual_portfolio 待处理投注）
    try:
        from src.monitor.auto_settle import auto_settle
        n2 = auto_settle()
        if n2:
            logger.info("✅ 虚拟组合结算: %s 条", n2)
    except Exception as e:
        logger.warning("⚠️  虚拟组合结算跳过: %s", e)
    # 自动结算（prediction_log 记录）
    try:
        from src.core.prediction_logger import batch_settle
        n = batch_settle()
        if n:
            logger.info("✅ 预测日志结算: %s 条", n)
    except Exception as e:
        logger.warning("⚠️  预测日志结算跳过: %s", e)
    try:
        from src.monitor.performance import update_performance
        update_performance()
        logger.info("✅ 赛后盈亏监控完成")
        # 组合归因更新
        try:
            from src.risk.attribution import compute_and_save
            compute_and_save()
            logger.info("✅ 组合归因已更新")
        except Exception:
            pass
    except Exception as e:
        logger.warning("⚠️  赛后监控失败：%s", e)
    # CLV 收盘价填充（赛前窗口内捕获收盘赔率）
    try:
        from src.monitor.clv_tracker import refresh_closing_odds
        cr = refresh_closing_odds()
        if cr.get("updated"):
            logger.info("✅ CLV 收盘价: %d 条已更新", cr["updated"])
    except Exception as e:
        logger.warning("⚠️  CLV 收盘价填充跳过: %s", e)
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
    # 模拟交易报告（只读聚合，不修改任何文件）
    try:
        from src.betting.paper_trader import PaperTrader
        pt = PaperTrader()
        state = pt.run()
        if state.get("readiness", {}).get("ready"):
            logger.info("")
            logger.info("=" * 60)
            logger.info("  ✅ PAPER TRADING: READY FOR LIVE TRADING!")
            logger.info("  All readiness checks passed. Review report and enable live trading.")
            logger.info("=" * 60)
    except Exception as e:
        logger.warning("Paper trading report skipped: %s", e)
    # 数据库同步（CSV/JSON → SQLAlchemy 表）
    try:
        import subprocess
        sync_script = ROOT / "scripts" / "sync_db.py"
        if sync_script.exists():
            subprocess.run([sys.executable, str(sync_script)], check=True,
                           capture_output=True, text=True, timeout=60)
            logger.info("✅ DB 同步完成")
    except Exception as e:
        logger.warning("⚠️  DB 同步跳过: %s", e)

def _run_ranking():
    """跨运动统一排名（非阻塞，失败不影响主流程）。"""
    try:
        from src.predict.rank_recommendations import main as rank_main
        rank_main()
    except Exception as e:
        logger.warning("⚠️  统一排名跳过: %s", e)


def _run_prematch_check(sport=None):
    """赛前赔率重检入口。"""
    from src.monitor.prematch_check import run_prematch_check
    run_prematch_check(sport=sport)


def main():
    parser = argparse.ArgumentParser(description="SportsBettingPro 每日预测入口")
    parser.add_argument("--sport", choices=["nba","football","nfl","wc","all"], default="all")
    parser.add_argument("--skip-monitor", action="store_true")
    parser.add_argument("--simulate", action="store_true", help="运行世界杯蒙特卡洛模拟")
    parser.add_argument("--prematch-check", action="store_true", help="赛前1-2小时赔率重检")
    args = parser.parse_args()

    if args.prematch_check:
        _run_prematch_check(sport=args.sport if args.sport != "all" else None)
        return

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
    if args.sport in ("wc","all"):
        # 世界杯期间自动运行（6月11日-7月19日）
        if not run_script(predict_dir/"daily_wc.py", "🌍 世界杯预测引擎"):
            errors.append("世界杯预测")
        if args.simulate:
            if not run_script(predict_dir/"wc_simulation.py", "🌍 世界杯蒙特卡洛模拟"):
                errors.append("世界杯模拟")
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
