"""测试组合业绩归因模块。"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.risk.attribution import PerformanceAttribution, _parse_settled_key


class TestParseSettledKey:
    def test_standard_key(self):
        r = _parse_settled_key("nba_NBA_湖人_勇士_主胜")
        assert r["sport"] == "nba"
        assert r["league"] == "NBA"
        assert r["market_type"] == "主胜"
        assert r["valid"]

    def test_short_key_invalid(self):
        r = _parse_settled_key("too_short")
        assert not r["valid"]

    def test_empty_key_invalid(self):
        r = _parse_settled_key("")
        assert not r["valid"]

    def test_team_names_with_underscores(self):
        r = _parse_settled_key("fb_英超_曼彻斯特联_利物浦_主胜")
        assert r["sport"] == "fb"
        assert r["league"] == "英超"
        assert r["market_type"] == "主胜"
        assert r["valid"]


class TestPerformanceAttribution:
    @pytest.fixture
    def sample_portfolio(self, tmp_path):
        """创建一个样本组合文件。"""
        data = {
            "settled": {
                "nba_NBA_湖人_勇士_主胜": "lost",
                "nba_NBA_湖人_勇士_客胜": "won",
                "epl_英超_阿森纳_切尔西_主胜": "won",
                "epl_英超_阿森纳_切尔西_客胜": "lost",
                "laliga_西甲_巴萨_皇马_主胜": "lost",
            },
            "pending_bets": [
                {"sport": "nba", "league": "NBA", "market_type": "h2h",
                 "stake": 100, "odds": 2.0, "model_prob": 0.55},
                {"sport": "nfl", "league": "NFL", "market_type": "spread",
                 "stake": 200, "odds": 1.91, "model_prob": 0.6},
            ],
            "balance": 10000,
            "history": [],
        }
        fp = tmp_path / "test_portfolio.json"
        fp.write_text(json.dumps(data))
        return fp

    def test_compute_with_sample(self, sample_portfolio):
        attr = PerformanceAttribution(sample_portfolio)
        report = attr.compute()
        assert report["n_settled"] == 7  # 5 settled + 2 pending
        assert report["overall"]["bets"] == 5
        assert report["overall"]["wins"] == 2
        assert report["overall"]["losses"] == 3
        assert report["overall"]["win_rate"] == 0.4

    def test_by_sport(self, sample_portfolio):
        attr = PerformanceAttribution(sample_portfolio)
        report = attr.compute()
        by_sport = report["by_sport"]
        assert "篮球" in by_sport  # nba → 篮球
        assert "足球" in by_sport  # soccer_* → 足球
        assert by_sport["篮球"]["bets"] == 2
        assert by_sport["篮球"]["win_rate"] == 0.5

    def test_by_league(self, sample_portfolio):
        attr = PerformanceAttribution(sample_portfolio)
        report = attr.compute()
        by_league = report["by_league"]
        assert "NBA" in by_league
        assert "英超" in by_league
        assert "西甲" in by_league
        assert by_league["NBA"]["bets"] == 2

    def test_by_market(self, sample_portfolio):
        attr = PerformanceAttribution(sample_portfolio)
        report = attr.compute()
        by_market = report["by_market"]
        assert "主胜" in by_market
        assert "客胜" in by_market
        # 主胜: 2 lost (湖人, 巴萨), 1 won (阿森纳) = 1/3
        assert by_market["主胜"]["win_rate"] == pytest.approx(1/3, abs=0.001)

    def test_by_sport_market_cross(self, sample_portfolio):
        attr = PerformanceAttribution(sample_portfolio)
        report = attr.compute()
        cross = report["by_sport_market"]
        assert "篮球" in cross
        assert "主胜" in cross["篮球"]

    def test_empty_portfolio(self, tmp_path):
        fp = tmp_path / "empty.json"
        fp.write_text(json.dumps({"settled": {}, "pending_bets": [], "balance": 10000, "history": []}))
        attr = PerformanceAttribution(fp)
        report = attr.compute()
        assert report["n_settled"] == 0
        assert report["overall"]["bets"] == 0

    def test_missing_file(self):
        attr = PerformanceAttribution(Path("/tmp/nonexistent_portfolio.json"))
        report = attr.compute()
        assert report["n_settled"] == 0

    def test_all_won(self, tmp_path):
        fp = tmp_path / "all_won.json"
        fp.write_text(json.dumps({
            "settled": {
                "nba_NBA_A_B_主胜": "won",
                "nba_NBA_C_D_主胜": "won",
            },
            "pending_bets": [], "balance": 10000, "history": [],
        }))
        attr = PerformanceAttribution(fp)
        report = attr.compute()
        assert report["overall"]["win_rate"] == 1.0
        assert report["overall"]["wins"] == 2
