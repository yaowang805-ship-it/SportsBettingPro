"""棒球 Line Shopping — 通过 the-odds-api 抓取 MLB 的 +EV 机会。

支持市场: h2h（独赢）/ spreads（让分盘）/ totals（大小分）
Pinnacle 覆盖 MLB，无 KBO/NPB。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR
from src.betting.sport_scanner import scan_and_save

logger = get_logger(__name__)

RESULTS_FILE = DATA_DIR / "mlb_line_shopping_results.json"

# MLB 只有 MLB 有 Pinnacle 覆盖
TRUSTED_BASEBALL_LEAGUES = {
    "baseball_mlb",
    "baseball_npb",  # 日本棒球，Pinnacle 覆盖 h2h+spreads+totals，16家零售
}


def scan_and_notify() -> int:
    """执行棒球扫描并保存 +EV 机会。"""
    from config.logging_config import setup_logging
    setup_logging()
    logger.info("=" * 60)
    logger.info("⚾ 棒球 Line Shopping 扫描")
    logger.info("=" * 60)
    return scan_and_save("baseball_", TRUSTED_BASEBALL_LEAGUES, RESULTS_FILE, "baseball")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="棒球 Line Shopping")
    parser.parse_args()
    from config.logging_config import setup_logging
    setup_logging()
    n = scan_and_notify()
    print(f"棒球: {n} 条")


if __name__ == "__main__":
    main()
