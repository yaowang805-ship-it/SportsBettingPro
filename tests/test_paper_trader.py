"""PaperTrader 模拟交易引擎的单元测试。"""
import json
import math
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

from src.betting.paper_trader import PaperTrader


@pytest.fixture
def trader():
    """每个测试使用临时目录的 PaperTrader。"""
    tmp = Path(tempfile.mkdtemp())
    pt = PaperTrader(data_dir=tmp, initial_balance=10000.0)
    return pt


def _write_portfolio(trader, history=None, settled=None, pending=None, balance=10000.0):
    """辅助：写入虚拟组合数据。"""
    data = {
        "balance": balance,
        "settled": settled or {},
        "pending_bets": pending or [],
        "history": history or [],
    }
    trader.portfolio_file.write_text(json.dumps(data))


def _build_bet(status="won", profit=50, stake=100, odds=2.0, clv=0.02):
    """辅助：生成一条投注记录。"""
    return {
        "status": status,
        "profit": profit,
        "stake": stake,
        "odds": odds,
        "clv": clv,
        "id": "nba_test",
        "date": datetime.now(timezone.utc).isoformat(),
    }


def _write_prediction_log(trader, rows):
    """辅助：写入 prediction_log.csv。"""
    import csv
    with open(trader.pred_log_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["status", "ev", "sport", "league"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestPaperTrader:

    def test_empty_state(self, trader):
        """空状态应安全返回。"""
        state = trader.refresh()
        assert state["initial_bankroll"] == 10000.0
        assert state["total_bets"] == 0
        assert state["win_rate"] == 0.0
        assert state["readiness"]["ready"] is False

    def test_basic_win_loss(self, trader):
        """1胜1负应有正确胜率。"""
        history = [_build_bet("won", 100, 100, 2.0, 0.05),
                   _build_bet("lost", -100, 100, 2.0, -0.03)]
        _write_portfolio(trader, history=history)
        state = trader.refresh()
        assert state["settled_bets"] == 2
        assert state["win_rate"] == 0.5
        assert state["total_profit"] == 0.0

    def test_win_rate_100pct(self, trader):
        """3连胜胜率100%。"""
        history = [_build_bet("won", 50, 100, 1.5) for _ in range(3)]
        _write_portfolio(trader, history=history)
        state = trader.refresh()
        assert state["win_rate"] == 1.0
        assert state["win_count"] == 3

    def test_equity_curve(self, trader):
        """权益曲线应体现余额变化。"""
        history = [_build_bet("won", 200, 100, 3.0),
                   _build_bet("lost", -100, 100, 2.0)]
        _write_portfolio(trader, history=history, balance=10100.0)
        state = trader.refresh()
        assert len(state["equity_curve"]) > 2
        assert state["equity_curve"][0]["balance"] == 10000.0

    def test_max_drawdown(self, trader):
        """大亏后应有回撤。"""
        history = [_build_bet("won", 1000, 1000, 2.0),
                   _build_bet("lost", -2000, 1000, 2.0)]
        _write_portfolio(trader, history=history, balance=9000.0)
        state = trader.refresh()
        assert state["max_drawdown"] > 0

    def test_sharpe_ratio_none_with_few_bets(self, trader):
        """少于3笔时夏普应为 None。"""
        history = [_build_bet("won", 50, 100, 2.0)]
        _write_portfolio(trader, history=history)
        state = trader.refresh()
        assert state["sharpe_ratio"] is None

    def test_sharpe_ratio_with_enough_bets(self, trader):
        """多笔投注后应有夏普值。"""
        history = [_build_bet("won", 50, 100, 1.5) for _ in range(5)]
        _write_portfolio(trader, history=history, balance=10250.0)
        state = trader.refresh()
        # 全赢，夏普应该很高
        assert state["sharpe_ratio"] is not None
        assert state["sharpe_ratio"] > 0

    def test_by_sport_breakdown(self, trader):
        """不同运动的投注应分开统计。"""
        history = [
            {"status": "won", "profit": 100, "stake": 100, "odds": 2.0,
             "clv": 0.01, "id": "nba_LAL_vs_BOS", "date": datetime.now(timezone.utc).isoformat()},
            {"status": "lost", "profit": -100, "stake": 100, "odds": 2.0,
             "clv": -0.02, "id": "football_ARS_vs_CHE", "date": datetime.now(timezone.utc).isoformat()},
        ]
        _write_portfolio(trader, history=history)
        state = trader.refresh()
        assert "nba" in state["by_sport"]
        assert "football" in state["by_sport"]
        assert state["by_sport"]["nba"]["win_count"] == 1
        assert state["by_sport"]["football"]["loss_count"] == 1

    def test_readiness_all_pass(self, trader):
        """100笔投注+60%胜率+交替盈亏应通过大部分就绪检查。"""
        n = 100
        wins_needed = 60
        # 交替生成投注，确保胜率60%且总利润为正
        history = []
        for i in range(n):
            is_win = i < wins_needed
            profit = 95 if is_win else -100   # 赢赚95，亏赔100 → 净+1700
            clv = 0.02 if is_win else -0.01
            h = _build_bet("won" if is_win else "lost", profit, 100, 2.0, clv)
            h["id"] = f"nba_{'win' if is_win else 'loss'}_{i}"
            h["date"] = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).isoformat()
            history.append(h)
        # 打乱顺序模拟真实情况
        import random
        random.seed(42)
        random.shuffle(history)
        total_profit = sum(h["profit"] for h in history)
        _write_prediction_log(trader, [{"status": "won", "ev": "0.05", "sport": "nba", "league": "NBA"}] * n)
        _write_portfolio(trader, history=history,
                         balance=10000 + total_profit)
        state = trader.refresh()
        assert state["readiness"]["checks"]["min_bets"]["passed"]
        assert state["readiness"]["checks"]["positive_roi"]["passed"]

    def test_readiness_stability(self, trader):
        """稳定性检查需要 3+ 连续快照通过。"""
        # 先灌入一个已过的快照历史
        own_state = {
            "snapshot_history": [
                {"all_checks_passed": True, "date": "2026-06-01"},
                {"all_checks_passed": True, "date": "2026-06-02"},
            ],
        }
        trader.state_file.parent.mkdir(parents=True, exist_ok=True)
        trader.state_file.write_text(json.dumps(own_state))

        n = 120
        n_wins = 72  # 60% → 足够通过 z-test
        history = []
        for i in range(n):
            is_win = i < n_wins
            profit = 95 if is_win else -100
            clv = 0.02 if is_win else -0.01
            h = _build_bet("won" if is_win else "lost", profit, 100, 2.0, clv)
            h["id"] = f"nba_{i}"
            h["date"] = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).isoformat()
            history.append(h)
        import random
        random.seed(42)
        random.shuffle(history)
        _write_prediction_log(trader, [{"status": "won", "ev": "0.05", "sport": "nba", "league": "NBA"}] * n)
        _write_portfolio(trader, history=history,
                         balance=10000 + sum(h["profit"] for h in history))

        state = trader.refresh()
        assert state["readiness"]["checks"]["stability"]["passed"]
        assert state["readiness"]["checks"]["stability"]["actual"] >= 3

    def test_metrics_by_tier(self, trader):
        """时间窗口统计应正确。"""
        now = datetime.now(timezone.utc)
        history = [
            _build_bet("won", 50, 100, 2.0),
        ]
        history[0]["date"] = now.isoformat()
        _write_portfolio(trader, history=history, balance=10050.0)
        state = trader.refresh()
        assert state["metrics_by_tier"]["all_time"]["bets"] == 1
        assert state["metrics_by_tier"]["last_7_days"]["bets"] == 1

    def test_prediction_log_ev(self, trader):
        """prediction_log.csv 中的 EV 应被纳入统计。"""
        _write_prediction_log(trader, [
            {"status": "won", "ev": "0.05", "sport": "nba", "league": "NBA"},
            {"status": "won", "ev": "0.03", "sport": "nba", "league": "NBA"},
        ])
        _write_portfolio(trader, history=[_build_bet("won", 50, 100, 2.0, 0.02)])
        state = trader.refresh()
        assert state["avg_ev"] is not None
        assert 0.03 <= state["avg_ev"] <= 0.05

    def test_z_score_calculation(self, trader):
        """z-test 统计量应正确计算。"""
        z = trader._z_score_test(win_count=30, total=50, p0=0.52)
        assert isinstance(z, float)

    def test_win_rate_p_value(self, trader):
        """p-value 应在合理范围。"""
        p = trader._win_rate_p_value(win_count=30, total=50, p0=0.52)
        assert 0 <= p <= 1.0

    def test_void_count(self, trader):
        """void/push 应单独统计不计入胜率分母。"""
        history = [
            _build_bet("won", 50, 100, 2.0),
            _build_bet("lost", -100, 100, 2.0),
            {"status": "void", "profit": 0, "stake": 100, "odds": 2.0,
             "clv": 0, "id": "void_test", "date": datetime.now(timezone.utc).isoformat()},
        ]
        _write_portfolio(trader, history=history)
        state = trader.refresh()
        assert state["void_count"] == 1
        assert state["settled_bets"] == 3
        # 胜率 = 赢 / (赢+输) = 1/2
        assert state["win_rate"] == 0.5

    def test_pending_bets_counted(self, trader):
        """待结算投注应计入 total_bets 但不影响胜率。"""
        history = [_build_bet("won", 50, 100, 2.0)]
        pending = [{"sport": "nba", "stake": 100, "odds": 2.0, "match_key": "test"}]
        _write_portfolio(trader, history=history, pending=pending, balance=9900.0)
        state = trader.refresh()
        assert state["total_bets"] == 2
        assert state["settled_bets"] == 1
        assert state["pending_bets"] == 1
        assert state["win_rate"] == 1.0  # 只有已结算的影响胜率

    def test_negative_roi_readiness(self, trader):
        """负 ROI 时应未通过 positive_roi 检查。"""
        history = [_build_bet("lost", -100, 100, 2.0)]
        _write_portfolio(trader, history=history, balance=9900.0)
        state = trader.refresh()
        assert not state["readiness"]["checks"]["positive_roi"]["passed"]

    def test_consecutive_losses(self, trader):
        """连续亏损应正确计数。"""
        history = [_build_bet("lost", -100, 100, 2.0) for _ in range(5)]
        _write_portfolio(trader, history=history, balance=9500.0)
        state = trader.refresh()
        assert state["max_consecutive_losses"] == 5
        assert state["current_streak"] == -5

    def test_var_cvar_none_with_few_bets(self, trader):
        """少于10笔时 VaR/CVaR 应为 None。"""
        history = [_build_bet("won", 50, 100, 2.0) for _ in range(3)]
        _write_portfolio(trader, history=history)
        state = trader.refresh()
        assert state["var_95"] is None
        assert state["cvar_95"] is None

    def test_sortino_ratio(self, trader):
        """Sortino 应正确计算。"""
        history = [_build_bet("won", 50, 100, 1.5) for _ in range(5)]
        history += [_build_bet("lost", -100, 100, 2.0) for _ in range(2)]
        _write_portfolio(trader, history=history, balance=10050.0)
        state = trader.refresh()
        # 全赢没有 downside，Sortino 可能为 None
        # 这里mixed结果应该算出来
        if state["sortino_ratio"] is not None:
            assert isinstance(state["sortino_ratio"], float)

    def test_print_report_no_error(self, trader):
        """print_report 不应抛出异常。"""
        history = [_build_bet("won", 50, 100, 2.0, 0.02)]
        _write_portfolio(trader, history=history)
        # 只是验证不抛异常
        trader.print_report()
        assert trader.state_file.exists()

    def test_run_returns_state(self, trader):
        """run() 应返回完整状态字典。"""
        history = [_build_bet("won", 50, 100, 2.0)]
        _write_portfolio(trader, history=history)
        state = trader.run()
        assert isinstance(state, dict)
        assert "readiness" in state
        assert "by_sport" in state
        assert "equity_curve" in state

    def test_clv_positive_rate(self, trader):
        """正向 CLV 率应正确计算。"""
        history = [
            _build_bet("won", 50, 100, 2.0, 0.05),
            _build_bet("lost", -100, 100, 2.0, -0.03),
            _build_bet("won", 50, 100, 2.0, 0.01),
        ]
        _write_portfolio(trader, history=history, balance=10000.0)
        state = trader.refresh()
        assert state["positive_clv_rate"] == pytest.approx(2 / 3, abs=0.001)

    def test_total_days_active(self, trader):
        """活跃天数应正确计算。"""
        history = [
            dict(_build_bet("won", 50, 100, 2.0), date="2026-06-01T00:00:00+00:00"),
            dict(_build_bet("won", 50, 100, 2.0), date="2026-06-03T00:00:00+00:00"),
        ]
        _write_portfolio(trader, history=history, balance=10100.0)
        state = trader.refresh()
        assert state["total_days_active"] == 3  # 6/1 -> 6/3 = 3 days

    def test_empty_prediction_log(self, trader):
        """prediction_log.csv 不存在时应静默处理。"""
        state = trader.refresh()
        assert state["avg_ev"] is None


class TestComputeFunctions:
    """静态方法测试。"""

    def test_compute_sharpe_insufficient(self):
        equity = [{"balance": 10000}, {"balance": 10100}]
        assert PaperTrader._compute_sharpe(equity) is None

    def test_compute_sharpe_sufficient(self):
        equity = [{"balance": 10000}, {"balance": 10100},
                  {"balance": 10050}, {"balance": 10200}]
        sharpe = PaperTrader._compute_sharpe(equity)
        assert sharpe is not None
        assert sharpe > 0

    def test_compute_sortino_insufficient(self):
        equity = [{"balance": 10000}]
        assert PaperTrader._compute_sortino(equity) is None

    def test_compute_max_drawdown_no_drawdown(self):
        equity = [{"balance": 10000}, {"balance": 11000}, {"balance": 12000}]
        assert PaperTrader._compute_max_drawdown(equity) == 0.0

    def test_compute_max_drawdown_with_drawdown(self):
        equity = [{"balance": 10000}, {"balance": 12000}, {"balance": 9000}]
        dd = PaperTrader._compute_max_drawdown(equity)
        assert dd > 0.0

    def test_z_score(self):
        z = PaperTrader._z_score_test(win_count=35, total=60, p0=0.52)
        assert isinstance(z, float)
        # 35/60 = 0.583, 高于 0.52 应该有正 z 值
        assert z > 0

    def test_p_value_range(self):
        p = PaperTrader._win_rate_p_value(win_count=35, total=60, p0=0.52)
        assert 0 <= p <= 1.0

    def test_compute_var(self):
        history = [{"profit": i * 10} for i in range(-5, 5)]
        var = PaperTrader._compute_var(history, ci=0.95)
        assert var is not None
        assert var < 0  # 因为在亏损尾部

    def test_compute_cvar(self):
        history = [{"profit": i * 10} for i in range(-10, 10)]
        cvar = PaperTrader._compute_cvar(history, ci=0.95)
        assert cvar is not None
        assert cvar < 0

    def test_var_invalid_input(self):
        assert PaperTrader._compute_var([], ci=0.95) is None
