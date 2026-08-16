"""去抽水(devig) — Shin 法, 替代比例法(multiplicative proportional)。

比例法 fair = odds × Σ(1/p) 会系统性高估热门概率、低估冷门概率
(favorite-longshot bias), 导致高赔端 EV 虚高(假阳性)。

Shin 法(Shin 1993)引入"知情资金比例 z"参数, 假设一部分资金是知情者
押在正确结果上, 其余均匀分布, 迭代求解 z 使去抽水概率和为 1。
Shin 法对 favorite-longshot 偏差的修正比比例法好。

用法:
    from src.scrapers.devig import devig_shin, shin_fair_odds
    probs = devig_shin([1.8, 3.5, 4.2])   # 去抽水概率 [p1, p2, p3], 和≈1
    fairs = shin_fair_odds([1.8, 3.5, 4.2])  # 公平十进制赔率 [1/p1, ...]
"""
import math


def devig_shin(odds: list) -> list:
    """Shin 法去抽水, 返回公平概率列表(和≈1)。

    Args:
        odds: 十进制赔率列表(2-way 或 3-way), 可能含 0/None 表示缺失。
    Returns:
        去抽水后的概率列表, 与输入等长; 无效赔率对应位置返回 0。
    """
    clean = [float(o) for o in odds if o and float(o) > 1.0]
    n = len(clean)
    if n < 2:
        return [1.0 / float(o) if o and float(o) > 1.0 else 0.0 for o in odds]

    b = [1.0 / o for o in clean]

    # 无抽水/underbroke 保护: 隐含概率和 <= 1 时没有 margin 可去,
    # 此时 Shin 二分会退到 z=0, 后续归一化把概率放大 → fair < raw (方向错误)。
    # 直接返回原始隐含概率 (fair odds = raw odds)。
    if sum(b) <= 1.0 + 1e-9:
        return [1.0 / float(o) if o and float(o) > 1.0 else 0.0 for o in odds]

    def _sum_pi(z):
        s = 0.0
        for bi in b:
            s += (math.sqrt(z * z + 4.0 * (1.0 - z) * bi * bi) - z) / (2.0 * (1.0 - z))
        return s

    # 二分求 z: sum_pi 随 z 单调递减, 目标是 sum_pi(z)=1
    lo, hi = 0.0, 1.0 - 1e-12
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _sum_pi(mid) > 1.0:
            lo = mid
        else:
            hi = mid
    z = (lo + hi) / 2.0

    probs = [(math.sqrt(z * z + 4.0 * (1.0 - z) * bi * bi) - z) / (2.0 * (1.0 - z))
             for bi in b]
    # 归一化到精确和=1(消除浮点误差)
    total = sum(probs)
    if total > 0:
        probs = [p / total for p in probs]

    # 回填到原始顺序
    result, idx = [], 0
    for o in odds:
        if o and float(o) > 1.0:
            result.append(probs[idx])
            idx += 1
        else:
            result.append(0.0)
    return result


def shin_fair_odds(odds: list) -> list:
    """Shin 法去抽水, 返回公平十进制赔率列表(1/prob)。"""
    probs = devig_shin(odds)
    return [round(1.0 / p, 4) if p and p > 0 else 0.0 for p in probs]


def devig_mult(odds: list) -> list:
    """比例法(multiplicative/proportional)去抽水, 返回公平概率(和=1)。

    仅作 Shin 法的交叉验证基准, 不直接用于 EV 计算:
    比例法会高估热门概率(低估热门公平赔率), 方向与 Shin 相反。
    两者在热门端的分歧大小 = devig 方法不确定性。
    """
    clean = [float(o) for o in odds if o and float(o) > 1.0]
    if len(clean) < 2:
        return [1.0 / float(o) if o and float(o) > 1.0 else 0.0 for o in odds]
    b = [1.0 / o for o in clean]
    s = sum(b)
    if s <= 1e-12:
        return [0.0] * len(odds)
    probs = [x / s for x in b]
    result, idx = [], 0
    for o in odds:
        if o and float(o) > 1.0:
            result.append(probs[idx])
            idx += 1
        else:
            result.append(0.0)
    return result


def devig_favorite_divergence(odds: list) -> float:
    """热门端 Shin vs 比例法 的公平赔率分歧(%)。

    返回 (shin_fair[热门] - mult_fair[热门]) / mult_fair[热门] * 100。
    负值 = Shin 把热门公平赔率算得更低 → 热门 EV 被 Shin 抬高(需警惕, 提门槛)。
    """
    shin = devig_shin(odds)
    mult = devig_mult(odds)
    valid = [(i, float(o)) for i, o in enumerate(odds) if o and float(o) > 1.0]
    if len(valid) < 2:
        return 0.0
    fav_i, _ = min(valid, key=lambda x: x[1])  # 热门 = 最低有效赔率
    p_s, p_m = shin[fav_i], mult[fav_i]
    if p_s <= 0 or p_m <= 0:
        return 0.0
    return (1.0 / p_s - 1.0 / p_m) / (1.0 / p_m) * 100.0
