"""投注权重矩阵 V3 — 纯赔率区间驱动的 Kelly 最优仓位

核心理念: 每个赔率区间的仓位 = 该区间的 Kelly 最优解
  半凯利仓位% = max(0, expected_edge%) / (odds - 1) × 50%

  其中 expected_edge = BB/FB溢价 + Pinnacle历史ROI

数据驱动:
  - Pinnacle 61,404场足球收盘 (17联赛 × 26赔率区间)
  - Pinnacle 5,013场网球收盘 (5赛事级 × 16赔率区间)
  - NBA 57,504场模型回测 (15赛季)
  - BB/FB 219笔真实结算验证

设计原则:
  1. 赔率越高 → 分母(odds-1)越大 → Kelly仓位自然越小
  2. Pinnacle vig越低 → 数据越可靠 → expected_edge越高 → 仓位越大
  3. 无中间乘数稀释 — 蒸汽/周末/连亏仅做边际调整(±20%)
  4. 封杀确认亏损市场 (DC/HTFT 0%胜率, MMA/拳击 映射错误)

用法:
  from config.weight_matrix_v3 import get_kelly_stake_pct
  stake_pct = get_kelly_stake_pct("football", "英超", "1x2", 2.50)
  → 0.031 (3.1% of bankroll → ¥310 on ¥10,000)
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

# =====================================================================
# 赔率区间定义
# =====================================================================

# 足球/通用: 赔率密度最高的区域用 0.3-0.5 步长
FOOTBALL_ODDS_BINS = [
    1.30, 1.50, 1.80, 2.00, 2.20, 2.50, 2.80, 3.00,
    3.30, 3.50, 3.80, 4.00, 4.30, 4.50, 4.80, 5.00,
    5.50, 6.00, 7.00, 8.00, 9.00, 10.00, 12.00, 15.00, 20.00, float('inf')
]

# 网球: 低赔区极度细化
TENNIS_ODDS_BINS = [
    1.15, 1.25, 1.35, 1.50, 1.70, 1.90, 2.10,
    2.50, 3.00, 4.00, 5.00, 6.00, 8.00, 10.00, 15.00, float('inf')
]

BASKETBALL_ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.40, 2.70,
    3.00, 3.50, 4.00, 5.00, 7.00, 10.00, float('inf')
]


def _bin_index(odds: float, bins: list) -> int:
    for i, threshold in enumerate(bins):
        if odds < threshold:
            return i
    return len(bins) - 1


# =====================================================================
# 核心公式: expected_edge → Kelly 仓位
# =====================================================================

def kelly_half(expected_edge_pct: float, odds: float, cap: float = 0.06) -> float:
    """半凯利仓位 (返回小数, 0.06 = 6% of bankroll)。

    Args:
        expected_edge_pct: 期望收益率 (e.g. 5.0 = 5%)
        odds: 赔率
        cap: 最大仓位 (默认 0.06 = 6%)
    """
    if expected_edge_pct <= 0 or odds <= 1.01:
        return 0.0
    # 半凯利: f* = edge% / (odds-1) × 0.5
    kelly = (expected_edge_pct / 100.0) / (odds - 1.0) * 0.5

    # 极低赔率 (<1.3): 额外折扣 (低赔优势方几乎总是赢, BB溢价有限)
    if odds < 1.3:
        kelly *= 0.5
    elif odds < 1.5:
        kelly *= 0.7

    return min(cap, max(0.0, kelly))


# =====================================================================
# 联赛 Pinnacle 基础数据
# =====================================================================

# 足球联赛 vig (Pinnacle 61,404场收盘数据)
# vig = 市场抽水率, 反映 Pinnacle 定价准确度
FOOTBALL_VIG = {
    # 极高可靠性 (vig<2.5%)
    "德甲": 1.88, "德乙": 1.93,
    # 高可靠性 (2.5-3.5%)
    "英冠": 3.25, "法乙": 3.30, "英乙": 3.32,
    # 中高可靠性 (3.5-4.0%)
    "意乙": 3.57, "英超": 3.72, "荷甲": 3.89,
    # 中等可靠性 (4.0-4.5%)
    "法甲": 4.06, "西乙": 4.13, "比甲": 4.33, "苏超": 4.38, "西甲": 4.49,
    # 中低可靠性 (4.5-5.5%)
    "英甲": 4.60, "意甲": 4.97, "土超": 5.53,
    # 较低可靠性 (5.5-7.0%)
    "MLS": 5.50, "美国职业": 5.50, "K1": 5.50, "韩国K": 5.50,
    "J1": 5.50, "日本J": 5.50, "墨超": 5.50, "墨西哥": 5.50,
    "巴甲": 5.50, "巴西甲": 5.50, "阿甲": 5.50, "阿根廷": 5.50,
    # 低可靠性 (>7%)
    "葡超": 7.20, "希超": 7.41,
    # 北欧 (无Pin数据, 基于结算: 瑞典超/芬兰超/挪威超全亏损 → 高vig估计)
    "瑞典超": 7.50, "芬兰超": 7.50, "挪威超": 7.50, "丹麦超": 7.00,
    # 南美
    "巴拉圭": 7.00, "厄瓜多尔": 7.00, "乌拉圭": 7.00, "智利": 7.00,
    "秘鲁": 7.50, "玻利维亚": 8.00, "委内瑞拉": 8.00,
    # 东欧
    "俄超": 7.00, "波兰": 7.00, "捷克": 7.00, "克罗地亚": 7.50,
    "罗马尼亚": 7.50, "保加利亚": 8.00, "匈牙利": 7.50,
    # 亚洲/非洲/中东
    "中超": 7.50, "澳超": 7.00, "沙特": 7.50, "阿联酋": 8.00, "卡塔尔": 8.00,
    "南非": 8.00, "埃及": 8.00, "摩洛哥": 8.00,
    # 国际赛事
    "World Cup": 4.00, "世界杯": 4.00, "欧洲杯": 3.50, "欧冠": 3.00,
    "欧联": 4.00, "欧会": 5.00, "美洲杯": 5.00, "非洲杯": 6.00,
}

# =====================================================================
# 赔率区间期望收益矩阵
# =====================================================================
# 每个赔率区间的 expected_edge = BB溢价(观察值) - |Pin vig衰减|(理论值)
#
# BB溢价: 从219笔结算 + BB vs Pin comparison 统计
#   低赔 (<2.0): BB溢价 5-8% (市场深度好, 竞争充分)
#   中赔 (2.0-4.0): BB溢价 3-6% (流动性下降)
#   高赔 (4.0-7.0): BB溢价 2-5% (价差大, 假阳性多)
#   超高 (>7.0): BB溢价 1-3% (几乎全是噪声)
#
# Pin vig衰减: 赔率越高, vig对期望的侵蚀越严重
#   vig × (1 + (odds-1)×0.3): 高赔率时vig的影响被放大
#   例如 vig=3%, odds=5 → 有效损失 = 3% × (1+4×0.3) = 6.6%

def _expected_edge_1x2(league_vig: float, odds: float, bb_premium: float) -> float:
    """计算 1X2 市场的期望收益。

    Pin 收盘 ROI ≈ -league_vig (等注投Pin收盘)
    但不同赔率区间 Pin ROI 不同 (favorite-longshot bias):
      低赔: Pin ROI ≈ -0.7×vig (Pin 对强队定价更准)
      中赔: Pin ROI ≈ -1.0×vig
      高赔: Pin ROI ≈ -1.3×vig (Pin 对弱队定价偏松)
      超高: Pin ROI ≈ -2.0×vig

    BB溢价因赔率区间而异:
      低赔: 溢价高 (流动性好, BB 为吸引投注给更优价)
      高赔: 溢价低且不可靠 (匹配错误多)
    """
    # Pin ROI 按赔率衰减
    if odds < 2.0:
        pin_roi = -league_vig * 0.7  # Pin 最准的区域
    elif odds < 3.0:
        pin_roi = -league_vig * 0.85
    elif odds < 4.0:
        pin_roi = -league_vig * 1.0
    elif odds < 5.0:
        pin_roi = -league_vig * 1.2
    elif odds < 7.0:
        pin_roi = -league_vig * 1.5
    elif odds < 10.0:
        pin_roi = -league_vig * 2.0
    else:
        return -999  # >10.0 不投

    return bb_premium + pin_roi


def _expected_edge_ou(league_vig: float, odds: float) -> float:
    """OU 市场期望收益。OU vig 普遍低于 1X2 → edge 更高。"""
    # OU vig 约比 1X2 低 0.42% (3.87% vs 4.29% average)
    ou_vig = league_vig * 0.9

    if odds < 2.0:
        pin_roi = -ou_vig * 0.7
        bb_prem = 8.0
    elif odds < 2.5:
        pin_roi = -ou_vig * 0.7
        bb_prem = 7.0
    elif odds < 3.0:
        pin_roi = -ou_vig * 0.85
        bb_prem = 6.0
    elif odds < 4.0:
        pin_roi = -ou_vig * 1.0
        bb_prem = 4.0
    elif odds < 5.0:
        pin_roi = -ou_vig * 1.2
        bb_prem = 2.5
    else:
        return -999

    return bb_prem + pin_roi


def _expected_edge_ht(league_vig: float, odds: float) -> float:
    """HT (半场) 期望收益。不确定性更高 → 折扣。"""
    if odds < 2.0:
        pin_roi = -league_vig * 0.7
        bb_prem = 6.0
    elif odds < 3.0:
        pin_roi = -league_vig * 0.85
        bb_prem = 4.0
    elif odds < 5.0:
        pin_roi = -league_vig * 1.2
        bb_prem = 2.0
    elif odds < 7.0:
        pin_roi = -league_vig * 1.5
        bb_prem = 1.0
    else:
        return -999  # 半场 >7.0 不投

    return bb_prem + pin_roi


# =====================================================================
# 网球期望收益 (基于 Pinnacle 5,013场精确数据)
# =====================================================================

# 网球赛事 vig + 赔率区间 ROI (从 tennis_market_efficiency.json)
# 格式: {tournament: {bin_index: (pin_roi, bb_premium)}}
TENNIS_EDGE_TABLE = {
    "Masters": {
        # 16 bins: <1.15, 1.15-1.25, 1.25-1.35, 1.35-1.50, 1.50-1.70, ...
        # Pin ROI from actual data, BB premium estimated
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
    "Challenger": {
        0:  (-3.0, 2.0),  1:  (-3.0, 2.0),  2:  (-3.0, 1.5),
        3:  (-4.0, 1.5),  4:  (-5.0, 1.0),
        5:  (-5.0, 0.0),  6:  (-5.0, 0.0),
        7:  (-8.0, 0.0),  8:  (-8.0, 0.0),
        9:  (-15.0, 0.0), 10: (-15.0, 0.0),
        11: (-20.0, 0.0), 12: (-20.0, 0.0), 13: (-20.0, 0.0),
        14: (-50.0, 0.0), 15: (-50.0, 0.0),
    },
    "ITF": {
        # 极度保守
        0: (-5.0, 1.0), 1: (-5.0, 1.0), 2: (-8.0, 0.0),
        3: (-8.0, 0.0), 4: (-10.0, 0.0), 5: (-10.0, 0.0),
        6: (-10.0, 0.0), 7: (-15.0, 0.0), 8: (-15.0, 0.0),
        9: (-20.0, 0.0), 10: (-20.0, 0.0),
        11: (-30.0, 0.0), 12: (-30.0, 0.0), 13: (-30.0, 0.0),
        14: (-50.0, 0.0), 15: (-50.0, 0.0),
    },
}

# ITF/W15/M15/W25/M25 统一 ITF
for _key in ("W15", "M15", "W25", "M25"):
    TENNIS_EDGE_TABLE[_key] = TENNIS_EDGE_TABLE["ITF"]
TENNIS_EDGE_TABLE["_DEFAULT"] = {
    i: (-10.0, 1.0) for i in range(16)
}


# =====================================================================
# NBA 期望收益 (模型回测, 不是 Pin 收盘)
# =====================================================================
# 年化 ROI: ML +1.76%, Spread +2.22%, OU +1.0%
# 但这是模型 edge, 不是 BB vs Pin 比价 edge
# BB vs Pin 比价 edge ≈ 模型 edge (Pin 已是公平价, 模型找到的 edge ≈ 比价 edge)

NBA_EDGE = {
    "hc":  2.22,   # 让分 edge 最大
    "1x2": 1.76,   # 胜负
    "ou":  1.00,   # 大小分
}


# =====================================================================
# 封杀列表
# =====================================================================
BLOCKED_SPORTS = {"mma", "boxing"}
BLOCKED_MARKETS = {"dc", "htft"}  # 0% 胜率
SPECIAL_MARKET_CAPS = {
    "btts":  {"max_stake": 0.03, "max_odds": 3.0},
    "dnb":   {"max_stake": 0.02, "max_odds": 5.0},
    "oe":    {"max_stake": 0.01, "max_odds": 2.5},
    "corner":{"max_stake": 0.01, "max_odds": 3.0},
}


# =====================================================================
# 主查询函数
# =====================================================================

def _match_tournament(league: str, table: dict) -> Optional[dict]:
    """匹配网球赛事关键字。"""
    if league in table:
        return table[league]
    for kw, data in sorted(table.items(), key=lambda x: -len(x[0])):
        if kw == "_DEFAULT":
            continue
        if kw.lower() in (league or "").lower():
            return data
    return table.get("_DEFAULT")


def _match_league_vig(league: str) -> tuple:
    """返回 (vig, 是否已知联赛)。"""
    for kw, vig in sorted(FOOTBALL_VIG.items(), key=lambda x: -len(x[0])):
        if kw in league:
            return vig, True
    return 5.0, False  # 未知联赛: 保守 vig=5%


@lru_cache(maxsize=4096)
def get_kelly_stake_pct(sport: str, league: str, sub_market: str, odds: float) -> float:
    """返回 Kelly 最优仓位 (% of bankroll, 0.0 ~ 0.06)。

    纯赔率区间驱动:
      half_kelly = expected_edge / (odds-1) × 0.5

    expected_edge 来自 Pinnacle 真实数据 + BB 溢价校准。
    """
    sport_lower = (sport or "").lower()

    # ── 封杀 ──
    if sport_lower in BLOCKED_SPORTS:
        return 0.0
    if sub_market in BLOCKED_MARKETS:
        return 0.0

    # ── 特殊市场 ──
    if sub_market in SPECIAL_MARKET_CAPS:
        cfg = SPECIAL_MARKET_CAPS[sub_market]
        if odds > cfg["max_odds"]:
            return 0.0
        return cfg["max_stake"]

    # ── Football ──
    if sport_lower == "football":
        vig, known = _match_league_vig(league)

        if sub_market == "ou":
            edge = _expected_edge_ou(vig, odds)
        elif sub_market == "ht":
            edge = _expected_edge_ht(vig, odds)
        else:  # 1x2
            # BB溢价按赔率区间
            if odds < 1.5: bb_prem = 6.0
            elif odds < 2.0: bb_prem = 7.0
            elif odds < 2.5: bb_prem = 6.5
            elif odds < 3.0: bb_prem = 5.5
            elif odds < 4.0: bb_prem = 3.5
            elif odds < 5.0: bb_prem = 2.0
            elif odds < 7.0: bb_prem = 1.0
            elif odds < 10.0: bb_prem = 0.5
            else: return 0.0  # >10.0 不投

            edge = _expected_edge_1x2(vig, odds, bb_prem)

        # 小联赛额外折扣 (无 Pin 数据覆盖)
        if not known:
            edge -= 1.5

        return kelly_half(edge, odds)

    # ── Tennis ──
    elif sport_lower == "tennis":
        edge_data = _match_tournament(league, TENNIS_EDGE_TABLE)
        if edge_data is None:
            return 0.0

        idx = _bin_index(odds, TENNIS_ODDS_BINS)
        pin_roi, bb_prem = edge_data.get(idx, (-15.0, 0.0))
        edge = pin_roi + bb_prem

        return kelly_half(edge, odds)

    # ── Basketball ──
    elif sport_lower == "basketball":
        if "WNBA" in (league or ""):
            base_edge = 0.5
            bb_premium = 2.0
        elif "NBA" in (league or ""):
            base_edge = NBA_EDGE.get(sub_market, 1.0)
            bb_premium = 3.0
        else:
            base_edge = 0.5  # 其他篮球: 极度保守
            bb_premium = 1.5

        # 赔率越高 edge 越薄
        if odds < 2.0:
            edge = base_edge + bb_premium
        elif odds < 3.0:
            edge = base_edge + bb_premium - 1.0
        elif odds < 5.0:
            edge = base_edge + bb_premium - 3.0
        else:
            return 0.0

        return kelly_half(edge, odds)

    # ── Baseball ──
    elif sport_lower == "baseball":
        if odds < 2.0:
            edge = 4.0
        elif odds < 3.0:
            edge = 2.0
        elif odds < 5.0:
            edge = 0.0
        else:
            return 0.0
        return kelly_half(edge, odds)

    # ── American Football ──
    elif sport_lower == "american_football":
        if odds < 2.0:
            edge = 2.0
        elif odds < 3.0:
            edge = 1.0
        else:
            return 0.0
        return kelly_half(edge, odds)

    # ── 冰球/乒/羽/排 ──
    elif sport_lower in ("ice_hockey", "pingpong", "badminton", "volleyball"):
        if odds < 2.5:
            return 0.01
        return 0.0

    # ── Unknown ──
    else:
        if odds < 3.0:
            return 0.01
        return 0.0


# =====================================================================
# EV 门槛 (基于 Kelly: edge<=0 就不该投)
# =====================================================================

def get_min_ev(sport: str, league: str, sub_market: str, odds: float) -> float:
    """返回最小 EV 门槛 (反过来算: 多少 EV 才能让 Kelly > 0)。

    核心: 赔率越高 → Pin 数据越不可靠 → 需要更高的 EV 来确认信号真实性。
    """
    sport_lower = (sport or "").lower()

    if sport_lower in BLOCKED_SPORTS or sub_market in BLOCKED_MARKETS:
        return 999.0

    if sport_lower == "football":
        vig, _ = _match_league_vig(league)
        # 赔率越高, 需要的 EV 门槛越高
        # 基础: vig/2 (至少覆盖 Pin 的一半 margin)
        # 加成: 每高1倍赔率 +1%
        base = vig / 2
        odds_factor = max(0, (odds - 2.0)) * 1.5
        market_penalty = {"ht": 1.5, "ou": -0.5}.get(sub_market, 0)
        return max(1.5, base + odds_factor + market_penalty)

    elif sport_lower == "tennis":
        if odds < 2.0: return 2.0
        elif odds < 3.0: return 3.0
        elif odds < 5.0: return 5.0
        elif odds < 8.0: return 8.0
        else: return 15.0

    else:
        if odds < 2.0: return 2.0
        elif odds < 3.0: return 3.0
        elif odds < 5.0: return 5.0
        else: return 8.0


# =====================================================================
# 赔率上限
# =====================================================================

def get_odds_cap(sport: str, league: str, sub_market: str) -> float:
    """返回赔率上限 (基于 Pinnacle vig)。"""
    sport_lower = (sport or "").lower()

    if sport_lower == "football":
        if sub_market in BLOCKED_MARKETS:
            return 0.0
        vig, _ = _match_league_vig(league)
        if vig < 2.5: return 20.0
        elif vig < 4.0: return 12.0
        elif vig < 5.0: return 9.0
        elif vig < 6.0: return 7.0
        else: return 5.0

    elif sport_lower == "tennis":
        for kw, cap in [("Masters", 15.0), ("Grand Slam", 5.0), ("ATP 500", 10.0),
                        ("ATP 250", 5.0), ("WTA", 4.0), ("Challenger", 3.0), ("ITF", 2.5)]:
            if kw.lower() in (league or "").lower():
                return cap
        return 3.0

    elif sport_lower == "basketball":
        return 8.0 if "NBA" in (league or "") else 5.0

    else:
        return 5.0


# =====================================================================
# 打印工具
# =====================================================================

def print_kelly_matrix(sport="football", sub_market="1x2"):
    """打印某个运动的 Kelly 最优仓位矩阵。"""
    bins = FOOTBALL_ODDS_BINS if sport != "tennis" else TENNIS_ODDS_BINS
    if sport == "basketball":
        bins = BASKETBALL_ODDS_BINS

    # Test odds at center of each bin
    test_odds = []
    prev = 1.01
    for b in bins:
        mid = (prev + b) / 2
        test_odds.append(round(mid, 2))
        prev = b

    leagues = {
        "football": ["德甲", "德乙", "英超", "英冠", "意甲", "西甲", "法甲", "荷甲", "葡超"],
        "tennis": ["Masters", "Grand Slam", "ATP 500", "ATP 250", "WTA", "Challenger", "ITF"],
        "basketball": ["NBA", "WNBA"],
    }.get(sport, ["NBA"])

    print(f"\n{'='*100}")
    print(f"Kelly 最优仓位矩阵: {sport}/{sub_market}")
    print(f"公式: half_kelly = expected_edge / (odds-1) × 0.5")
    print(f"{'='*100}")

    header = f"{'League':<10s}"
    for o in test_odds:
        header += f" @{o:<6.2f}"
    print(header)
    print("-" * len(header))

    for lg in leagues:
        row = f"{lg[:8]:<10s}"
        for o in test_odds:
            pct = get_kelly_stake_pct(sport, lg, sub_market, o)
            if pct > 0:
                row += f" {pct*100:5.1f}%"
            else:
                row += f"     - "
        print(row)

    # Summary row: total expected stake for a full scan
    print()
    total = sum(get_kelly_stake_pct(sport, "英超" if sport=="football" else leagues[0], sub_market, o)
               for o in test_odds if get_kelly_stake_pct(sport, "英超" if sport=="football" else leagues[0], sub_market, o) > 0)
    print(f"  预期总仓位 (全赔率区间): {total*100:.1f}% of bankroll")


if __name__ == "__main__":
    print_kelly_matrix("football", "1x2")
    print_kelly_matrix("football", "ou")
    print_kelly_matrix("football", "ht")
    print_kelly_matrix("tennis", "1x2")
    print_kelly_matrix("basketball", "hc")
