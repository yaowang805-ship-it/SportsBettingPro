"""投注系统共用常量 — 单一事实来源

所有模块应从此处导入，而非各自定义。
"""
import json
from pathlib import Path

from config.settings import DATA_DIR

# ── 资金管理 ──
BANKROLL = 50000.0                 # 日预算（bb_ev_push BANKROLL / bb_virtual_bet DAILY_BANKROLL）
INITIAL_BALANCE = 50000.0          # 初始资金（仅 bb_virtual_bet 使用）

# ── EV 相关 ──
EV_CAP = 12.0                      # 最大 EV %（bb_ev_push EV_CAP / bb_virtual_bet MAX_EV_PCT）
MIN_EV_PCT = 3.0                   # 最小 EV %（与 T1 门槛一致）

# ── 数量限制 ──
MAX_BETS = 50                      # 每日最多投注数 / 推送机会数

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
    """返回联赛所属 Tier (1-4)，不认识的联赛默认 Tier 3。"""
    tiers_file = DATA_DIR / "league_tiers.json"
    if tiers_file.exists():
        tiers = json.loads(tiers_file.read_text())
        for kw, tier in tiers.items():
            if kw in league:
                return tier
    return 3


def league_multiplier(league: str) -> float:
    """根据联赛等级返回投注额乘数。"""
    tier = get_league_tier(league)
    return {1: 1.0, 2: 0.9, 3: 0.7, 4: 0.5}.get(tier, 0.7)
