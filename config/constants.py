"""投注系统共用常量 — 单一事实来源

所有模块应从此处导入，而非各自定义。
"""
import json
from pathlib import Path

from config.settings import DATA_DIR

# ── 资金管理 ──
BANKROLL = 10000.0                 # 日预算
INITIAL_BALANCE = 50000.0          # 初始资金（累计余额）

# ── EV 相关 ──
EV_CAP = 12.0                      # 最大 EV %（bb_ev_push EV_CAP / bb_virtual_bet MAX_EV_PCT）
MIN_EV_PCT = 3.0                   # 最小 EV %（与 T1 门槛一致）

# ── 数量限制 ──
MAX_BETS = 100                     # 每日最多投注数 / 推送机会数

# ── Kelly 计算 ──
KELLY_FRACTION = 0.50              # Kelly 分数（bb_virtual_bet 0.50 / 旧 bb_ev_push 0.75 → 统一为 0.50）

# ── 仓位限制 ──
MAX_STAKE_PCT = 0.06               # 单注最大仓位比例
PER_MATCH_CAP_PCT = 0.06           # 单场多个盘口总仓位比例

# ── 运动排序（推送展示用） ──
SPORT_ORDER = {
    "football": 0, "basketball": 1, "tennis": 2,
    "baseball": 3, "american_football": 4,
    "pingpong": 5, "badminton": 6, "volleyball": 7,
    "boxing": 8, "mma": 9, "ice_hockey": 10,
}
_SPORT_SORT_TUPLE = tuple(SPORT_ORDER.keys())


def get_league_tier(league: str) -> int:
    """返回联赛所属 Tier (1-4)，不认识的联赛默认 Tier 3。

    双向匹配: kw in league (短名匹配长名) 或 league in kw (长名匹配短名)
    """
    tiers_file = DATA_DIR / "league_tiers.json"
    if tiers_file.exists():
        tiers = json.loads(tiers_file.read_text())
        # 精确匹配优先
        if league in tiers:
            return tiers[league]
        # 双向模糊匹配
        for kw, tier in tiers.items():
            if kw in league or league in kw:
                return tier
    return 3


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
