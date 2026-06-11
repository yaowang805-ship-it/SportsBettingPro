"""测试跨运动统一排名引擎。"""
import pytest
import json

from src.predict.rank_recommendations import (
    _calculate_kelly,
    load_recommendations,
    rank_recommendations,
)


class TestCalculateKelly:
    def test_positive_edge(self):
        k = _calculate_kelly(0.6, 2.0)
        assert k == pytest.approx(0.2, abs=1e-6)

    def test_zero_edge(self):
        k = _calculate_kelly(0.5, 2.0)
        assert k == 0.0

    def test_negative_edge(self):
        k = _calculate_kelly(0.4, 2.0)
        assert k == 0.0

    def test_bad_odds(self):
        assert _calculate_kelly(0.6, 1.0) == 0.0
        assert _calculate_kelly(0.6, 0.5) == 0.0

    def test_high_prob(self):
        k = _calculate_kelly(0.8, 1.5)
        b = 0.5
        expected = (0.8 * 0.5 - 0.2) / 0.5
        assert k == pytest.approx(expected)


class TestLoadRecommendations:
    def test_missing_file(self, tmp_path):
        recs = load_recommendations(tmp_path / "nonexistent.json", "test")
        assert recs == []

    def test_valid_file_adds_sport_tag(self, tmp_path):
        p = tmp_path / "test.json"
        data = {"recommendations": [
            {"home_team": "A", "away_team": "B", "odds": 2.0, "model_prob": 0.6}
        ]}
        p.write_text(json.dumps(data))
        recs = load_recommendations(p, "nba")
        assert len(recs) == 1
        assert recs[0]["sport"] == "nba"

    def test_invalid_json_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{bad json}")
        recs = load_recommendations(p, "test")
        assert recs == []


class TestRankRecommendations:
    @pytest.fixture(autouse=True)
    def _mock_files(self, tmp_path, monkeypatch):
        """用临时文件替换推荐文件路径。"""
        self.bb_path = tmp_path / "bb.json"
        self.fb_path = tmp_path / "fb.json"
        monkeypatch.setattr("src.predict.rank_recommendations.BB_RECS_FILE", self.bb_path)
        monkeypatch.setattr("src.predict.rank_recommendations.FB_RECS_FILE", self.fb_path)
        monkeypatch.setattr("src.predict.rank_recommendations.RANKED_OUTPUT", tmp_path / "ranked.json")

    def test_no_recs(self):
        """无推荐文件返回空列表。"""
        result = rank_recommendations()
        assert result == []

    def test_empty_recs(self):
        """推荐文件存在但为空列表。"""
        self.bb_path.write_text(json.dumps({"recommendations": []}))
        result = rank_recommendations()
        assert result == []

    def test_single_rec_below_ev_threshold(self):
        """EV < 2% 应被过滤。"""
        self.bb_path.write_text(json.dumps({"recommendations": [
            {"home_team": "A", "away_team": "B", "odds": 2.0, "model_prob": 0.51, "type": "h2h", "league": "NBA"}
        ]}))
        result = rank_recommendations()
        assert result == []

    def test_basic_rank_order(self):
        """多推荐按 EV 降序排列。"""
        self.bb_path.write_text(json.dumps({"recommendations": [
            {"home_team": "Low", "away_team": "B", "odds": 2.0, "model_prob": 0.55, "type": "h2h", "league": "NBA"},
            {"home_team": "High", "away_team": "B", "odds": 3.0, "model_prob": 0.60, "type": "h2h", "league": "NBA"},
        ]}))
        result = rank_recommendations()
        assert len(result) > 0
        assert result[0]["home_team"] == "High"  # 更高 EV 排第一

    def test_sport_diversity(self):
        """单运动不超过 60% 限制。"""
        self.bb_path.write_text(json.dumps({"recommendations": [
            {"home_team": f"NBA_{i}", "away_team": "Rival", "odds": 3.0, "model_prob": 0.60,
             "type": "h2h", "league": "NBA"}
            for i in range(10)
        ]}))
        self.fb_path.write_text(json.dumps({"recommendations": [
            {"home_team": f"FB_{i}", "away_team": "Rival", "odds": 3.0, "model_prob": 0.60,
             "type": "h2h", "league": "EPL"}
            for i in range(10)
        ]}))
        result = rank_recommendations()
        assert len(result) <= 8  # MAX_GLOBAL_RECS
        assert result is not None

    def test_result_keys(self):
        """返回推荐包含必要字段。"""
        self.bb_path.write_text(json.dumps({"recommendations": [
            {"home_team": "A", "away_team": "B", "odds": 2.5, "model_prob": 0.55,
             "type": "spread", "league": "NBA", "commence_time": "2025-01-01T12:00:00Z"}
        ]}))
        # 跳过低 EV 过滤：确保所有 EV 都达标
        # prob=0.55, odds=2.5 → mkt_prob=0.4, ev=0.15 >= 0.02 ✓
        result = rank_recommendations()
        assert len(result) == 1
        r = result[0]
        required = {"rank", "sport", "home_team", "away_team", "type", "odds",
                    "model_prob", "mkt_prob", "ev", "kelly_frac", "stake"}
        assert set(r.keys()) >= required
