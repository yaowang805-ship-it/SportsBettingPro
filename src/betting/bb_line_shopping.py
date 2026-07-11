"""篮球 Line Shopping — 通过 the-odds-api 抓取 WNBA 等篮球联赛的 +EV 机会。

支持市场: h2h（独赢）/ spreads（让分盘）/ totals（大小分盘）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR
from src.betting.sport_scanner import scan_and_save

logger = get_logger(__name__)

RESULTS_FILE = DATA_DIR / "bb_line_shopping_results.json"

# 可信篮球联赛（有 Pinnacle 覆盖的成熟联赛）
TRUSTED_BB_LEAGUES = {
    "basketball_wnba",
    "basketball_nba",
    "basketball_nba_preseason",
    "basketball_euroleague",
    "basketball_nbl",
}


def scan_and_notify() -> int:
    """执行篮球扫描并保存 +EV 机会。"""
    from config.logging_config import setup_logging
    setup_logging()
    logger.info("=" * 60)
    logger.info("🏀 篮球 Line Shopping 扫描")
    logger.info("=" * 60)
    return scan_and_save("basketball_", TRUSTED_BB_LEAGUES, RESULTS_FILE, "basketball")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="篮球 Line Shopping")
    parser.parse_args()
    from config.logging_config import setup_logging
    setup_logging()
    n = scan_and_notify()
    print(f"篮球: {n} 条")


if __name__ == "__main__":
    main()
