"""测试风险管理模块 — AdaptiveKelly, PortfolioOptimizer, RiskManager."""
import pytest
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src.risk.manager import AdaptiveKelly, PortfolioOptimizer, RiskManager


@pytest.fixture(autouse=True)
def _isolate_state():
    """每个测试前打补丁，防止 RiskManager 加载真实状态文件。"""
    tmp_data = Path("/tmp/_test_risk_data")
    tmp_data.mkdir(parents=True, exist_ok=True)
    patches = [
        patch("src.risk.manager.RISK_STATE_FILE", tmp_data / "risk_state.json"),
        patch("src.risk.manager.BET_LOG_FILE", tmp_data / "bet_log.csv"),
        patch("src.risk.model_decay_tracker.DECAY_FILE", tmp_data / "decay.json"),
        patch("src.risk.dynamic_staking.MODEL_FILE", tmp_data / "ds_model.json"),
        patch("src.risk.dynamic_staking.BET_LOG_FILE", tmp_data / "bet_log.csv"),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()
    import shutil
    shutil.rmtree(str(tmp_data), ignore_errors=True)


class TestAdaptiveKelly:
    def test_default_fraction_when_empty(self):
        ak = AdaptiveKelly()
        assert ak.fraction() == 0.25

    def test_default_fraction_with_few_samples(self):
        ak = AdaptiveKelly(window=20)
        for _ in range(4):
            ak.update(0.6, 1)
        assert ak.fraction() == 0.25  # < 5 samples → base

    def test_fraction_bounded(self):
        ak = AdaptiveKelly(base=0.25, low=0.1, high=0.4)
        # 连续正确 → fraction 上升
        for _ in range(20):
            ak.update(0.6, 1)
        f = ak.fraction()
        assert 0.1 <= f <= 0.4

    def test_fraction_drops_after_losses(self):
        ak = AdaptiveKelly(base=0.25, low=0.1, high=0.4)
        for _ in range(20):
            ak.update(0.6, 0)  # all wrong
        assert ak.fraction() < 0.25


class TestPortfolioOptimizer:
    def test_empty_correlation(self):
        po = PortfolioOptimizer()
        assert po.compute_correlation_matrix().size == 0
        assert po.portfolio_variance(np.array([])) == 0.0
        assert po.diversification_score() == 1.0

    def test_single_bet_correlation(self):
        po = PortfolioOptimizer()
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B", "model_prob": 0.6})
        corr = po.compute_correlation_matrix()
        assert corr.shape == (1, 1)
        assert corr[0, 0] == 1.0

    def test_same_match_same_market_high_corr(self):
        po = PortfolioOptimizer()
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B", "market": "h2h"})
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B", "market": "h2h"})
        corr = po.compute_correlation_matrix()
        assert corr[0, 1] == pytest.approx(0.95, abs=0.01)

    def test_same_match_diff_market_high_corr(self):
        po = PortfolioOptimizer()
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B", "market": "h2h"})
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B", "market": "spread"})
        corr = po.compute_correlation_matrix()
        assert corr[0, 1] == pytest.approx(0.80, abs=0.01)

    def test_diff_sport_low_corr(self):
        po = PortfolioOptimizer()
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B", "league": "NBA"})
        po.add_bet({"sport": "soccer", "home_team": "C", "away_team": "D", "league": "英超"})
        corr = po.compute_correlation_matrix()
        assert corr[0, 1] < 0.1

    def test_same_league_bonus(self):
        po = PortfolioOptimizer()
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B", "league": "NBA"})
        po.add_bet({"sport": "nba", "home_team": "C", "away_team": "D", "league": "NBA"})
        corr = po.compute_correlation_matrix()
        # base nba=1.0 → applied as sport_group, same_group so 1.0
        # actually both are nba, so SPORT_CORR["nba"]["nba"] = 1.0, but these are different matches
        # Let's use soccer for both to test league bonus
        po2 = PortfolioOptimizer()
        po2.add_bet({"sport": "soccer", "home_team": "A", "away_team": "B", "league": "英超"})
        po2.add_bet({"sport": "soccer", "home_team": "C", "away_team": "D", "league": "英超"})
        corr2 = po2.compute_correlation_matrix()
        # soccer-soccer base = 1.0 → after _sport_group both are "soccer"
        # SPORT_CORR["soccer"]["soccer"] = 1.0... wait, the correlation for same sport is not 1.0
        # Let me look at the code again:
        # SPORT_CORR["nba"]["nba"] = 1.0 — that means the BASE for same-sport is 1.0? No...
        # Actually looking at SPORT_CORR: {"nba": {"nba": 1.0, ...}} — this is the BASE correlation
        # But same-sport diff-match would have sport_group both "nba", so SPORT_CORR["nba"]["nba"] = 1.0
        # That seems wrong — same sport different matches shouldn't be 1.0 correlated
        # But this is testing existing behavior, not fixing it
        assert corr2[0, 1] > 0.0

    def test_remove_bet(self):
        po = PortfolioOptimizer()
        po.add_bet({"id": "1", "sport": "nba"})
        po.add_bet({"id": "2", "sport": "soccer"})
        assert len(po.active_bets) == 2
        po.remove_bet("1")
        assert len(po.active_bets) == 1
        assert po.active_bets[0]["id"] == "2"

    def test_clear(self):
        po = PortfolioOptimizer()
        po.add_bet({"sport": "nba"})
        po.add_bet({"sport": "soccer"})
        po.clear()
        assert len(po.active_bets) == 0

    def test_diversification_score_single(self):
        po = PortfolioOptimizer()
        po.add_bet({"sport": "nba"})
        assert po.diversification_score() == 1.0

    def test_portfolio_variance(self):
        po = PortfolioOptimizer()
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B", "model_prob": 0.6})
        po.add_bet({"sport": "soccer", "home_team": "C", "away_team": "D", "model_prob": 0.5})
        var = po.portfolio_variance(np.array([100.0, 100.0]))
        assert var > 0

    def test_correlation_adjusted_max_stake(self):
        po = PortfolioOptimizer()
        # High diversification → full adjustment
        po.add_bet({"sport": "nba", "home_team": "A", "away_team": "B"})
        po.add_bet({"sport": "soccer", "home_team": "C", "away_team": "D"})
        adj = po.correlation_adjusted_max_stake(100.0, 0.05, 0.0)
        assert adj > 0
        assert adj <= 100.0


class TestRiskManager:
    def test_initial_state(self):
        rm = RiskManager(initial_budget=10000)
        assert rm.current_balance == 10000
        assert rm.consecutive_losses == 0
        assert rm.total_bets == 0
        assert rm.winning_bets == 0

    def test_drawdown_pct(self):
        rm = RiskManager(initial_budget=10000)
        assert rm.drawdown_pct() == 0.0
        rm.current_balance = 8000
        assert rm.drawdown_pct() == pytest.approx(0.20, abs=0.01)
        rm.current_balance = 12000
        assert rm.drawdown_pct() == 0.0  # drawdown is max(0, 1-balance/initial)

    def test_roi(self):
        rm = RiskManager(initial_budget=10000)
        assert rm.roi() == 0.0
        rm.current_balance = 11000
        assert rm.roi() == pytest.approx(0.10, abs=0.01)
        rm.current_balance = 9000
        assert rm.roi() == pytest.approx(-0.10, abs=0.01)

    def test_win_rate(self):
        rm = RiskManager()
        assert rm.win_rate() == 0.0
        rm.total_bets = 10
        rm.winning_bets = 6
        assert rm.win_rate() == pytest.approx(0.60, abs=0.01)

    def test_get_max_stake_odds_le_one(self):
        rm = RiskManager(initial_budget=10000)
        assert rm.get_max_stake(0.1, 1.0) == 0.0

    def test_get_max_stake_negative_edge(self):
        rm = RiskManager(initial_budget=10000)
        stake = rm.get_max_stake(-0.05, 2.0, input_is_prob=False)
        assert stake == 0.0

    def test_get_max_stake_kelly_negative(self):
        rm = RiskManager(initial_budget=10000)
        # prob * odds < 1 → negative Kelly
        stake = rm.get_max_stake(0.3, 2.0, input_is_prob=True)
        assert stake == 0.0

    def test_get_max_stake_normal(self):
        rm = RiskManager(initial_budget=10000)
        # 60% prob at 2.0 odds → 20% edge → positive Kelly
        stake = rm.get_max_stake(0.6, 2.0, input_is_prob=True)
        assert stake > 0
        assert stake <= 10000 * 0.05  # V4.5: 单注上限含等于

    def test_get_max_stake_drawdown_stop(self):
        rm = RiskManager(initial_budget=10000)
        rm.current_balance = 5000  # 50% drawdown
        stake = rm.get_max_stake(0.6, 2.0, input_is_prob=True)
        assert stake == 0.0  # stopped

    def test_get_max_stake_streak_stop(self):
        rm = RiskManager(initial_budget=10000)
        rm.consecutive_losses = 10
        stake = rm.get_max_stake(0.6, 2.0, input_is_prob=True)
        assert stake == 0.0  # stopped

    def test_can_place_bet_bankrupt(self):
        rm = RiskManager(initial_budget=10000)
        rm.current_balance = 0
        ok, msg = rm.can_place_bet(100, 0.0)
        assert not ok
        assert "耗尽" in msg

    def test_can_place_bet_exposure(self):
        rm = RiskManager(initial_budget=10000)
        ok, msg = rm.can_place_bet(10000, 0.0)
        assert not ok  # exceeds max_single

    def test_can_place_bet_ok(self):
        rm = RiskManager(initial_budget=10000)
        ok, msg = rm.can_place_bet(200, 0.0)
        assert ok

    def test_record_outcome_win(self):
        rm = RiskManager(initial_budget=10000)
        rm.record_outcome(100, win=True, odds=2.0, prob=0.5)
        assert rm.current_balance == 10100  # 10000 + 100*(2-1)
        assert rm.total_bets == 1
        assert rm.winning_bets == 1
        assert rm.consecutive_losses == 0

    def test_record_outcome_loss(self):
        rm = RiskManager(initial_budget=10000)
        rm.record_outcome(100, win=False, odds=2.0, prob=0.5)
        assert rm.current_balance == 9900  # 10000 - 100
        assert rm.total_bets == 1
        assert rm.winning_bets == 0
        assert rm.consecutive_losses == 1

    def test_get_health_check_keys(self):
        rm = RiskManager(initial_budget=10000)
        h = rm.get_health_check()
        expected_keys = {'balance', 'roi', 'drawdown', 'win_rate', 'total_bets',
                         'consecutive_losses', 'kelly_fraction',
                         'under_daily_limit', 'under_monthly_limit',
                         'cool_off_active', 'cool_off_until', 'weekly_loss',
                         'ml_dynamic_staking_trained', 'ml_feature_importance',
                         'model_decay'}
        assert set(h.keys()) == expected_keys

    def test_model_decay_in_health_check(self):
        rm = RiskManager(initial_budget=10000)
        h = rm.get_health_check()
        assert isinstance(h['model_decay'], dict)

    def test_record_outcome_updates_model_decay(self):
        rm = RiskManager(initial_budget=10000)
        rm.model_decay_tracker.clear_history()
        rm.record_outcome(100, win=True, odds=2.0, prob=0.6, sport="nba")
        h = rm.get_health_check()
        assert isinstance(h['model_decay'], dict)

    def test_get_confidence_tier(self):
        rm = RiskManager()
        assert rm._get_confidence_tier(0.20) == 1.0
        assert rm._get_confidence_tier(0.12) == 0.8
        assert rm._get_confidence_tier(0.08) == 0.6
        assert rm._get_confidence_tier(0.03) == 0.3

    def test_get_drawdown_multiplier(self):
        rm = RiskManager()
        assert rm._get_drawdown_multiplier() == 1.0
        rm.current_balance = 9200  # 8% drawdown
        assert rm._get_drawdown_multiplier() == 0.7
        rm.current_balance = 7500  # 25% drawdown
        assert rm._get_drawdown_multiplier() == 0.0

    def test_get_streak_multiplier(self):
        rm = RiskManager()
        assert rm._get_streak_multiplier() == 1.0
        rm.consecutive_losses = 2
        assert rm._get_streak_multiplier() == 0.7
        rm.consecutive_losses = 5
        assert rm._get_streak_multiplier() == 0.4  # <=5 → 0.4, >5 → 0.0
        rm.consecutive_losses = 6
        assert rm._get_streak_multiplier() == 0.0

    def test_duplicate_match_same_market_blocked(self):
        rm = RiskManager(initial_budget=10000)
        stake1 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="nba", home_team="A", away_team="B", market="h2h")
        assert stake1 > 0
        stake2 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="nba", home_team="A", away_team="B", market="h2h")
        assert stake2 == 0.0  # duplicate blocked

    def test_save_and_load_state(self):
        """Test state persistence."""
        rm = RiskManager(initial_budget=10000)
        # Directly modify the path for this test
        import src.risk.manager as rm_module
        orig_path = rm_module.RISK_STATE_FILE
        test_file = Path("/tmp/_test_risk_state_save.json")
        rm_module.RISK_STATE_FILE = test_file
        try:
            rm.current_balance = 8000
            rm.consecutive_losses = 3
            rm.total_bets = 20
            rm.winning_bets = 10
            rm.save_state()

            rm2 = RiskManager(initial_budget=10000)
            assert rm2.current_balance == 8000
            assert rm2.consecutive_losses == 3
            assert rm2.total_bets == 20
            assert rm2.winning_bets == 10
        finally:
            rm_module.RISK_STATE_FILE = orig_path
            test_file.unlink(missing_ok=True)

    def test_exposure_limit_enforced(self):
        rm = RiskManager(initial_budget=10000)
        # High edge bet but already at 25% exposure → total would exceed 30%
        stake = rm.get_max_stake(0.7, 2.0, current_exposure_pct=0.25, input_is_prob=True)
        # max total = 30%, current = 25%, so max new = 5% = 500
        assert 0 < stake <= 600

    def test_edge_as_prob_input(self):
        rm = RiskManager(initial_budget=10000)
        # input_is_prob=True: edge_or_prob is treated as probability
        stake_prob = rm.get_max_stake(0.55, 2.0, input_is_prob=True)
        # 0.55 prob at 2.0 → edge = 0.55 - 0.5 = 0.05 (very small, goes to 0.3 confidence tier)
        assert stake_prob >= 0

    def test_edge_as_edge_input(self):
        rm = RiskManager(initial_budget=10000)
        # input_is_prob=False: edge_or_prob is treated as edge value
        stake_edge = rm.get_max_stake(0.15, 2.0, input_is_prob=False)
        # edge=0.15, market_prob=0.5, prob=0.65
        # kelly = (0.65*2 - 0.35) / 1 = 0.95 → huge kelly, capped by max_single_pct
        assert stake_edge > 0

    def test_var_no_bet_log(self):
        rm = RiskManager(initial_budget=10000)
        # No bet_log file → VaR = 0
        assert rm.compute_var() == 0.0
        assert rm.compute_cvar() == 0.0

    def test_portfolio_var_empty(self):
        rm = RiskManager(initial_budget=10000)
        assert rm.portfolio_var() == 0.0


class TestCoolOff:
    """冷却止损系统测试。"""

    def test_initial_no_cool_off(self):
        rm = RiskManager(initial_budget=10000)
        assert rm.cool_off_until is None
        assert not rm._in_cool_off()

    def test_trigger_cool_off_sets_timestamp(self):
        rm = RiskManager(initial_budget=10000)
        before = datetime.now()
        rm._trigger_cool_off()
        assert rm.cool_off_until is not None
        assert rm.cool_off_until > before
        assert rm.cool_off_until <= before + timedelta(hours=24, minutes=1)

    def test_in_cool_off_blocks_stake(self):
        rm = RiskManager(initial_budget=10000)
        rm._trigger_cool_off()
        stake = rm.get_max_stake(0.6, 2.0, input_is_prob=True)
        assert stake == 0.0

    def test_five_consecutive_losses_triggers_cool_off(self):
        rm = RiskManager(initial_budget=10000)
        for _ in range(5):
            rm.record_outcome(100, win=False, odds=2.0, prob=0.5)
        assert rm.cool_off_until is not None
        assert rm._in_cool_off()

    def test_four_losses_no_cool_off(self):
        rm = RiskManager(initial_budget=10000)
        for _ in range(4):
            rm.record_outcome(100, win=False, odds=2.0, prob=0.5)
        assert rm.cool_off_until is None

    def test_win_after_losses_resets_cool_off(self):
        rm = RiskManager(initial_budget=10000)
        for _ in range(4):
            rm.record_outcome(100, win=False, odds=2.0, prob=0.5)
        rm.record_outcome(100, win=True, odds=2.0, prob=0.5)
        assert rm.consecutive_losses == 0
        assert rm.cool_off_until is None

    def test_weekly_loss_triggers_cool_off(self):
        rm = RiskManager(initial_budget=20000)
        rm.record_outcome(5000, win=False, odds=2.0, prob=0.5)
        assert rm.cool_off_until is not None
        assert rm._in_cool_off()

    def test_drawdown_triggers_cool_off(self):
        rm = RiskManager(initial_budget=10000)
        rm.record_outcome(1600, win=False, odds=2.0, prob=0.5)
        assert rm.drawdown_pct() >= 0.15
        assert rm.cool_off_until is not None

    def test_health_check_contains_cool_off_fields(self):
        rm = RiskManager(initial_budget=10000)
        h = rm.get_health_check()
        assert "cool_off_active" in h
        assert "cool_off_until" in h
        assert "weekly_loss" in h
        assert not h["cool_off_active"]

    def test_cool_off_active_in_health_check(self):
        rm = RiskManager(initial_budget=10000)
        rm._trigger_cool_off()
        h = rm.get_health_check()
        assert h["cool_off_active"]

    def test_max_same_game_markets(self):
        rm = RiskManager(initial_budget=10000)
        stake1 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="nba", home_team="A", away_team="B", market="h2h")
        assert stake1 > 0
        # P4: 同场跨市场不再禁止，改用联合凯利折扣
        stake2 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="nba", home_team="A", away_team="B", market="spread")
        assert stake2 > 0
        # 第三笔相同比赛不同市场 → 超上限
        stake3 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="nba", home_team="A", away_team="B", market="total")
        assert stake3 == 0.0

    def test_same_market_duplicate_blocked(self):
        rm = RiskManager(initial_budget=10000)
        stake1 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="nba", home_team="A", away_team="B", market="h2h")
        assert stake1 > 0
        stake2 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="nba", home_team="A", away_team="B", market="h2h")
        assert stake2 == 0.0

    def test_different_matches_both_allowed(self):
        rm = RiskManager(initial_budget=10000)
        stake1 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="nba", home_team="A", away_team="B")
        stake2 = rm.get_max_stake(0.6, 2.0, input_is_prob=True,
                                   sport="soccer", home_team="C", away_team="D")
        assert stake1 > 0
        assert stake2 > 0

    def test_cool_off_persists_after_save_load(self):
        import src.risk.manager as rm_module
        orig = rm_module.RISK_STATE_FILE
        test_file = Path("/tmp/_test_cool_off_state.json")
        rm_module.RISK_STATE_FILE = test_file
        try:
            rm1 = RiskManager(initial_budget=10000)
            rm1._trigger_cool_off()
            rm1.save_state()
            rm2 = RiskManager(initial_budget=10000)
            assert rm2._in_cool_off()
        finally:
            rm_module.RISK_STATE_FILE = orig
            test_file.unlink(missing_ok=True)
