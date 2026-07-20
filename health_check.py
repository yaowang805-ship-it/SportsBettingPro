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
        ('config.dingtalk', 'DingTalk Direct'),
        ('src.scrapers.bb_api_fetcher', 'BB API Fetcher'),
        ('src.scrapers.bb_vs_pinnacle', 'BB vs Pinnacle Comparison'),
        ('src.report.bb_ev_push', '+EV Push Report'),
        ('src.betting.bb_virtual_bet', 'Virtual Betting'),
        ('src.scrapers.bb_incremental_scanner', 'Incremental Scanner'),
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
        ('data/storage/bb_odds_extracted.json', 'BB Extracted Odds'),
        ('data/storage/bb_vs_pinnacle_comparison.json', 'BB vs Pinnacle Comparison'),
        ('data/storage/pinnacle_league_structure.json', 'Pinnacle League Structure'),
        ('data/storage/team_name_map.json', 'Team Name Mappings'),
        ('data/storage/virtual_portfolio.json', 'Virtual Portfolio'),
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
        DINGTALK_WEBHOOK
    )

    logger.info("钉钉通知: %s", '已配置 ✅' if DINGTALK_WEBHOOK else '未配置 ❌')

    logger.info("=" * 80)
    return bool(DINGTALK_WEBHOOK)


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
        logger.info("  ./pipeline.sh scan       # 全量扫描+对比+推送")
        logger.info("  ./pipeline.sh daemon start  # 启动守护进程")
        return 0
    else:
        logger.error("\n❌ 系统检查失败，请检查上述错误")
        return 1


if __name__ == '__main__':
    sys.exit(main())
