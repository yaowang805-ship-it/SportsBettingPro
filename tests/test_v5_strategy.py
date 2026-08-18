"""V5.1 核心策略逻辑回归测试 — 防止 DC门槛/赔率策略/存档的 bug 反复。

覆盖今天修复的关键逻辑:
1. DC/HT-DC/HT 的 EV 门槛 (get_min_ev)
2. per-sport 赔率策略 (get_odds_strategy)
3. 分层投注策略 (get_tier_strategy)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def test_dc_ev_threshold_raised():
    """DC(双重机会)推导自1X2(Shin无偏)但非独立数据 — V5.9 门槛 4%."""
    from config.weight_matrix_v5 import get_min_ev
    assert get_min_ev("football", "英超", "dc", 1.8) == 4.0
    assert get_min_ev("football", "英超", "dc", 3.0) == 4.0
    assert get_min_ev("football", "英超", "dc", 4.0) == 4.0


def test_ht_dc_ev_threshold_higher():
    """上半场DC纯推导(ht_dc霸屏假阳性源) — V5.9 门槛 6%."""
    from config.weight_matrix_v5 import get_min_ev
    assert get_min_ev("football", "英超", "ht_dc", 1.8) == 6.0
    assert get_min_ev("football", "英超", "ht_dc", 4.0) == 6.0


def test_ht_ev_threshold_raised():
    """上半场盘口推导自半场分布 — V5.9 门槛 6%."""
    from config.weight_matrix_v5 import get_min_ev
    assert get_min_ev("football", "英超", "ht", 1.8) == 6.0
    assert get_min_ev("football", "英超", "ht", 4.0) == 6.0


def test_dnb_ev_blocked():
    """DNB(平局退款)封杀 — BB无此盘口(曾误读'第3粒进球')."""
    from config.weight_matrix_v5 import get_min_ev
    assert get_min_ev("football", "英超", "dnb", 1.8) == 999.0


def test_per_sport_odds_strategy():
    """per-sport 赔率策略正确."""
    from src.evolve.odds_strategy_optimizer import get_odds_strategy
    # 足球 @2.5 → 2-3x 区间: EV≥2.5%, Kelly 100%, max 3.0
    fb = get_odds_strategy(2.5, "football")
    assert fb["max_odds"] == 3.0
    # 网球 @4.0 → 3-5x 区间: 独特优待 Kelly 80%
    tn = get_odds_strategy(4.0, "tennis")
    assert tn["kelly_mult"] == 0.8
    assert tn["max_odds"] == 5.0


def test_tier_strategy():
    """分层投注策略 T1/T3 差异化."""
    from config.constants import get_tier_strategy
    t1 = get_tier_strategy("football", "英超", 1)
    t3 = get_tier_strategy("football", "芬兰丁级联赛", 3)
    assert t1["ev_floor"] < t3["ev_floor"], "T3门槛应高于T1"
    assert t1["max_stake_pct"] > t3["max_stake_pct"], "T1仓位应高于T3"
    assert t1["allow_suggest"], "T1应允许建议"
    assert t3["allow_suggest"], "T3允许建议(低于min_stake显示建议, V5.1放宽)"


def test_odds_archiver_processed_format():
    """odds_archiver 应兼容处理后的 matchup 格式 (顶层home/away)."""
    from src.evolve.odds_archiver import archive_matchups
    # 处理后格式: home/away 是顶层字段, moneyline 里是 price_decimal
    processed_matchups = [{
        "matchup_id": 999001,
        "home": "Test Home",
        "away": "Test Away",
        "moneyline": [{
            "type": "moneyline", "period": 0,
            "prices": [
                {"designation": "home", "price_decimal": 1.8},
                {"designation": "away", "price_decimal": 2.1},
            ]
        }],
        "spread": [], "total": [],
    }]
    inserted, total = archive_matchups("football", 999, "测试联赛", processed_matchups, [])
    assert inserted >= 2, f"应至少插入2条价格, 实际{inserted}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
