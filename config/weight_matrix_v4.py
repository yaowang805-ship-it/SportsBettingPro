"""投注权重矩阵 V4 — Pinnacle 107,896场全量历史数据驱动

核心理念: 每个赔率区间 × 联赛 × 盘口的权重 = 该区间的 Kelly 最优解
  半凯利仓位% = max(0, actual_wr × BB_odds - 1) / (BB_odds - 1) × 0.5

数据源 (全量外部数据, 非结算数据):
  足球 1X2: Pinnacle 107,896笔收盘赔率 (20联赛 × 13赛季, football-data.co.uk)
  足球 OU:  Pinnacle 46,727笔收盘赔率
  网球:     Pinnacle 5,013笔 (tennis_market_efficiency.json)
  NBA:      模型15季回测 (57,504场)
  BB溢价:   从 BB/FB vs Pinnacle comparison 统计

BB溢价校准 (按赔率区间):
  低赔 (<2.0): BB比Pin高 7-9% (流动性好, 竞争充分)
  中赔 (2.0-4.0): BB比Pin高 5-7%
  高赔 (4.0-7.0): BB比Pin高 3-5%
  超高 (>7.0): BB比Pin高 1-3% (假阳性多)

生成日期: 2026-07-31
"""

from functools import lru_cache
from typing import Optional

# =====================================================================
# 赔率区间 (30 bins, 0.2-1.0 步长)
# =====================================================================
ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.30, 2.50, 2.70, 2.90,
    3.10, 3.30, 3.50, 3.70, 3.90, 4.20, 4.50, 4.80,
    5.20, 5.60, 6.00, 6.50, 7.00, 7.50, 8.00, 9.00,
    10.00, 12.00, 15.00, 20.00, float('inf')
]

TENNIS_ODDS_BINS = [
    1.15, 1.25, 1.35, 1.50, 1.70, 1.90, 2.10,
    2.50, 3.00, 4.00, 5.00, 6.00, 8.00, 10.00, 15.00, float('inf')
]

BASKETBALL_ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.40, 2.70,
    3.00, 3.50, 4.00, 5.00, 7.00, 10.00, float('inf')
]


def _bin_index(odds: float, bins: list) -> int:
    for i, t in enumerate(bins):
        if odds < t:
            return i
    return len(bins) - 1


# =====================================================================
# Kelly 公式
# =====================================================================
def kelly_half(actual_wr: float, avg_odds: float, bb_premium: float, cap: float = 0.06) -> float:
    """半凯利仓位 (返回小数, 0.06 = 6%)。

    公式: half_kelly = max(0, wr×BB_odds - 1) / (BB_odds - 1) × 0.5
    """
    if actual_wr <= 0 or avg_odds <= 1.01:
        return 0.0
    bb_odds = avg_odds * (1.0 + bb_premium)
    roi = actual_wr * bb_odds - 1.0
    if roi <= 0:
        return 0.0
    half_kelly = roi / (bb_odds - 1.0) * 0.5
    # 极低赔折扣 (Kelly高估了极低赔的优势)
    if avg_odds < 1.3:
        half_kelly *= 0.5
    elif avg_odds < 1.5:
        half_kelly *= 0.7
    return min(cap, max(0.0, half_kelly))


# =====================================================================
# BB 溢价表 (按赔率区间, 从 BB vs Pinnacle comparison 统计)
# =====================================================================
def _bb_premium_1x2(odds: float) -> float:
    """1X2 市场的 BB 溢价 (BB赔率/Pin赔率 - 1)。"""
    if odds < 1.5: return 0.07    # 低赔: 竞争最充分
    elif odds < 2.0: return 0.08  # 1.5-2.0: 溢价最高
    elif odds < 2.5: return 0.07
    elif odds < 3.0: return 0.065
    elif odds < 4.0: return 0.06
    elif odds < 5.0: return 0.05
    elif odds < 7.0: return 0.04
    elif odds < 10.0: return 0.03
    else: return 0.02


def _bb_premium_ou(odds: float) -> float:
    """OU 市场的 BB 溢价 (OU vig更低 → BB溢价空间类似)。"""
    if odds < 1.5: return 0.075
    elif odds < 1.7: return 0.08
    elif odds < 2.0: return 0.085   # OU 1.7-2.0 溢价最丰
    elif odds < 2.5: return 0.07
    elif odds < 3.0: return 0.06
    elif odds < 4.0: return 0.05
    else: return 0.03


def _bb_premium_ht(odds: float) -> float:
    """HT 半场溢价 (折扣15%)。"""
    return _bb_premium_1x2(odds) * 0.85


# =====================================================================
# 1X2 权重矩阵 (从 Pinnacle 107,896场直接计算)
# =====================================================================
# 格式: {league: {bin_index: (actual_wr, avg_odds, num_bets)}}
# 只包含 n>=10 的可靠数据点
# 每个联赛的数据点都是该联赛独有的赔率区间盈利模式

PIN_1X2_DATA = {
    "英超": {
        0: (0.805, 1.25, 473), 1: (0.726, 1.40, 665), 2: (0.625, 1.60, 699),
        3: (0.580, 1.80, 684), 4: (0.493, 2.00, 617), 5: (0.435, 2.20, 620),
        6: (0.393, 2.40, 591), 7: (0.385, 2.60, 496), 8: (0.331, 2.80, 408),
        9: (0.290, 3.00, 442), 10: (0.310, 3.20, 703), 11: (0.297, 3.40, 1261),
        12: (0.279, 3.60, 1115), 13: (0.246, 3.80, 842), 14: (0.235, 4.05, 909),
        15: (0.221, 4.35, 605), 16: (0.183, 4.65, 471), 17: (0.183, 5.00, 470),
        18: (0.178, 5.40, 387), 19: (0.183, 5.80, 338), 20: (0.134, 6.25, 337),
        21: (0.155, 6.75, 245), 22: (0.148, 7.25, 182), 23: (0.114, 7.75, 167),
        24: (0.089, 8.50, 248), 25: (0.123, 9.50, 171), 26: (0.105, 11.00, 239),
        27: (0.059, 13.50, 202), 28: (0.056, 17.50, 142), 29: (0.044, 25.00, 91),
    },
    "德甲": {
        0: (0.801, 1.24, 362), 1: (0.709, 1.40, 436), 2: (0.602, 1.60, 510),
        3: (0.545, 1.80, 538), 4: (0.453, 2.00, 543), 5: (0.413, 2.20, 530),
        6: (0.412, 2.40, 561), 7: (0.370, 2.60, 486), 8: (0.332, 2.80, 395),
        9: (0.323, 3.00, 418), 10: (0.302, 3.20, 454), 11: (0.313, 3.40, 793),
        12: (0.279, 3.60, 1106), 13: (0.268, 3.80, 858), 14: (0.243, 4.05, 760),
        15: (0.250, 4.35, 512), 16: (0.203, 4.65, 375), 17: (0.196, 5.00, 393),
        18: (0.201, 5.40, 293), 19: (0.160, 5.80, 219), 20: (0.147, 6.25, 204),
        21: (0.130, 6.75, 193), 22: (0.102, 7.25, 128), 23: (0.079, 7.75, 114),
        24: (0.182, 8.50, 176), 25: (0.123, 9.50, 106), 26: (0.062, 11.00, 145),
        27: (0.100, 13.50, 140), 28: (0.043, 17.50, 94), 29: (0.034, 25.00, 89),
    },
    "英冠": {
        0: (0.886, 1.24, 44), 1: (0.698, 1.39, 268), 2: (0.614, 1.60, 684),
        3: (0.566, 1.80, 964), 4: (0.471, 2.00, 1205), 5: (0.436, 2.20, 1288),
        6: (0.422, 2.40, 1300), 7: (0.365, 2.60, 1120), 8: (0.331, 2.80, 1012),
        9: (0.337, 3.00, 1070), 10: (0.299, 3.20, 2000), 11: (0.280, 3.40, 2967),
        12: (0.296, 3.60, 2273), 13: (0.248, 3.80, 1264), 14: (0.227, 4.05, 1189),
        15: (0.214, 4.35, 672), 16: (0.187, 4.65, 471), 17: (0.220, 5.00, 463),
        18: (0.150, 5.40, 327), 19: (0.181, 5.80, 238), 20: (0.206, 6.25, 175),
        21: (0.147, 6.75, 136), 22: (0.088, 7.25, 102), 23: (0.034, 7.75, 58),
        24: (0.161, 8.50, 93), 25: (0.080, 9.50, 50), 26: (0.034, 11.00, 58),
        27: (0.000, 13.50, 18), 28: (0.000, 17.50, 9),
    },
    "意甲": {
        0: (0.846, 1.24, 332), 1: (0.713, 1.40, 676), 2: (0.639, 1.60, 731),
        3: (0.576, 1.80, 687), 4: (0.505, 2.00, 630), 5: (0.449, 2.20, 657),
        6: (0.419, 2.40, 590), 7: (0.395, 2.60, 484), 8: (0.338, 2.80, 397),
        9: (0.332, 3.00, 479), 10: (0.292, 3.20, 1003), 11: (0.299, 3.40, 1333),
        12: (0.273, 3.60, 1097), 13: (0.246, 3.80, 773), 14: (0.215, 4.05, 834),
        15: (0.189, 4.35, 599), 16: (0.191, 4.65, 488), 17: (0.192, 5.00, 522),
        18: (0.196, 5.40, 419), 19: (0.133, 5.80, 279), 20: (0.128, 6.25, 282),
        21: (0.141, 6.75, 234), 22: (0.124, 7.25, 177), 23: (0.141, 7.75, 170),
        24: (0.070, 8.50, 242), 25: (0.053, 9.50, 169), 26: (0.090, 11.00, 177),
        27: (0.067, 13.50, 164), 28: (0.048, 17.50, 105), 29: (0.000, 25.00, 57),
    },
    "西甲": {
        0: (0.850, 1.25, 367), 1: (0.735, 1.39, 328), 2: (0.593, 1.60, 386),
        3: (0.513, 1.80, 382), 4: (0.499, 2.00, 425), 5: (0.451, 2.20, 408),
        6: (0.415, 2.40, 347), 7: (0.339, 2.60, 289), 8: (0.288, 2.80, 267),
        9: (0.345, 3.00, 293), 10: (0.334, 3.20, 530), 11: (0.291, 3.40, 886),
        12: (0.257, 3.60, 672), 13: (0.288, 3.80, 462), 14: (0.259, 4.05, 499),
        15: (0.253, 4.35, 328), 16: (0.165, 4.65, 272), 17: (0.201, 5.00, 269),
        18: (0.183, 5.40, 202), 19: (0.150, 5.80, 147), 20: (0.111, 6.25, 180),
        21: (0.192, 6.75, 130), 22: (0.124, 7.25, 129), 23: (0.112, 7.75, 89),
        24: (0.111, 8.50, 126), 25: (0.071, 9.50, 98), 26: (0.083, 11.00, 156),
        27: (0.080, 13.50, 137), 28: (0.064, 17.50, 141), 29: (0.031, 25.00, 160),
    },
    "法甲": {
        0: (0.813, 1.25, 114), 1: (0.705, 1.40, 283), 2: (0.616, 1.60, 418),
        3: (0.553, 1.80, 446), 4: (0.479, 2.00, 416), 5: (0.442, 2.20, 394),
        6: (0.403, 2.40, 343), 7: (0.365, 2.60, 301), 8: (0.315, 2.80, 273),
        9: (0.306, 3.00, 299), 10: (0.315, 3.20, 498), 11: (0.289, 3.40, 730),
        12: (0.268, 3.60, 483), 13: (0.284, 3.80, 386), 14: (0.234, 4.05, 397),
        15: (0.217, 4.35, 250), 16: (0.196, 4.65, 194), 17: (0.163, 5.00, 195),
        18: (0.176, 5.40, 165), 19: (0.137, 5.80, 102), 20: (0.167, 6.25, 84),
        21: (0.143, 6.75, 63), 22: (0.108, 7.25, 65), 23: (0.082, 7.75, 49),
        24: (0.128, 8.50, 47), 25: (0.067, 9.50, 45), 26: (0.069, 11.00, 58),
        27: (0.103, 13.50, 39), 28: (0.065, 17.50, 31),
    },
    # 大样本聚合 (所有联赛合并, 用于缺失联赛的默认值)
    "_AGGREGATE": {
        0: (0.824, 1.25, 3785), 1: (0.710, 1.39, 6894), 2: (0.610, 1.60, 11493),
        3: (0.548, 1.80, 14682), 4: (0.478, 2.00, 16883), 5: (0.443, 2.19, 18002),
        6: (0.406, 2.40, 17846), 7: (0.372, 2.60, 16030), 8: (0.343, 2.80, 14598),
        9: (0.326, 3.00, 15339), 10: (0.309, 3.20, 23155), 11: (0.287, 3.40, 37656),
        12: (0.272, 3.60, 32611), 13: (0.249, 3.80, 20202), 14: (0.234, 4.05, 18289),
        15: (0.225, 4.35, 11173), 16: (0.196, 4.65, 7766), 17: (0.194, 5.00, 7638),
        18: (0.191, 5.40, 5684), 19: (0.167, 5.80, 4130), 20: (0.143, 6.25, 3795),
        21: (0.143, 6.75, 2861), 22: (0.121, 7.25, 2136), 23: (0.111, 7.75, 1651),
        24: (0.106, 8.50, 2391), 25: (0.097, 9.50, 1585), 26: (0.085, 11.00, 1980),
        27: (0.067, 13.50, 1515), 28: (0.049, 17.50, 1113), 29: (0.029, 25.00, 805),
    },
}

# 更小联赛(数据少)直接用聚合数据
for _lg in ("英甲", "英乙", "苏超", "德乙", "意乙", "西乙", "法乙",
            "荷甲", "比甲", "葡超", "土超", "希超", "英议联", "苏甲"):
    PIN_1X2_DATA[_lg] = PIN_1X2_DATA["_AGGREGATE"]


# =====================================================================
# OU 权重 (从 46,727场比赛)
# =====================================================================
# bin → (actual_wr, avg_odds, num_bets)
PIN_OU_AGGREGATE = {
    0: (0.813, 1.24, 139),   1: (0.698, 1.42, 2426),  2: (0.608, 1.62, 13671),
    3: (0.542, 1.80, 26049), 4: (0.485, 1.99, 24480), 5: (0.427, 2.18, 14616),
    6: (0.378, 2.38, 6654),  7: (0.396, 2.58, 2841),  8: (0.288, 2.78, 1151),
    9: (0.286, 2.99, 590),   10: (0.290, 3.19, 348),  11: (0.338, 3.38, 272),
    12: (0.200, 3.57, 90),   13: (0.308, 3.81, 39),
}


# =====================================================================
# 封杀
# =====================================================================
BLOCKED_SPORTS = {"mma", "boxing"}
BLOCKED_MARKETS = {"dc", "htft"}  # 0% 胜率 或 Pinnacle 无对应盘口
BLOCKED_LEAGUES = {"中超", "Chinese Super League", "China Super League"}  # 中国联赛不碰 或 Pinnacle 无对应盘口

# =====================================================================
# 核心查询
# =====================================================================

def _match_league(league: str, data_dict: dict):
    """匹配联赛数据, 优先精确匹配, 然后模糊匹配。"""
    if league in data_dict:
        return data_dict[league]
    for kw in sorted(data_dict.keys(), key=lambda x: -len(x)):
        if kw == "_AGGREGATE":
            continue
        if len(kw) <= 2 and not (league or "").startswith(kw):
            continue
        if kw in (league or ""):
            return data_dict[kw]
    return data_dict.get("_AGGREGATE")


@lru_cache(maxsize=4096)
def get_kelly_stake_pct(sport: str, league: str, sub_market: str, odds: float) -> float:
    """返回 Kelly 最优仓位 (小数, 0.06 = 6% of bankroll)。

    纯粹由 Pinnacle 历史数据 + BB 溢价驱动, 没有任何结算数据参与。
    """
    sport_lower = (sport or "").lower()

    # ── 封杀 ──
    if sport_lower in BLOCKED_SPORTS or sub_market in BLOCKED_MARKETS:
        return 0.0
    for banned in BLOCKED_LEAGUES:
        if banned in (league or ""):
            return 0.0

    # ── Football ──
    if sport_lower == "football":
        idx = _bin_index(odds, ODDS_BINS)

        if sub_market == "ou":
            data = PIN_OU_AGGREGATE.get(idx)
            if not data:
                return 0.0
            wr, avg_o, n = data
            if n < 15:
                return 0.0
            bb_prem = _bb_premium_ou(odds)
            return kelly_half(wr, avg_o, bb_prem)

        elif sub_market == "ht":
            league_data = _match_league(league, PIN_1X2_DATA)
            if not league_data:
                return 0.0
            data = league_data.get(idx)
            if not data:
                return 0.0
            wr, avg_o, n = data
            if n < 15:
                return 0.0
            # HT: 15% 折扣在溢价和最终结果上
            bb_prem = _bb_premium_ht(odds)
            stake = kelly_half(wr, avg_o, bb_prem)
            return stake * 0.85  # 额外 HT 折扣

        else:  # 1X2 (also used for BTTS/DNB/OE/Corner fallback)
            # 🔵 特殊市场: 无 Pinnacle 对应盘口 → 固定上限
            if sub_market in SPECIAL_MARKET_CAPS:
                cfg = SPECIAL_MARKET_CAPS[sub_market]
                if odds > cfg["max_odds"]:
                    return 0.0
                return cfg["max_stake"]

            league_data = _match_league(league, PIN_1X2_DATA)
            # 🟡 联赛无自有 Pin 数据 → 用全量聚合 × 0.7
            if league_data is PIN_1X2_DATA.get("_AGGREGATE"):
                discount = 0.7
            else:
                discount = 1.0

            if not league_data:
                return 0.0
            data = league_data.get(idx)
            if not data:
                return 0.0
            wr, avg_o, n = data
            if n < 10:
                return 0.0
            bb_prem = _bb_premium_1x2(odds)
            stake = kelly_half(wr, avg_o, bb_prem)
            return stake * discount

    # ── Tennis ──
    elif sport_lower == "tennis":
        # Pinnacle 5,013场: Grand Slam / Masters / ATP500 / ATP250 / WTA
        # 挑战赛/ITF 无可靠数据 → 封杀
        for kw in ("Challenger", "ITF", "W15", "M15", "W25", "M25"):
            if kw.lower() in (league or "").lower():
                return 0.0

        # 复用 V3 中编码的 Pinnacle 网球 ROI 数据
        from config.weight_matrix_v3 import get_kelly_stake_pct as _tn
        return _tn(sport, league, sub_market, odds)

    # ── Basketball ──
    elif sport_lower == "basketball":
        # 🟢 NBA: 模型回测57K场 → 直接用
        # 🟡 WNBA/其他: 无独立数据 → NBA × 0.3
        from config.weight_matrix_v3 import get_kelly_stake_pct as _bb
        stake = _bb(sport, league, sub_market, odds)
        if "NBA" in (league or ""):
            return stake
        else:
            return stake * 0.3  # 非NBA: 3折

    # ── Baseball ──
    elif sport_lower == "baseball":
        # 🔴 无 Pinnacle 收盘数据 → 足球聚合 ×0.5
        if odds >= 3.0:
            return 0.0
        agg = PIN_1X2_DATA["_AGGREGATE"]
        idx = _bin_index(odds, ODDS_BINS)
        data = agg.get(idx)
        if not data or data[2] < 20:
            return 0.0
        bb_prem = _bb_premium_1x2(odds) * 0.8  # 棒球 BB 溢价估计是足球的 80%
        stake = kelly_half(data[0], data[1], bb_prem)
        return stake * 0.5  # 5折: 无 Pin 验证

    # ── American Football ──
    elif sport_lower == "american_football":
        # 🔴 无 Pinnacle 收盘数据 → 足球聚合 ×0.3
        if odds >= 3.0:
            return 0.0
        agg = PIN_1X2_DATA["_AGGREGATE"]
        idx = _bin_index(odds, ODDS_BINS)
        data = agg.get(idx)
        if not data or data[2] < 20:
            return 0.0
        stake = kelly_half(data[0], data[1], _bb_premium_1x2(odds) * 0.7)
        return stake * 0.3  # 3折: 无 Pin 验证

    # ── Ice Hockey ──
    elif sport_lower == "ice_hockey":
        # 🔴 无 Pinnacle 收盘数据 → 足球聚合 ×0.2
        if odds >= 2.5:
            return 0.0
        agg = PIN_1X2_DATA["_AGGREGATE"]
        idx = _bin_index(odds, ODDS_BINS)
        data = agg.get(idx)
        if not data or data[2] < 20:
            return 0.0
        stake = kelly_half(data[0], data[1], _bb_premium_1x2(odds) * 0.5)
        return stake * 0.2  # 2折: 最保守

    # ── 乒/羽/排 ──
    elif sport_lower in ("pingpong", "badminton", "volleyball"):
        # 🔴 无任何外部数据 → 固定极小额
        if odds > 2.5:
            return 0.0
        return 0.01  # 固定 1%

    # ── 完全未知 ──
    else:
        if odds > 2.5:
            return 0.0
        return 0.005  # 固定 0.5%


# =====================================================================
# 特殊市场 (无 Pinnacle 对应盘口 → 固定上限)
# =====================================================================
SPECIAL_MARKET_CAPS = {
    "btts":  {"max_stake": 0.03, "max_odds": 3.0},
    "dnb":   {"max_stake": 0.02, "max_odds": 5.0},
    "oe":    {"max_stake": 0.01, "max_odds": 2.5},
    "corner":{"max_stake": 0.01, "max_odds": 3.0},
}

# =====================================================================
# EV 门槛
# =====================================================================
def get_min_ev(sport: str, league: str, sub_market: str, odds: float) -> float:
    """最小 EV 门槛。

    有 Pin 数据: stake>0 的区间门槛合理, stake=0 的区间门槛=999
    无 Pin 数据: 赔率越高门槛越高, 越缺数据门槛越高
    """
    sport_lower = (sport or "").lower()
    if sport_lower in BLOCKED_SPORTS or sub_market in BLOCKED_MARKETS:
        return 999.0

    if sport_lower == "football":
        stake = get_kelly_stake_pct(sport, league, sub_market, odds)
        if stake > 0:
            if odds < 2.0: return 2.0
            elif odds < 3.0: return 3.0
            elif odds < 5.0: return 5.0
            elif odds < 7.0: return 7.0
            else: return 10.0
        else:
            return 999.0

    elif sport_lower == "tennis":
        if odds < 2.0: return 2.0
        elif odds < 3.0: return 3.0
        elif odds < 5.0: return 5.0
        else: return 8.0

    elif sport_lower == "basketball":
        if odds < 2.0: return 2.0
        elif odds < 3.0: return 3.0
        else: return 5.0

    elif sport_lower in ("baseball", "american_football"):
        if odds < 2.0: return 3.0
        elif odds < 3.0: return 5.0
        else: return 999.0

    elif sport_lower in ("ice_hockey", "pingpong", "badminton", "volleyball"):
        if odds < 2.5: return 4.0
        else: return 999.0

    else:
        return 5.0


def get_odds_cap(sport: str, league: str, sub_market: str) -> float:
    """赔率上限。有 Pin 数据的运动可以放宽, 没数据的严格限制。"""
    if sub_market in BLOCKED_MARKETS:
        return 0.0
    sport_lower = (sport or "").lower()

    if sport_lower == "football":
        return 20.0 if sub_market not in SPECIAL_MARKET_CAPS else SPECIAL_MARKET_CAPS[sub_market]["max_odds"]
    elif sport_lower == "tennis":
        for kw, cap in [("Masters",15.0),("Grand Slam",5.0),("ATP 500",10.0),
                        ("ATP 250",5.0),("WTA",4.0),("Challenger",3.0),("ITF",2.5)]:
            if kw.lower() in (league or "").lower():
                return cap
        return 3.0
    elif sport_lower == "basketball":
        return 8.0 if "NBA" in (league or "") else 5.0
    elif sport_lower == "baseball":
        return 5.0
    elif sport_lower in ("american_football", "ice_hockey"):
        return 3.0
    else:
        return 2.5


# =====================================================================
# 打印
# =====================================================================
def print_matrix():
    """打印完整权重矩阵。"""
    leagues = ["英超","德甲","英冠","意甲","西甲","法甲"]
    bins = ODDS_BINS

    print(f"{'='*100}")
    print(f"V4 权重矩阵: 1X2 (Pinnacle 107,896场, BB+7%溢价)")
    print(f"每格 = half-kelly% of bankroll, cap=6%")
    print(f"{'='*100}")

    test_odds = []
    prev = 1.01
    for b in bins[:20]:  # first 20 bins
        test_odds.append(round((prev + b) / 2, 1))
        prev = b

    # Header
    hdr = f"{'League':<8s}"
    for o in test_odds:
        hdr += f" @{o:<4.1f}"
    print(hdr)
    print("-" * len(hdr))

    for lg in leagues:
        row = f"{lg:<8s}"
        for o in test_odds:
            pct = get_kelly_stake_pct("football", lg, "1x2", o)
            if pct > 0:
                row += f" {pct*100:4.1f}"
            else:
                row += "    -"
        print(row)

    print()
    print(f"公式: half_kelly = max(0, actual_wr × BB_odds - 1) / (BB_odds - 1) × 0.5")
    print(f"BB_odds = Pin_odds × (1 + bb_premium)")
    print(f"数据源: football-data.co.uk Pinnacle 收盘赔率 2012-2025")


if __name__ == "__main__":
    print_matrix()
