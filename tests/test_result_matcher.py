"""测试赛果匹配引擎。"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from src.monitor.result_matcher import (
    _norm, _cn_to_en, _parse_game, settle_from_portfolio,
    settle_from_history, auto_settle,
)


class TestNorm:
    def test_remove_parentheses(self):
        assert _norm("Team (2024)") == "team"

    def test_lowercase_and_strip(self):
        assert _norm("  Hello World  ") == "hello world"

    def test_special_chars_removed(self):
        result = _norm("FC Barcelona!")
        assert "barcelona" in result

    def test_chinese_characters_preserved(self):
        result = _norm("巴塞罗那")
        assert "巴塞罗那" in result

    def test_empty_string(self):
        assert _norm("") == ""
        assert _norm(None) == ""


class TestCnToEn:
    def test_basic_conversion(self, tmp_path):
        from src.monitor import result_matcher
        # Set up a temp mapping file
        mapping_file = tmp_path / "team_mapping.json"
        mapping_file.write_text(json.dumps({"Arsenal": "阿森纳", "Chelsea": "切尔西"}))
        result_matcher._cn_path = mapping_file
        result_matcher._CN2EN = {}
        result_matcher._TEAM_ALIASES = {}
        # Reload mappings
        exec(open(result_matcher.__file__).read().split("# ═══════════════════════════════════════════════════════════")[0])
        # Actually, let's just test _cn_to_en by directly setting up the mapping
        result_matcher._CN2EN = {"阿森纳": "arsenal"}
        assert _cn_to_en("阿森纳") == "arsenal"

    def test_fallback_to_original(self):
        from src.monitor import result_matcher
        result_matcher._CN2EN = {}
        assert _cn_to_en("UnknownTeam") == "UnknownTeam"


class TestParseGame:
    def test_vs_separator(self):
        assert _parse_game("Team A vs Team B") == ("team a", "team b")

    def test_hyphen_separator(self):
        assert _parse_game("TeamA - TeamB") == ("teama", "teamb")

    def test_empty_string(self):
        assert _parse_game("") == ("", "")


class TestSettleFromPortfolio:
    def test_settle_from_portfolio(self, tmp_path):
        from src.monitor import result_matcher
        # Set temp paths
        perf_file = tmp_path / "performance_history.csv"
        portfolio_file = tmp_path / "virtual_portfolio.json"
        result_matcher.PERF_FILE = perf_file
        result_matcher.PORTFOLIO_FILE = portfolio_file

        portfolio_file.write_text(json.dumps({
            "history": [
                {"id": "bet_1", "status": "won", "stake": 100, "odds": 2.0, "profit": 100, "date": "2026-01-01"},
            ],
            "balance": 10100,
        }))
        count = settle_from_portfolio()
        assert count > 0

    def test_no_portfolio_file(self, tmp_path):
        from src.monitor import result_matcher
        result_matcher.PERF_FILE = tmp_path / "perf.csv"
        result_matcher.PORTFOLIO_FILE = tmp_path / "nonexistent.json"
        assert settle_from_portfolio() == 0

    def test_invalid_portfolio_json(self, tmp_path):
        from src.monitor import result_matcher
        pf = tmp_path / "virtual_portfolio.json"
        pf.write_text("invalid json")
        result_matcher.PORTFOLIO_FILE = pf
        assert settle_from_portfolio() == 0


class TestAutoSettle:
    @patch("src.monitor.result_matcher.settle_from_portfolio")
    @patch("src.monitor.result_matcher.settle_from_history")
    def test_auto_settle_calls_sub_functions(self, mock_history, mock_portfolio):
        mock_portfolio.return_value = 1
        mock_history.return_value = 2
        result = auto_settle()
        assert result["total"] == 3
        assert result["portfolio"] == 1
        assert result["history"] == 2
