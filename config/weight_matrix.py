"""投注权重矩阵 — 基于全量历史回测

数据源与质量等级:
  S级 (Pin收盘赔率 × 赛果, 大样本):
    - 足球 1X2: 39,493场, 18联赛, 5个赔率区间, Pinnacle收盘
    - 网球:    5,013场, 4赛事级别×7赔率区间, Pinnacle收盘
  A级 (长期回测, 无个体赔率区间):
    - NBA:    57,504场, 15赛季, ML/Spread/OU年化ROI
  B级 (仅单赛季, 小样本):
    - 足球 OU2.5: 6,500场, 2425赛季
  C级 (无Pin历史数据, 基于我们267笔结算):
    - 棒球/美式足球/MMA/拳击/冰球
    - 半全场/双重机会/BTTS等盘口

每项权重 = (运动, 联赛关键字, 盘口类型, 赔率区间) → 仓位%
"""
from functools import lru_cache

# =====================================================================
# FOOTBALL 1X2 权重矩阵 [S级]
# 数据源: Football-Data Pinnacle收盘赔率 39,493场 × 3 outcomes
# =====================================================================
FB_1X2_WEIGHTS = {
    # league_keyword → [<2.0, 2.0-3.0, 3.0-5.0, 5.0-10.0, >10.0]
    # 数据源: Football-Data Pinnacle 收盘 39,493 场 × 3 outcomes = 118,479 bets
    # 权重逻辑: Pin收盘ROI + BB溢价(约+7%) → 综合优势 → 仓位%
    #   PinROI > 0% → BB edge on top = strong → 4-6%
    #   PinROI -5%~0% → BB edge covers vig → 2-4%
    #   PinROI -10%~-5% → BB edge partially covers → 1-2%
    #   PinROI < -10% → BB edge insufficient → 0%

    # --- >10.0 正收益联赛 (favorite-longshot bias confirmed) ---
    # 法甲 >10 +29%, 西甲 >10 +8%, 意甲 >10 +5%, 英超 >10 +5%, 德甲 >10 +4%
    #    [ <2.0, 2.0-3.0, 3.0-5.0, 5.0-10.0, >10.0 ]
    "德甲": [4, 6, 4, 2, 2],    # >10 +4%, 5-10 -5%. 3-5 +1.7%!
    "德乙": [2, 2, 4, 0, 0],    # 3-5 -1.5% (best available)
    "英超": [4, 6, 2, 2, 2],    # >10 +5%!, 5-10 +2%!
    "英冠": [4, 4, 2, 3, 0],    # 5-10 +8.1%!! best high-odds league
    "英甲": [6, 6, 2, 0, 0],    # <2 +1.9% (top tier)
    "英乙": [2, 4, 2, 0, 0],    # 5-10 -9.4% (skip)
    "西甲": [4, 4, 2, 0, 2],    # >10 +8%!, 5-10 -19% skip
    "西乙": [4, 2, 2, 2, 0],    # 5-10 -1.2% borderline
    "意甲": [4, 2, 2, 0, 2],    # >10 +5%!, 5-10 -21% skip
    "意乙": [4, 2, 2, 2, 0],    # 5-10 -1.2% borderline
    "法甲": [4, 4, 2, 0, 3],    # >10 +29%!!! best >10 league
    "法乙": [4, 6, 0, 0, 0],    # 3-5 -5.8% borderline skip
    "荷甲": [2, 2, 4, 0, 0],    # 3-5 +0.3% barely positive
    "葡超": [2, 6, 2, 0, 0],    # 5-10 -22% hard skip
    "土超": [6, 4, 0, 0, 0],    # <2 +6.3%!! best low-odds league
    "比甲": [4, 2, 2, 0, 0],    # 3-5 -7.3% skip
    "苏超": [4, 4, 2, 0, 0],    # 5-10 -15% skip
    "希超": [2, 2, 2, 0, 0],    # 5-10 -16% skip, 3-5 +0.4% marginal
    "_DEFAULT": [3, 3, 0, 0, 0],  # 未覆盖联赛：≥3.0全跳过(结算数据-12%~-21% ROI)
}

# =====================================================================
# FOOTBALL OU2.5 权重矩阵
# =====================================================================
FB_OU_WEIGHTS = {
    "德甲": [4, 2, 4, 0, 0],
    "德乙": [4, 2, 0, 0, 0],
    "英超": [2, 6, 4, 0, 0],
    "英冠": [4, 2, 0, 0, 0],
    "英甲": [4, 2, 0, 0, 0],
    "英乙": [4, 2, 0, 0, 0],
    "西甲": [2, 2, 2, 0, 0],
    "西乙": [2, 2, 2, 0, 0],
    "意甲": [4, 2, 0, 0, 0],
    "意乙": [2, 6, 0, 0, 0],
    "法甲": [4, 2, 0, 0, 0],
    "法乙": [4, 2, 0, 0, 0],
    "荷甲": [2, 6, 0, 0, 0],
    "葡超": [4, 2, 0, 0, 0],
    "土超": [2, 4, 0, 0, 0],
    "希超": [2, 2, 0, 0, 0],
    "_DEFAULT": [2, 2, 1, 0, 0],
}

# =====================================================================
# TENNIS 权重 (按赛事级别 + 赔率区间)
# 数据源: Pinnacle 5,013场
# =====================================================================
TENNIS_WEIGHTS = {
    # 学术研究: ML模型 + 1.70-1.85阈值 → Grand Slam +10.33% ROI
    # [<1.3, 1.3-1.5, 1.5-2.0, 2.0-3.0, 3.0-5.0, >5.0]
    "Grand Slam": [4, 3, 4, 1, 0, 0],   # 1.5-2.0 提至 4% (ML研究验证)
    "Masters":    [4, 4, 4, 3, 0, 0],    # 1.5-2.0 提至 4%
    "ATP 500":    [3, 3, 3, 1, 0, 0],    # 1.5-2.0 提至 3%
    "ATP 250":    [3, 4, 3, 0, 0, 0],    # 1.5-2.0 提至 3%
    "WTA":        [3, 3, 3, 0, 0, 0],
    "Challenger": [2, 2, 1, 0, 0, 0],
    "ITF":        [1, 1, 0, 0, 0, 0],
    "W15":        [1, 1, 0, 0, 0, 0],
    "M15":        [1, 1, 0, 0, 0, 0],
    "W25":        [1, 1, 0, 0, 0, 0],
    "M25":        [1, 1, 0, 0, 0, 0],
    "_DEFAULT":   [2, 2, 1, 0, 0, 0],
}

# 网球赔率区间 (不同于足球, 网球赔率更低)
# [<1.3, 1.3-1.5, 1.5-2.0, 2.0-3.0, 3.0-5.0, >5.0]
TENNIS_ODDS_BUCKETS = [1.3, 1.5, 2.0, 3.0, 5.0, float('inf')]


def _get_odds_index(odds: float, sport: str = "football") -> int:
    """根据赔率和运动返回权重表索引。"""
    if sport in ("tennis",):
        for i, threshold in enumerate(TENNIS_ODDS_BUCKETS):
            if odds < threshold:
                return i
        return len(TENNIS_ODDS_BUCKETS) - 1
    else:
        # 足球/篮球/棒球等使用通用区间
        if odds < 2.0: return 0
        elif odds < 3.0: return 1
        elif odds <= 5.0: return 2   # 5.0 归入 3-5 区间（含边界）
        elif odds <= 10.0: return 3
        else: return 4


@lru_cache(maxsize=512)
def get_stake_pct(sport: str, league: str, sub_market: str, odds: float) -> float:
    """返回该投注的最大仓位比例 (0.0 ~ 0.06)。

    Args:
        sport: 运动类型 (football, tennis, basketball, baseball, etc.)
        league: 联赛名
        sub_market: 盘口类型 (1x2, hc, ou, dc, ht, btts, etc.)
        odds: BB赔率

    Returns:
        仓位比例 (0.0 = 不投, 0.06 = 6%)
    """
    idx = _get_odds_index(odds, sport)

    if sport == "football":
        if sub_market in ("1x2", "ht", "dc"):
            table = FB_1X2_WEIGHTS
        elif sub_market in ("ou",):
            table = FB_OU_WEIGHTS
        elif sub_market in ("btts", "oe", "dnb"):
            table = FB_1X2_WEIGHTS  # 用1X2作为代理 (数据有限)
        elif sub_market in ("hc",):
            # AH: 学术研究(Hegarty & Whelan 2025)确认无 favorite-longshot bias
            # BB 在此盘口对 Pinnacle 无定价优势，全体跳过
            # 注: 非整数线(-0.5/-0.25等)损失率 4.7% vs 整数线 2.9%
            return 0.0
        elif sub_market in ("htft",):
            # 半全场: 所有数据源都显示虚高
            return 0.0
        else:
            table = FB_1X2_WEIGHTS

    elif sport == "tennis":
        return _get_tennis_stake(league, idx)

    elif sport == "basketball":
        # NBA 让分 +2.22% 年化, 稳定15年 → 6%
        # 其他篮球联赛数据有限 → 2%
        if "NBA" in league:
            return 0.06
        elif sub_market == "hc":
            return 0.04
        else:
            return 0.03

    elif sport == "baseball":
        # 小样本高ROI (3笔 +112%)
        if odds < 3.0:
            return 0.04
        else:
            return 0.02

    elif sport in ("mma", "boxing"):
        # UFC/拳击映射错误率高, 且数据极少 → 跳过
        return 0.0

    elif sport == "american_football":
        # NFL/NCAAF 数据极有限
        if odds < 3.0:
            return 0.02
        else:
            return 0.0

    else:
        # 未知运动: 最低限度
        return 0.01 if odds < 3.0 else 0.0

    # --- League lookup ---
    # 按关键字长度降序匹配，短关键字(<=2字)必须出现在联赛名开头，防止 "西甲" 误匹配 "巴西甲级联赛"
    for keyword, weights in sorted(table.items(), key=lambda x: -len(x[0])):
        if keyword == "_DEFAULT":
            continue
        if len(keyword) <= 2 and not league.startswith(keyword):
            continue  # 短关键字必须是前缀，避免子串误匹配
        if keyword in league:
            return weights[min(idx, len(weights) - 1)] / 100.0

    # 未匹配的联赛用默认值
    default = table.get("_DEFAULT", [3, 3, 2, 1, 0])
    return default[min(idx, len(default) - 1)] / 100.0


def _get_tennis_stake(league: str, idx: int) -> float:
    """网球专属权重查询。"""
    for keyword, weights in TENNIS_WEIGHTS.items():
        if keyword == "_DEFAULT":
            continue
        if keyword.lower() in league.lower():
            return weights[min(idx, len(weights) - 1)] / 100.0
    default = TENNIS_WEIGHTS["_DEFAULT"]
    return default[min(idx, len(default) - 1)] / 100.0
