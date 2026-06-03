"""测试赔率归一化模块 — OddsNormalizer, find_best_odds, get_bookmaker_list."""
import pytest
from datetime import datetime, timezone

from src.core.normalizer import (
    OddsNormalizer,
    find_best_odds,
    get_bookmaker_list,
    _find_best_h2h,
    _find_best_spread,
    _find_best_total,
)
from src.core.models import Match, Odds


# ── 测试用赔率数据 ──

SINGLE_MATCH_H2H = {
    "id": "test_1",
    "sport_key": "basketball_nba",
    "commence_time": "2026-05-27T18:00:00Z",
    "home_team": "Lakers",
    "away_team": "Celtics",
    "bookmakers": [
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Lakers", "price": 1.95},
                        {"name": "Celtics", "price": 1.91},
                    ]
                }
            ]
        },
        {
            "key": "bet365",
            "title": "Bet365",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Lakers", "price": 2.00},
                        {"name": "Celtics", "price": 1.87},
                    ]
                }
            ]
        }
    ]
}

SINGLE_MATCH_FULL = {
    "id": "test_2",
    "sport_key": "basketball_nba",
    "commence_time": "2026-05-27T18:00:00Z",
    "home_team": "Lakers",
    "away_team": "Celtics",
    "bookmakers": [
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Lakers", "price": 1.95},
                        {"name": "Celtics", "price": 1.91},
                    ]
                },
                {
                    "key": "spreads",
                    "outcomes": [
                        {"name": "Lakers", "price": 1.91, "point": -4.5},
                        {"name": "Celtics", "price": 1.91, "point": 4.5},
                    ]
                },
                {
                    "key": "totals",
                    "outcomes": [
                        {"name": "Over", "price": 1.91, "point": 225.5},
                        {"name": "Under", "price": 1.91, "point": 225.5},
                    ]
                }
            ]
        }
    ]
}

SINGLE_MATCH_NO_BOOKMAKERS = {
    "id": "test_3",
    "sport_key": "basketball_nba",
    "commence_time": "2026-05-27T18:00:00Z",
    "home_team": "Lakers",
    "away_team": "Celtics",
    "bookmakers": []
}

RAW_JSON_MULTI = [
    SINGLE_MATCH_FULL,
    SINGLE_MATCH_NO_BOOKMAKERS,
]


class TestFindBestH2H:
    def test_finds_best_home_odds(self):
        best, bm = _find_best_h2h(SINGLE_MATCH_H2H["bookmakers"], "Lakers")
        assert best == pytest.approx(2.00, abs=0.01)
        assert bm == "Bet365"

    def test_case_insensitive(self):
        best, bm = _find_best_h2h(SINGLE_MATCH_H2H["bookmakers"], "lakers")
        assert best == pytest.approx(2.00, abs=0.01)

    def test_no_match_returns_none(self):
        best, bm = _find_best_h2h(SINGLE_MATCH_H2H["bookmakers"], "NonExistent")
        assert best is None
        assert bm is None

    def test_empty_bookmakers(self):
        best, bm = _find_best_h2h([], "Lakers")
        assert best is None
        assert bm is None


class TestFindBestSpread:
    def test_finds_spread(self):
        pt, odds, bm = _find_best_spread(SINGLE_MATCH_FULL["bookmakers"], "Lakers")
        assert pt == -4.5
        assert odds == 1.91
        assert bm == "Pinnacle"

    def test_no_match_returns_none(self):
        pt, odds, bm = _find_best_spread([], "Lakers")
        assert pt is None
        assert odds is None
        assert bm is None


class TestFindBestTotal:
    def test_finds_total(self):
        pt, odds, bm = _find_best_total(SINGLE_MATCH_FULL["bookmakers"])
        assert pt == 225.5
        assert odds == 1.91
        assert bm == "Pinnacle"

    def test_empty_bookmakers(self):
        pt, odds, bm = _find_best_total([])
        assert pt is None
        assert odds is None
        assert bm is None


class TestOddsNormalizer:
    def test_from_api_response_parses_matches(self):
        matches = OddsNormalizer.from_api_response(RAW_JSON_MULTI)
        assert len(matches) == 2

    def test_match_with_odds(self):
        matches = OddsNormalizer.from_api_response(RAW_JSON_MULTI)
        m = matches[0]
        assert m.home_team == "Lakers"
        assert m.away_team == "Celtics"
        assert m.odds is not None
        assert m.odds.home_odds == pytest.approx(1.95, abs=0.01)
        assert m.odds.spread_point == -4.5
        assert m.odds.over_odds == 1.91
        assert m.odds.total_point == 225.5

    def test_match_without_bookmakers(self):
        matches = OddsNormalizer.from_api_response(RAW_JSON_MULTI)
        m = matches[1]
        assert m.odds is None

    def test_empty_input(self):
        matches = OddsNormalizer.from_api_response([])
        assert len(matches) == 0

    def test_datetime_parsing(self):
        matches = OddsNormalizer.from_api_response(RAW_JSON_MULTI)
        m = matches[0]
        assert m.date.tzinfo is not None

    def test_home_win_property(self):
        matches = OddsNormalizer.from_api_response([
            {**SINGLE_MATCH_H2H, "id": "test"},
            {**SINGLE_MATCH_H2H, "id": "test2"},
        ])
        # Home_win is based on scores which default to 0
        m = matches[0]
        assert hasattr(m, "home_win")
        # No score set → home_win should be False (0 == 0)
        # Actually home_win = home_score > away_score, both default 0
        assert not m.home_win


class TestFindBestOdds:
    def test_h2h_market(self):
        price, bm, pt = find_best_odds(SINGLE_MATCH_H2H, "h2h")
        assert price == pytest.approx(2.00, abs=0.01)
        assert bm == "Bet365"
        assert pt is None

    def test_spread_market(self):
        price, bm, pt = find_best_odds(SINGLE_MATCH_FULL, "spreads")
        assert price == 1.91
        assert pt == -4.5
        assert bm == "Pinnacle"

    def test_totals_market(self):
        price, bm, pt = find_best_odds(SINGLE_MATCH_FULL, "totals")
        assert price == 1.91
        assert pt == 225.5

    def test_no_bookmakers(self):
        price, bm, pt = find_best_odds(SINGLE_MATCH_NO_BOOKMAKERS, "h2h")
        assert price is None
        assert bm is None
        assert pt is None

    def test_empty_match(self):
        price, bm, pt = find_best_odds({}, "h2h")
        assert price is None
        assert bm is None
        assert pt is None

    def test_no_home_team(self):
        price, bm, pt = find_best_odds({"bookmakers": [{}]}, "h2h")
        assert price is None


class TestGetBookmakerList:
    def test_returns_bookmaker_names(self):
        bms = get_bookmaker_list(SINGLE_MATCH_H2H)
        assert len(bms) == 2
        assert "Pinnacle" in bms
        assert "Bet365" in bms

    def test_empty_bookmakers(self):
        bms = get_bookmaker_list(SINGLE_MATCH_NO_BOOKMAKERS)
        assert bms == []

    def test_empty_input(self):
        bms = get_bookmaker_list({})
        assert bms == []
