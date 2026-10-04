"""概率桶分档 — 按运动定制（2026-10-03 从赔率区间改为概率桶）。

favorite-longshot bias（冷门被高估）本质是「概率偏差」：散户高估小概率、低估大概率。
所以按「去水公平概率」（隐含概率 = 1/Betfair公平价）分桶，比按「BB 赔率」分档更贴合机制，
且概率维度均匀（赔率区间在概率维度极不均匀：1.0-1.5 是 34pp 概率跨度、4.5-5.0 只有 2pp）。

各运动隐含概率分布实测(10分位)差异巨大：
  足球 8%~74%(三结果平局稀释, 跨度大); 棒球 52%~67%(两结果且胜负接近, 极集中)。
故概率桶也按运动定制(同赔率区间)。尾段(>X%)表示高概率热门收尾, 不再细分。

用法:
    from src.scrapers.odds_interval import prob_bucket
    prob_bucket(0.35, "football")   # "30-40%"
    prob_bucket(0.56, "baseball")   # "55-58%"
"""

# (上界概率, 标签)，None 表示 ">最后上界"
_PROB_BUCKETS = {
    "football": [
        (0.10, "<10%"),
        (0.20, "10-20%"),
        (0.30, "20-30%"),
        (0.40, "30-40%"),
        (0.50, "40-50%"),
        (0.55, "50-55%"),
        (0.60, "55-60%"),
        (0.70, "60-70%"),
        (None, ">70%"),
    ],
    "basketball": [
        (0.45, "<45%"),
        (0.55, "45-55%"),
        (0.65, "55-65%"),
        (0.75, "65-75%"),
        (None, ">75%"),
    ],
    "baseball": [
        (0.55, "<55%"),
        (0.58, "55-58%"),
        (0.61, "58-61%"),
        (0.64, "61-64%"),
        (None, ">64%"),
    ],
    "ice_hockey": [
        (0.45, "<45%"),
        (0.55, "45-55%"),
        (0.65, "55-65%"),
        (0.75, "65-75%"),
        (None, ">75%"),
    ],
}

# 其他运动（网球/美足/排球等，样本少）沿用通用概率桶
_DEFAULT_PROB = [
    (0.20, "<20%"),
    (0.40, "20-40%"),
    (0.60, "40-60%"),
    (0.80, "60-80%"),
    (None, ">80%"),
]


def prob_bucket(implied_prob, sport=None):
    """隐含概率 → 按运动定制的概率桶标签。

    implied_prob: 隐含概率, 0-1(小数)或 0-100(百分比)均可; sport: 英文运动名。
    返回 "?" 表示概率无效(<=0 或无法解析)。
    """
    if implied_prob is None:
        return "?"
    try:
        p = float(implied_prob)
    except (TypeError, ValueError):
        return "?"
    if p <= 0:
        return "?"
    if p > 1.0:  # 百分比(0-100) → 小数
        p = p / 100.0
    intervals = _PROB_BUCKETS.get(sport, _DEFAULT_PROB)
    for bound, label in intervals:
        if bound is None or p < bound:
            return label
    return intervals[-1][1]


def prob_bucket_from_fair(fair_price, sport=None, fallback_odds=None):
    """公平价 → 概率桶（内部算隐含概率 = 1/fair）。

    fair_price: Betfair 公平价(>1 有效); fallback_odds: fair 无效时用 BB 赔率兜底(隐含偏高)。
    返回 "?" 表示 fair 和 fallback 都无效。
    """
    if fair_price is not None:
        try:
            f = float(fair_price)
            if f > 1.0:
                return prob_bucket(1.0 / f, sport)
        except (TypeError, ValueError):
            pass
    if fallback_odds is not None:
        try:
            o = float(fallback_odds)
            if o > 1.0:
                return prob_bucket(1.0 / o, sport)
        except (TypeError, ValueError):
            pass
    return "?"
