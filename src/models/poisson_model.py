"""泊松进球模型 — 独立泊松回归预测足球比分。

使用 sklearn 的 PoissonRegressor 学习攻击/防守参数。
生成得分矩阵 → 胜/平/负 + 大小球概率。

与 Bayesian Dixon-Coles 的区别：
  - DC: 关联参数 rho 捕捉低分相关性
  - Poisson: 独立模型，更简单但更稳定，小数据上不易过拟合

用法:
    model = PoissonGoalModel()
    model.fit(df)
    pred = model.predict_proba("Liverpool", "ManCity")
    pred["home_win"]  # 0.42
"""
import sys, json, pickle
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import PoissonRegressor

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)


class PoissonGoalModel:
    """独立泊松进球模型。

    home_goals ~ Poisson(exp(mu + home_adv + attack_h + defense_a))
    away_goals ~ Poisson(exp(mu + attack_a + defense_h))

    attack/defense 为正则化泊松回归参数。
    """

    def __init__(self, alpha: float = 1.0, max_iter: int = 500):
        self.home_model = PoissonRegressor(alpha=alpha, max_iter=max_iter)
        self.away_model = PoissonRegressor(alpha=alpha, max_iter=max_iter)
        self.teams_: List[str] = []
        self.team_to_idx: Dict[str, int] = {}
        self.n_teams: int = 0
        self.mu: float = 0.0
        self.home_adv: float = 0.0
        self.fitted: bool = False

    def _build_design(self, df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """构建泊松回归的设计矩阵。

        主场模型: X_home = [attack_home, defense_away, home_adv_constant]
        客场模型: X_away = [attack_away, defense_home]

        Returns:
            X_home: (n_games, n_teams*2 + 1)
            y_home: (n_games,) home_goals
            X_away: (n_games, n_teams*2)
            y_away: (n_games,) away_goals
        """
        all_teams = sorted(set(df['home'].unique()) | set(df['away'].unique()))
        self.teams_ = all_teams
        self.n_teams = len(all_teams)
        self.team_to_idx = {t: i for i, t in enumerate(all_teams)}
        n = len(df)

        X_home = np.zeros((n, self.n_teams * 2 + 1))
        y_home = np.zeros(n)
        X_away = np.zeros((n, self.n_teams * 2))
        y_away = np.zeros(n)

        for i, (_, row) in enumerate(df.iterrows()):
            hi = self.team_to_idx[row['home']]
            ai = self.team_to_idx[row['away']]

            X_home[i, hi] = 1
            X_home[i, self.n_teams + ai] = 1
            X_home[i, -1] = 1
            y_home[i] = row['home_goals']

            X_away[i, ai] = 1
            X_away[i, self.n_teams + hi] = 1
            y_away[i] = row['away_goals']

        return X_home, y_home, X_away, y_away

    def fit(self, df: pd.DataFrame):
        """在历史比赛数据上训练模型。"""
        df = df.dropna(subset=['home_goals', 'away_goals']).copy()
        logger.info('  泊松模型: %d 场比赛, 球队编码中...', len(df))

        X_home, y_home, X_away, y_away = self._build_design(df)
        self.home_model.fit(X_home, y_home)
        self.away_model.fit(X_away, y_away)

        # 提取基线参数
        self.mu = self.home_model.intercept_  # home model base
        self.home_adv = self.home_model.coef_[-1]  # 最后一列是 home_adv

        self.fitted = True
        logger.info('  泊松模型训练完成: %d 支球队, mu=%.3f, home_adv=%.3f',
                    self.n_teams, self.mu, self.home_adv)
        return self

    def predict_proba(self, home_team: str, away_team: str, max_goals: int = 10) -> Dict:
        """预测比赛的概率分布。

        Returns:
            home_win, draw, away_win, over_2.5, under_2.5, lambda_home, lambda_away, score_matrix
        """
        if not self.fitted:
            return {"error": "模型尚未训练"}

        # 构建特征向量
        if home_team not in self.team_to_idx or away_team not in self.team_to_idx:
            return self._predict_unknown(home_team, away_team, max_goals)

        hi = self.team_to_idx[home_team]
        ai = self.team_to_idx[away_team]

        x_home = np.zeros((1, self.n_teams * 2 + 1))
        x_home[0, hi] = 1
        x_home[0, self.n_teams + ai] = 1
        x_home[0, -1] = 1

        x_away = np.zeros((1, self.n_teams * 2))
        x_away[0, ai] = 1
        x_away[0, self.n_teams + hi] = 1

        lambda_home = float(self.home_model.predict(x_home)[0])
        lambda_away = float(self.away_model.predict(x_away)[0])

        # 构建得分矩阵
        from scipy.stats import poisson
        score_matrix = np.zeros((max_goals + 1, max_goals + 1))
        for i in range(max_goals + 1):
            pi = poisson.pmf(i, lambda_home)
            if pi < 1e-15:
                continue
            for j in range(max_goals + 1):
                score_matrix[i, j] = pi * poisson.pmf(j, lambda_away)

        sm_sum = score_matrix.sum()
        if sm_sum > 0:
            score_matrix /= sm_sum

        # 胜平负
        g = np.arange(max_goals + 1)
        home_win = score_matrix[g[:, None] > g[None, :]].sum()
        draw = score_matrix[g[:, None] == g[None, :]].sum()
        away_win = score_matrix[g[:, None] < g[None, :]].sum()

        # 大小球
        total_grid = g[:, None] + g[None, :]
        over_25 = score_matrix[total_grid > 2.5].sum()
        under_25 = 1.0 - over_25

        return {
            "home_win": float(home_win),
            "draw": float(draw),
            "away_win": float(away_win),
            "over_2.5": float(over_25),
            "under_2.5": float(under_25),
            "btts": float(score_matrix[1:, 1:].sum()),
            "lambda_home": lambda_home,
            "lambda_away": lambda_away,
            "score_matrix": score_matrix.tolist(),
        }

    def _predict_unknown(self, home_team: str, away_team: str, max_goals: int = 10) -> Dict:
        """当球队未知时使用联赛平均参数预测。"""
        logger.debug("  泊松未知球队: %s vs %s, 使用联赛均值", home_team, away_team)
        lambda_home = np.exp(self.mu + self.home_adv)
        lambda_away = np.exp(self.mu)

        from scipy.stats import poisson
        score_matrix = np.zeros((max_goals + 1, max_goals + 1))
        for i in range(max_goals + 1):
            pi = poisson.pmf(i, lambda_home)
            if pi < 1e-15:
                continue
            for j in range(max_goals + 1):
                score_matrix[i, j] = pi * poisson.pmf(j, lambda_away)
        score_matrix /= score_matrix.sum()

        g = np.arange(max_goals + 1)
        home_win = score_matrix[g[:, None] > g[None, :]].sum()
        draw = score_matrix[g[:, None] == g[None, :]].sum()
        away_win = score_matrix[g[:, None] < g[None, :]].sum()
        total_grid = g[:, None] + g[None, :]
        over_25 = score_matrix[total_grid > 2.5].sum()

        return {
            "home_win": float(home_win),
            "draw": float(draw),
            "away_win": float(away_win),
            "over_2.5": float(over_25),
            "under_2.5": float(1.0 - over_25),
            "btts": float(score_matrix[1:, 1:].sum()),
            "lambda_home": float(lambda_home),
            "lambda_away": float(lambda_away),
            "score_matrix": score_matrix.tolist(),
        }

    def save(self, path: str):
        """保存模型。"""
        data = {
            "teams": self.teams_,
            "team_to_idx": self.team_to_idx,
            "mu": self.mu,
            "home_adv": self.home_adv,
            "n_teams": self.n_teams,
        }
        with open(path, 'wb') as f:
            pickle.dump({
                "home_model": self.home_model,
                "away_model": self.away_model,
                "meta": data,
            }, f)

    def load(self, path: str):
        """加载模型。"""
        with open(path, 'rb') as f:
            obj = pickle.load(f)
        self.home_model = obj["home_model"]
        self.away_model = obj["away_model"]
        meta = obj["meta"]
        self.teams_ = meta["teams"]
        self.team_to_idx = meta["team_to_idx"]
        self.mu = meta["mu"]
        self.home_adv = meta["home_adv"]
        self.n_teams = meta["n_teams"]
        self.fitted = True


def train_poisson_model(save_path: str = "models/poisson_model.pkl") -> PoissonGoalModel:
    """从足球历史数据训练泊松模型。"""
    csv_path = ROOT / "data" / "storage" / "football_history.csv"
    if not csv_path.exists():
        logger.warning("未找到足球历史数据: %s", csv_path)
        return PoissonGoalModel()

    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], format="mixed", utc=True).dt.tz_localize(None)

    model = PoissonGoalModel(alpha=1.0)
    model.fit(df[["home", "away", "home_goals", "away_goals"]])

    if model.fitted:
        save_path_full = ROOT / save_path
        save_path_full.parent.mkdir(parents=True, exist_ok=True)
        model.save(str(save_path_full))
        logger.info("泊松模型已保存至 %s", save_path_full)

    return model


if __name__ == "__main__":
    model = train_poisson_model()
    if model.fitted:
        print("\n预测示例: Liverpool vs Manchester City")
        pred = model.predict_proba("Liverpool", "ManCity")
        for k in ["home_win", "draw", "away_win", "over_2.5", "btts"]:
            print(f"  {k}: {pred[k]:.1%}")
