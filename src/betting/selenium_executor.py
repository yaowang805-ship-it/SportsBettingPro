"""Selenium-based executor for Chinese sportsbooks (stub).

Requires:
  - SELENIUM_PLATFORM_URL, SELENIUM_PLATFORM_USERNAME, SELENIUM_PLATFORM_PASSWORD in .env
  - ChromeDriver installed at SELENIUM_DRIVER_PATH

Full implementation requires platform-specific CSS selectors for:
  1. Login form (username/password fields, submit button)
  2. Match search/navigation
  3. Odds cells (click to add to bet slip)
  4. Stake input field
  5. Confirm/submit button
  6. Bet history for settlement checking
"""
from typing import Optional

from src.betting.base import BaseExecutor
from src.betting.models import BetOrder, BetResult
from config.logging_config import get_logger
from config.settings import SELENIUM_DRIVER_PATH

logger = get_logger(__name__)


class SeleniumExecutor(BaseExecutor):
    """Executor for Chinese sportsbooks via Selenium WebDriver (stub)."""

    def __init__(self, platform_url: str = "",
                 username: str = "", password: str = "",
                 driver_path: str = "", headless: bool = True):
        self.platform_url = platform_url
        self.username = username
        self.password = password
        self.driver_path = driver_path or SELENIUM_DRIVER_PATH
        self.headless = headless
        self.driver = None
        logger.info("SeleniumExecutor initialized (stub mode — place_bet not yet implemented)")

    def place_bet(self, order: BetOrder) -> BetResult:
        logger.warning("SeleniumExecutor.place_bet is a stub — requires platform-specific selectors")
        return BetResult(
            prediction_id=order.prediction_id,
            external_id="",
            status="error",
            executed_odds=order.odds,
            executed_stake=0,
            error_message="SeleniumExecutor not implemented — configure SELENIUM_PLATFORM_URL and selectors",
        )

    def cancel_bet(self, external_id: str) -> bool:
        logger.warning("SeleniumExecutor.cancel_bet is a stub")
        return False

    def get_bet_status(self, external_id: str) -> str:
        return "unknown"

    def settle_bet(self, external_id: str) -> Optional[BetResult]:
        return None

    def fetch_live_odds(self, sport: str, home: str, away: str,
                        market: str) -> Optional[float]:
        return None
