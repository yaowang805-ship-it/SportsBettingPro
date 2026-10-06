"""Pin 公平价 — Shin 去水 (未来 Pin 锚点的核心「真溢价」逻辑)。

背景: Pin 是 sharp 庄家但有抽水(2~4%)。直接用 Pin 赔率当公平价会把抽水灌进隐含概率,
系统性高估隐含 → 低估 edge → 假溢价。必须去水。

Shin 方法(1993, "Measuring the Incidence of Insider Trading"): 假设庄家面对一部分
"内幕/精明资金"比例 z, 为最大化利润设的赔率带抽水。去水后的真实概率:
    p_i = [sqrt(z^2 + 4(1-z)m_i^2) - z] / [2(1-z)]
其中 m_i = 1/o_i (隐含概率), z 由二分搜索定, 使得 sum(p_i) = 1。

对比比例去水(proportional, 现有 sbo_fair_price 用的): 比例去水把所有结果的隐含概率
按占比缩放到 100%, 等价于假设抽水均匀分摊。Shin 假设抽水集中在"热门"(favorite-longshot
bias 的反向), 对两结果/三结果盘口更贴近真实庄家行为, 是职业团队标准做法。

用法:
    from src.scrapers.pin_fair import pin_fair_price
    pin_fair_price({"home": 1.8, "draw": 3.5, "away": 4.2})
    # -> {"home": 0.55..., "draw": 0.28..., "away": 0.23...}  (真实概率, 和为 1)
"""
import math


def shin_devig(odds):
    """Shin 去水: odds 是 {方向: 十进制赔率}, 返回 {方向: 真实概率}(和为 1)。

    odds 里 <=1 的无效赔率会被剔除。<2 个有效赔率时退化: 直接归一化隐含概率。
    """
    valid = {k: float(v) for k, v in odds.items() if v and float(v) > 1.0}
    n = len(valid)
    if n == 0:
        return {}
    m = {k: 1.0 / v for k, v in valid.items()}
    if n == 1:
        return {k: 1.0 for k in m}

    def _sum_prob(z):
        s = 0.0
        for mi in m.values():
            num = math.sqrt(z * z + 4.0 * (1.0 - z) * mi * mi) - z
            den = 2.0 * (1.0 - z)
            s += num / den
        return s

    # 二分搜索 z ∈ [0, 1), 使得 sum(p_i) = 1
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _sum_prob(mid) > 1.0:
            lo = mid
        else:
            hi = mid
    z = (lo + hi) / 2.0

    probs = {}
    for k, mi in m.items():
        num = math.sqrt(z * z + 4.0 * (1.0 - z) * mi * mi) - z
        den = 2.0 * (1.0 - z)
        probs[k] = num / den
    # 归一化(消除浮点误差)
    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}
    return probs


def pin_fair_price(odds):
    """Pin 赔率 → Shin 去水公平价(十进制)。

    返回 {方向: 公平价}。公平价 = 1/真实概率。例: 概率 0.55 -> 公平价 1.818。
    """
    probs = shin_devig(odds)
    return {k: round(1.0 / p, 4) for k, p in probs.items() if p > 0}


def implied_sum(odds):
    """隐含概率和(未去水): 用于自检「多盘口隐含和 ≈ 1」。>1 说明有抽水, 越大抽水越重。"""
    valid = [float(v) for v in odds.values() if v and float(v) > 1.0]
    return sum(1.0 / v for v in valid) if valid else 0.0
