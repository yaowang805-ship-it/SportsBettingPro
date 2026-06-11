"""测试预测日志系统。"""
import pandas as pd
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.core.prediction_logger import (
    log_prediction, settle_prediction, batch_settle,
    _determine_result, _next_id,
)


class TestNextId:
    def test_next_id_format(self, tmp_path):
        from src.core.prediction_logger import LOG_DIR, LOG_FILE
        # Can't easily mock the ROOT, so test the format directly
        rid = _next_id()
        parts = rid.split("_")
        assert len(parts) == 2
        assert len(parts[0]) == 8  # YYYYMMDD
        assert parts[1].isdigit()


class TestDetermineResult:
    def test_h2h_home_win(self):
        assert _determine_result("h2h", "主胜", 2, 1) is True

    def test_h2h_home_loss(self):
        assert _determine_result("h2h", "主胜", 0, 2) is False

    def test_h2h_away_win(self):
        assert _determine_result("h2h", "客胜", 1, 2) is True

    def test_h2h_away_loss(self):
        assert _determine_result("h2h", "客胜", 2, 1) is False

    def test_h2h_draw_in_draw_market(self):
        assert _determine_result("h2h", "平局", 1, 1) is True

    def test_h2h_draw_not_win(self):
        assert _determine_result("h2h", "主胜", 1, 1) is False

    def test_total_over_hit(self):
        assert _determine_result("total", "大 2.5", 3, 0) is True

    def test_total_over_miss(self):
        assert _determine_result("total", "大 2.5", 1, 0) is False

    def test_total_under_hit(self):
        assert _determine_result("total", "小 2.5", 1, 0) is True

    def test_total_under_miss(self):
        assert _determine_result("total", "小 2.5", 3, 0) is False

    def test_total_mixed_format(self):
        assert _determine_result("totals", "over 217.5", 110, 110) is True
        assert _determine_result("totals", "under 217.5", 100, 100) is True  # total 200 < 217.5
        assert _determine_result("totals", "under 217.5", 120, 110) is False  # total 230 > 217.5

    def test_spread_home_covers(self):
        assert _determine_result("spread", "主队 -5.5", 110, 100) is True

    def test_spread_home_fails(self):
        assert _determine_result("spread", "主队 -5.5", 100, 97) is False

    def test_spread_away_covers(self):
        assert _determine_result("spread", "客队 +5.5", 100, 110) is True

    def test_win_market_home(self):
        assert _determine_result("win", "home", 2, 0) is True

    def test_win_market_away(self):
        assert _determine_result("win", "away", 0, 2) is True

    def test_unknown_market_defaults_false(self):
        assert _determine_result("unknown", "", 1, 0) is False


class TestSettlePrediction:
    def test_settle_won(self, tmp_path):
        from src.core import prediction_logger
        log_file = tmp_path / "prediction_log.csv"
        prediction_logger.LOG_FILE = log_file
        df = pd.DataFrame([{
            "id": "test_0", "status": "pending", "sport": "nba",
            "league": "NBA", "odds": 2.0, "model_prob": 0.6,
        }])
        df.to_csv(log_file, index=False)
        settle_prediction("test_0", won=True, result_odds=1.91)
        result = pd.read_csv(log_file)
        assert result["status"].iloc[0] == "won"
        assert result["settled_at"].iloc[0] != ""

    def test_settle_lost(self, tmp_path):
        from src.core import prediction_logger
        log_file = tmp_path / "prediction_log.csv"
        prediction_logger.LOG_FILE = log_file
        df = pd.DataFrame([{
            "id": "test_0", "status": "pending", "sport": "nba",
            "league": "NBA", "odds": 2.0,
        }])
        df.to_csv(log_file, index=False)
        settle_prediction("test_0", won=False)
        result = pd.read_csv(log_file)
        assert result["status"].iloc[0] == "lost"

    def test_settle_nonexistent(self, tmp_path):
        from src.core import prediction_logger
        log_file = tmp_path / "prediction_log.csv"
        prediction_logger.LOG_FILE = log_file
        df = pd.DataFrame([{"id": "other", "status": "pending"}])
        df.to_csv(log_file, index=False)
        settle_prediction("nonexistent", won=True)  # should not raise
        result = pd.read_csv(log_file)
        assert result["status"].iloc[0] == "pending"

    def test_settle_no_file(self):
        # Should not raise
        settle_prediction("test", won=True)


class TestBatchSettle:
    @patch("fetchers.espn_scores.fetch_espn_scores")
    def test_batch_settle_nba(self, mock_fetch, tmp_path):
        from src.core import prediction_logger
        log_file = tmp_path / "prediction_log.csv"
        prediction_logger.LOG_FILE = log_file
        df = pd.DataFrame([{
            "id": "nba_0", "status": "pending", "sport": "nba",
            "league": "NBA", "odds": 2.0, "model_prob": 0.55,
            "match_time": "2026-06-09T20:00:00+00:00",
            "home_team_cn": "湖人", "away_team_cn": "勇士",
            "home_team_en": "Lakers", "away_team_en": "Warriors",
            "market_type": "h2h", "market_detail": "主胜",
        }])
        df.to_csv(log_file, index=False)
        mock_fetch.return_value = [
            {"home_team": "Lakers", "away_team": "Warriors",
             "home_score": 112, "away_score": 98, "completed": True},
        ]
        batch_settle()
        result = pd.read_csv(log_file)
        assert result["status"].iloc[0] == "won"

    @patch("fetchers.espn_scores.fetch_espn_scores")
    def test_batch_settle_no_match(self, mock_fetch, tmp_path):
        from src.core import prediction_logger
        log_file = tmp_path / "prediction_log.csv"
        prediction_logger.LOG_FILE = log_file
        df = pd.DataFrame([{
            "id": "nba_0", "status": "pending", "sport": "nba",
            "league": "NBA", "odds": 2.0,
            "match_time": "2026-06-09T20:00:00+00:00",
            "home_team_cn": "湖人", "away_team_cn": "勇士",
            "home_team_en": "Lakers", "away_team_en": "Warriors",
            "market_type": "h2h", "market_detail": "主胜",
        }])
        df.to_csv(log_file, index=False)
        mock_fetch.return_value = [
            {"home_team": "Celtics", "away_team": "Knicks",
             "home_score": 100, "away_score": 90, "completed": True},
        ]
        batch_settle()
        result = pd.read_csv(log_file)
        assert result["status"].iloc[0] == "pending"  # not matched

    def test_batch_settle_no_pending(self, tmp_path):
        from src.core import prediction_logger
        log_file = tmp_path / "prediction_log.csv"
        prediction_logger.LOG_FILE = log_file
        df = pd.DataFrame([{
            "id": "nba_0", "status": "won", "sport": "nba",
            "league": "NBA", "odds": 2.0,
        }])
        df.to_csv(log_file, index=False)
        batch_settle()  # should not crash
