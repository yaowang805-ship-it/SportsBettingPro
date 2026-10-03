"""赔率区间分档 — 按运动定制（2026-10-03 基于观察库实测赔率分位数）。

favorite-longshot bias（冷门被高估）使同一盘口不同赔率区间 edge 分化巨大，必须分区间判断。
但各运动赔率分布差异巨大：足球跨度 1.4~13.5、棒球 90% 挤在 1.6~2.1。
统一 5 档（1.5/2.0/3.0/5.0）对棒球来说 1.5-2.0 一档就吞掉 90% 样本，其余 4 档几乎空。

故按运动定制分档边界（边界取自各运动观察库 bb_odds 的 10 分位数整数化，数据驱动非拍脑门）：
  足球 8 档；篮球/棒球/冰球各 5 档；其他运动沿用旧 5 档（样本太少，先保口径一致）。

用法:
    from src.scrapers.odds_interval import odds_interval
    odds_interval(1.6, "football")   # "1.45-1.70"
    odds_interval(1.75, "baseball")  # "1.70-1.80"
"""

# (上界, 标签)，None 表示 ">最后上界"
_INTERVALS = {
    "football": [
        (1.45, "1.0-1.45"),
        (1.70, "1.45-1.70"),
        (2.00, "1.70-2.00"),
        (2.40, "2.00-2.40"),
        (3.00, "2.40-3.00"),
        (4.50, "3.00-4.50"),
        (7.00, "4.50-7.00"),
        (None, ">7.00"),
    ],
    "basketball": [
        (1.45, "1.0-1.45"),
        (1.70, "1.45-1.70"),
        (2.00, "1.70-2.00"),
        (2.50, "2.00-2.50"),
        (None, ">2.50"),
    ],
    "baseball": [
        (1.70, "1.0-1.70"),
        (1.80, "1.70-1.80"),
        (1.90, "1.80-1.90"),
        (2.10, "1.90-2.10"),
        (None, ">2.10"),
    ],
    "ice_hockey": [
        (1.60, "1.0-1.60"),
        (2.00, "1.60-2.00"),
        (2.40, "2.00-2.40"),
        (2.80, "2.40-2.80"),
        (None, ">2.80"),
    ],
}

# 其他运动（网球/美足/排球/乒乓/MMA/拳击/羽毛球）沿用旧 5 档
_DEFAULT = [
    (1.50, "1.0-1.5"),
    (2.00, "1.5-2.0"),
    (3.00, "2.0-3.0"),
    (5.00, "3.0-5.0"),
    (None, ">5.0"),
]


def odds_interval(odds, sport=None):
    """BB 赔率 → 按运动定制的赔率区间标签。

    odds: BB 十进制赔率(可为 str/float); sport: 英文运动名(football/basketball/...),
    None/未知则用默认 5 档。返回 "?" 表示赔率无效(<=1.0 或 None/无法解析)。
    """
    if odds is None:
        return "?"
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return "?"
    if o <= 1.0:
        return "?"
    intervals = _INTERVALS.get(sport, _DEFAULT)
    for bound, label in intervals:
        if bound is None or o < bound:
            return label
    return intervals[-1][1]
