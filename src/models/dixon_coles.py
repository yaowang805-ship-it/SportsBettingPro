"""Dixon-Coles 双变量 Poisson 足球比分预测模型。

这是职业博彩领域足球预测的黄金标准模型 (Dixon & Coles, 1997)：
  - 预测精确比分的概率分布
  - 可用于定价亚洲盘口、大小球、胜平负
  - 时间衰减让近期比赛权重更高

用法:
    from src.models.dixon_coles import DixonColesModel
    model = DixonColesModel()
    model.fit(df)  # df 需要包含 date, home, away, home_goals, away_goals
    probs = model.predict("Liverpool", "Manchester City")
    # probs = {home_win: 0.45, draw: 0.25, away_win: 0.30, ...}
"""
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

from config.logging_config import get_logger

logger = get_logger(__name__)


def _dc_log_likelihood_vec(params, home_idx, away_idx, home_goals, away_goals, weights):
    """向量化 Dixon-Coles 负对数似然（比循环快 50x）。"""
    n_teams = len(set(home_idx) | set(away_idx))
    mu = params[0]; ha = params[1]; rho = params[2]
    attack = params[3:3 + n_teams]; defence = params[3 + n_teams:]

    log_lh = mu + ha + attack[home_idx] + defence[away_idx]
    log_la = mu + attack[away_idx] + defence[home_idx]
    lh = np.exp(np.clip(log_lh, -5, 5))
    la = np.exp(np.clip(log_la, -5, 5))

    tau = np.ones_like(lh)
    z0 = (home_goals == 0) & (away_goals == 0)
    z0h = (home_goals == 0) & (away_goals == 1)
    z0a = (home_goals == 1) & (away_goals == 0)
    z11 = (home_goals == 1) & (away_goals == 1)
    tau[z0] = 1.0 - rho * lh[z0] * la[z0]
    tau[z0h] = 1.0 + rho * lh[z0h]
    tau[z0a] = 1.0 + rho * la[z0a]
    tau[z11] = 1.0 - rho

    prob = tau * poisson.pmf(home_goals, lh) * poisson.pmf(away_goals, la)
    prob = np.maximum(prob, 1e-10)
    return -float(np.sum(weights * np.log(prob)))


class DixonColesModel:
    """Dixon-Coles 足球比分预测模型。"""

    def __init__(self, decay_halflife_days: int = 100):
        """
        Args:
            decay_halflife_days: 时间衰减半衰期（天），默认 100 天
        """
        self.decay_halflife_days = decay_halflife_days
        self.team_mapping: Dict[str, int] = {}
        self.team_names: List[str] = []
        self.params: Optional[np.ndarray] = None
        self.mu: float = 0.0
        self.home_adv: float = 0.0
        self.rho: float = 0.0
        self.attack_params: Dict[str, float] = {}
        self.defence_params: Dict[str, float] = {}
        self.n_teams: int = 0
        self.fitted: bool = False

    def _compute_weights(self, dates: pd.Series) -> np.ndarray:
        """计算时间衰减权重。"""
        if len(dates) == 0:
            return np.array([])
        most_recent = dates.max()
        days_diff = (most_recent - dates).dt.total_seconds() / (24 * 3600)
        decay_const = np.log(2) / self.decay_halflife_days
        return np.exp(-decay_const * days_diff.values)

    def fit(self, df: pd.DataFrame):
        """训练模型。

        Args:
            df: 必须包含 date, home, away, home_goals, away_goals
        """
        df = df.dropna(subset=["home_goals", "away_goals"]).copy()
        df["date"] = pd.to_datetime(df["date"])

        # 构建球队编号映射
        all_teams = sorted(set(df["home"].unique()) | set(df["away"].unique()))
        self.team_mapping = {t: i for i, t in enumerate(all_teams)}
        self.team_names = all_teams
        self.n_teams = len(all_teams)

        home_idx = np.array(df["home"].map(self.team_mapping), dtype=int)
        away_idx = np.array(df["away"].map(self.team_mapping), dtype=int)
        home_goals = df["home_goals"].values.astype(float)
        away_goals = df["away_goals"].values.astype(float)
        weights = self._compute_weights(df["date"])

        # 初始参数估计
        league_avg_goals = float(np.mean([*home_goals, *away_goals]))
        mu = np.log(max(league_avg_goals, 0.5))
        home_adv = np.log(max(np.mean(home_goals) / max(league_avg_goals, 0.5), 0.1))
        rho = -0.1  # 典型初始值

        n_params = 3 + 2 * self.n_teams
        x0 = np.zeros(n_params)
        x0[0] = mu
        x0[1] = home_adv
        x0[2] = rho

        # 参数约束：攻击参数和为 0，防守参数和为 0
        cons = (
            {'type': 'eq', 'fun': lambda p: np.sum(p[3:3 + self.n_teams])},
            {'type': 'eq', 'fun': lambda p: np.sum(p[3 + self.n_teams:])},
        )

        try:
            result = minimize(
                _dc_log_likelihood_vec, x0,
                args=(home_idx, away_idx, home_goals, away_goals, weights),
                method='SLSQP', constraints=cons,
                options={'maxiter': 500, 'ftol': 1e-4},
            )
            self.params = result.x
            self.mu = result.x[0]
            self.home_adv = result.x[1]
            self.rho = np.clip(result.x[2], -0.5, 0.5)

            for i, team in enumerate(all_teams):
                self.attack_params[team] = result.x[3 + i]
                self.defence_params[team] = result.x[3 + self.n_teams + i]

            self.fitted = True
            logger.info("  Dixon-Coles 模型训练完成: %d 支球队, %d 场比赛, ρ=%.3f",
                        self.n_teams, len(df), self.rho)

        except Exception as e:
            logger.warning("  Dixon-Coles 优化失败: %s", e)
            self.fitted = False

    def predict(self, home_team: str, away_team: str,
                max_goals: int = 10) -> Dict:
        """预测一场比赛的完整比分概率分布。

        Args:
            home_team: 主队名
            away_team: 客队名
            max_goals: 最大进球数（超出此数的概率被忽略）

        Returns:
            {
                "home_win": 主胜概率,
                "draw": 平局概率,
                "away_win": 客胜概率,
                "home_exact_goals": [概率数组],
                "away_exact_goals": [概率数组],
                "score_matrix": [[p_0_0, p_0_1, ...], ...],
                "lambda_home": 预期主队进球,
                "lambda_away": 预期客队进球,
                "over_2_5": 大2.5球概率,
                "under_2_5": 小2.5球概率,
                "btts": 双方进球概率,
            }
        """
        if not self.fitted:
            return {"error": "模型尚未训练"}

        ha = self.attack_params.get(home_team, 0)
        hd = self.defence_params.get(home_team, 0)
        aa = self.attack_params.get(away_team, 0)
        ad = self.defence_params.get(away_team, 0)

        lambda_h = np.exp(self.mu + self.home_adv + ha + ad)
        lambda_a = np.exp(self.mu + aa + hd)

        # 构建比分概率矩阵
        score_matrix = np.zeros((max_goals + 1, max_goals + 1))
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                tau = 1.0
                if i == 0 and j == 0:
                    tau = 1.0 - self.rho * lambda_h * lambda_a
                elif i == 0 and j == 1:
                    tau = 1.0 + self.rho * lambda_h
                elif i == 1 and j == 0:
                    tau = 1.0 + self.rho * lambda_a
                elif i == 1 and j == 1:
                    tau = 1.0 - self.rho
                score_matrix[i, j] = tau * poisson.pmf(i, lambda_h) * poisson.pmf(j, lambda_a)

        total_prob = score_matrix.sum()
        if total_prob > 0:
            score_matrix /= total_prob

        # 胜平负概率
        home_win = float(np.tril(score_matrix, -1).sum())  # i > j
        draw = float(np.diag(score_matrix).sum())  # i == j
        away_win = float(np.triu(score_matrix, 1).sum())  # i < j

        # 大小球概率
        over_2_5 = 0.0
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                if i + j > 2.5:
                    over_2_5 += score_matrix[i, j]

        # 双方进球
        btts = 0.0
        for i in range(1, max_goals + 1):
            for j in range(1, max_goals + 1):
                btts += score_matrix[i, j]

        # 边际进球分布
        home_goal_probs = [float(score_matrix[i, :].sum()) for i in range(max_goals + 1)]
        away_goal_probs = [float(score_matrix[:, j].sum()) for j in range(max_goals + 1)]

        return {
            "home_win": home_win,
            "draw": draw,
            "away_win": away_win,
            "home_exact_goals": home_goal_probs,
            "away_exact_goals": away_goal_probs,
            "score_matrix": score_matrix.tolist(),
            "lambda_home": float(lambda_h),
            "lambda_away": float(lambda_a),
            "over_2_5": float(over_2_5),
            "under_2_5": float(1.0 - over_2_5),
            "btts": float(btts),
        }

    def predict_asian_handicap(self, home_team: str, away_team: str,
                                handicap: float = -0.5) -> Dict:
        """计算亚洲盘口的覆盖概率。

        Args:
            home_team: 主队
            away_team: 客队
            handicap: 盘口（正 = 主队受让，负 = 主队让球）

        Returns:
            {home_cover_prob, away_cover_prob, push_prob}
        """
        pred = self.predict(home_team, away_team)
        if "error" in pred:
            return pred

        score_matrix = np.array(pred["score_matrix"])
        max_goals = score_matrix.shape[0] - 1

        home_covers = 0.0
        away_covers = 0.0
        push = 0.0

        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                effective_home = i + handicap
                if effective_home > j:
                    home_covers += score_matrix[i, j]
                elif effective_home < j:
                    away_covers += score_matrix[i, j]
                else:
                    push += score_matrix[i, j]

        return {
            "home_cover": float(home_covers),
            "away_cover": float(away_covers),
            "push": float(push),
            "handicap": handicap,
            "lambda_home": pred["lambda_home"],
            "lambda_away": pred["lambda_away"],
        }

    def predict_over_under(self, home_team: str, away_team: str,
                            line: float = 2.5) -> Dict:
        """计算大小球的覆盖概率。"""
        pred = self.predict(home_team, away_team)
        if "error" in pred:
            return pred

        score_matrix = np.array(pred["score_matrix"])
        max_goals = score_matrix.shape[0] - 1

        over = 0.0
        under = 0.0
        push = 0.0

        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                total = i + j
                if total > line:
                    over += score_matrix[i, j]
                elif total < line:
                    under += score_matrix[i, j]
                else:
                    push += score_matrix[i, j]

        return {
            "over": float(over),
            "under": float(under),
            "push": float(push),
            "line": line,
        }

    def save(self, path: str):
        """保存模型参数。"""
        import json
        data = {
            "mu": self.mu,
            "home_adv": self.home_adv,
            "rho": self.rho,
            "team_names": self.team_names,
            "attack": self.attack_params,
            "defence": self.defence_params,
            "decay_halflife_days": self.decay_halflife_days,
        }
        Path(path).write_text(json.dumps(data, indent=2))

    def load(self, path: str):
        """加载模型参数。"""
        import json
        data = json.loads(Path(path).read_text())
        self.mu = data["mu"]
        self.home_adv = data["home_adv"]
        self.rho = data["rho"]
        self.team_names = data["team_names"]
        self.attack_params = data["attack"]
        self.defence_params = data["defence"]
        self.decay_halflife_days = data["decay_halflife_days"]
        self.fitted = True
        self.n_teams = len(self.team_names)
        self.team_mapping = {t: i for i, t in enumerate(self.team_names)}


def train_dixon_coles(save_path: str = "models/dixon_coles_model.json") -> DixonColesModel:
    """从足球历史数据训练 Dixon-Coles 模型并保存。"""
    base = Path(__file__).resolve().parent.parent.parent
    csv_path = base / "data" / "storage" / "football_history.csv"
    if not csv_path.exists():
        logger.warning("未找到足球历史数据: %s", csv_path)
        return DixonColesModel()

    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], format="mixed", utc=True).dt.tz_localize(None)

    model = DixonColesModel(decay_halflife_days=100)
    model.fit(df[["date", "home", "away", "home_goals", "away_goals"]])

    if model.fitted:
        save_path_full = base / save_path
        save_path_full.parent.mkdir(parents=True, exist_ok=True)
        model.save(str(save_path_full))
        logger.info("Dixon-Coles 模型已保存到 %s", save_path_full)

    return model


if __name__ == "__main__":
    model = train_dixon_coles()
    if model.fitted:
        print("\n预测示例: Liverpool vs Manchester City")
        result = model.predict("Liverpool", "Manchester City")
        print(f"  主胜: {result['home_win']:.1%}")
        print(f"  平局: {result['draw']:.1%}")
        print(f"  客胜: {result['away_win']:.1%}")
        print(f"  大2.5球: {result['over_2_5']:.1%}")
        print(f"  预期进球: {result['lambda_home']:.2f} - {result['lambda_away']:.2f}")

        print("\n亚洲盘口: Liverpool -0.5")
        ah = model.predict_asian_handicap("Liverpool", "Manchester City", -0.5)
        print(f"  主队覆盖: {ah['home_cover']:.1%}")
        print(f"  客队覆盖: {ah['away_cover']:.1%}")
        print(f"  退款: {ah['push']:.1%}")
