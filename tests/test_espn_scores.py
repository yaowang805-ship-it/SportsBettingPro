"""测试ESPN比分数据源。"""
from unittest.mock import patch, MagicMock

from fetchers.espn_scores import (
    _parse_espn_game, LEAGUE_ESPN_PATH, SPORT_KEY_TO_LEAGUE,
)


class TestParseEspnGame:
    def test_basic_parsing(self):
        event = {
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Lakers"}, "score": "112"},
                    {"homeAway": "away", "team": {"displayName": "Warriors"}, "score": "98"},
                ],
                "status": {"type": {"name": "STATUS_FINAL"}},
            }],
        }
        result = _parse_espn_game(event)
        assert result["home_team"] == "Lakers"
        assert result["away_team"] == "Warriors"
        assert result["home_score"] == 112
        assert result["away_score"] == 98
        assert result["completed"] is True

    def test_no_competitions(self):
        assert _parse_espn_game({}) is None

    def test_insufficient_competitors(self):
        event = {
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Lakers"}, "score": "100"},
                ],
                "status": {"type": {"name": "STATUS_FINAL"}},
            }],
        }
        assert _parse_espn_game(event) is None

    def test_not_final_not_completed(self):
        event = {
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Lakers"}, "score": "50"},
                    {"homeAway": "away", "team": {"displayName": "Warriors"}, "score": "45"},
                ],
                "status": {"type": {"name": "STATUS_IN_PROGRESS"}},
            }],
        }
        result = _parse_espn_game(event)
        assert result["completed"] is False

    def test_missing_home_away_fallback(self):
        """无 homeAway 字段时默认第一个是主队。"""
        event = {
            "competitions": [{
                "competitors": [
                    {"team": {"displayName": "Lakers"}, "score": "100"},
                    {"team": {"displayName": "Celtics"}, "score": "90"},
                ],
                "status": {"type": {"name": "STATUS_FINAL"}},
            }],
        }
        result = _parse_espn_game(event)
        assert result["home_team"] == "Lakers"
        assert result["away_team"] == "Celtics"

    def test_none_score_defaults_zero(self):
        event = {
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Lakers"}, "score": None},
                    {"homeAway": "away", "team": {"displayName": "Warriors"}, "score": "98"},
                ],
                "status": {"type": {"name": "STATUS_FINAL"}},
            }],
        }
        result = _parse_espn_game(event)
        assert result["home_score"] == 0

    def test_game_date_extracted(self):
        event = {
            "date": "2026-01-15T20:00Z",
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Lakers"}, "score": "100"},
                    {"homeAway": "away", "team": {"displayName": "Warriors"}, "score": "90"},
                ],
                "status": {"type": {"name": "STATUS_FINAL"}},
            }],
        }
        result = _parse_espn_game(event)
        assert "2026-01-15" in result["game_date"]


class TestLeagueMappings:
    def test_league_espn_path_coverage(self):
        """所有主要联赛应有 ESPN 路径。"""
        required = ["NBA", "英超", "西甲", "德甲", "意甲", "法甲",
                     "NFL", "欧冠", "欧联"]
        for r in required:
            assert r in LEAGUE_ESPN_PATH, f"缺少 {r}"

    def test_sport_key_to_league_coverage(self):
        required = ["basketball_nba", "soccer_epl", "soccer_spain_la_liga",
                     "americanfootball_nfl"]
        for r in required:
            assert r in SPORT_KEY_TO_LEAGUE, f"缺少 {r}"

    def test_league_espn_path_format(self):
        for league, (path, display) in LEAGUE_ESPN_PATH.items():
            assert "/" in path, f"{league} 路径格式错误: {path}"
            assert display, f"{league} 缺少显示名"


class TestFetchEspnScores:
    @patch("fetchers.espn_scores._fetch_json")
    def test_fetch_espn_scores_empty_on_unknown_league(self, mock_fetch):
        from fetchers.espn_scores import fetch_espn_scores
        result = fetch_espn_scores("未知联赛")
        assert result == []
        mock_fetch.assert_not_called()

    @patch("fetchers.espn_scores._fetch_json")
    def test_fetch_espn_scores_parses_events(self, mock_fetch):
        mock_fetch.return_value = {
            "events": [
                {
                    "competitions": [{
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "Lakers"}, "score": "112"},
                            {"homeAway": "away", "team": {"displayName": "Warriors"}, "score": "98"},
                        ],
                        "status": {"type": {"name": "STATUS_FINAL"}},
                    }],
                },
                {
                    "competitions": [{
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "Celtics"}, "score": "105"},
                            {"homeAway": "away", "team": {"displayName": "Knicks"}, "score": "100"},
                        ],
                        "status": {"type": {"name": "STATUS_FINAL"}},
                    }],
                },
            ],
        }
        from fetchers.espn_scores import fetch_espn_scores
        result = fetch_espn_scores("NBA", days_back=1)
        assert len(result) == 2
        assert result[0]["home_team"] == "Lakers"

    @patch("fetchers.espn_scores._fetch_json")
    def test_fetch_only_completed(self, mock_fetch):
        mock_fetch.return_value = {
            "events": [
                {
                    "competitions": [{
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "A"}, "score": "1"},
                            {"homeAway": "away", "team": {"displayName": "B"}, "score": "0"},
                        ],
                        "status": {"type": {"name": "STATUS_FINAL"}},
                    }],
                },
                {
                    "competitions": [{
                        "competitors": [
                            {"homeAway": "home", "team": {"displayName": "C"}, "score": "0"},
                            {"homeAway": "away", "team": {"displayName": "D"}, "score": "0"},
                        ],
                        "status": {"type": {"name": "STATUS_IN_PROGRESS"}},
                    }],
                },
            ],
        }
        from fetchers.espn_scores import fetch_espn_scores
        result = fetch_espn_scores("NBA", days_back=1)
        assert len(result) == 1  # only the completed game

    @patch("fetchers.espn_scores._fetch_json")
    def test_fetch_espn_scores_by_sport_key(self, mock_fetch):
        mock_fetch.return_value = {
            "events": [{
                "competitions": [{
                    "competitors": [
                        {"homeAway": "home", "team": {"displayName": "Lakers"}, "score": "100"},
                        {"homeAway": "away", "team": {"displayName": "Warriors"}, "score": "90"},
                    ],
                    "status": {"type": {"name": "STATUS_FINAL"}},
                }],
            }],
        }
        from fetchers.espn_scores import fetch_espn_scores_by_sport_key
        result = fetch_espn_scores_by_sport_key("basketball_nba", days_back=1)
        assert len(result) == 1

    @patch("fetchers.espn_scores._fetch_json")
    def test_fetch_by_sport_key_unknown(self, mock_fetch):
        from fetchers.espn_scores import fetch_espn_scores_by_sport_key
        result = fetch_espn_scores_by_sport_key("unknown_sport")
        assert result == []
        mock_fetch.assert_not_called()

    @patch("fetchers.espn_scores.fetch_espn_scores")
    def test_build_espn_result_map(self, mock_fetch):
        mock_fetch.return_value = [
            {"home_team": "Lakers", "away_team": "Warriors",
             "home_score": 112, "away_score": 98, "completed": True},
        ]
        from fetchers.espn_scores import build_espn_result_map
        result = build_espn_result_map("basketball_nba")
        assert ("lakers", "warriors") in result
        winner, hs, aw = result[("lakers", "warriors")]
        assert winner == "Lakers"
