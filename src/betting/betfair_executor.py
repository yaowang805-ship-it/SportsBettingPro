"""Betfair Exchange REST API executor (stub).

Requires:
  - BETFAIR_API_KEY, BETFAIR_USERNAME, BETFAIR_PASSWORD in .env
  - Betfair API certificate (https://docs.developer.betfair.com/)

Full implementation requires:
  1. Certificate-based authentication (identitysso-cert.betfair.com)
  2. Market catalogue lookup via listMarketCatalogue
  3. Price fetching via listMarketBook
  4. Order placement via placeOrders
  5. Settlement via listClearedOrders
"""
from typing import Optional

from src.betting.base import BaseExecutor
from src.betting.models import BetOrder, BetResult
from config.logging_config import get_logger

logger = get_logger(__name__)


class BetfairExecutor(BaseExecutor):
    """Executor for Betfair Exchange (stub — requires API credentials)."""

    def __init__(self, api_key: str, username: str = "", password: str = ""):
        self.api_key = api_key
        self.username = username
        self.password = password
        self.session_token: Optional[str] = None
        logger.info("BetfairExecutor initialized (stub mode — place_bet not yet implemented)")

    def place_bet(self, order: BetOrder) -> BetResult:
        logger.warning("BetfairExecutor.place_bet is a stub — not implemented")
        return BetResult(
            prediction_id=order.prediction_id,
            external_id="",
            status="error",
            executed_odds=order.odds,
            executed_stake=0,
            error_message="BetfairExecutor not implemented — requires certificate-based auth setup",
        )

    def cancel_bet(self, external_id: str) -> bool:
        logger.warning("BetfairExecutor.cancel_bet is a stub")
        return False

    def get_bet_status(self, external_id: str) -> str:
        return "unknown"

    def settle_bet(self, external_id: str) -> Optional[BetResult]:
        return None

    def fetch_live_odds(self, sport: str, home: str, away: str,
                        market: str) -> Optional[float]:
        return None
