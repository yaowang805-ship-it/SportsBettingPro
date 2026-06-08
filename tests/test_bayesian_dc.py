"""测试贝叶斯 Dixon-Coles 模型 — 加载态预测 （无需 MCMC）。"""
import pytest
import json
import numpy as np
import tempfile
from pathlib import Path

from src.models.bayesian_dixon_coles import BayesianDixonColes, poisson_pmf


@pytest.fixture
def sample_model_params():
    """一组已知模型参数，无需 MCMC 即可构造。"""
    n_teams = 4
    teams = ["Liverpool", "ManCity", "Arsenal", "Chelsea"]
    return {
        "mu": np.log(1.5),
        "home_adv": 0.25,
        "rho": -0.05,
        "sigma_attack": 0.6,
        "sigma_defense": 0.6,
        "team_names": teams,
        "attack": {t: np.random.RandomState(42).normal(0, 0.3) for t in teams},
        "defence": {t: np.random.RandomState(99).normal(0, 0.3) for t in teams},
        "attack_std": {t: 0.2 for t in teams},
        "defence_std": {t: 0.2 for t in teams},
        "decay_halflife_days": 100,
    }


@pytest.fixture
def loaded_model(sample_model_params, tmp_path):
    """通过 load() 构造的模型（跳过 MCMC）。"""
    path = tmp_path / "test_dc.json"
    path.write_text(json.dumps(sample_model_params))
    model = BayesianDixonColes()
    model.load(str(path))
    return model


class TestBayesianDixonColes:
    def test_load_model(self, loaded_model):
        assert loaded_model.fitted
        assert loaded_model.n_teams == 4
        assert loaded_model.team_names == ["Liverpool", "ManCity", "Arsenal", "Chelsea"]
        assert loaded_model.home_adv == 0.25
        assert loaded_model.rho == -0.05

    def test_predict_returns_all_keys(self, loaded_model):
        result = loaded_model.predict("Liverpool", "ManCity")
        expected_keys = {"home_win", "draw", "away_win", "over_2_5", "under_2_5",
                         "btts", "lambda_home", "lambda_away", "score_matrix"}
        assert set(result.keys()) == expected_keys

    def test_predict_probabilities_sum_to_one(self, loaded_model):
        result = loaded_model.predict("Liverpool", "ManCity")
        total = result["home_win"] + result["draw"] + result["away_win"]
        assert abs(total - 1.0) < 1e-6

    def test_predict_home_advantage(self, loaded_model):
        """主场优势应使主场胜率 > 客场胜率（对称球队时）。"""
        # 两支等实力的球队
        result = loaded_model.predict("Liverpool", "Chelsea")
        assert result["home_win"] > result["away_win"]

    def test_predict_return_uncertainty(self, loaded_model):
        result = loaded_model.predict("Liverpool", "ManCity", return_uncertainty=True)
        assert "uncertainty" in result
        u = result["uncertainty"]
        assert "home_win_ci" in u
        assert "draw_ci" in u
        assert "away_win_ci" in u
        assert len(u["home_win_ci"]) == 2
        assert u["home_win_ci"][0] <= u["home_win_ci"][1]

    def test_unknown_team_shrinkage(self, loaded_model):
        """未知球队应使用分层先验收缩，不报错。"""
        result = loaded_model.predict("UnknownTeam", "AnotherUnknown")
        assert "error" not in result
        assert result["home_win"] > 0.0 and result["away_win"] > 0.0
        total = result["home_win"] + result["draw"] + result["away_win"]
        assert abs(total - 1.0) < 1e-6

    def test_unknown_team_returns_default_params(self, loaded_model):
        att, att_s, deff, deff_s = loaded_model._get_team_params("UnknownTeam")
        assert att == 0.0
        assert deff == 0.0
        assert att_s == loaded_model.sigma_attack
        assert deff_s == loaded_model.sigma_defense

    def test_known_team_returns_own_params(self, loaded_model):
        att, att_s, deff, deff_s = loaded_model._get_team_params("Liverpool")
        assert att == loaded_model.attack_params["Liverpool"]
        assert att_s == loaded_model.attack_std["Liverpool"]
        assert deff == loaded_model.defence_params["Liverpool"]
        assert deff_s == loaded_model.defence_std["Liverpool"]

    def test_predict_positive_goals(self, loaded_model):
        """预期进球应大于 0。"""
        result = loaded_model.predict("Liverpool", "ManCity")
        assert result["lambda_home"] > 0
        assert result["lambda_away"] > 0

    def test_predict_unfitted_returns_error(self):
        model = BayesianDixonColes()
        assert "error" in model.predict("Liverpool", "ManCity")

    def test_save_save_state(self, loaded_model, tmp_path):
        p = tmp_path / "saved.json"
        loaded_model.save(str(p))
        assert p.exists()
        data = json.loads(p.read_text())
        assert data["mu"] == loaded_model.mu
        assert data["home_adv"] == loaded_model.home_adv
        assert list(data["attack"].keys()) == loaded_model.team_names

    def test_asian_handicap_total_prob(self, loaded_model):
        result = loaded_model.predict_asian_handicap("Liverpool", "ManCity", handicap=-0.5)
        assert "home_cover" in result
        assert "away_cover" in result
        total = result["home_cover"] + result["away_cover"] + result["push"]
        assert abs(total - 1.0) < 1e-6

    def test_asian_handicap_zero_handicap(self, loaded_model):
        """0 盘口 → home_cover = home_win, away_cover = away_win."""
        ah = loaded_model.predict_asian_handicap("Liverpool", "ManCity", handicap=0.0)
        pred = loaded_model.predict("Liverpool", "ManCity")
        assert ah["push"] == pytest.approx(pred["draw"], abs=1e-6)


class TestPoissonPMF:
    def test_zero_goals(self):
        assert poisson_pmf(0, 1.0) == pytest.approx(np.exp(-1.0))

    def test_one_goal(self):
        assert poisson_pmf(1, 1.0) == pytest.approx(np.exp(-1.0) * 1.0)

    def test_sum_to_near_one(self):
        total = sum(poisson_pmf(k, 1.5) for k in range(0, 10))
        assert abs(total - 1.0) < 1e-4

    def test_zero_lambda(self):
        assert poisson_pmf(0, 0.0) == 1.0
        assert poisson_pmf(5, 0.0) == 0.0


class TestComputeWeights:
    def test_recent_game_weight_one(self):
        m = BayesianDixonColes()
        import pandas as pd
        dates = pd.Series(pd.to_datetime(["2025-01-01", "2025-01-01"]))
        w = m._compute_weights(dates)
        assert abs(w[0] - 1.0) < 1e-6

    def test_older_game_lower_weight(self):
        m = BayesianDixonColes(decay_halflife_days=100)
        import pandas as pd
        dates = pd.Series(pd.to_datetime(["2025-01-01", "2025-07-01"]))
        w = m._compute_weights(dates)
        assert w[1] == pytest.approx(1.0, abs=0.01)  # recent = 1
        assert w[0] < 1.0  # older < recent
