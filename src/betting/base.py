"""Abstract base class for all betting executors."""
from abc import ABC, abstractmethod
from typing import Optional

from src.betting.models import BetOrder, BetResult


class BaseExecutor(ABC):
    """Abstract base class for betting execution platforms.

    Subclasses implement platform-specific logic for:
      - Betfair Exchange (REST API)
      - Chinese sportsbooks (Selenium automation)
    """

    @abstractmethod
    def place_bet(self, order: BetOrder) -> BetResult:
        """Place a single bet.

        Before placing, implementations should:
          1. Re-fetch current odds for the match
          2. Compare to recommended odds
          3. Reject if slippage exceeds MAX_ODDS_SLIPPAGE
        """
        ...

    @abstractmethod
    def cancel_bet(self, external_id: str) -> bool:
        """Cancel an unfilled bet."""
        ...

    @abstractmethod
    def get_bet_status(self, external_id: str) -> str:
        """Check bet status (won/lost/void/pending)."""
        ...

    @abstractmethod
    def settle_bet(self, external_id: str) -> Optional[BetResult]:
        """Settle a completed bet and return the result.

        Returns:
            BetResult with profit/loss filled, or None if unsettled.
        """
        ...

    @abstractmethod
    def fetch_live_odds(self, sport: str, home: str, away: str,
                        market: str) -> Optional[float]:
        """Re-fetch live odds for pre-bet validation.

        Returns:
            Current best odds decimal, or None if unavailable.
        """
        ...

    def validate_odds(self, recommended_odds: float,
                      live_odds: float,
                      max_slippage: float = 0.05) -> tuple:
        """Validate that live odds have not moved beyond tolerance.

        Args:
            recommended_odds: Odds at recommendation time
            live_odds: Current live market odds
            max_slippage: Maximum relative change allowed (default 5%)

        Returns:
            (is_valid: bool, reason: str)
        """
        if live_odds <= 0:
            return False, "活盘赔率不可用"
        change = abs(live_odds - recommended_odds) / max(recommended_odds, 0.01)
        if change > max_slippage:
            return False, (
                f"赔率偏差 {change:.1%} 超过上限 {max_slippage:.1%} "
                f"(推荐 {recommended_odds:.2f}, 当前 {live_odds:.2f})"
            )
        # Favorable movement (odds improved) is always OK
        if live_odds >= recommended_odds:
            return True, "赔率有利变动"
        return True, "赔率在容忍范围内"
