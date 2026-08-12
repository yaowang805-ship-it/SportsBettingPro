"""投注系统共用常量 — 单一事实来源

所有模块应从此处导入，而非各自定义。
"""
import json
from pathlib import Path

from config.settings import DATA_DIR

# ── 资金管理 ──
BANKROLL = 20000.0                 # 日预算（基准）
INITIAL_BALANCE = 20000.0          # 初始资金（与 BANKROLL 一致）


def get_dynamic_bankroll() -> float:
    """V4.5: 固定日预算 ¥10,000. 约30场/天 → 平均 ¥333/场."""
    return BANKROLL

# ── EV 相关 ──
EV_CAP = 12.0                      # 最大 EV %（bb_ev_push EV_CAP / bb_virtual_bet MAX_EV_PCT）
MIN_EV_PCT = 3.0                   # 最小 EV %（与 T1 门槛一致）

# ── 数量限制 ──
MAX_BETS = 100                     # 每日最多投注数 / 推送机会数

# ── Kelly 计算 ──
KELLY_FRACTION = 0.50              # Kelly 分数（bb_virtual_bet 0.50 / 旧 bb_ev_push 0.75 → 统一为 0.50）

# ── 仓位限制 ──
MAX_STAKE_PCT = 0.06               # 单注最大仓位比例
PER_MATCH_CAP_PCT = 1.0            # 单场不设限

# ── 运动排序（推送展示用） ──
# V5.1: 足球第一, 篮球=网球同等优先, 乒乓/羽/排/拳击封杀
SPORT_ORDER = {
    "football": 0,
    "basketball": 1, "tennis": 1,     # 同等优先级
    "baseball": 2, "american_football": 2,
    "mma": 3, "ice_hockey": 3,
}
_SPORT_SORT_TUPLE = tuple(sorted(SPORT_ORDER.keys(), key=lambda k: SPORT_ORDER[k]))


def get_league_tier(league: str) -> int:
    """返回联赛所属 Tier (1-4)，不认识的联赛默认 Tier 3。

    双向匹配: kw in league (短名匹配长名) 或 league in kw (长名匹配短名)
    自动分级: 根据联赛名中的级别关键词推断
    """
    tiers_file = DATA_DIR / "league_tiers.json"
    if tiers_file.exists():
        tiers = json.loads(tiers_file.read_text())
        if league in tiers:
            return tiers[league]
        for kw, tier in tiers.items():
            if kw in league or league in kw:
                return tier

    # 自动分级: 根据联赛名推断
    league_lower = league.lower()
    # T1: 顶级联赛关键词
    if any(kw in league for kw in ['英超','西甲','意甲','德甲','法甲','NBA','MLB','NFL','UFC','NHL','WNBA',
                                     'Premier League','La Liga','Serie A','Bundesliga','Ligue 1',
                                     'Champions League','Europa League','World Cup']):
        return 1
    # T2: 次级/挑战赛
    if any(kw in league for kw in ['Challenger','冠军联赛','欧联','欧会','甲级','超级','Major',
                                     'NCAA','夏季联赛','季前赛','公开赛']):
        return 2
    # T4: 明显低级别
    if any(kw in league for kw in ['U19','U20','U21','U23','后备','青年','丁级','丙级',
                                     '友谊赛','女子','女篮','3x3','室内']):
        return 4
    return 3  # default


def league_multiplier(league: str, sport: str = "") -> float:
    """根据联赛等级 + Pinnacle准确度返回投注额乘数。

    Pinnacle越准(低抽水) → 比价越可靠 → 乘数加成。
    不包含结算验证（结算是门禁，能投/不能投，不是权重）。
    """
    tier = get_league_tier(league)
    base = {1: 1.0, 2: 0.9, 3: 0.7, 4: 0.5}.get(tier, 0.7)

    # Pinnacle准确度加成 (基于真实数据, 按运动区分)
    try:
        from src.report.bb_ev_push import (PINNACLE_LEAGUE_ACCURACY,
                                             PINNACLE_TENNIS_ACCURACY,
                                             _get_tennis_accuracy)
        if sport == "tennis":
            accuracy_bonus = _get_tennis_accuracy(league)
        else:
            accuracy_bonus = PINNACLE_LEAGUE_ACCURACY.get(league, 1.0)
        base *= accuracy_bonus
    except ImportError:
        pass

    return base

# ═══════════════════════════════════════════════════════════════
# V5.1 全运动分层投注策略
# ═══════════════════════════════════════════════════════════════
# 职业团队不做全量投注。按 Tier 分层控制 EV 门槛和仓位。

def get_tier_strategy(sport: str, league: str, tier: int = None) -> dict:
    """返回 (sport, tier) 的投注策略: {ev_floor, max_stake_pct, max_odds, allow_suggest}"""
    if tier is None:
        tier = get_league_tier(league)
    
    # 默认策略 (T3)
    strategy = {
        "ev_floor": 3.0,        # 基础 EV 门槛
        "tier_ev_bonus": 0.0,   # 额外 EV 加成
        "max_stake_pct": 0.04,  # 仓位上限
        "max_odds": 20.0,       # 最高赔率
        "allow_suggest": True,  # 低于最低投注时是否显示"建议"
        "min_stake": 0,         # 最低投注额 (0=不限制)
    }
    
    # ── 运动差异化 ──
    if sport == "football":
        if tier == 1:
            strategy.update({"ev_floor": 2.0, "max_stake_pct": 0.04, "max_odds": 20.0, "allow_suggest": True})
        elif tier == 2:
            strategy.update({"ev_floor": 2.5, "max_stake_pct": 0.03, "max_odds": 10.0, "allow_suggest": True})
        elif tier == 3:
            strategy.update({"ev_floor": 4.0, "max_stake_pct": 0.02, "max_odds": 5.0, "allow_suggest": False, "min_stake": 50})
        elif tier == 4:
            strategy.update({"ev_floor": 6.0, "max_stake_pct": 0.01, "max_odds": 3.0, "allow_suggest": False, "min_stake": 100})
    
    elif sport == "basketball":
        if tier == 1:  # NBA/WNBA
            strategy.update({"ev_floor": 2.0, "max_stake_pct": 0.03, "max_odds": 10.0})
        elif tier == 2:  # EuroLeague/ACB (有Pin覆盖)
            strategy.update({"ev_floor": 3.0, "max_stake_pct": 0.02, "max_odds": 5.0})
        elif tier in (3, 4):  # 拉美小联赛
            strategy.update({"ev_floor": 5.0, "max_stake_pct": 0.01, "max_odds": 3.0, "allow_suggest": False, "min_stake": 50})
    
    elif sport == "tennis":
        if tier == 1:  # Grand Slam
            strategy.update({"ev_floor": 2.0, "max_stake_pct": 0.02, "max_odds": 5.0})
        elif tier == 2:  # Masters/ATP500/WTA
            strategy.update({"ev_floor": 2.5, "max_stake_pct": 0.02, "max_odds": 5.0})
        elif tier in (3, 4):  # ATP250 + 其他 (Challenger/ITF已在weight_matrix封杀)
            strategy.update({"ev_floor": 4.0, "max_stake_pct": 0.01, "max_odds": 3.0, "allow_suggest": False, "min_stake": 50})
    
    elif sport == "baseball":
        if tier == 1:  # MLB
            strategy.update({"ev_floor": 2.5, "max_stake_pct": 0.03, "max_odds": 8.0})
        else:  # NPB/KBO/CPBL
            strategy.update({"ev_floor": 4.0, "max_stake_pct": 0.01, "max_odds": 4.0, "allow_suggest": False, "min_stake": 50})
    
    elif sport == "american_football":
        strategy.update({"ev_floor": 3.0, "max_stake_pct": 0.02, "max_odds": 5.0})  # NFL只有T1, 大学T2
    
    elif sport == "ice_hockey":
        strategy.update({"ev_floor": 3.5, "max_stake_pct": 0.02, "max_odds": 4.0})
    
    elif sport == "mma":
        strategy.update({"ev_floor": 3.5, "max_stake_pct": 0.02, "max_odds": 5.0})
    
    elif sport == "boxing":
        strategy.update({"ev_floor": 5.0, "max_stake_pct": 0.01, "max_odds": 3.0, "allow_suggest": False, "min_stake": 50})
    
    return strategy
