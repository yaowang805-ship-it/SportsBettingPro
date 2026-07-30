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
    # [<2.0, 2.0-3.0, 3.0-4.0, 4.0-5.0, 5.0-7.0, 7.0-10.0, >10.0]
    # 基于 Pinnacle 39,493场收盘赔率 × 10区间 ROIs
    # 权重 = f(PinROI): >5%→6%, 0-5%→4%, -5-0%→2%, <-5%→0%
    # BB溢价(~+7%)已内化: PinROI +7% > 0 → 正期望

    # ── 德甲: 3.5-4 +4.3%, 7-10 +4.9%, >10 +3.6% ──
    "德甲":     [4, 6, 4, 4, 2, 2, 2],
    # ── 德乙: <1.5 +5.7%, 4-5 +5.5%, 3-4 mixed ──
    "德乙":     [2, 2, 2, 4, 0, 0, 0],
    # ── 英超: 5-7 +10.6%!, >10 +4.9%, 1.5-2 +0.2% ──
    "英超":     [4, 6, 2, 2, 4, 2, 2],
    # ── 英冠: 5-7 +7.6%!, 7-10 +10%!, <2 mostly OK ──
    "英冠":     [4, 4, 2, 2, 4, 2, 0],
    # ── 英甲: <2 best, higher deteriorating fast ──
    "英甲":     [6, 6, 2, 0, 0, 0, 0],
    # ── 英乙: 4-5 +4.1%, 5-7 flat, rest mixed ──
    "英乙":     [2, 4, 2, 2, 2, 0, 0],
    # ── 西甲: <1.5 +4.3%, 2-2.5 +4%, >10 +7.9%, 5-10 bad ──
    "西甲":     [4, 4, 2, 0, 0, 0, 3],
    # ── 西乙: 5-7 +2.7%, 7-10 bad ──
    "西乙":     [4, 2, 2, 2, 2, 0, 0],
    # ── 意甲: <1.5 good, >10 +5.2%, 5-10 terrible ──
    "意甲":     [4, 2, 2, 2, 0, 0, 2],
    # ── 意乙: 3-3.5 +1.1%, 5-7 +9.6%!, 7-10 terrible ──
    "意乙":     [4, 2, 2, 2, 4, 0, 0],
    # ── 法甲: >10 +28.7%!!, 2-2.5 +1.3%, rest modest ──
    "法甲":     [4, 4, 2, 2, 0, 0, 6],
    # ── 法乙: <1.5 +9.3%!, 2-2.5 +2.3%, higher ranges bad ──
    "法乙":     [6, 4, 0, 0, 0, 0, 0],
    # ── 荷甲: 3-3.5 +6.4%!, 4-5 +2.4%, 5+ bad ──
    "荷甲":     [2, 2, 4, 2, 0, 0, 0],
    # ── 葡超: 2.5-3 best (not split), 3-3.5 +3.4%, 4+ terrible ──
    "葡超":     [2, 6, 2, 0, 0, 0, 0],
    # ── 土超: <2 +4~7%!, 2+ terrible ──
    "土超":     [6, 4, 0, 0, 0, 0, 0],
    # ── 比甲: <3 OK, 3+ mostly negative ──
    "比甲":     [4, 2, 2, 0, 0, 0, 0],
    # ── 苏超: 5-7 +2.7%, 7+ terrible ──
    "苏超":     [4, 4, 2, 2, 2, 0, 0],
    # ── 希超: 3-3.5 +6.1%!, 4+ all negative ──
    "希超":     [2, 2, 4, 0, 0, 0, 0],
    # ── 默认: 保守, 等待更多数据 ──
    "_DEFAULT": [3, 3, 1, 1, 1, 0, 0],
}

# =====================================================================
# FOOTBALL OU2.5 权重矩阵
# =====================================================================
FB_OU_WEIGHTS = {
    # [<2.0, 2.0-3.0, 3.0-4.0, 4.0-5.0, 5.0-7.0, 7.0-10.0, >10.0]
    # OU赔率集中在1.5-2.5, 高赔率几乎不存在, 但仍留7元素保持一致性
    "德甲": [4, 2, 1, 4, 0, 0, 0],
    "德乙": [4, 2, 1, 0, 0, 0, 0],
    "英超": [2, 6, 1, 4, 0, 0, 0],
    "英冠": [4, 2, 1, 0, 0, 0, 0],
    "英甲": [4, 2, 1, 0, 0, 0, 0],
    "英乙": [4, 2, 1, 0, 0, 0, 0],
    "西甲": [2, 2, 1, 2, 0, 0, 0],
    "西乙": [2, 2, 1, 2, 0, 0, 0],
    "意甲": [4, 2, 1, 0, 0, 0, 0],
    "意乙": [2, 6, 1, 0, 0, 0, 0],
    "法甲": [4, 2, 1, 0, 0, 0, 0],
    "法乙": [4, 2, 1, 0, 0, 0, 0],
    "荷甲": [2, 6, 1, 0, 0, 0, 0],
    "葡超": [4, 2, 1, 0, 0, 0, 0],
    "土超": [2, 4, 1, 0, 0, 0, 0],
    "希超": [2, 2, 1, 0, 0, 0, 0],
    "_DEFAULT": [2, 2, 0, 1, 0, 0, 0],
}

# =====================================================================
# TENNIS 权重 (按赛事级别 + 赔率区间)
# 数据源: Pinnacle 5,013场
# =====================================================================
TENNIS_WEIGHTS = {
    # 基于 Pinnacle 5,013场 × 4赛事级别 × 7赔率区间
    # [<1.3, 1.3-1.5, 1.5-2.0, 2.0-3.0, 3.0-5.0, 5.0-10.0, >10.0]
    # ── Grand Slam: vig 5.69%, 1.3-1.5 +6.65%!, >10 -34% ──
    "Grand Slam": [4, 6, 4, 2, 0, 0, 0],
    # ── Masters: vig 1.8%(最锐利), 3-5 +9.4%!!, >10 +7.7%! ──
    "Masters":    [4, 4, 4, 2, 4, 0, 2],
    # ── ATP 500: vig 5.34%, <1.3 +0.5%, 5-10 terrible ──
    "ATP 500":    [4, 4, 4, 2, 2, 0, 0],
    # ── ATP 250: 1.3-1.5 +3.3%!, 5-10 +17%!!, 3-5 terrible ──
    "ATP 250":    [3, 4, 4, 2, 0, 4, 0],
    # ── WTA: 数据有限, 保守参照ATP250 ──
    "WTA":        [3, 3, 3, 2, 0, 0, 0],
    # ── Challenger/ITF: 匹配质量差, 仅低赔率 ──
    "Challenger": [2, 2, 1, 0, 0, 0, 0],
    "ITF":        [1, 1, 0, 0, 0, 0, 0],
    "W15":        [1, 1, 0, 0, 0, 0, 0],
    "M15":        [1, 1, 0, 0, 0, 0, 0],
    "W25":        [1, 1, 0, 0, 0, 0, 0],
    "M25":        [1, 1, 0, 0, 0, 0, 0],
    "_DEFAULT":   [2, 2, 1, 0, 0, 0, 0],
}

# 网球赔率区间 [<1.3, 1.3-1.5, 1.5-2.0, 2.0-3.0, 3.0-5.0, 5.0-10.0, >10.0]
TENNIS_ODDS_BUCKETS = [1.3, 1.5, 2.0, 3.0, 5.0, 10.0, float('inf')]


def _get_odds_index(odds: float, sport: str = "football") -> int:
    """根据赔率和运动返回权重表索引。"""
    if sport in ("tennis",):
        for i, threshold in enumerate(TENNIS_ODDS_BUCKETS):
            if odds < threshold:
                return i
        return len(TENNIS_ODDS_BUCKETS) - 1
    else:
        # 足球/篮球/棒球等: 7区间 (基于39K场Pin收盘10区间ROI合并)
        if odds < 2.0: return 0
        elif odds < 3.0: return 1
        elif odds < 4.0: return 2    # 3.0-4.0 (Pin数据: 联赛差异大)
        elif odds <= 5.0: return 3   # 4.0-5.0
        elif odds <= 7.0: return 4   # 5.0-7.0 (英超+11%! 意乙+10%!)
        elif odds <= 10.0: return 5  # 7.0-10.0 (大部分负, 仅英冠+10%/德甲+5%)
        else: return 6               # >10.0 (五大联赛正ROI)


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
        # NBA: 15年57K场回测, ML+1.76%/Spread+2.22%/OU+1.0%
        # 赔率区间保守估计: NBA赔率集中在1.5-3.0
        if "NBA" in league:
            nba_w = [6, 6, 3, 2, 1, 0, 0]  # 高赔率NBA几乎不存在
            return nba_w[min(idx, 6)] / 100.0
        # 其他篮球联赛: 数据极有限, 保守
        return 0.02 if odds < 4.0 else 0.0

    elif sport == "baseball":
        # MLB: 少量结算(+112% ROI但仅3笔), Pin数据缺失
        # 保守: 低赔率给4%, 中赔率2%, 高赔率跳过
        bb_w = [4, 4, 2, 2, 1, 0, 0]
        return bb_w[min(idx, 6)] / 100.0

    elif sport in ("mma", "boxing"):
        # MMA/拳击: 映射错误率高, 无Pin历史数据 → 跳过
        return 0.0

    elif sport == "american_football":
        # NFL/NCAAF: 无Pin历史数据, 仅少量结算
        nfl_w = [2, 2, 1, 1, 0, 0, 0]
        return nfl_w[min(idx, 6)] / 100.0

    elif sport == "ice_hockey":
        # NHL: 无Pin历史数据
        return 0.01 if odds < 3.0 else 0.0

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
