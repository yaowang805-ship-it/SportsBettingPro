"""Betting orchestrator — ties RiskManager + Executor + PredictionLogger.

Execution flow:
  1. Receive recommendation details
  2. Query RiskManager.can_place_bet() → if denied, skip
  3. Create BetOrder from recommendation
  4. Call executor.place_bet(order)
     - Internally re-fetches live odds and validates slippage
  5. On success: log prediction, track active order
  6. On failure: log error, return rejected BetResult
  7. Periodic settle_active_bets() checks all active orders
"""
from typing import List

from config.logging_config import get_logger
try:
    from config.settings import BETTING_PLATFORM
except ImportError:
    BETTING_PLATFORM = "manual"  # 已废弃，兼容旧代码
from src.betting.base import BaseExecutor
from src.betting.models import BetOrder, BetResult
from src.risk.manager import RiskManager

logger = get_logger(__name__)


class BettingOrchestrator:
    """Central orchestrator for bet execution lifecycle."""

    def __init__(self, executor: BaseExecutor, risk_manager: RiskManager):
        self.executor = executor
        self.risk_manager = risk_manager
        self._active_orders: List[BetOrder] = []

    def execute_recommendation(
        self,
        sport: str,
        league: str,
        home_team: str,
        away_team: str,
        market_type: str,
        market_detail: str,
        odds: float,
        model_prob: float,
        market_prob: float,
        ev: float,
        stake: float,
        match_time=None,
        source: str = "global_top5",
    ) -> BetResult:
        """Execute a single recommendation through the full pipeline.

        Returns:
            BetResult with status and external_id.
        """
        # Step 1: Risk check
        current_exposure = sum(o.stake for o in self._active_orders) / \
                           max(self.risk_manager.current_balance, 1)
        can_bet, reason = self.risk_manager.can_place_bet(stake, current_exposure)
        if not can_bet:
            logger.warning("Bet rejected by RiskManager: %s [%s vs %s, %.0f¥]",
                           reason, home_team, away_team, stake)
            return BetResult(
                prediction_id="",
                external_id="",
                status="rejected",
                executed_odds=odds,
                executed_stake=stake,
                error_message=reason,
            )

        # Step 2: Log prediction to get prediction_id
        from src.core.prediction_logger import log_prediction
        prediction_id = log_prediction(
            sport=sport, league=league,
            home_team_cn=home_team, away_team_cn=away_team,
            market_type=market_type, market_detail=market_detail,
            odds=odds, model_prob=model_prob,
            market_prob=market_prob, ev=ev, stake=stake,
            match_time=match_time, source=source,
            home_team_en=home_team, away_team_en=away_team,
        )

        # Step 3: Create BetOrder
        order = BetOrder(
            prediction_id=prediction_id,
            platform=BETTING_PLATFORM,
            sport=sport, league=league,
            home_team=home_team, away_team=away_team,
            market_type=market_type, market_detail=market_detail,
            odds=odds, stake=stake,
            model_prob=model_prob, market_prob=market_prob,
            ev=ev, match_time=match_time,
        )

        # Step 4: Execute
        try:
            result = self.executor.place_bet(order)
        except Exception as e:
            logger.exception("Bet execution failed: %s", e)
            result = BetResult(
                prediction_id=prediction_id,
                external_id="",
                status="error",
                executed_odds=odds,
                executed_stake=0,
                error_message=str(e),
            )

        # Step 5: Handle result
        if result.status == "accepted":
            logger.info("Bet placed: %s %s vs %s @ %.2f | stake=%.0f | id=%s",
                        league, home_team, away_team, odds, stake, result.external_id)
            order.external_id = result.external_id
            self._active_orders.append(order)
            self.risk_manager.correlation_filter.add_bet(
                sport, home_team, away_team, market_type
            )
        else:
            logger.error("Bet failed: %s %s vs %s | reason=%s",
                         league, home_team, away_team, result.error_message)

        return result

    def settle_active_bets(self) -> List[BetResult]:
        """Check all active orders for settlement.

        Returns:
            List of BetResult for newly settled bets.
        """
        settled = []
        for order in list(self._active_orders):
            if not order.external_id:
                self._active_orders.remove(order)
                continue

            try:
                result = self.executor.settle_bet(order.external_id)
            except Exception as e:
                logger.error("Settlement check failed for %s: %s",
                             order.external_id, e)
                continue

            if result and result.status in ("won", "lost"):
                self.risk_manager.record_outcome(
                    stake=result.executed_stake,
                    win=(result.status == "won"),
                    odds=result.executed_odds,
                    prob=order.model_prob,
                    sport=order.sport or "",
                    home_team=order.home_team or "",
                    away_team=order.away_team or "",
                )
                from src.core.prediction_logger import settle_prediction
                settle_prediction(
                    order.prediction_id,
                    won=(result.status == "won"),
                    result_odds=result.executed_odds,
                )
                settled.append(result)
                self._active_orders.remove(order)
                logger.info("Bet settled: %s -> %s (profit=%.0f)",
                            order.external_id, result.status, result.profit)

        return settled

    @property
    def active_count(self) -> int:
        return len(self._active_orders)

    @property
    def active_exposure(self) -> float:
        return sum(o.stake for o in self._active_orders)
