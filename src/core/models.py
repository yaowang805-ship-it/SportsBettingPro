from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass(frozen=True)
class Odds:
    home_odds: float
    spread_point: float
    spread_odds: float
    total_point: float
    over_odds: float
    bookmaker: str = ""       # 最优赔率来源博彩公司
    spread_bookmaker: str = ""  # 最优让分盘来源
    total_bookmaker: str = ""  # 最优大小球来源

@dataclass(frozen=True)
class Match:
    date: datetime
    home_team: str
    away_team: str
    odds: Optional[Odds] = None
    home_score: Optional[int] = None
    away_score: Optional[int] = None

    @property
    def home_win(self) -> Optional[bool]:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score > self.away_score
