"""测试泊松进球模型 — 合成数据验证参数恢复。"""
import pytest
import numpy as np
import pandas as pd

from src.models.poisson_model import PoissonGoalModel


@pytest.fixture
def synthetic_football_data():
    """生成已知攻击/防守参数的合成比赛数据。"""
    np.random.seed(42)
    teams = ["Arsenal", "Chelsea", "Liverpool", "ManCity", "Tottenham"]
    n_games = 200

    attack = {"Arsenal": 0.3, "Chelsea": 0.1, "Liverpool": 0.4, "ManCity": 0.5, "Tottenham": 0.0}
    defence = {"Arsenal": -0.1, "Chelsea": 0.0, "Liverpool": -0.2, "ManCity": -0.3, "Tottenham": 0.2}
    mu = np.log(1.5)
    home_adv = 0.3

    rows = []
    for _ in range(n_games):
        home = np.random.choice(teams)
        away = np.random.choice([t for t in teams if t != home])
        lam_h = np.exp(mu + home_adv + attack[home] + defence[away])
        lam_a = np.exp(mu + attack[away] + defence[home])
        rows.append({
            "home": home,
            "away": away,
            "home_goals": np.random.poisson(lam_h),
            "away_goals": np.random.poisson(lam_a),
        })
    return pd.DataFrame(rows)


class TestPoissonGoalModel:
    def test_fit_and_predict(self, synthetic_football_data):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        assert model.fitted
        assert model.n_teams == 5

    def test_predict_returns_all_keys(self, synthetic_football_data):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        pred = model.predict_proba("Arsenal", "Chelsea")
        expected = {"home_win", "draw", "away_win", "over_2.5", "under_2.5",
                    "btts", "lambda_home", "lambda_away", "score_matrix"}
        assert set(pred.keys()) == expected

    def test_probabilities_sum_to_one(self, synthetic_football_data):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        pred = model.predict_proba("Arsenal", "Chelsea")
        total = pred["home_win"] + pred["draw"] + pred["away_win"]
        assert abs(total - 1.0) < 1e-6

    def test_home_advantage(self, synthetic_football_data):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        pred = model.predict_proba("ManCity", "Tottenham")
        assert pred["home_win"] > pred["away_win"]

    def test_strong_team_beats_weak(self, synthetic_football_data):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        # ManCity (attack=0.5, defense=-0.3) vs Tottenham (attack=0.0, defense=0.2)
        pred = model.predict_proba("ManCity", "Tottenham")
        assert pred["home_win"] > 0.5

    def test_unknown_team_uses_league_average(self, synthetic_football_data):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        pred = model.predict_proba("Unknown", "FC")
        assert "error" not in pred
        total = pred["home_win"] + pred["draw"] + pred["away_win"]
        assert abs(total - 1.0) < 1e-6

    def test_unfitted_returns_error(self):
        model = PoissonGoalModel()
        assert "error" in model.predict_proba("Arsenal", "Chelsea")

    def test_over_under_25(self, synthetic_football_data):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        pred = model.predict_proba("Liverpool", "ManCity")
        assert abs(pred["over_2.5"] + pred["under_2.5"] - 1.0) < 1e-6

    def test_positive_lambda(self, synthetic_football_data):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        pred = model.predict_proba("Arsenal", "Chelsea")
        assert pred["lambda_home"] > 0
        assert pred["lambda_away"] > 0

    def test_save_and_load(self, synthetic_football_data, tmp_path):
        model = PoissonGoalModel(alpha=0.1)
        model.fit(synthetic_football_data)
        p = tmp_path / "poisson.pkl"
        model.save(str(p))
        assert p.exists()

        model2 = PoissonGoalModel()
        model2.load(str(p))
        assert model2.fitted
        assert model2.teams_ == model.teams_
        pred = model2.predict_proba("Arsenal", "Chelsea")
        assert abs(pred["home_win"] + pred["draw"] + pred["away_win"] - 1.0) < 1e-6
