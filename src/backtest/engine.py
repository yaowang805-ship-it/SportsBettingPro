import numpy as np
from typing import List, Tuple
from datetime import datetime
import sys
sys.path.append('.')
from src.core.models import Match

class BacktestEngine:
    """独立回测引擎，只依赖标准化的 Match 对象"""
    
    def __init__(self, initial_capital: float = 1000, kelly_fraction: float = 0.25):
        self.initial = initial_capital
        self.kelly_frac = kelly_fraction
        
    def run(self, matches: List[Match], predictions: List[float]) -> dict:
        """
        执行回测
        matches: 按时间排序的标准化比赛列表
        predictions: 对应每场比赛的主队胜率预测 (0~1)
        """
        capital = self.initial
        equity = [capital]
        bet_count = 0
        win_count = 0
        
        for match, prob in zip(matches, predictions):
            # 跳过无赔率或无结果的比赛
            if not match.odds or not match.is_finished:
                equity.append(capital)
                continue
                
            odds = match.odds.home_odds
            # 计算凯利
            edge = prob - (1.0 / odds)
            
            if edge > 0.02:  # 至少2%的优势才下注
                kelly = (prob * odds - 1) / (odds - 1)
                stake_frac = min(max(kelly * self.kelly_frac, 0), 0.02)
                stake = capital * stake_frac
                
                # 结算
                if match.home_win:
                    capital += stake * (odds - 1)
                    win_count += 1
                else:
                    capital -= stake
                bet_count += 1
            
            equity.append(capital)
            
        # 计算绩效指标
        returns = np.diff(equity) / equity[:-1]
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(len(returns)) if len(returns) > 0 else 0
        max_drawdown = np.max(np.maximum.accumulate(equity) - equity)
        total_return = (capital - self.initial) / self.initial
        
        return {
            "final_capital": capital,
            "total_return": total_return,
            "sharpe_ratio": sharpe,
            "max_drawdown": max_drawdown,
            "bet_count": bet_count,
            "win_rate": win_count / bet_count if bet_count else 0,
            "equity_curve": equity
        }
