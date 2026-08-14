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
