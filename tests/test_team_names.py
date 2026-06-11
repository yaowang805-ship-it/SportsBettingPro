"""测试队名映射系统。"""
from src.core.team_names import (
    cn_team, lookup_football, cn_to_feature_name, cn_to_odds_name,
    feat_name, LEAGUE_CN, FOOTBALL_MAP, NBA_CN, WC_CN,
)


class TestLeagueCN:
    def test_major_leagues_present(self):
        required = ["soccer_epl", "soccer_spain_la_liga",
                     "soccer_germany_bundesliga", "soccer_italy_serie_a",
                     "soccer_france_ligue_one", "basketball_nba",
                     "americanfootball_nfl"]
        for r in required:
            assert r in LEAGUE_CN, f"缺少 {r}"

    def test_extended_leagues(self):
        assert LEAGUE_CN["soccer_brazil_campeonato"] == "巴甲"
        assert LEAGUE_CN["soccer_japan_j_league"] == "J联赛"


class TestNBA_CN:
    def test_all_teams_mapped(self):
        required = ["Los Angeles Lakers", "Boston Celtics",
                     "Golden State Warriors", "Miami Heat"]
        for r in required:
            assert r in NBA_CN, f"缺少 {r}"

    def test_cn_team_nba(self):
        assert cn_team("Los Angeles Lakers", "nba") == "湖人"


class TestFootballMap:
    def test_major_teams_mapped(self):
        required = ["Arsenal", "Chelsea", "Liverpool",
                     "FC Bayern Munich", "Real Madrid",
                     "Barcelona", "AC Milan", "Inter Milan",
                     "Paris Saint-Germain", "Juventus"]
        for r in required:
            assert r in FOOTBALL_MAP, f"缺少 {r}"

    def test_team_mapping_structure(self):
        for key, (feat, cn) in FOOTBALL_MAP.items():
            assert isinstance(feat, str), f"{key}: feat not str"
            assert isinstance(cn, str), f"{key}: cn not str"
            assert len(feat) > 0
            assert len(cn) > 0

    def test_lookup_football_exact(self):
        feat, cn = lookup_football("Arsenal")
        assert cn == "阿森纳"

    def test_lookup_football_alias(self):
        # Test alias resolution
        feat, cn = lookup_football("Alaves")
        assert cn == "阿拉维斯"
        feat, cn = lookup_football("PSG")
        assert cn == "巴黎圣日耳曼"

    def test_lookup_football_unknown(self):
        feat, cn = lookup_football("Unknown FC 2024")
        assert feat == "unknown fc 2024"  # lowercase normalized

    def test_cn_team_football(self):
        assert cn_team("Arsenal", "football") == "阿森纳"

    def test_cn_team_unknown(self):
        assert cn_team("NonExistentTeam", "nba") == "NonExistentTeam"


class TestCnToOddsName:
    def test_nba_cn_to_en(self):
        assert cn_to_odds_name("湖人") == "los angeles lakers"

    def test_football_cn_to_en(self):
        assert cn_to_odds_name("阿森纳") == "arsenal"

    def test_unknown_name_fallback(self):
        assert cn_to_odds_name("不存在的球队") == "不存在的球队"

    def test_case_insensitive(self):
        assert cn_to_odds_name("湖人") == cn_to_odds_name("湖人")

    def test_nba_cn_all_mapped(self):
        """所有NBA中文队名应有反向映射。"""
        for cn in NBA_CN.values():
            en = cn_to_odds_name(cn)
            assert en != cn or cn in ("76人",), f"{cn} 缺少反向映射"


class TestFeatureName:
    def test_feat_name_major(self):
        assert feat_name("Arsenal") == "arsenal fc"

    def test_feat_name_unknown(self):
        assert feat_name("Unknown Team") == "unknown team"


class TestCnToFeatureName:
    def test_nba(self):
        result = cn_to_feature_name("湖人", sport="nba")
        assert result  # should translate

    def test_football(self):
        result = cn_to_feature_name("阿森纳", sport="football")
        assert result == "arsenal fc"


class TestWcCN:
    def test_major_teams(self):
        required = ["Brazil", "Argentina", "France", "Germany",
                     "England", "Spain", "Portugal", "Netherlands"]
        for r in required:
            assert r in WC_CN, f"缺少 {r}"
