"""测试自动结算模块。"""
from src.monitor.auto_settle import _match_bet


def _make_game(home, away, home_score, away_score, completed=True):
    return {
        "home_team": home, "away_team": away,
        "completed": completed,
        "scores": [
            {"name": home, "score": str(home_score)},
            {"name": away, "score": str(away_score)},
        ],
    }


def _make_bet(home_cn, away_cn, market_type, home_team="", away_team=""):
    return {
        "home_cn": home_cn, "away_cn": away_cn,
        "home_team": home_team or home_cn,
        "away_team": away_team or away_cn,
        "market_type": market_type,
    }


class TestH2H:
    def test_home_win_main(self):
        bet = _make_bet("湖人", "勇士", "主胜")
        game = _make_game("Lakers", "Warriors", 112, 98)
        assert _match_bet(bet, [game]) == "won"

    def test_home_win_lost(self):
        bet = _make_bet("湖人", "勇士", "主胜")
        game = _make_game("Lakers", "Warriors", 98, 112)
        assert _match_bet(bet, [game]) == "lost"

    def test_away_win(self):
        bet = _make_bet("湖人", "勇士", "客胜")
        game = _make_game("Lakers", "Warriors", 98, 112)
        assert _match_bet(bet, [game]) == "won"

    def test_away_win_lost(self):
        bet = _make_bet("湖人", "勇士", "客胜")
        game = _make_game("Lakers", "Warriors", 112, 98)
        assert _match_bet(bet, [game]) == "lost"

    def test_draw_in_draw_market(self):
        bet = _make_bet("阿森纳", "切尔西", "平")
        game = _make_game("Arsenal", "Chelsea", 1, 1)
        assert _match_bet(bet, [game]) == "won"

    def test_draw_in_main_market(self):
        bet = _make_bet("阿森纳", "切尔西", "主胜")
        game = _make_game("Arsenal", "Chelsea", 1, 1)
        assert _match_bet(bet, [game]) == "lost"

    def test_home_cn_en_mismatch(self):
        """中文名 vs 英文队名匹配。"""
        bet = _make_bet("阿森纳", "切尔西", "主胜", home_team="Arsenal", away_team="Chelsea")
        game = _make_game("Arsenal", "Chelsea", 2, 0)
        assert _match_bet(bet, [game]) == "won"


class TestOverUnder:
    def test_over_hit(self):
        bet = _make_bet("阿森纳", "切尔西", "大 2.5")
        game = _make_game("Arsenal", "Chelsea", 3, 0)
        assert _match_bet(bet, [game]) == "won"

    def test_over_miss(self):
        bet = _make_bet("阿森纳", "切尔西", "大 2.5")
        game = _make_game("Arsenal", "Chelsea", 1, 0)
        assert _match_bet(bet, [game]) == "lost"

    def test_under_hit(self):
        bet = _make_bet("阿森纳", "切尔西", "小 2.5")
        game = _make_game("Arsenal", "Chelsea", 1, 0)
        assert _match_bet(bet, [game]) == "won"

    def test_under_miss(self):
        bet = _make_bet("阿森纳", "切尔西", "小 2.5")
        game = _make_game("Arsenal", "Chelsea", 3, 0)
        assert _match_bet(bet, [game]) == "lost"

    def test_over_exact_push_not_won(self):
        """整数线 total=line → push（V4.5 独立追踪 push，不强制归为 lost）。"""
        bet = _make_bet("阿森纳", "切尔西", "大 3")
        game = _make_game("Arsenal", "Chelsea", 2, 1)
        assert _match_bet(bet, [game]) == "push"

    def test_over_with_team_space(self):
        """市场类型含空格。"""
        bet = _make_bet("阿森纳", "切尔西", "大 2.5")
        game = _make_game("Arsenal", "Chelsea", 2, 1)
        assert _match_bet(bet, [game]) == "won"

    def test_decimal_line(self):
        bet = _make_bet("湖人", "勇士", "大 210.5")
        game = _make_game("Lakers", "Warriors", 110, 105)
        assert _match_bet(bet, [game]) == "won"

    def test_multiple_games(self):
        bet = _make_bet("巴塞罗那", "皇家马德里", "小 2.5")
        games = [
            _make_game("Atletico", "Sevilla", 1, 0),
            _make_game("Barcelona", "Real Madrid", 1, 0),
        ]
        assert _match_bet(bet, games) == "won"


class TestEdgeCases:
    def test_not_completed(self):
        bet = _make_bet("湖人", "勇士", "主胜")
        game = _make_game("Lakers", "Warriors", 112, 98, completed=False)
        assert _match_bet(bet, [game]) is None

    def test_unknown_market_returns_none(self):
        """未知 market_type 不误判。"""
        bet = _make_bet("湖人", "勇士", "unknown_market")
        game = _make_game("Lakers", "Warriors", 112, 98)
        assert _match_bet(bet, [game]) is None

    def test_no_team_match(self):
        bet = _make_bet("湖人", "勇士", "主胜")
        game = _make_game("Celtics", "Knicks", 100, 90)
        assert _match_bet(bet, [game]) is None

    def test_no_scores(self):
        bet = _make_bet("湖人", "勇士", "主胜")
        game = {"home_team": "Lakers", "away_team": "Warriors", "completed": True, "scores": []}
        assert _match_bet(bet, [game]) is None


class TestTeamMatching:
    def test_exact_match(self):
        from src.monitor.auto_settle import _team_matches
        assert _team_matches("lakers", "lakers")

    def test_word_token_match(self):
        from src.monitor.auto_settle import _team_matches
        assert _team_matches("lakers", "los angeles lakers")

    def test_token_in_all(self):
        from src.monitor.auto_settle import _team_matches
        assert _team_matches("lakers", "lakers gsw")

    def test_substring_long_name(self):
        from src.monitor.auto_settle import _team_matches
        assert _team_matches("barcelona", "fc barcelona")

    def test_generic_token_filtered(self):
        """通用词 'fc' 不应通过分词匹配误匹配。"""
        from src.monitor.auto_settle import _team_matches
        assert not _team_matches("fc", "fc barcelona")

    def test_alias_resolution(self):
        from src.monitor.auto_settle import _team_matches
        assert _team_matches("mancity", "manchester city")

    def test_alias_common(self):
        from src.monitor.auto_settle import _team_matches
        assert _team_matches("barca", "barcelona")

    def test_no_match_unrelated(self):
        from src.monitor.auto_settle import _team_matches
        assert not _team_matches("lakers", "yankees")
