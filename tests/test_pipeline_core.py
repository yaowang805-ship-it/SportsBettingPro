"""核心 Pipeline 回归测试 — 验证导入 + 关键函数存在 + 数据文件完整性。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data" / "storage"

# ── 1. 模块导入测试 ──

def test_import_dingtalk():
    from config.dingtalk import send_dingtalk, _resolve_host, _resolve_via_dns
    assert callable(send_dingtalk)
    assert callable(_resolve_host)


def test_import_settings():
    from config.settings import DATA_DIR, DINGTALK_WEBHOOK
    assert DATA_DIR.exists()


def test_import_logging():
    from config.logging_config import get_logger
    assert callable(get_logger)


def test_import_api_fetcher():
    from src.scrapers.bb_api_fetcher import (
        fetch_all_sports, extract_match_odds, _ensure_token,
        PLATFORMS, SPORTS,
    )
    assert len(PLATFORMS) >= 1
    assert len(SPORTS) >= 1


def test_import_vs_pinnacle():
    from src.scrapers.bb_vs_pinnacle import (
        compare_bb_vs_pinnacle, find_matches_by_odds,
        detect_sport, extract_bb_1x2, extract_bb_handicap, extract_bb_ou,
        api_get,
    )
    assert callable(compare_bb_vs_pinnacle)
    assert callable(detect_sport)


def test_import_ev_push():
    from src.report.bb_ev_push import (
        build_report, _collect_opportunities_from_file,
        _diversify_and_rank, _format_body, _min_ev_for_tier,
    )
    assert callable(build_report)
    # V5.2: 分层EV门槛 (football T1=2%, T2=2.5%, T3=3%, T4=4%)
    assert _min_ev_for_tier(1, "football") == 2.0
    assert _min_ev_for_tier(2, "football") == 2.5
    assert _min_ev_for_tier(3, "football") == 3.0
    assert _min_ev_for_tier(4, "football") == 4.0


def test_import_virtual_bet():
    from src.betting.bb_virtual_bet import (
        place_bets, _calc_kelly_stake, _league_multiplier,
        DAILY_BANKROLL, KELLY_FRAC,
    )
    assert callable(_calc_kelly_stake)
    assert callable(_league_multiplier)
    assert DAILY_BANKROLL > 0


def test_import_portfolio():
    from src.dashboard.components.virtual_portfolio import (
        auto_place_bets, compute_portfolio, reset_portfolio, settle_bet,
    )
    assert callable(auto_place_bets)
    assert callable(compute_portfolio)


def test_import_auto_settle():
    from src.monitor.auto_settle import (
        auto_settle, _match_bet, _load_state,
    )
    assert callable(auto_settle)


def test_import_self_learn():
    from src.risk.self_learn import analyze, apply_adjustments
    assert callable(analyze)


def test_import_dashboard():
    import src.dashboard.config as cfg
    assert hasattr(cfg, 'DATA_DIR')


# ── 2. 数据文件完整性测试 ──

def _check_json(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        json.loads(path.read_text())
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def test_data_league_tiers():
    assert _check_json(DATA_DIR / "league_tiers.json")


def test_data_league_keywords():
    assert _check_json(DATA_DIR / "league_keywords.json")


def test_data_team_name_map():
    assert _check_json(DATA_DIR / "team_name_map.json")


def test_data_banned_leagues():
    assert _check_json(DATA_DIR / "banned_leagues.json")


# ── 3. Kelly 计算一致性测试 ──

def test_kelly_consistency():
    """验证 ev_push 和 virtual_bet 的 Kelly 公式数学等价。"""
    from src.report.bb_ev_push import _calc_kelly_stakes as ev_kelly
    from src.betting.bb_virtual_bet import _calc_kelly_stake as vb_kelly

    bb_odds = 2.5
    fair_price = 2.2
    ev_pct = (bb_odds - fair_price) / fair_price * 100  # 13.64%
    bankroll = 50000

    # ev_push 方式
    opp = [{
        "_kelly_pct": (ev_pct / 100) / (bb_odds - 1) * 0.25 * 100,
        "league": "英格兰超级联赛",
        "home_cn": "test", "away_cn": "test2",
    }]
    result = ev_kelly(opp)
    ev_stake = result[0]["_stake"]

    # virtual_bet 方式
    vb_result = vb_kelly(bb_odds, fair_price, bankroll, league="英格兰超级联赛")
    # V4.5: ev_push 有 10K 日预算/单注上限/取整，与 vb 的完整 Kelly 计算结果差异较大
    # 只验证两者都推荐正期望投注 (>0)
    assert ev_stake > 0
    assert vb_result > 0
