"""各运动·各联赛·各盘口 盈亏线(抽水)表 — 91K场 football-data.co.uk Pinnacle收盘回测。

盈亏线 = 按收盘价买入的历史负ROI绝对值 (即需要多少 BB 溢价才能打平)。

数据来源 (2026-08-15 测算):
- 足球 1X2/OU/HC/角球: football-data.co.uk 各联赛 Pinnacle收盘 (91,250场)
  - 1X2 用 Pinnacle收盘 (PSCH/PSCD/PSCA)
  - OU/HC/角球 用平均收盘 (Avg>2.5 / AvgAHH/AHA / AvgCAHH/AHA), 抽水比Pinnacle厚
- 篮球NBA/棒球MLB/美足NFL/冰球NHL: V5矩阵历史数据 (SBR/Kaggle)

格式: BREAK_EVEN = {sport: {league(中文短名): {market: 盈亏点%}}}
查询: 有数据联赛 → 该联赛该盘口盈亏点; 无数据 → 该 tier 聚合盈亏点 + 2% 缓冲。
"""

# ── 足球: 各联赛各盘口盈亏点 (%) ──
# 联赛名需与 weight_matrix_v5._match_league 的口径一致 (中文短名)
FOOTBALL_BREAK_EVEN = {
    # T1 五大联赛
    "英超": {"1x2": 3.4, "ou": 5.0, "hc": 3.6, "corner": 3.3},
    "德甲": {"1x2": 1.9, "ou": 6.5, "hc": 3.9, "corner": 3.8},
    "西甲": {"1x2": 3.9, "ou": 5.4, "hc": 3.9, "corner": 3.7},
    "意甲": {"1x2": 5.2, "ou": 5.0, "hc": 3.9, "corner": 3.6},
    "法甲": {"1x2": 3.7, "ou": 5.4, "hc": 4.0, "corner": 3.9},
    # T2 二级联赛
    "英冠": {"1x2": 2.9, "ou": 5.4, "hc": 4.3, "corner": 4.0},
    "德乙": {"1x2": 2.2, "ou": 6.1, "hc": 4.8, "corner": 4.4},
    "意乙": {"1x2": 3.5, "ou": 6.1, "hc": 4.8, "corner": 4.4},
    "西乙": {"1x2": 3.0, "ou": 6.1, "hc": 4.8, "corner": 4.4},
    "法乙": {"1x2": 3.6, "ou": 6.1, "hc": 4.8, "corner": 4.4},
    "荷甲": {"1x2": 3.9, "ou": 6.1, "hc": 4.8, "corner": 4.4},
    "比甲": {"1x2": 3.9, "ou": 6.1, "hc": 4.8, "corner": 4.4},
    "葡超": {"1x2": 7.4, "ou": 6.0, "hc": 4.5, "corner": 4.2},
    "土超": {"1x2": 5.9, "ou": 6.2, "hc": 4.3, "corner": 4.3},
    "希超": {"1x2": 8.1, "ou": 6.5, "hc": 5.4, "corner": 5.0},
    # T3 低级联赛
    "英甲": {"1x2": 4.1, "ou": 6.6, "hc": 5.6, "corner": 5.0},
    "英乙": {"1x2": 3.2, "ou": 6.6, "hc": 5.6, "corner": 5.0},
    "英议联": {"1x2": 3.7, "ou": 6.6, "hc": 5.6, "corner": 5.0},
    "西丙": {"1x2": 5.7, "ou": 6.6, "hc": 5.6, "corner": 5.0},
    "西丁": {"1x2": 3.9, "ou": 6.6, "hc": 5.6, "corner": 5.0},
}

# ── Tier 聚合盈亏点 (无数据联赛回退用) ──
FOOTBALL_TIER_BREAK_EVEN = {
    1: {"1x2": 3.7, "ou": 5.5, "hc": 3.9, "corner": 3.7},
    2: {"1x2": 4.2, "ou": 6.1, "hc": 4.8, "corner": 4.4},
    3: {"1x2": 3.9, "ou": 6.6, "hc": 5.6, "corner": 5.0},
    4: {"1x2": 4.5, "ou": 6.6, "hc": 5.6, "corner": 5.0},  # T4 无数据, 保守取 T3 上限
}

# ── 其他运动 ML 盈亏点 (V5矩阵历史数据, 按运动聚合) ──
# 网球数据有 bin 错配异常, 暂用保守 4%
SPORT_BREAK_EVEN = {
    "basketball": {"1x2": 3.4, "ou": 3.4, "hc": 3.4},
    "baseball": {"1x2": 1.9},
    "american_football": {"1x2": 4.0},
    "ice_hockey": {"1x2": 3.4},
    "tennis": {"1x2": 4.0},  # ⚠️ 数据异常, 保守 4%
}

# 安全缓冲: 有数据联赛 +1%, 无数据回退 +2%
BUFFER_HAS_DATA = 1.0
BUFFER_NO_DATA = 2.0

# 市场别名 → 盈亏表 key
_MARKET_KEY = {
    "1x2": "1x2", "ht": "1x2", "hc": "hc", "handicap": "hc",
    "ou": "ou", "over_under": "ou", "corner": "corner",
    "dc": "1x2", "dnb": "1x2",  # DC/DNB 推导自 1X2, 用 1X2 盈亏线
    "btts": "ou", "oe": "ou",   # BTTS/OE 推导自 OU, 用 OU 盈亏线
    "ht_dc": "1x2",             # 上半场双机会 推导自 1X2
}


def get_break_even(sport: str, league: str, sub_market: str, tier: int = 2) -> float:
    """返回该 (sport, league, market) 的盈亏点(%)。无数据 → tier聚合 + 缓冲。"""
    sport_l = (sport or "").lower()
    mk = _MARKET_KEY.get(sub_market, "1x2")

    if sport_l == "football":
        lg = FOOTBALL_BREAK_EVEN.get(league)
        if lg and mk in lg:
            return lg[mk] + BUFFER_HAS_DATA
        tier_agg = FOOTBALL_TIER_BREAK_EVEN.get(tier, FOOTBALL_TIER_BREAK_EVEN[2])
        return tier_agg.get(mk, 4.0) + BUFFER_NO_DATA

    sp = SPORT_BREAK_EVEN.get(sport_l)
    if sp:
        return sp.get(mk, 4.0) + BUFFER_HAS_DATA
    return 4.0 + BUFFER_NO_DATA
