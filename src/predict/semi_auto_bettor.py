#!/usr/bin/env python3
"""半自动下单系统：生成推荐、等待人工审核、记录结果。"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from config.settings import DATA_DIR
from src.risk.manager import RiskManager

DATA_DIR.mkdir(parents=True, exist_ok=True)
RECOMMENDATIONS_FILE = DATA_DIR / 'daily_recommendations.json'
BET_HISTORY_FILE = DATA_DIR / 'bet_history.csv'


class SemiAutoBettor:
    """半自动投注系统。"""
    
    def __init__(self):
        self.risk_manager = RiskManager()
        self.recommendations = []
        self.pending_bets = []
    
    def load_recommendations(self):
        """加载每日推荐。"""
        if RECOMMENDATIONS_FILE.exists():
            with open(RECOMMENDATIONS_FILE, encoding='utf-8') as f:
                data = json.load(f)
                self.recommendations = data.get('recommendations', [])
        return self.recommendations
    
    def display_recommendations(self):
        """显示所有推荐的下注。"""
        self.load_recommendations()
        if not self.recommendations:
            print("今日无推荐")
            return
        
        print("=" * 80)
        print(f"📊 今日投注推荐 ({len(self.recommendations)} 条)")
        print("=" * 80)
        
        current_exposure = 0.0
        for i, rec in enumerate(self.recommendations[:10], 1):
            max_stake = self.risk_manager.get_max_stake(rec.get('edge', 0.06), rec.get('odds', 2.0))
            can_bet, msg = self.risk_manager.can_place_bet(max_stake, current_exposure)
            current_exposure += max_stake / self.risk_manager.current_balance
            
            status = "✅" if can_bet else "⛔"
            print(f"\n{status} [{i}] {rec.get('home_cn')} vs {rec.get('away_cn')}")
            print(f"    市场: {rec.get('market')} | 赔率: {rec.get('odds'):.2f}")
            print(f"    模型概率: {rec.get('model_prob'):.1%} | 市场概率: {rec.get('market_prob'):.1%}")
            print(f"    期望值: +{rec.get('edge', 0):.1%} | 建议注额: {max_stake:.0f}")
            if not can_bet:
                print(f"    ⚠️ {msg}")
    
    def record_bet(self, rec_index: int, actual_result: str = 'pending', notes: str = ''):
        """记录一个下注。"""
        if rec_index < 0 or rec_index >= len(self.recommendations):
            print(f"❌ 索引 {rec_index} 超出范围")
            return False
        
        rec = self.recommendations[rec_index]
        stake = self.risk_manager.get_max_stake(rec.get('edge', 0.06), rec.get('odds', 2.0))
        
        bet_record = {
            'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'home': rec.get('home_cn'),
            'away': rec.get('away_cn'),
            'market': rec.get('market'),
            'odds': rec.get('odds'),
            'model_prob': rec.get('model_prob'),
            'edge': rec.get('edge'),
            'stake': stake,
            'result': actual_result,
            'pnl': 0.0,
            'notes': notes,
        }
        
        # 计算损益
        if actual_result == 'won':
            bet_record['pnl'] = stake * (rec.get('odds', 2.0) - 1.0)
            self.risk_manager.record_outcome(stake, True, rec.get('odds', 2.0))
        elif actual_result == 'lost':
            bet_record['pnl'] = -stake
            self.risk_manager.record_outcome(stake, False)
        elif actual_result == 'pending':
            bet_record['pnl'] = 0.0
        
        # 保存到历史
        if BET_HISTORY_FILE.exists():
            history = pd.read_csv(BET_HISTORY_FILE)
        else:
            history = pd.DataFrame()
        
        history = pd.concat([history, pd.DataFrame([bet_record])], ignore_index=True)
        history.to_csv(BET_HISTORY_FILE, index=False, encoding='utf-8')
        
        print(f"✅ 已记录投注 #{rec_index}: {rec.get('home_cn')} vs {rec.get('away_cn')} | 注额: {stake:.0f}")
        return True
    
    def show_portfolio_status(self):
        """显示当前投送组合状态。"""
        health = self.risk_manager.get_health_check()
        print("\n" + "=" * 80)
        print("💰 投资组合状态")
        print("=" * 80)
        print(f"账户余额: {health['balance']:.2f}")
        print(f"投资回报率 (ROI): {health['roi']:+.2%}")
        print(f"最大回撤: {health['drawdown']:.2%}")
        print(f"日限检查 (−10%): {'✅ 通过' if health['under_daily_limit'] else '❌ 超限'}")
        print(f"月限检查 (−25%): {'✅ 通过' if health['under_monthly_limit'] else '❌ 超限'}")


def interactive_mode():
    """交互模式：显示推荐、接收用户输入。"""
    bettor = SemiAutoBettor()
    
    while True:
        print("\n" + "=" * 80)
        print("📋 半自动投注系统")
        print("=" * 80)
        print("[1] 显示今日推荐")
        print("[2] 记录投注结果")
        print("[3] 查看投资组合状态")
        print("[4] 退出")
        
        choice = input("\n请选择操作 (1-4): ").strip()
        
        if choice == '1':
            bettor.display_recommendations()
        elif choice == '2':
            idx = input("输入推荐索引 (0-based): ").strip()
            result = input("投注结果 (won/lost/pending): ").strip().lower()
            notes = input("备注 (可选): ").strip()
            if result in ['won', 'lost', 'pending']:
                bettor.record_bet(int(idx), result, notes)
            else:
                print("❌ 无效结果")
        elif choice == '3':
            bettor.show_portfolio_status()
        elif choice == '4':
            print("👋 再见")
            break
        else:
            print("❌ 无效选择")


if __name__ == '__main__':
    interactive_mode()
