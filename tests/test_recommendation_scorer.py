"""测试推荐质量评分器。"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from src.core.recommendation_scorer import RecommendationScorer, EFFICIENCY_PATH


class TestRecommendationScorer:
    def test_init_without_efficiency_file(self):
        with patch.object(RecommendationScorer, '_load_efficiency', return_value={}):
            scorer = RecommendationScorer()
            assert scorer._efficiency == {}

    def test_score_model_confidence(self):
        scorer = RecommendationScorer()
        # Large deviation → high score
        score = scorer._score_model_confidence(0.65, 0.45)
        assert score > 0
        # Small deviation → low score
        score_small = scorer._score_model_confidence(0.51, 0.49)
        assert score_small < score
        # Zero market prob → zero
        score_zero = scorer._score_model_confidence(0.6, 0.0)
        assert score_zero == 0.0

    def test_score_model_confidence_caps_at_30(self):
        scorer = RecommendationScorer()
        # 25pp deviation → 50 capped at 30
        score = scorer._score_model_confidence(0.75, 0.50)
        assert score == 30.0

    def test_score_market_efficiency_no_data(self):
        with patch.object(RecommendationScorer, '_load_efficiency', return_value={}):
            scorer = RecommendationScorer()
            score = scorer._score_market_efficiency("nba", "NBA", "h2h")
            assert score == 0.0

    def test_score_market_efficiency_with_data(self):
        eff_data = {
            "details": {
                "nba/NBA/h2h": {
                    "sharpe": 0.8,
                    "confidence_score": 90,
                    "n": 100,
                }
            }
        }
        with patch.object(RecommendationScorer, '_load_efficiency', return_value=eff_data):
            scorer = RecommendationScorer()
            score = scorer._score_market_efficiency("nba", "NBA", "h2h")
            assert score > 0

    def test_score_market_efficiency_low_sharpe(self):
        eff_data = {
            "details": {
                "nba/NBA/h2h": {
                    "sharpe": -0.2,
                    "confidence_score": 10,
                    "n": 50,
                }
            }
        }
        with patch.object(RecommendationScorer, '_load_efficiency', return_value=eff_data):
            scorer = RecommendationScorer()
            score = scorer._score_market_efficiency("nba", "NBA", "h2h")
            # sharpe=0 (negative), conf=10/100*12.5=1.25, n=50→multiplier=1.0
            assert score == pytest.approx(1.25, abs=0.01)

    def test_smart_money_high_agreement(self):
        scorer = RecommendationScorer()
        # model=0.65, market=0.55, sharp=0.60
        # sm_index = (0.60-0.55)/0.55*100 = 9.09, |sm_index| >= 5 so not neutral
        # model_deviation = 0.65-0.55 = 0.10 > 0, same direction as sm_index > 0
        # agreement = 9.09/100 = 0.0909, return 10 + 0.0909*10 = 10.909
        score = scorer._score_smart_money(0.65, 0.55, 0.60)
        assert score == pytest.approx(10.9, abs=0.1)

    def test_smart_money_disagreement(self):
        scorer = RecommendationScorer()
        # model says > market but market says < sharp
        score = scorer._score_smart_money(0.65, 0.55, 0.50)
        assert score < 10.0

    def test_smart_money_missing_sharp(self):
        scorer = RecommendationScorer()
        score = scorer._score_smart_money(0.65, 0.55, None)
        assert score == 10.0  # neutral

    def test_score_calibration(self):
        scorer = RecommendationScorer()
        score = scorer._score_calibration("nba", "NBA", "h2h")
        assert score >= 0

    def test_cold_start_penalty_low_data(self):
        with patch.object(RecommendationScorer, '_load_efficiency', return_value={}):
            scorer = RecommendationScorer()
            scorer._total_settled = 5
            score = scorer._score_cold_start()
            assert score == 0.0  # full penalty (no data)

    def test_cold_start_high_data(self):
        with patch.object(RecommendationScorer, '_load_efficiency', return_value={}):
            scorer = RecommendationScorer()
            scorer._total_settled = 500
            score = scorer._score_cold_start()
            assert score == 10.0  # no penalty

    def test_score_integration_direct(self):
        """完整的评分流程（不依赖外部文件）。"""
        eff_data = {
            "details": {
                "nba/NBA/h2h": {
                    "sharpe": 0.5, "confidence_score": 80, "n": 50,
                    "brier": 0.2, "cal_error": 0.05,
                }
            }
        }
        with patch.object(RecommendationScorer, '_load_efficiency', return_value=eff_data):
            scorer = RecommendationScorer()
            result = scorer.score({
                "sport": "nba", "league": "NBA",
                "model_prob": 0.65, "market_home_prob": 0.50,
                "sharp_home_prob": None,
                "market_type": "h2h", "market_detail": "主胜",
                "odds": 2.0,
            }, market_type="h2h")
            assert "score" in result
            assert "tier" in result
            assert 0 <= result["score"] <= 100

    def test_tier_high(self):
        eff_data = {
            "details": {
                "nba/NBA/h2h": {
                    "sharpe": 0.8, "confidence_score": 95, "n": 100,
                    "brier": 0.15, "cal_error": 0.03,
                }
            }
        }
        with patch.object(RecommendationScorer, '_load_efficiency', return_value=eff_data):
            scorer = RecommendationScorer()
            result = scorer.score({
                "sport": "nba", "league": "NBA",
                "model_prob": 0.80, "market_home_prob": 0.40,
                "sharp_home_prob": 0.75,
                "market_type": "h2h", "market_detail": "主胜",
                "odds": 2.5,
            }, market_type="h2h")
            assert result["tier"] == "high"

    def test_tier_low(self):
        with patch.object(RecommendationScorer, '_load_efficiency', return_value={}):
            scorer = RecommendationScorer()
            result = scorer.score({
                "sport": "nba", "league": "NBA",
                "model_prob": 0.51, "market_home_prob": 0.50,
                "sharp_home_prob": 0.50,
                "market_type": "h2h", "market_detail": "主胜",
                "odds": 1.91,
            })
            assert result["tier"] == "low"
