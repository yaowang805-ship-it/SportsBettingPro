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

# ── 按市场类型动态 Kelly (2026-07-24 回测调整) ──
# 见 reports/回测分析与优化方案_20260724.md
KELLY_BY_MARKET = {
    "ou":    0.60,   # 大小球: 回测+26.6% ROI, 最可靠
    "btts":  0.55,   # 双边进球: 回测+20.3% ROI
    "hc":    0.50,   # 让球: 保持基准
    "dnb":   0.40,   # 平局退款: 新市场保守
    "ht":    0.30,   # 上半场: 上半场客胜-38.1%, 保守
    "dc":    0.30,   # 双重机会: 新市场保守
    "1x2":   0.25,   # 独赢: 全面亏损(home-47.1%, away-27.6%, draw-100%)
    "oe":    0.25,   # 单双: 新市场最保守
    "corner": 0.20,  # 角球: 新市场最保守
    "htft":  0.20,   # 半全场: 新市场最保守
}

# ── 屏蔽的杯赛/比赛类型 (Tier 4, 不推送不投注) ──
BANNED_COMPETITIONS = [
    "World Cup",
    "European Championship",
    "Copa America",
    "Africa Cup",
    "Asian Cup",
    "Confederations Cup",
    "International Match",
    "国际友谊赛",
    "国家队友谊赛",
]

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
