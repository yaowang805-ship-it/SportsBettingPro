#!/usr/bin/env python3
"""职业级风险管理与仓位控制系统 — 增强版。"""
import json
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

import numpy as np
import pandas as pd

from config.settings import DEFAULT_BUDGET, KELLY_FRACTION, MAX_SINGLE_BET_PCT, MAX_TOTAL_EXPOSURE, DATA_DIR
from src.risk.model_decay_tracker import ModelDecayTracker
from src.risk.dynamic_staking import DynamicStakingModel

RISK_STATE_FILE = DATA_DIR / 'risk_state.json'
BET_LOG_FILE = DATA_DIR / 'bet_history.csv'
BLOCKLIST_PATH = ROOT / "models" / "market_blocklist.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class AdaptiveKelly:
    """自适应凯利系数 — 根据近期表现动态调整。"""
    def __init__(self, base=0.25, window=20, low=0.1, high=0.4):
        self.base = base
        self.window = deque(maxlen=window)
        self.low = low
        self.high = high

    def update(self, prob, outcome):
        self.window.append(np.log(prob) if outcome == 1 else np.log(1 - prob))

    def fraction(self):
        if len(self.window) < 5:
            return self.base
        avg = np.mean(self.window)
        adj = np.clip(2 * (avg + 0.5), 0.5, 1.5)
        return np.clip(self.base * adj, self.low, self.high)


class PortfolioOptimizer:
    """组合优化器 — Markowitz 协方差模型用于多比赛同时下注。

    职业博彩的关键区别：
      - 业余：单独计算每笔下注的凯利仓位
      - 专业：考虑所有同时下注的相关性后优化仓位

    相关性假设（基于研究文献）：
      - 同一场比赛不同盘口: ρ = 0.8~0.95
      - 同一联赛不同比赛: ρ = 0.15~0.3
      - 同一运动不同联赛: ρ = 0.05~0.1
      - 不同运动: ρ = 0.0~0.05
    """

    # 相关性基数矩阵
    SPORT_CORR = {
        "nba": {"nba": 1.0, "nfl": 0.15, "mlb": 0.1, "nhl": 0.1, "soccer": 0.05, "tennis": 0.02, "other": 0.03},
        "nfl": {"nfl": 1.0, "nba": 0.15, "soccer": 0.05, "other": 0.03},
        "soccer": {"soccer": 1.0, "nba": 0.05, "nfl": 0.05, "other": 0.03},
        "other": {"other": 1.0},
    }

    LEAGUE_CORR_BONUS = {
        "同一联赛": 0.15,
        "同级联赛": 0.08,
        "不同联赛": 0.0,
    }

    def __init__(self):
        self.active_bets: List[Dict] = []

    def _sport_group(self, sport: str) -> str:
        s = sport.lower()
        if "nba" in s or "basketball" in s or "ncaa" in s:
            return "nba"
        if "soccer" in s or "epl" in s or "liga" in s or "serie" in s or "bundes" in s or "ligue" in s:
            return "soccer"
        if "nfl" in s or "football" in s:
            return "nfl"
        return "other"

    def _league_bonus(self, league1: str, league2: str) -> float:
        if not league1 or not league2:
            return 0.0
        if league1 == league2:
            return 0.15
        # 同级别联赛（如五大联赛之间）
        top5 = {"英超", "西甲", "德甲", "意甲", "法甲"}
        if league1 in top5 and league2 in top5:
            return 0.08
        return 0.0

    def _estimated_corr(self, bet1: Dict, bet2: Dict) -> float:
        """估算两笔投注之间的相关系数。"""
        # 同场比赛 → 高度相关
        if (bet1.get("home_team") == bet2.get("home_team") and
                bet1.get("away_team") == bet2.get("away_team") and
                bet1.get("sport") == bet2.get("sport")):
            # 同市场类型 → 几乎完美相关
            if bet1.get("market", "") == bet2.get("market", ""):
                return 0.95
            return 0.80

        # 不同比赛
        sg1 = self._sport_group(bet1.get("sport", ""))
        sg2 = self._sport_group(bet2.get("sport", ""))
        base_corr = self.SPORT_CORR.get(sg1, {}).get(sg2, 0.03)

        # 同联赛加成
        league_bonus = self._league_bonus(
            bet1.get("league", ""), bet2.get("league", ""))
        return min(1.0, base_corr + league_bonus)

    def add_bet(self, bet_info: Dict):
        """添加一笔投注到活跃组合。"""
        self.active_bets.append(bet_info)

    def remove_bet(self, bet_id: str):
        self.active_bets = [b for b in self.active_bets if b.get("id") != bet_id]

    def clear(self):
        self.active_bets.clear()

    def compute_correlation_matrix(self) -> np.ndarray:
        """计算当前组合的相关系数矩阵。"""
        n = len(self.active_bets)
        if n == 0:
            return np.array([])
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                c = self._estimated_corr(self.active_bets[i], self.active_bets[j])
                corr[i, j] = c
                corr[j, i] = c
        return corr

    def portfolio_variance(self, stakes: np.ndarray) -> float:
        """计算组合方差（给定仓位比例）。"""
        if len(self.active_bets) == 0:
            return 0.0
        corr = self.compute_correlation_matrix()
        # 假设每笔投注的方差为 p*(1-p)
        variances = np.array([
            b.get("model_prob", 0.5) * (1 - b.get("model_prob", 0.5))
            for b in self.active_bets
        ])
        cov_matrix = np.outer(np.sqrt(variances), np.sqrt(variances)) * corr
        return float(stakes.T @ cov_matrix @ stakes)

    def diversification_score(self) -> float:
        """组合分散度评分（0~1, 越高越分散）。"""
        if len(self.active_bets) <= 1:
            return 1.0
        corr = self.compute_correlation_matrix()
        # 取上三角的平均值
        n = corr.shape[0]
        upper_tri = [corr[i, j] for i in range(n) for j in range(i + 1, n)]
        avg_corr = np.mean(upper_tri) if upper_tri else 0
        return max(0.0, 1.0 - avg_corr)

    def correlation_adjusted_max_stake(self, stake: float, max_single_pct: float,
                                        current_exposure: float) -> float:
        """根据组合分散度调整最大仓位。"""
        ds = self.diversification_score()
        if ds < 0.3:  # 高度集中
            adj = 0.5
        elif ds < 0.6:  # 中度分散
            adj = 0.75
        else:
            adj = 1.0  # 高度分散

        return min(stake, max_single_pct * (1 - current_exposure)) * adj

    def solve_kelly_portfolio(self, bankroll: float = 10000,
                              max_single_pct: float = 0.05,
                              max_total_pct: float = 0.30) -> dict:
        """用 KellyPortfolioOptimizer 求解真正的组合优化。

        替代启发式分散度调整，直接求解联合 Kelly 最优分配。

        Returns:
            {"weights": np.ndarray, "allocations": [...], "meta": {...}}
        """
        from src.risk.portfolio import KellyPortfolioOptimizer

        if not self.active_bets:
            return {"weights": np.array([]), "allocations": []}

        kelly_inputs = []
        for bet in self.active_bets:
            prob = bet.get("model_prob", 0.5)
            odds = bet.get("odds", 2.0)
            if prob <= 0 or odds <= 1.0:
                continue
            kelly_inputs.append({"prob": prob, "odds": odds})

        if not kelly_inputs:
            return {"weights": np.array([]), "allocations": []}

        opt = KellyPortfolioOptimizer(max_single=max_single_pct, max_total=max_total_pct)

        if len(kelly_inputs) > 1:
            corr = self.compute_correlation_matrix()
            if corr.shape == (len(kelly_inputs), len(kelly_inputs)):
                weights, meta = opt.solve_with_correlation(kelly_inputs, corr)
            else:
                weights, meta = opt.solve(kelly_inputs)
        else:
            weights, meta = opt.solve(kelly_inputs)

        allocations = []
        for i, bet in enumerate(self.active_bets):
            if i < len(weights) and weights[i] > 0:
                allocations.append({
                    "sport": bet.get("sport", ""),
                    "home_team": bet.get("home_team", ""),
                    "away_team": bet.get("away_team", ""),
                    "market": bet.get("market", ""),
                    "weight_pct": round(float(weights[i]), 4),
                    "stake": round(float(weights[i] * bankroll), 2),
                })

        return {"weights": weights, "allocations": allocations, "meta": meta}


class RiskManager:
    """职业级风险管理器 — 冷却止损 + 相关投注互斥 + ML 动态仓位版。"""

    COOL_OFF_HOURS = 24         # 触发冷却后停注 N 小时
    MAX_SAME_GAME_MARKETS = 2   # 同一场比赛最多下注 N 个不同市场（联合凯利折扣后）

    def __init__(self, initial_budget: float = DEFAULT_BUDGET):
        self.initial_budget = initial_budget
        self.current_balance = initial_budget
        self.max_single_pct = MAX_SINGLE_BET_PCT
        self.max_total_exposure = MAX_TOTAL_EXPOSURE
        self.daily_loss_limit = initial_budget * 0.10
        self.monthly_loss_limit = initial_budget * 0.25
        self.consecutive_losses = 0
        self.total_bets = 0
        self.winning_bets = 0
        self.adaptive_kelly = AdaptiveKelly(base=KELLY_FRACTION)
        self.portfolio_optimizer = PortfolioOptimizer()
        self._daily_bets = []
        self._last_reset_date = datetime.now().date()
        # 冷却止损状态
        self.cool_off_until: Optional[datetime] = None
        self.weekly_loss = 0.0
        self._week_start = datetime.now().date()
        self.load_state()
        # ── 市场效率屏蔽名单（P3）──
        self._market_blocklist = self._load_blocklist()
        # ── 同场相关投注跟踪（P4）──
        self._correlation_groups: Dict[str, list] = {}
        # ── ML 动态仓位模型（P6 风控升级）──
        self.model_decay_tracker = ModelDecayTracker()
        self.dynamic_staking = DynamicStakingModel()
        # 后台训练（不阻塞）
        try:
            self.dynamic_staking.train()
        except Exception:
            pass
        # 初始化时检查冷却状态
        if self._in_cool_off():
            logger.warning("  🛑 冷却中直至 %s（连败 %d 次）",
                          self.cool_off_until.isoformat(), self.consecutive_losses)

    def _load_blocklist(self) -> set:
        """加载市场效率屏蔽名单。"""
        if not BLOCKLIST_PATH.exists():
            return set()
        try:
            with open(BLOCKLIST_PATH, encoding='utf-8') as f:
                entries = json.load(f)
            blocked = set()
            for e in entries:
                key = f"{e.get('sport','')}/{e.get('league','')}/{e.get('market_type','')}"
                blocked.add(key)
            if blocked:
                logger.info("  🚫 市场屏蔽: %d 个类别", len(blocked))
            return blocked
        except Exception as e:
            logger.warning("  ⚠️ 屏蔽名单加载失败: %s", e)
            return set()

    def _sport_to_model_name(self, sport: str) -> str:
        """将 sport 标识映射到模型名（用于模型退化追踪）。"""
        s = sport.lower()
        if "nba" in s or "basketball" in s or "ncaa" in s:
            return "bb"
        if "nfl" in s or "football" in s:
            return "nfl"
        if "soccer" in s or "epl" in s or any(lk in s for lk in ["liga", "serie", "bundes", "ligue"]):
            return "fb"
        return "fb"  # 默认映射到足球模型（覆盖面最广）

    def _check_market_efficiency(self, sport: str, league: str, market_type: str) -> bool:
        """检查市场效率。True = 允许下注, False = 被屏蔽。"""
        if not self._market_blocklist:
            return True
        key = f"{sport}/{league}/{market_type}"
        if key in self._market_blocklist:
            logger.info("  🚫 市场屏蔽: %s（历史ROI不达标）", key)
            return False
        return True

    def _in_cool_off(self) -> bool:
        """是否处于冷却期（禁止所有下注）。"""
        if self.cool_off_until is None:
            return False
        if datetime.now() < self.cool_off_until:
            return True
        # 冷却期已过，重置
        self.cool_off_until = None
        self.save_state()
        return False

    def _trigger_cool_off(self):
        """触发冷却：停止下注 COOL_OFF_HOURS 小时。"""
        self.cool_off_until = datetime.now() + timedelta(hours=self.COOL_OFF_HOURS)
        logger.warning("  🛑 触发冷却停注 %d 小时（连败 %d 次，回撤 %.1f%%）",
                      self.COOL_OFF_HOURS, self.consecutive_losses, self.drawdown_pct() * 100)
        self.save_state()

    def load_state(self):
        if RISK_STATE_FILE.exists():
            try:
                with open(RISK_STATE_FILE, encoding='utf-8') as f:
                    state = json.load(f)
                self.current_balance = state.get('balance', self.initial_budget)
                self.consecutive_losses = state.get('consecutive_losses', 0)
                self.total_bets = state.get('total_bets', 0)
                self.winning_bets = state.get('winning_bets', 0)
                # 冷却状态
                until = state.get('cool_off_until')
                if until:
                    self.cool_off_until = datetime.fromisoformat(until)
                self.weekly_loss = state.get('weekly_loss', 0.0)
            except Exception:
                pass

    def save_state(self):
        RISK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        from src.storage.file_lock import locked_open
        with locked_open(str(RISK_STATE_FILE), 'w', encoding='utf-8') as f:
            json.dump({
                'balance': self.current_balance,
                'consecutive_losses': self.consecutive_losses,
                'total_bets': self.total_bets,
                'winning_bets': self.winning_bets,
                'cool_off_until': self.cool_off_until.isoformat() if self.cool_off_until else None,
                'weekly_loss': self.weekly_loss,
                'updated': datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)

    def _get_confidence_tier(self, edge: float) -> float:
        """根据边际置信度返回凯利乘数。"""
        if edge >= 0.15:
            return 1.0   # 高置信度：全凯利
        elif edge >= 0.10:
            return 0.8   # 中高置信度
        elif edge >= 0.06:
            return 0.6   # 中置信度
        else:
            return 0.3   # 低置信度（谨慎）

    def _get_drawdown_multiplier(self) -> float:
        """回撤保护：根据目前回撤程度降低仓位。"""
        dd = self.drawdown_pct()
        if dd <= 0.0:
            return 1.0
        elif dd <= 0.05:
            return 0.9   # 5% 回撤：减10%
        elif dd <= 0.10:
            return 0.7   # 10% 回撤：减30%
        elif dd <= 0.20:
            return 0.4   # 20% 回撤：减60%
        else:
            return 0.0   # 20%+ 回撤：停手

    def _get_streak_multiplier(self) -> float:
        """连败保护。"""
        if self.consecutive_losses <= 1:
            return 1.0
        elif self.consecutive_losses <= 3:
            return 0.7   # 2-3连败：减30%
        elif self.consecutive_losses <= 5:
            return 0.4   # 4-5连败：减60%
        else:
            return 0.0   # 5+连败：停手

    def get_max_stake(self, edge_or_prob: float, odds: float, current_exposure_pct: float = 0.0,
                      input_is_prob: bool = False, sport: str = '', home_team: str = '',
                      away_team: str = '', market: str = '',
                      league: str = '', market_type: str = '') -> float:
        """计算最大下注额（增强版）。

        综合凯利准则 + 置信度分档 + 回撤保护 + 连败保护 + 冷却保护 +
        组合分散度优化 + 市场效率过滤 + 同场相关投注联合凯利。
        """
        # ── 冷却检查 ──
        if self._in_cool_off():
            return 0.0

        # ── 市场效率过滤（P3）──
        if not self._check_market_efficiency(sport, league, market_type or market):
            return 0.0

        # 停手检查
        dd_mult = self._get_drawdown_multiplier()
        if dd_mult <= 0:
            return 0.0
        streak_mult = self._get_streak_multiplier()
        if streak_mult <= 0:
            return 0.0

        max_single = self.current_balance * self.max_single_pct
        if odds <= 1.0:
            return 0.0

        if input_is_prob:
            prob = max(0.0, min(1.0, edge_or_prob))
            edge = prob - (1.0 / odds)
        else:
            market_prob = 1.0 / odds
            prob = market_prob + max(0.0, edge_or_prob)
            edge = max(0.0, edge_or_prob)

        b = odds - 1.0
        kelly = (prob * b - (1.0 - prob)) / b
        if kelly <= 0:
            return 0.0

        # 多层风险调整：ML 动态仓位 + 模型退化追踪 + 阈值回退
        model_decay_mult = self.model_decay_tracker.get_confidence_multiplier(
            self._sport_to_model_name(sport))
        dyn_mult = self.dynamic_staking.predict_multiplier({
            "edge": edge,
            "model_prob": prob,
            "odds": odds,
            "drawdown_pct": self.drawdown_pct(),
            "consecutive_losses": self.consecutive_losses,
            "adaptive_kelly_frac": self.adaptive_kelly.fraction(),
            "n_active_bets": len(self.portfolio_optimizer.active_bets),
            "win_rate": self.win_rate(),
            "total_bets": self.total_bets,
        })
        kelly_fraction = KELLY_FRACTION * model_decay_mult * dyn_mult

        stake_pct = max(0.0, min(kelly * kelly_fraction, self.max_single_pct))
        stake = self.current_balance * stake_pct
        total_exposure = current_exposure_pct + stake / max(self.current_balance, 1.0)
        if total_exposure > self.max_total_exposure:
            stake = max(0.0, self.max_total_exposure * self.current_balance
                        - current_exposure_pct * self.current_balance)

        final_stake = min(stake, max_single)

        # ── 相关投注互斥检测（P3+P4）──
        if sport and home_team and away_team:
            same_match = [
                b for b in self.portfolio_optimizer.active_bets
                if b.get("sport") == sport
                and b.get("home_team") == home_team
                and b.get("away_team") == away_team
            ]
            # 同一场比赛同一盘口 → 禁止
            for existing in same_match:
                if existing.get("market") == (market or ""):
                    return 0.0

            # 同一场比赛跨市场 → 相关投注联合凯利（P4）
            if same_match:
                group_key = f"{sport}/{home_team}/{away_team}"

                # 构建此场比赛的所有相关投注（已有 + 当前）
                all_bets = []
                for eb in same_match:
                    all_bets.append({
                        "market": eb.get("market", ""),
                        "prob": eb.get("model_prob", 0.5),
                        "odds": eb.get("odds", 2.0),
                        "stake": eb.get("stake", 0),
                    })
                # 当前这笔还没加入，计算其个体凯利
                current_stake = final_stake
                all_bets.append({
                    "market": market or "",
                    "prob": prob,
                    "odds": odds,
                    "stake": current_stake,
                })

                # 联合凯利：总计 = sum(个体) * 折扣因子(0.6)
                total_individual = sum(b["stake"] for b in all_bets)
                joint_total = total_individual * 0.6

                # 按 EV 比例分配
                ev_weights = []
                for b in all_bets:
                    p = b["prob"]
                    o = b["odds"]
                    ev = p * o - 1  # 期望值
                    ev_weights.append(max(0, ev))
                total_ev = sum(ev_weights)

                if total_ev > 0:
                    for i, b in enumerate(all_bets):
                        b["stake"] = joint_total * (ev_weights[i] / total_ev)

                # 更新 portfolio_optimizer 中已有投注的仓位
                for i, eb in enumerate(same_match):
                    eb["stake"] = all_bets[i]["stake"]

                # 当前这笔使用分配后的仓位
                final_stake = all_bets[-1]["stake"]

                # 更新跟踪
                self._correlation_groups[group_key] = all_bets

                logger.info("  🔗 同场相关投注: %s vs %s %d 个市场, 联合折扣 ¥%.1f (原 ¥%.1f)",
                           home_team, away_team, len(all_bets), joint_total, total_individual)

            # 同场跨市场数量上限
            if len(same_match) >= self.MAX_SAME_GAME_MARKETS:
                logger.info("  🔒 同场投注上限: %s vs %s 已达 %d 个市场",
                           home_team, away_team, self.MAX_SAME_GAME_MARKETS)
                return 0.0

        # 组合分散度调整
        if final_stake > 0 and (sport or home_team or away_team):
            ds = self.portfolio_optimizer.diversification_score()
            if ds < 0.3:
                final_stake *= 0.5
            elif ds < 0.6:
                final_stake *= 0.75

        # 记录到组合优化器
        if final_stake > 0 and sport and home_team and away_team:
            self.portfolio_optimizer.add_bet({
                "sport": sport,
                "home_team": home_team,
                "away_team": away_team,
                "market": market or "",
                "stake": final_stake,
                "model_prob": prob,
                "odds": odds,
            })

        return final_stake

    def can_place_bet(self, stake: float, current_exposure_pct: float = 0.0) -> tuple[bool, str]:
        if self.current_balance <= 0:
            return False, "账户资金已耗尽"

        if stake > self.current_balance * self.max_single_pct:
            return False, "单注超过限额"

        # 日亏损检查
        today = datetime.now().date()
        if today != self._last_reset_date:
            self._daily_bets = []
            self._last_reset_date = today

        total_exposure = current_exposure_pct + (stake / self.current_balance)
        if total_exposure > self.max_total_exposure:
            return False, "总仓位超过限额"

        # 回撤停手
        if self.drawdown_pct() > 0.25:
            return False, "回撤超过25%，暂停下注"

        return True, "允许下注"

    def record_outcome(self, stake: float, win: bool, odds: float = 2.0, prob: float = 0.5,
                       sport: str = "", home_team: str = "", away_team: str = "",
                       bet_type: str = "h2h"):
        self.total_bets += 1
        if win:
            pnl = stake * (odds - 1.0)
            self.current_balance += pnl
            self.winning_bets += 1
            self.consecutive_losses = 0
        else:
            pnl = -stake
            self.current_balance -= stake
            self.consecutive_losses += 1

        # 模型退化追踪
        model_name = self._sport_to_model_name(sport)
        self.model_decay_tracker.record_prediction(model_name, prob, win)

        # 滚动亏损跟踪
        self.weekly_loss = max(0.0, self.weekly_loss - pnl)

        # 更新自适应凯利
        self.adaptive_kelly.update(prob, 1 if win else 0)

        self.save_state()
        self._append_bet_log(stake, win, odds, prob, sport=sport,
                             home_team=home_team, away_team=away_team,
                             bet_type=bet_type)

        # ── 冷却触发条件 ──
        # 1) 5+ 连败
        if self.consecutive_losses >= 5:
            self._trigger_cool_off()
        # 2) 周亏损超 20%
        elif self.weekly_loss >= self.initial_budget * 0.20:
            self._trigger_cool_off()
        # 3) 回撤超 15%
        elif self.drawdown_pct() >= 0.15:
            self._trigger_cool_off()

    def _append_bet_log(self, stake, win, odds, prob,
                        sport="", home_team="", away_team="", bet_type="h2h"):
        log_entry = {
            'date': datetime.now().isoformat(),
            'stake': stake,
            'win': win,
            'odds': odds,
            'model_prob': prob,
            'balance_after': self.current_balance,
        }
        # CSV (backwards compatibility)
        needs_header = not BET_LOG_FILE.exists() or BET_LOG_FILE.stat().st_size == 0
        from src.storage.file_lock import locked_open
        with locked_open(str(BET_LOG_FILE), 'a', encoding='utf-8') as f:
            if needs_header:
                f.write("date,stake,win,odds,model_prob,balance_after\n")
            f.write(f"{log_entry['date']},{stake},{win},{odds},{prob},{self.current_balance}\n")
        # SQLite
        try:
            from src.storage.database import db
            db.record_bet(match_key=f"{home_team} vs {away_team}" if home_team or away_team else "",
                          home=home_team, away=away_team, sport=sport, bet_type=bet_type,
                          stake=stake, odds=odds, prob=prob,
                          notes=f"win={win}")
        except Exception:
            pass

    def drawdown_pct(self) -> float:
        if self.initial_budget <= 0:
            return 0.0
        return max(0.0, 1.0 - self.current_balance / self.initial_budget)

    def roi(self) -> float:
        if self.initial_budget <= 0:
            return 0.0
        return (self.current_balance - self.initial_budget) / self.initial_budget

    def win_rate(self) -> float:
        if self.total_bets <= 0:
            return 0.0
        return self.winning_bets / self.total_bets

    def get_health_check(self) -> dict:
        decay_health = self.model_decay_tracker.get_all_health()
        fi = self.dynamic_staking.get_feature_importance()
        return {
            'balance': self.current_balance,
            'roi': self.roi(),
            'drawdown': self.drawdown_pct(),
            'win_rate': self.win_rate(),
            'total_bets': self.total_bets,
            'consecutive_losses': self.consecutive_losses,
            'kelly_fraction': self.adaptive_kelly.fraction(),
            'under_daily_limit': self.current_balance >= self.initial_budget - self.daily_loss_limit,
            'under_monthly_limit': self.current_balance >= self.initial_budget - self.monthly_loss_limit,
            'cool_off_active': self._in_cool_off(),
            'cool_off_until': self.cool_off_until.isoformat() if self.cool_off_until else None,
            'weekly_loss': self.weekly_loss,
            'ml_dynamic_staking_trained': self.dynamic_staking.is_trained,
            'ml_feature_importance': fi,
            'model_decay': decay_health,
        }

    # ── 止损断路器 ─────────────────────────────────────────────

    def compute_daily_loss(self) -> float:
        """计算当日累计亏损（基于 _daily_bets）。"""
        today = datetime.now().date()
        if today != self._last_reset_date:
            return 0.0
        return max(0.0, -sum(b.get("pnl", 0) for b in self._daily_bets if b.get("pnl", 0) < 0))

    def circuit_breaker_status(self) -> dict:
        """断路器状态 — 是否应该停止下注及原因。

        Returns:
            {tripped, reason, cool_off_remaining_hours, consecutive_losses,
             drawdown_pct, balance, message}
        """
        # 1. 冷却期检查
        if self._in_cool_off():
            remaining = (self.cool_off_until - datetime.now()).total_seconds() / 3600
            return {
                "tripped": True,
                "reason": "cool_off",
                "cool_off_remaining_hours": round(remaining, 1),
                "consecutive_losses": self.consecutive_losses,
                "drawdown_pct": round(self.drawdown_pct(), 4),
                "balance": round(self.current_balance, 2),
                "message": f"🛑 冷却中（剩余 {remaining:.1f} 小时，连败 {self.consecutive_losses} 次）",
            }

        # 2. 回撤检查
        dd = self.drawdown_pct()
        if dd >= 0.15:
            return {
                "tripped": True,
                "reason": "drawdown",
                "cool_off_remaining_hours": 0.0,
                "consecutive_losses": self.consecutive_losses,
                "drawdown_pct": round(dd, 4),
                "balance": round(self.current_balance, 2),
                "message": f"🛑 回撤 {dd:.1%} 超过 15% 阈值，停止下注",
            }

        # 3. 周亏损检查
        weekly_loss_pct = self.weekly_loss / max(self.initial_budget, 1)
        if weekly_loss_pct >= 0.20:
            return {
                "tripped": True,
                "reason": "weekly_loss",
                "cool_off_remaining_hours": 0.0,
                "consecutive_losses": self.consecutive_losses,
                "drawdown_pct": round(dd, 4),
                "balance": round(self.current_balance, 2),
                "message": f"🛑 周亏损 {weekly_loss_pct:.1%} 超过 20% 阈值，停止下注",
            }

        # 4. 空仓 / 低回撤警告（未触发）
        warning = ""
        if dd >= 0.10:
            warning = f"⚠️ 回撤 {dd:.1%} 接近触发线（15%）"
        elif self.consecutive_losses >= 3:
            warning = f"⚠️ 连败 {self.consecutive_losses} 次，注意风险"

        return {
            "tripped": False,
            "reason": "none",
            "cool_off_remaining_hours": 0.0,
            "consecutive_losses": self.consecutive_losses,
            "drawdown_pct": round(dd, 4),
            "balance": round(self.current_balance, 2),
            "message": "✅ 正常" if not warning else warning,
        }

    # ── VaR / CVaR ──────────────────────────────────────────────

    def compute_var(self, confidence: float = 0.95) -> float:
        """Value at Risk — 历史模拟法。

        从 bet_log.csv 读取已结算注单的盈亏分布，
        返回给定置信水平下的最大预期亏损（金额）。
        confidence=0.95 表示 95% 的概率亏损不超过此值。
        """
        if not BET_LOG_FILE.exists():
            return 0.0
        try:
            log = pd.read_csv(BET_LOG_FILE)
            if log.empty or 'win' not in log.columns or 'stake' not in log.columns or 'odds' not in log.columns:
                return 0.0
            # 计算每笔盈亏: stake * (odds - 1) if win else -stake
            pnl = np.where(log['win'].astype(int) == 1,
                           log['stake'].astype(float) * (log['odds'].astype(float) - 1.0),
                           -log['stake'].astype(float))
            if len(pnl) < 10:
                return 0.0
            return float(np.percentile(pnl, (1.0 - confidence) * 100))
        except Exception:
            return 0.0

    def compute_cvar(self, confidence: float = 0.95) -> float:
        """Conditional VaR（Expected Shortfall）— 超出 VaR 的期望亏损。"""
        if not BET_LOG_FILE.exists():
            return 0.0
        try:
            log = pd.read_csv(BET_LOG_FILE)
            if log.empty or 'win' not in log.columns:
                return 0.0
            pnl = np.where(log['win'].astype(int) == 1,
                           log['stake'].astype(float) * (log['odds'].astype(float) - 1.0),
                           -log['stake'].astype(float))
            if len(pnl) < 10:
                return 0.0
            var = self.compute_var(confidence)
            tail = pnl[pnl <= var]
            if len(tail) == 0:
                return var
            return float(tail.mean())
        except Exception:
            return 0.0

    def portfolio_var(self, confidence: float = 0.95) -> float:
        """组合 VaR — 基于当前活跃下注的持仓。

        使用方差-协方差法（参数法），假设正态分布。
        """
        n = len(self.portfolio_optimizer.active_bets)
        if n == 0:
            return 0.0
        stakes = np.array([b.get("stake", 0) for b in self.portfolio_optimizer.active_bets])
        total_stake = stakes.sum()
        if total_stake == 0:
            return 0.0
        # 组合标准差
        pvar = self.portfolio_optimizer.portfolio_variance(stakes)
        port_std = np.sqrt(pvar) if pvar > 0 else 0.0
        if port_std <= 0:
            return 0.0
        from scipy.stats import norm
        z = norm.ppf(1.0 - confidence)
        return float(z * port_std)

    def batch_optimize(self, recs: list, bankroll: float = None) -> list:
        """对所有已选推荐执行联合凯利组合优化。

        替代逐个调 get_max_stake() 的启发式分散调整，
        用 KellyPortfolioOptimizer 联合求解所有推荐的最优仓位。

        Returns:
            更新后的推荐列表（stake 替换为优化值，stake=0 的被过滤）
        """
        if not recs:
            return recs

        # 清除候选阶段注册的幽灵条目
        self.portfolio_optimizer.clear()

        for r in recs:
            prob = r.get("model_prob", 0.5)
            odds = r.get("odds", 2.0)
            if prob <= 0 or odds <= 1.0:
                continue
            self.portfolio_optimizer.add_bet({
                "sport": r.get("sport", ""),
                "home_team": r.get("home_team", ""),
                "away_team": r.get("away_team", ""),
                "market": r.get("market", r.get("type", "")),
                "model_prob": prob,
                "odds": odds,
            })

        b = bankroll or self.current_balance
        result = self.portfolio_optimizer.solve_kelly_portfolio(
            bankroll=b,
            max_single_pct=self.max_single_pct,
            max_total_pct=self.max_total_exposure,
        )

        if not result.get("allocations"):
            return recs

        # 建立 key → stake 映射
        opt_map = {}
        for a in result["allocations"]:
            key = f"{a['sport']}/{a['home_team']}/{a['away_team']}/{a['market']}"
            opt_map[key] = a["stake"]

        updated = []
        for r in recs:
            key = (f"{r.get('sport','')}/{r.get('home_team','')}/"
                   f"{r.get('away_team','')}/{r.get('market', r.get('type',''))}")
            new_stake = opt_map.get(key, 0.0)
            if new_stake > 0:
                r["stake"] = new_stake
                updated.append(r)
            else:
                r["stake"] = 0.0

        return updated

    def reset_portfolio(self):
        """每日重置组合优化器。"""
        self.portfolio_optimizer.clear()


def main():
    rm = RiskManager()
    health = rm.get_health_check()
    logger.info("💰 风险管理系统状态（增强版）:")
    logger.info("  当前资金: ¥%.2f", health['balance'])
    logger.info("  ROI: %s", "{:+.2%}".format(health['roi']))
    logger.info("  回撤: %s", "{:.2%}".format(health['drawdown']))
    logger.info("%s", "  胜率: {:.1%}".format(health['win_rate']) if health['total_bets'] > 0 else "  胜率: N/A")
    logger.info("  总下注: %s", health['total_bets'])
    logger.info("  连败: %s", health['consecutive_losses'])
    logger.info("  自适应凯利: %.3f", health['kelly_fraction'])
    logger.info("  日限检查: %s", "✅" if health['under_daily_limit'] else "❌")
    logger.info("  月限检查: %s", "✅" if health['under_monthly_limit'] else "❌")


if __name__ == '__main__':
    main()
