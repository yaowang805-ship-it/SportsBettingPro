#!/usr/bin/env python3
"""系统完整性检查。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

from config.logging_config import get_logger
logger = get_logger(__name__)


def check_imports():
    """检查所有关键模块是否可导入。"""
    modules = [
        ('config.settings', 'Core Configuration'),
        ('src.core.risk', 'Risk Calculation'),
        ('src.core.evaluation', 'Performance Evaluation'),
        ('src.features.bb_pipeline', 'Basketball Features'),
        ('src.features.football_pipeline', 'Football Features'),
        ('src.features.tournament_pipeline', 'Tournament Features'),
        ('src.models.train_models', 'Model Training'),
        ('src.models.auto_retrain', 'Auto Retrain'),
        ('src.backtest.backtest_runner', 'Backtest Engine'),
        ('src.risk.manager', 'Risk Manager'),
        ('src.predict.semi_auto_bettor', 'Semi Auto Bettor'),
    ]

    logger.info("=" * 80)
    logger.info("🔍 系统模块完整性检查")
    logger.info("=" * 80)

    all_ok = True
    for module_name, description in modules:
        try:
            __import__(module_name)
            logger.info("✅ %-40s (%s)", description, module_name)
        except Exception as e:
            logger.error("❌ %-40s (%s)", description, module_name)
            logger.error("   错误: %s", e)
            all_ok = False

    logger.info("=" * 80)
    return all_ok


def check_data_files():
    """检查关键数据文件。"""
    files = [
        ('data/processed/bb_features.csv', 'NBA Features'),
        ('data/processed/fb_features.csv', 'Football Features'),
        ('models/model_bb_win_ensemble.pkl', 'NBA Win Ensemble'),
        ('models/model_bb_spread_result_ensemble.pkl', 'NBA Spread Ensemble'),
        ('models/model_bb_total_result_ensemble.pkl', 'NBA Total Ensemble'),
        ('models/model_fb_win_ensemble.pkl', 'FB Win Ensemble'),
        ('models/model_fb_spread_result_ensemble.pkl', 'FB Spread Ensemble'),
        ('models/model_fb_total_result_ensemble.pkl', 'FB Total Ensemble'),
        ('models/model_fb_features.json', 'Football Features List'),
        ('models/model_bb_features.json', 'NBA Features List'),
        ('models/model_metadata.json', 'Model Metadata'),
    ]

    logger.info("\n📂 数据文件检查")
    logger.info("=" * 80)

    all_exist = True
    for file_path, description in files:
        p = Path(file_path)
        if p.exists():
            size = p.stat().st_size / (1024 * 1024)  # MB
            logger.info("✅ %-40s (%.1f MB)", description, size)
        else:
            logger.warning("⚠️  %-40s (未找到)", description)
            all_exist = False

    logger.info("=" * 80)
    return all_exist


def check_config():
    """检查配置。"""
    logger.info("\n⚙️  配置检查")
    logger.info("=" * 80)

    from config.settings import (
        DEFAULT_BUDGET, MAX_SINGLE_BET_PCT, KELLY_FRACTION,
        MIN_EDGE, ODDS_API_KEY, DINGTALK_WEBHOOK
    )

    logger.info("初始资金: %s", DEFAULT_BUDGET)
    logger.info("单注最高: %.1f%%", MAX_SINGLE_BET_PCT * 100)
    logger.info("凯利系数: %s", KELLY_FRACTION)
    logger.info("最小 EV: %.1f%%", MIN_EDGE * 100)
    logger.info("API Key: %s", '已配置 ✅' if ODDS_API_KEY else '未配置 ❌')
    logger.info("钉钉通知: %s", '已配置 ✅' if DINGTALK_WEBHOOK else '未配置 ❌')

    logger.info("=" * 80)
    return ODDS_API_KEY and DINGTALK_WEBHOOK


def main():
    results = {
        'imports': check_imports(),
        'files': check_data_files(),
        'config': check_config(),
    }

    logger.info("\n" + "=" * 80)
    logger.info("📊 检查结果摘要")
    logger.info("=" * 80)
    logger.info("模块导入: %s", '✅ 通过' if results['imports'] else '❌ 失败')
    logger.warning("数据文件: %s", '✅ 齐全' if results['files'] else '⚠️  不完整')
    logger.info("配置文件: %s", '✅ 已配置' if results['config'] else '❌ 缺失')
    logger.info("=" * 80)

    if all(results.values()):
        logger.info("\n✅ 系统已准备好！可以运行：")
        logger.info("  bash quick_start.sh  # 快速启动")
        logger.info("  python main.py       # 日常自动化")
        logger.info("  python src/predict/semi_auto_bettor.py  # 半自动投注审核")
        return 0
    else:
        logger.error("\n❌ 系统检查失败，请检查上述错误")
        return 1


if __name__ == '__main__':
    sys.exit(main())
