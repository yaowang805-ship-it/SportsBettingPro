"""网球 Line Shopping — 通过 the-odds-api 抓取网球比赛的 +EV 机会。

支持市场: h2h（独赢）/ spreads（让分盘）/ totals（大小分）
动态发现所有活跃的网球联赛（如 tennis_atp_wimbledon）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR
from src.betting.sport_scanner import scan_and_save

logger = get_logger(__name__)

RESULTS_FILE = DATA_DIR / "tennis_line_shopping_results.json"


def scan_and_notify() -> int:
    """执行网球扫描并保存 +EV 机会。"""
    from config.logging_config import setup_logging
    setup_logging()
    logger.info("=" * 60)
    logger.info("🎾 网球 Line Shopping 扫描")
    logger.info("=" * 60)
    # trusted_leagues=None = 动态发现所有活跃网球联赛
    return scan_and_save("tennis_", None, RESULTS_FILE, "tennis")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="网球 Line Shopping")
    parser.parse_args()
    from config.logging_config import setup_logging
    setup_logging()
    n = scan_and_notify()
    print(f"网球: {n} 条")


if __name__ == "__main__":
    main()
