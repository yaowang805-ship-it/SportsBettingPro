"""测试集成预测引擎。"""
import json
import numpy as np
import pandas as pd
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.predict.ensemble_predictor import (
    _is_sharp_book, _haversine, EnsemblePredictor,
)


class TestSharpBook:
    def test_pinnacle_is_sharp(self):
        assert _is_sharp_book("Pinnacle") is True

    def test_betfair_is_sharp(self):
        assert _is_sharp_book("Betfair") is True

    def test_bovada_not_sharp(self):
        assert _is_sharp_book("Bovada") is False

    def test_fanduel_not_sharp(self):
        assert _is_sharp_book("FanDuel") is False

    def test_case_insensitive(self):
        assert _is_sharp_book("pinnacle") is True

    def test_spaces_ignored(self):
        assert _is_sharp_book("Bet fair") is True  # "betfair" in "betfair"
        assert _is_sharp_book("SMARKETS") is True


class TestHaversine:
    def test_same_point_zero(self):
        assert _haversine(40.0, -74.0, 40.0, -74.0) == 0.0

    def test_nyc_to_london(self):
        dist = _haversine(40.7128, -74.0060, 51.5074, -0.1278)
        assert 5500 < dist < 5600  # ~5570 km

    def test_symmetric(self):
        d1 = _haversine(35.0, 135.0, 40.0, -74.0)
        d2 = _haversine(40.0, -74.0, 35.0, 135.0)
        assert abs(d1 - d2) < 0.01


class TestEnsemblePredictor:
    def _make_features_file(self, tmp_path, prefix="model_bb"):
        """Create a minimal features JSON file."""
        feat_file = tmp_path / f"{prefix}_features.json"
        feat_file.write_text(json.dumps(["col1", "col2", "col3"]))
        return feat_file

    def test_init_bb(self, tmp_path):
        with patch("src.predict.ensemble_predictor.MODEL_DIR_PATH", tmp_path):
            self._make_features_file(tmp_path, "model_bb")
            predictor = EnsemblePredictor("bb")
            assert predictor.sport == "bb"
            assert predictor.prefix == "model_bb"

    def test_init_fb(self, tmp_path):
        with patch("src.predict.ensemble_predictor.MODEL_DIR_PATH", tmp_path):
            self._make_features_file(tmp_path, "model_fb")
            predictor = EnsemblePredictor("fb")
            assert predictor.sport == "fb"
            assert predictor.prefix == "model_fb"

    def test_init_nfl(self, tmp_path):
        with patch("src.predict.ensemble_predictor.MODEL_DIR_PATH", tmp_path):
            self._make_features_file(tmp_path, "model_nfl")
            predictor = EnsemblePredictor("nfl")
            assert predictor.sport == "nfl"
            assert predictor.prefix == "model_nfl"

    def test_init_invalid_sport_uses_fb_prefix(self, tmp_path):
        with patch("src.predict.ensemble_predictor.MODEL_DIR_PATH", tmp_path):
            self._make_features_file(tmp_path, "model_fb")
            predictor = EnsemblePredictor("invalid")
            assert predictor.sport == "invalid"
            assert predictor.prefix == "model_fb"

    def test_no_models_loaded(self, tmp_path):
        """When no model pickle files exist, models dict is empty."""
        with patch("src.predict.ensemble_predictor.MODEL_DIR_PATH", tmp_path):
            self._make_features_file(tmp_path, "model_bb")
            predictor = EnsemblePredictor("bb")
            assert len(predictor.models) == 0

    def test_haversine_distance(self):
        """验证行程距离计算。"""
        # London to Paris ~ 344 km
        dist = _haversine(51.5074, -0.1278, 48.8566, 2.3522)
        assert 300 < dist < 400
