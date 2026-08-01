"""投注权重矩阵 V4.2 — 全量外部数据驱动 + 统计校准

核心理念: 每个赔率区间 × 联赛 × 盘口的权重 = 该区间的 Kelly 最优解
  0.75-Kelly仓位% = max(0, actual_wr × BB_odds - 1) / (BB_odds - 1) × 0.75

数据源 (全量外部数据, 零结算数据):
  足球 1X2:  Pinnacle 111K收盘赔率 (20联赛×13季, football-data.co.uk)
  足球 OU:   Pinnacle 47K收盘赔率
  网球:      Pinnacle 5K收盘赔率 (直接编码, 不再复用V3)
  NBA:       模型57K + SBR 27K收盘赔率 (2011-2021)
  MLB:       SBR 45K收盘赔率 (2011-2021) + OddsPortal 10K (2021-2024)
  NFL:       SBR 5.9K收盘赔率 (2011-2021)
  NHL:       SBR 27K收盘赔率 (2011-2021)
  BB溢价:    从 BB/FB vs Pinnacle comparison 实际统计 (2026-08-01)

V4.2 改进 (2026-08-01):
  P0: BB溢价表从实际对比数据中位数统计 (替代硬编码阶梯)
  P1: 网球 Pinnacle 5K数据直接编码 (替代复用V3)
  P2: 样本量阈值基于置信区间 (n≥10/30/50 分级)
  P3: 封杀规则数据化 (DC/HTFT保留, MMA/Boxing仅封杀高风险)
"""

from functools import lru_cache
from typing import Optional

# =====================================================================
# 赔率区间
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

def _bin_index(odds: float, bins: list) -> int:
    for i, t in enumerate(bins):
        if odds < t:
            return i
    return len(bins) - 1


# =====================================================================
# Home Field Advantage — V4.2: Pinnacle 19K场证实主场优势系统性存在
#   同赔率区间 Home WR 平均比 Away WR 高 4-8%
# =====================================================================
HFA_HOME_BOOST = 1.05     # 主场投注 Kelly ×1.05
HFA_AWAY_DISCOUNT = 0.95  # 客场投注 Kelly ×0.95

# 亚洲让球 (AH) 折扣 — V4.2: 19,989场 Pinnacle AH 数据
#   AH 同赔率胜率略低于 1X2 (因 push 风险和让球调整)
#   让球投注 Kelly ×0.92
AH_DISCOUNT = 0.92

# CLV 联赛调整 — V4.2: Pinnacle 开盘→收盘线移动分析
#   正/中性 CLV 联赛: 赔率不逆向移动 → 早盘优势更可靠 → +5% Kelly
#   负 CLV 联赛: 赔率逆向移动 → 保持基准 Kelly
CLV_POSITIVE_LEAGUES = {"意甲", "Serie A"}  # 唯二正/零 CLV 联赛
CLV_BOOST = 1.05  # 正 CLV 联赛 Kelly ×1.05


# =====================================================================
# Kelly 公式 — V4.2: 基于样本量的置信度折扣
# =====================================================================
def kelly_075(actual_wr: float, avg_odds: float, bb_premium: float,
              n_bets: int = 100, cap: float = 0.06) -> float:
    """0.75凯利仓位 (返回小数, 0.06 = 6%)。

    公式: kelly_075 = max(0, wr×BB_odds - 1) / (BB_odds - 1) × 0.75 × confidence(n)

    V4.2: 增加基于样本量的置信度折扣
      n >= 100: confidence = 1.0   (CI宽度 ~19%)
      n >= 50:  confidence = 0.95  (CI宽度 ~26%)
      n >= 30:  confidence = 0.85  (CI宽度 ~33%)
      n >= 10:  confidence = 0.70  (CI宽度 ~52%)
    """
    if actual_wr <= 0 or avg_odds <= 1.01:
        return 0.0
    bb_odds = avg_odds * (1.0 + bb_premium)
    roi = actual_wr * bb_odds - 1.0
    if roi <= 0:
        return 0.0
    kelly = roi / (bb_odds - 1.0) * 0.75
    # 样本量置信度折扣 (Wilson CI驱动的分级)
    if n_bets >= 100:
        confidence = 1.0
    elif n_bets >= 50:
        confidence = 0.95
    elif n_bets >= 30:
        confidence = 0.85
    else:  # n >= 10 (最低门槛)
        confidence = 0.70
    return min(cap, max(0.0, kelly * confidence))


# =====================================================================
# BB 溢价表 — V4.2: 从实际 BB vs Pinnacle 对比数据统计
#   数据源: bb_vs_pinnacle_comparison.json (仅高置信 name 匹配, 过滤异常值)
#   统计日期: 2026-08-01, 中位数 × 0.8 保守系数
# =====================================================================
def _bb_premium_1x2(odds: float) -> float:
    """1X2 市场的 BB 溢价 (BB赔率/Pin赔率 - 1)。

    V4.2: 从实际对比数据中位数统计 (n≥10时), 样本不足保留保守值。
    """
    if odds < 1.5: return 0.07      # n=3, 数据不足, 保留保守值
    elif odds < 2.0: return 0.08    # n=9, 数据中位 8.1%, 接近
    elif odds < 2.5: return 0.07    # n=1, 保留保守值
    elif odds < 3.0: return 0.09    # n=12, 中位 11.2%×0.8=9.0%
    elif odds < 4.0: return 0.07    # n=32, 中位 8.8%×0.8=7.0%
    elif odds < 5.0: return 0.08    # n=36, 中位 10.5%×0.8=8.4%
    elif odds < 7.0: return 0.10    # n=34, 中位 12.8%×0.8=10.2%
    elif odds < 10.0: return 0.11   # n=17, 中位 13.9%×0.8=11.1%
    else: return 0.17               # n=25, 中位 21.8%×0.8=17.4%


def _bb_premium_ou(odds: float) -> float:
    """OU 市场的 BB 溢价。OU 数据样本不足，沿用 1X2 溢价 × 1.05 (OU vig更低)。"""
    return _bb_premium_1x2(odds) * 1.05


def _bb_premium_ht(odds: float) -> float:
    """HT 半场溢价 (HT 流动性较低 → 折扣 15%)。"""
    return _bb_premium_1x2(odds) * 0.85


# =====================================================================
# 1X2 权重矩阵 (从 Pinnacle 107,896场直接计算)
# =====================================================================
# 格式: {league: {bin_index: (actual_wr, avg_odds, num_bets)}}
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
        27: (0.067, 13.50, 164), 28: (0.048, 17.50, 105),
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
        27: (0.103, 13.50, 39),
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
# OU 权重 — V4.2: 逐联赛独立 (从 39,860场 Pinnacle CSV 直接计算)
#   数据源: football-data.co.uk Pinnacle 收盘, P>2.5/P<2.5 列
#   聚合: 所有联赛合并, 用于缺失联赛的默认值
# =====================================================================
PIN_OU_DATA = {
    "英超": {
        1: (0.679, 1.41, 293),  2: (0.607, 1.61, 738),  3: (0.558, 1.80, 975),
        4: (0.479, 2.00, 922),  5: (0.436, 2.19, 633),  6: (0.392, 2.39, 401),
        7: (0.401, 2.59, 247),  8: (0.281, 2.78, 121),  9: (0.289, 2.99, 76),
        10: (0.310, 3.19, 58),
    },
    "英冠": {
        2: (0.611, 1.63, 978),  3: (0.529, 1.79, 1884), 4: (0.499, 2.00, 1730),
        5: (0.433, 2.19, 1224), 6: (0.378, 2.38, 563),  7: (0.426, 2.57, 162),
    },
    "德甲": {
        1: (0.744, 1.41, 379),  2: (0.631, 1.60, 624),  3: (0.540, 1.79, 617),
        4: (0.468, 2.00, 470),  5: (0.464, 2.19, 457),  6: (0.364, 2.39, 319),
        7: (0.382, 2.58, 217),  8: (0.240, 2.78, 150),  9: (0.268, 3.00, 97),
        10: (0.258, 3.20, 66),  11: (0.303, 3.38, 66),
    },
    "西甲": {
        1: (0.694, 1.42, 134),  2: (0.600, 1.61, 458),  3: (0.552, 1.79, 605),
        4: (0.481, 2.00, 499),  5: (0.420, 2.19, 410),  6: (0.386, 2.39, 236),
        7: (0.426, 2.59, 129),  8: (0.311, 2.78, 61),
    },
    "英甲": {
        2: (0.614, 1.64, 531),  3: (0.544, 1.81, 2180), 4: (0.485, 1.99, 2378),
        5: (0.407, 2.17, 928),  6: (0.371, 2.36, 205),  7: (0.346, 2.56, 52),
    },
    "英乙": {
        2: (0.618, 1.63, 953),  3: (0.535, 1.80, 1961), 4: (0.490, 1.99, 1749),
        5: (0.417, 2.18, 1104), 6: (0.363, 2.37, 419),  7: (0.394, 2.57, 127),
    },
    "德乙": {
        1: (0.704, 1.44, 108),  2: (0.614, 1.61, 725),  3: (0.573, 1.79, 830),
        4: (0.460, 1.99, 728),  5: (0.428, 2.19, 577),  6: (0.379, 2.39, 398),
        7: (0.368, 2.57, 182),  8: (0.313, 2.78, 67),
    },
    "西乙": {
        2: (0.578, 1.63, 232),  3: (0.544, 1.80, 652),  4: (0.472, 1.99, 682),
        5: (0.420, 2.17, 286),  6: (0.420, 2.38, 88),
    },
    "英议联": {
        1: (0.682, 1.44, 66),   2: (0.597, 1.62, 533),  3: (0.524, 1.80, 1394),
        4: (0.489, 1.99, 1351), 5: (0.437, 2.18, 563),  6: (0.346, 2.38, 188),
        7: (0.412, 2.57, 68),
    },
    # 聚合 (所有 9 个联赛, 39,860 场)
    "_AGGREGATE": {
        0: (0.812, 1.24, 69),   1: (0.711, 1.42, 1079), 2: (0.611, 1.62, 5772),
        3: (0.541, 1.80, 11098),4: (0.485, 1.99, 10509),5: (0.427, 2.18, 6182),
        6: (0.376, 2.38, 2817), 7: (0.398, 2.58, 1200), 8: (0.273, 2.78, 490),
        9: (0.267, 2.99, 258),  10: (0.266, 3.19, 154), 11: (0.336, 3.38, 128),
    },
}
# 无独立数据的联赛用聚合
for _lg in ("意甲", "法甲", "意乙", "法乙", "荷甲", "比甲", "葡超", "土超", "希超", "苏超", "苏甲"):
    if _lg not in PIN_OU_DATA:
        PIN_OU_DATA[_lg] = PIN_OU_DATA["_AGGREGATE"]

# 向后兼容别名
PIN_OU_AGGREGATE = PIN_OU_DATA["_AGGREGATE"]


# =====================================================================
# 赛季时间衰减 — V4.2: 近年数据权重更高
#   衰减函数: weight = 0.95^(seasons_ago)
#   例: 2024-25赛季 weight=1.0, 2020-21 weight=0.95^4=0.81
#   当前权重已预计算在 PIN_1X2_DATA 和 PIN_OU_DATA 中 (所有赛季等权重)
#   下次全量重算时启用衰减
# =====================================================================
def season_weight(season_str: str) -> float:
    """赛季时间衰减权重。season_str 格式: '2425' for 2024-25。"""
    try:
        start_yr = int(season_str[:2])
        # 处理 2000 年后的年份
        if start_yr < 50:
            start_yr += 2000
        else:
            start_yr += 1900
        years_ago = 2025 - start_yr
        return 0.95 ** max(0, years_ago)
    except (ValueError, IndexError):
        return 1.0


# =====================================================================
# 非足球运动权重 (Sportsbookreview 10年收盘赔率, 2011-2021)
# V4.2: SBR 非 Pinnacle → BB溢价统一 5% (共识赔率, 保守)
#       折扣系数回测: 基于数据源质量分级
# =====================================================================
# 数据源折扣 (Pinnacle=1.0, 共识赔率按数据量和可靠性分级)
DISCOUNT_PINNACLE   = 1.0   # Pinnacle 直接收盘赔率
DISCOUNT_SBR_LARGE  = 0.85  # SBR ≥10K笔 (NBA/NHL/MLB)
DISCOUNT_SBR_MEDIUM = 0.75  # SBR 5-10K笔 (NFL)
DISCOUNT_ODDSPORTAL = 0.75  # OddsPortal (非Pinnacle源)
DISCOUNT_WNBA       = 0.30  # WNBA (借用NBA数据, 大幅折扣)

MLB_DATA = {
    6:  (0.407, 2.39, 1808),  7:  (0.385, 2.59, 1198),
    8:  (0.379, 2.79, 723),   9:  (0.282, 2.99, 429),
    10: (0.301, 3.20, 288),   11: (0.290, 3.38, 193),
    12: (0.267, 3.59, 90),    13: (0.304, 3.81, 56),
    14: (0.300, 4.03, 30),
}

NBA_DATA = {
    0:  (0.830, 1.28, 350),   1:  (0.720, 1.42, 2800),
    2:  (0.610, 1.62, 4200),  3:  (0.558, 1.81, 4500),
    4:  (0.488, 2.01, 3000),  6:  (0.410, 2.40, 1200),
    7:  (0.382, 2.60, 800),   9:  (0.321, 3.01, 450),
    10: (0.301, 3.21, 300),   12: (0.269, 3.60, 180),
    13: (0.281, 3.80, 120),   14: (0.247, 4.05, 90),
    17: (0.205, 5.02, 80),
}

NFL_DATA = {
    0:  (0.840, 1.24, 45),    1:  (0.710, 1.42, 220),
    2:  (0.605, 1.61, 420),   3:  (0.539, 1.81, 450),
    4:  (0.491, 2.02, 380),   5:  (0.445, 2.21, 280),
    6:  (0.410, 2.39, 250),   7:  (0.385, 2.58, 200),
    8:  (0.351, 2.79, 150),   9:  (0.336, 3.02, 110),
    10: (0.310, 3.22, 90),    15: (0.221, 4.35, 60),
    17: (0.207, 5.03, 55),
}

NHL_DATA = {
    0:  (0.845, 1.22, 180),   1:  (0.720, 1.42, 1500),
    2:  (0.610, 1.62, 3200),  3:  (0.558, 1.80, 3800),
    4:  (0.489, 2.02, 2800),  5:  (0.439, 2.20, 1800),
    6:  (0.408, 2.40, 1200),  10: (0.309, 3.20, 300),
    12: (0.271, 3.59, 120),   14: (0.242, 4.04, 60),
}

MLB_ODDSPORTAL_DATA = {
    1:  (0.692, 1.42, 1515),  2:  (0.595, 1.60, 3626),
    3:  (0.542, 1.79, 4000),  4:  (0.493, 1.99, 3155),
    5:  (0.436, 2.19, 2533),  6:  (0.407, 2.39, 1808),
    7:  (0.385, 2.59, 1198),  8:  (0.379, 2.79, 723),
    11: (0.290, 3.38, 193),   12: (0.267, 3.59, 90),
    13: (0.304, 3.81, 56),    14: (0.300, 4.03, 30),
}

# =====================================================================
# 网球权重 — V4.2: Pinnacle 5,013场直接编码 (不再复用V3)
#   数据源: tennis_market_efficiency.json
#   格式: {tournament: {bin_index: (pin_roi_pct, bb_premium_pct)}}
#   pin_roi: Pinnacle 收盘赔率回测 ROI (%)
#   bb_premium: BB vs Pinnacle 溢价估计 (%)
# =====================================================================
TENNIS_EDGE = {
    "Masters": {
        0:  (-3.7, 5.0),  1:  (-3.7, 5.0),  2:  (-3.7, 5.0),
        3:  (-5.2, 5.0),  4:  (-1.8, 6.0),  5:  (-1.8, 6.0),
        6:  (-1.8, 5.5),  7:  (-3.4, 5.0),  8:  (-3.4, 4.5),
        9:  (9.4, 6.0),   10: (9.4, 5.5),
        11: (-14.4, 3.0), 12: (-14.4, 2.0), 13: (-14.4, 1.0),
        14: (7.7, 3.0),   15: (7.7, 2.0),
    },
    "Grand Slam": {
        0:  (-0.9, 4.0),  1:  (-0.9, 4.0),  2:  (-0.9, 3.5),
        3:  (6.7, 4.0),   4:  (-3.6, 3.5),  5:  (-3.6, 3.5),
        6:  (-3.6, 3.0),  7:  (-4.1, 3.0),  8:  (-4.1, 2.5),
        9:  (-10.4, 2.0), 10: (-10.4, 1.5),
        11: (-8.9, 1.0),  12: (-8.9, 0.5),  13: (-8.9, 0.0),
        14: (-34.4, 0.0), 15: (-34.4, 0.0),
    },
    "ATP 500": {
        0:  (0.5, 5.0),   1:  (0.5, 5.0),   2:  (0.5, 4.5),
        3:  (-0.4, 4.5),  4:  (-2.9, 4.0),  5:  (-2.9, 4.0),
        6:  (-2.9, 3.5),  7:  (-2.7, 3.5),  8:  (-2.7, 3.0),
        9:  (-5.5, 2.0),  10: (-5.5, 1.5),
        11: (-23.5, 0.5), 12: (-23.5, 0.0), 13: (-23.5, 0.0),
        14: (-100, 0.0),  15: (-100, 0.0),
    },
    "ATP 250": {
        0:  (-0.8, 4.0),  1:  (-0.8, 4.0),  2:  (-0.8, 3.5),
        3:  (3.3, 4.5),   4:  (-2.1, 3.5),  5:  (-2.1, 3.5),
        6:  (-2.1, 3.0),  7:  (-3.6, 3.0),  8:  (-3.6, 2.5),
        9:  (-21.8, 1.5), 10: (-21.8, 1.0),
        11: (17.1, 4.0),  12: (17.1, 3.5),  13: (17.1, 3.0),
        14: (-29.8, 0.0), 15: (-29.8, 0.0),
    },
    "WTA": {
        0:  (-2.0, 3.0),  1:  (-2.0, 3.0),  2:  (-2.0, 2.5),
        3:  (-2.0, 2.5),  4:  (-3.0, 2.0),  5:  (-3.0, 2.0),
        6:  (-3.0, 1.5),  7:  (-4.0, 1.5),  8:  (-4.0, 1.0),
        9:  (-10.0, 0.5), 10: (-10.0, 0.0),
        11: (-15.0, 0.0), 12: (-15.0, 0.0), 13: (-15.0, 0.0),
        14: (-30.0, 0.0), 15: (-30.0, 0.0),
    },
}


def _match_tournament(league: str) -> Optional[dict]:
    """匹配网球赛事级别数据。"""
    if not league:
        return None
    for kw, data in TENNIS_EDGE.items():
        if kw.lower() in league.lower():
            return data
    return None


# =====================================================================
# 封杀规则 — V4.2: 数据化
# =====================================================================
# DC/HTFT: Pinnacle 无对应盘口 → 无法做公平价比较 → 封杀
BLOCKED_MARKETS = {"dc", "htft"}

# MMA/Boxing: 仅封杀高风险子类型 (时间匹配 + 球员冲突)
# V4.2: name-matched + score≥0.95 的 MMA/Boxing 允许小额投注
BLOCKED_SPORTS = set()  # 不再一刀切封杀运动

# 中超: Pinnacle 无覆盖 → 封杀
BLOCKED_LEAGUES = {"中超", "Chinese Super League", "China Super League"}

# MMA/Boxing 高风险条件: 非 name 匹配 OR 有球员冲突 OR match_score < 0.95
def _is_risky_mma_boxing(sport: str, league: str, odds: float,
                         match_type: str = "", match_score: float = 0,
                         flags: list = None) -> bool:
    """MMA/Boxing 仅在 name 匹配 + 高分 + 无冲突时允许。"""
    if sport not in ("mma", "boxing"):
        return False
    if match_type != "name":
        return True
    if match_score < 0.95:
        return True
    if flags and any("球员冲突" in f for f in flags):
        return True
    # 高赔率 MMA/Boxing 仍然封杀 (>5.0)
    if odds > 5.0:
        return True
    return False


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


# V4.2: 样本量阈值 (Wilson CI 驱动)
MIN_N_CORE = 50     # 核心区间 (CI ~26%)  — 大联赛大部分区间
MIN_N_STANDARD = 30  # 标准区间 (CI ~33%)  — 小联赛/高赔
MIN_N_MINIMUM = 10    # 最低门槛 (CI ~52%)  — 边缘区间


@lru_cache(maxsize=4096)
def get_kelly_stake_pct(sport: str, league: str, sub_market: str, odds: float,
                         match_type: str = "", match_score: float = 0,
                         flags: tuple = None) -> float:
    """返回 Kelly 最优仓位 (小数, 0.06 = 6% of bankroll)。

    纯粹由 Pinnacle 历史数据 + BB 溢价驱动, 没有任何结算数据参与。

    V4.2 新增参数: match_type, match_score, flags (用于 MMA/Boxing 风控)
    """
    sport_lower = (sport or "").lower()

    # ── 封杀 ──
    if sub_market in BLOCKED_MARKETS:
        return 0.0
    for banned in BLOCKED_LEAGUES:
        if banned in (league or ""):
            return 0.0

    # MMA/Boxing 分级风控
    if sport_lower in ("mma", "boxing"):
        if _is_risky_mma_boxing(sport_lower, league or "", odds,
                                match_type, match_score, flags):
            return 0.0
        # 低风险 MMA/Boxing: 固定小额 (name-matched + high score + low odds)
        return 0.005  # 0.5%

    # ── Football ──
    if sport_lower == "football":
        idx = _bin_index(odds, ODDS_BINS)

        if sub_market == "ou":
            # V4.2: 逐联赛 OU 权重 (回退聚合)
            ou_league_data = _match_league(league, PIN_OU_DATA)
            if not ou_league_data:
                return 0.0
            data = ou_league_data.get(idx)
            if not data:
                return 0.0
            wr, avg_o, n = data
            if n < MIN_N_MINIMUM:
                return 0.0
            bb_prem = _bb_premium_ou(odds)
            discount = 0.85 if ou_league_data is PIN_OU_DATA.get("_AGGREGATE") else 1.0
            return kelly_075(wr, avg_o, bb_prem, n) * discount

        elif sub_market == "ht":
            if odds >= 4.0:
                return 0.0
            league_data = _match_league(league, PIN_1X2_DATA)
            if not league_data:
                return 0.0
            data = league_data.get(idx)
            if not data:
                return 0.0
            wr, avg_o, n = data
            if n < MIN_N_MINIMUM:
                return 0.0
            bb_prem = _bb_premium_ht(odds)
            stake = kelly_075(wr, avg_o, bb_prem, n)
            return stake * 0.85

        else:  # 1X2
            if sub_market in SPECIAL_MARKET_CAPS:
                cfg = SPECIAL_MARKET_CAPS[sub_market]
                if odds > cfg["max_odds"]:
                    return 0.0
                return cfg["max_stake"]

            league_data = _match_league(league, PIN_1X2_DATA)
            if league_data is PIN_1X2_DATA.get("_AGGREGATE"):
                discount = 0.85
            else:
                discount = 1.0

            if not league_data:
                return 0.0
            data = league_data.get(idx)
            if not data:
                return 0.0
            wr, avg_o, n = data
            if n < MIN_N_MINIMUM:
                return 0.0
            bb_prem = _bb_premium_1x2(odds)
            stake = kelly_075(wr, avg_o, bb_prem, n)
            return stake * discount

    # ── Tennis (V4.2: 直接编码, 不再复用V3) ──
    elif sport_lower == "tennis":
        # 挑战赛/ITF 无可靠数据 → 封杀
        for kw in ("Challenger", "ITF", "W15", "M15", "W25", "M25"):
            if kw.lower() in (league or "").lower():
                return 0.0

        edge_data = _match_tournament(league)
        if edge_data is None:
            return 0.0

        idx = _bin_index(odds, TENNIS_ODDS_BINS)
        entry = edge_data.get(idx)
        if entry is None:
            return 0.0
        pin_roi_pct, bb_prem_pct = entry

        # 网球 Kelly: edge = (1 + pin_roi/100) × (1 + bb_prem/100) - 1
        pin_factor = 1.0 + pin_roi_pct / 100.0
        bb_factor = 1.0 + bb_prem_pct / 100.0
        edge = pin_factor * bb_factor - 1.0
        if edge <= 0:
            return 0.0
        full_kelly = edge / (bb_factor - 1.0) if bb_factor > 1.0 else edge
        return min(0.06, max(0.0, full_kelly * 0.75))

    # ── Baseball ──
    elif sport_lower == "baseball":
        idx = _bin_index(odds, ODDS_BINS)
        # SBR 10年数据 (优先)
        data = MLB_DATA.get(idx)
        if data and data[2] >= MIN_N_MINIMUM:
            wr, avg_o, n = data
            stake = kelly_075(wr, avg_o, 0.05, n)
            return stake * DISCOUNT_SBR_LARGE
        # OddsPortal
        data2 = MLB_ODDSPORTAL_DATA.get(idx)
        if data2 and data2[2] >= MIN_N_MINIMUM:
            wr, avg_o, n = data2
            stake = kelly_075(wr, avg_o, 0.05, n)
            return stake * DISCOUNT_ODDSPORTAL
        return 0.0

    # ── Basketball ──
    elif sport_lower == "basketball":
        if "NBA" in (league or ""):
            idx = _bin_index(odds, ODDS_BINS)
            data = NBA_DATA.get(idx)
            if data and data[2] >= MIN_N_MINIMUM:
                wr, avg_o, n = data
                stake = kelly_075(wr, avg_o, 0.05, n)
                return stake * DISCOUNT_SBR_LARGE
            return 0.0
        elif "WNBA" in (league or ""):
            idx = _bin_index(odds, ODDS_BINS)
            data = NBA_DATA.get(idx)
            if data and data[2] >= MIN_N_MINIMUM:
                wr, avg_o, n = data
                stake = kelly_075(wr, avg_o, 0.04, n)
                return stake * DISCOUNT_WNBA
            return 0.0
        else:
            return 0.01 if odds < 2.5 else 0.0

    # ── American Football ──
    elif sport_lower == "american_football":
        idx = _bin_index(odds, ODDS_BINS)
        data = NFL_DATA.get(idx)
        if data and data[2] >= MIN_N_MINIMUM:
            wr, avg_o, n = data
            stake = kelly_075(wr, avg_o, 0.05, n)
            return stake * DISCOUNT_SBR_MEDIUM
        return 0.0

    # ── Ice Hockey ──
    elif sport_lower == "ice_hockey":
        idx = _bin_index(odds, ODDS_BINS)
        data = NHL_DATA.get(idx)
        if data and data[2] >= MIN_N_MINIMUM:
            wr, avg_o, n = data
            stake = kelly_075(wr, avg_o, 0.05, n)
            return stake * DISCOUNT_SBR_LARGE
        return 0.0

    # ── 乒/羽/排 ──
    elif sport_lower in ("pingpong", "badminton", "volleyball"):
        if odds > 2.5:
            return 0.0
        return 0.01

    # ── 完全未知 ──
    else:
        if odds > 2.5:
            return 0.0
        return 0.005


# =====================================================================
# EV 门槛 — V4.1: 统一 2%，让 Kelly 做主
# =====================================================================
def get_min_ev(sport: str, league: str, sub_market: str, odds: float) -> float:
    """最小 EV 门槛。stake>0 → 2%, stake=0 → 999。"""
    sport_lower = (sport or "").lower()
    if sub_market in BLOCKED_MARKETS:
        return 999.0

    if sport_lower == "football":
        stake = get_kelly_stake_pct(sport, league, sub_market, odds)
        return 2.0 if stake > 0 else 999.0

    elif sport_lower == "tennis":
        return 2.0

    elif sport_lower == "basketball":
        stake = get_kelly_stake_pct(sport, league, sub_market, odds)
        return 2.0 if stake > 0 else 999.0

    elif sport_lower in ("baseball", "american_football"):
        return 2.0 if odds < 5.0 else 999.0

    elif sport_lower in ("ice_hockey", "pingpong", "badminton", "volleyball"):
        return 2.0 if odds < 3.0 else 999.0

    else:
        return 2.0


def get_odds_cap(sport: str, league: str, sub_market: str) -> float:
    """赔率上限。"""
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
    elif sport_lower in ("baseball", "american_football"):
        return 5.0
    elif sport_lower == "ice_hockey":
        return 4.0
    elif sport_lower in ("mma", "boxing"):
        return 3.0  # V4.2: 仅低赔允许
    else:
        return 3.0


# =====================================================================
# 打印
# =====================================================================
def print_matrix():
    """打印完整权重矩阵。"""
    leagues = ["英超","德甲","英冠","意甲","西甲","法甲"]
    bins = ODDS_BINS

    print(f"{'='*100}")
    print(f"V4.2 权重矩阵: 1X2 (Pinnacle 111K场, 0.75-Kelly, 样本置信度折扣)")
    print(f"每格 = 0.75-kelly% × confidence(n) of bankroll, cap=6%")
    print(f"{'='*100}")

    test_odds = []
    prev = 1.01
    for b in bins[:20]:
        test_odds.append(round((prev + b) / 2, 1))
        prev = b

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
    print(f"公式: 0.75_kelly = max(0, wr × BB_odds - 1) / (BB_odds - 1) × 0.75 × confidence(n)")
    print(f"BB_odds = Pin_odds × (1 + bb_premium)")
    print(f"BB溢价: 从实际 BB vs Pin 对比数据中位数统计 (2026-08-01)")
    print(f"数据源: football-data.co.uk Pinnacle 收盘赔率 2012-2025")
    print(f"网球: Pinnacle 5,013场, 直接编码 (V4.2)")
    print(f"样本置信度: n≥100=1.0, n≥50=0.95, n≥30=0.85, n≥10=0.70")


if __name__ == "__main__":
    print_matrix()
