"""Betting data models — BetOrder, BetResult dataclasses."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BetOrder:
    """A bet order ready for execution."""
    prediction_id: str
    platform: str                # "selenium" | "betfair"
    sport: str
    league: str
    home_team: str
    away_team: str
    market_type: str             # WIN / SPREAD / TOTAL / H2H
    market_detail: str           # e.g. "主胜", "主队 -5.5"
    odds: float
    stake: float                 # in CNY
    model_prob: float
    market_prob: float
    ev: float
    match_time: Optional[datetime] = None
    external_id: str = ""        # Bookie's bet ID, filled after placement
    placed_at: Optional[datetime] = None
    status: str = "pending"      # pending / accepted / rejected / error
    error_message: str = ""


@dataclass
class BetResult:
    """Result of a bet execution or settlement."""
    prediction_id: str
    external_id: str
    status: str                  # accepted / rejected / won / lost / void / error
    executed_odds: float
    executed_stake: float
    profit: float = 0.0
    settled_at: Optional[datetime] = None
    error_message: str = ""
