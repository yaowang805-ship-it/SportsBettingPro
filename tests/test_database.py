"""SQLite 数据库层的测试。"""
import os
import tempfile
import pytest


@pytest.fixture(autouse=True)
def _fresh_db():
    """每个测试使用独立的临时数据库文件。"""
    tmp = tempfile.mktemp(suffix=".db")
    from src.storage.database import SportsDatabase
    db = SportsDatabase(tmp)
    yield db
    try:
        os.remove(tmp)
    except OSError:
        pass


class TestBetLog:
    def test_record_bet(self, _fresh_db):
        db = _fresh_db
        bet_id = db.record_bet(home="Lakers", away="Celtics", sport="basketball",
                               stake=100, odds=1.91, prob=0.55)
        assert bet_id > 0

    def test_record_and_query(self, _fresh_db):
        db = _fresh_db
        db.record_bet(home="Lakers", away="Celtics", stake=100, odds=2.0, prob=0.5)
        bets = db.get_recent_bets(limit=10)
        assert len(bets) == 1
        assert bets[0]["home_team"] == "Lakers"
        assert bets[0]["stake"] == 100

    def test_settle_bet(self, _fresh_db):
        db = _fresh_db
        bid = db.record_bet(home="A", away="B", stake=100, odds=2.0, prob=0.5)
        db.settle_bet(bid, "win", 90)
        bets = db.get_recent_bets()
        assert bets[0]["result"] == "win"
        assert bets[0]["profit"] == 90

    def test_bet_stats(self, _fresh_db):
        db = _fresh_db
        for i in range(3):
            bid = db.record_bet(home=f"Team{i}", away="Opp", stake=100, odds=2.0, prob=0.5)
            db.settle_bet(bid, "win" if i < 2 else "loss", 90 if i < 2 else -100)
        stats = db.get_bet_stats()
        assert stats["total"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert stats["total_profit"] == 80  # 90 + 90 - 100

    def test_empty_stats(self, _fresh_db):
        stats = _fresh_db.get_bet_stats()
        assert stats["total"] == 0

    def test_bet_without_match_key(self, _fresh_db):
        bid = _fresh_db.record_bet(home="Home", away="Away", stake=50, odds=3.0, prob=0.33)
        bets = _fresh_db.get_recent_bets()
        assert len(bets) == 1
        assert "Home vs Away" in bets[0]["match_key"]


class TestModelAccuracy:
    def test_update_accuracy(self, _fresh_db):
        db = _fresh_db
        db.update_accuracy("fb_win", "h2h", correct=True, prob=0.7)
        accs = db.get_accuracy("fb_win")
        assert len(accs) == 1
        assert accs[0]["accuracy"] == 1.0

    def test_accuracy_accumulates(self, _fresh_db):
        db = _fresh_db
        for _ in range(4):
            db.update_accuracy("model_a", "target_x", correct=True, prob=0.6)
        db.update_accuracy("model_a", "target_x", correct=False, prob=0.4)
        accs = db.get_accuracy("model_a", "target_x")
        assert len(accs) == 1
        assert accs[0]["total_predictions"] == 5
        assert accs[0]["correct"] == 4
        assert round(accs[0]["accuracy"], 2) == 0.80

    def test_multiple_models(self, _fresh_db):
        db = _fresh_db
        db.update_accuracy("m1", "t1", correct=True, prob=0.8)
        db.update_accuracy("m2", "t1", correct=False, prob=0.3)
        all_accs = db.get_accuracy()
        assert len(all_accs) == 2


class TestPerformance:
    def test_record_performance(self, _fresh_db):
        db = _fresh_db
        db.record_performance(balance=10000, roi=0.05, win_rate=0.6,
                              total_bets=50, settled_bets=30)
        history = db.get_performance_history()
        assert len(history) == 1
        assert history[0]["balance"] == 10000
        assert history[0]["roi"] == 0.05

    def test_performance_history(self, _fresh_db):
        db = _fresh_db
        for i in range(5):
            db.record_performance(balance=10000 + i * 100, roi=0.01 * i)
        history = db.get_performance_history(limit=3)
        assert len(history) == 3  # limited to 3


class TestCLV:
    def test_record_clv(self, _fresh_db):
        db = _fresh_db
        db.record_clv("Lakers vs Celtics", "pinnacle", "h2h", 1.90, 1.95)
        summary = db.get_clv_summary()
        assert len(summary) == 1
        assert summary[0]["clv"] == pytest.approx(0.05)


class TestOddsCache:
    def test_save_and_query_odds(self, _fresh_db):
        db = _fresh_db
        game = {
            "sport_key": "basketball_nba",
            "home_team": "Lakers",
            "away_team": "Celtics",
            "commence_time": "2026-06-04T00:00:00Z",
            "bookmakers": [{
                "key": "pinnacle",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Lakers", "price": 1.91},
                        {"name": "Celtics", "price": 1.91},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": 1.85, "point": 215.5},
                        {"name": "Under", "price": 1.85, "point": 215.5},
                    ]},
                ],
            }],
        }
        db.save_odds("basketball_nba", [game])
        history = db.get_odds_history(sport_key="basketball_nba")
        assert len(history) == 1
        assert history[0]["h2h_home"] == 1.91

    def test_odds_history_filter(self, _fresh_db):
        db = _fresh_db
        db.save_odds("basketball_nba", [{
            "home_team": "Lakers", "away_team": "Celtics",
            "commence_time": "", "bookmakers": [{"key": "bm", "markets": []}]
        }])
        db.save_odds("soccer_epl", [{
            "home_team": "Arsenal", "away_team": "Chelsea",
            "commence_time": "", "bookmakers": [{"key": "bm", "markets": []}]
        }])
        bb = db.get_odds_history(sport_key="basketball_nba")
        fb = db.get_odds_history(sport_key="soccer_epl")
        assert len(bb) == 1
        assert len(fb) == 1


class TestDatabaseBackend:
    def test_resolve_db_url_default(self):
        from src.storage.database import _resolve_db_url
        url = _resolve_db_url()
        assert url.startswith("sqlite:///")

    def test_resolve_db_url_postgres(self):
        from src.storage.database import _resolve_db_url
        url = _resolve_db_url("postgresql://user:pass@host:5432/sportsbetting")
        assert url.startswith("postgresql://")

    def test_resolve_db_url_explicit_path(self):
        from src.storage.database import _resolve_db_url
        url = _resolve_db_url("/tmp/mydb.db")
        assert "sqlite" in url

    def test_get_db_type_sqlite(self):
        import tempfile
        from src.storage.database import SportsDatabase
        db = SportsDatabase(tempfile.mktemp(suffix=".db"))
        assert db.get_db_type() == "sqlite"

    def test_db_vacuum_sqlite(self):
        import tempfile
        from src.storage.database import SportsDatabase
        db = SportsDatabase(tempfile.mktemp(suffix=".db"))
        db.vacuum()

    def test_get_db_size_sqlite(self):
        import tempfile
        from src.storage.database import SportsDatabase
        db = SportsDatabase(tempfile.mktemp(suffix=".db"))
        size = db.get_db_size()
        assert size >= 0

    def test_close_then_reopen(self):
        import tempfile
        from src.storage.database import SportsDatabase
        db = SportsDatabase(tempfile.mktemp(suffix=".db"))
        db.record_bet(home="A", away="B", stake=100, odds=2.0, prob=0.5)
        db.close()
        db2 = SportsDatabase(tempfile.mktemp(suffix=".db"))
        db2.record_bet(home="C", away="D", stake=100, odds=2.0, prob=0.5)
