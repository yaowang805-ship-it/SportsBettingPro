"""Shin 去抽水回归测试 — 防止 sum_b2 归一化 bug 复发。

关键: 去抽水后公平价必须 ≥ 原始赔率(去掉抽水后更优), 反过来就是 bug。
(2026-08-14 曾因 bi²/sum_b2 归一化导致热门公平价算低 → 假 +EV)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_shin_fair_odds_higher_than_input():
    """公平价必须 ≥ 输入赔率(去抽水方向正确)。"""
    from src.scrapers.devig import shin_fair_odds
    cases = [
        [1.64, 2.30],      # 2-way
        [1.8, 3.5, 4.2],   # 3-way
        [1.23, 3.0, 6.5],  # 强热门 + 大冷门
        [1.5, 2.5],        # 2-way 均衡
    ]
    for odds in cases:
        fairs = shin_fair_odds(odds)
        for f, o in zip(fairs, odds):
            assert f >= o - 1e-6, f"公平价 {f} < 原始 {o} (去抽水方向反了)"


def test_shin_probabilities_sum_to_one():
    """去抽水概率和应为 1。"""
    from src.scrapers.devig import devig_shin
    cases = [[1.64, 2.30], [1.8, 3.5, 4.2], [1.23, 3.0, 6.5]]
    for odds in cases:
        probs = devig_shin(odds)
        assert abs(sum(probs) - 1.0) < 1e-6, f"概率和 {sum(probs)} ≠ 1"


def test_shin_underbroke_no_inflation():
    """underbroke (隐含概率和 ≤1) 时不得把概率放大 → fair 不得 < raw。

    2026-08-15 发现: 输入赔率和 ≤1 时 Shin 二分退到 z=0, 归一化放大概率
    → 热门公平价 < 原始赔率 → 假 +EV。应直接返回原始隐含概率(fair=raw)。
    """
    from src.scrapers.devig import shin_fair_odds
    underbroke = [
        [1.4, 4.89],       # 1/1.4 + 1/4.89 = 0.919 < 1
        [3.0, 2.48],       # 1/3.0 + 1/2.48 = 0.737 < 1
        [2.0, 2.5, 5.0],   # 0.5 + 0.4 + 0.2 = 1.1 > 1 (不是 underbroke, 但验证不炸)
    ]
    for odds in underbroke:
        fairs = shin_fair_odds(odds)
        for f, o in zip(fairs, odds):
            assert f >= o - 1e-6, f"underbroke 公平价 {f} < 原始 {o} (概率被错误放大)"


def test_shin_close_to_proportional():
    """Shin 公平价不应与比例法偏差过大(<10%), 防止过度修正。"""
    from src.scrapers.devig import shin_fair_odds
    odds = [1.64, 2.30]
    fairs = shin_fair_odds(odds)
    imp = sum(1.0 / o for o in odds)
    prop = [round(o * imp, 4) for o in odds]  # 比例法: [1.713, 2.4024]
    for f, p in zip(fairs, prop):
        rel = abs(f - p) / p
        assert rel < 0.10, f"Shin {f} 与比例法 {p} 偏差 {rel:.1%} 过大"
