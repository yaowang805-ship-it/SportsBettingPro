"""SportsBettingPro Betting Execution API.

Usage:
    from src.betting import get_executor, BettingOrchestrator
    from src.risk.manager import RiskManager

    executor = get_executor('selenium')  # or 'betfair'
    orchestrator = BettingOrchestrator(executor, RiskManager())
    result = orchestrator.execute_recommendation(
        sport='NBA', league='NBA',
        home_team='湖人', away_team='凯尔特人',
        market_type='WIN', market_detail='主胜',
        odds=2.10, model_prob=0.52, market_prob=0.48,
        ev=0.04, stake=50, match_time=None,
    )
"""
from typing import Optional

from config.logging_config import get_logger
from config.settings import (
    BETTING_PLATFORM,
    BETFAIR_API_KEY, BETFAIR_USERNAME, BETFAIR_PASSWORD,
    SELENIUM_PLATFORM_URL, SELENIUM_PLATFORM_USERNAME, SELENIUM_PLATFORM_PASSWORD,
)
from src.betting.base import BaseExecutor

logger = get_logger(__name__)


def get_executor(platform: Optional[str] = None) -> BaseExecutor:
    """Factory: returns the appropriate executor based on platform config.

    Args:
        platform: Override BETTING_PLATFORM setting ("selenium" | "betfair").

    Returns:
        BaseExecutor instance.

    Raises:
        ValueError: If platform is unknown or credentials are missing.
    """
    platform = platform or BETTING_PLATFORM

    if platform == "betfair":
        if not BETFAIR_API_KEY:
            raise ValueError("BETFAIR_API_KEY not configured. Set it in .env file.")
        # Lazy import to avoid heavy dependencies
        from src.betting.betfair_executor import BetfairExecutor
        return BetfairExecutor(
            username=BETFAIR_USERNAME or "",
            password=BETFAIR_PASSWORD or "",
            app_key=BETFAIR_API_KEY,
        )

    elif platform == "selenium":
        if not SELENIUM_PLATFORM_URL:
            logger.warning("SELENIUM_PLATFORM_URL not configured, using selenium executor in dry-run mode")
        from src.betting.selenium_executor import SeleniumExecutor
        return SeleniumExecutor(
            platform_url=SELENIUM_PLATFORM_URL,
            username=SELENIUM_PLATFORM_USERNAME,
            password=SELENIUM_PLATFORM_PASSWORD,
        )

    else:
        raise ValueError(f"Unknown betting platform: {platform}")
