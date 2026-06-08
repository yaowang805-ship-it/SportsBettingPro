"""贝叶斯 Dixon-Coles 足球比分预测模型（PyMC MCMC 版本）。

相比原版点估计模型的核心优势：
  1. 分层先验 → 未知球队自动收缩到联赛均值（不再固定 27.4% 平局）
  2. MCMC 后验采样 → 完整不确定性量化 → 更精确的凯利仓位
  3. 天然正则化 → 小样本球队不过拟合

用法:
    from src.models.bayesian_dixon_coles import BayesianDixonColes
    model = BayesianDixonColes()
    model.fit(df)  # ~3-5 分钟 MCMC 采样
    probs = model.predict("Liverpool", "Manchester City")
    probs["home_win"]  # 0.45 + 可信区间
"""
import sys, json, pickle, warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

# ── PyMC 延迟导入（仅在需要时加载，避免启动耗时） ──
# scipy 1.13+ 兼容性补丁（arviz 旧版需要）
import scipy.signal as _ss
if not hasattr(_ss, "gaussian"):
    _ss.gaussian = _ss.windows.gaussian

_PYMC_AVAILABLE = False
try:
    import pymc as pm
    import aesara.tensor as pt

    _PYMC_AVAILABLE = True
except ImportError:
    pm = None
    pt = None


def _dc_tau(home_goals, away_goals, lambda_h, lambda_a, rho):
    """Dixon-Coles 低分调整因子 τ（向量化实现）。"""
    tau = pt.ones_like(lambda_h)
    # (0,0)
    z00 = pt.eq(home_goals, 0) & pt.eq(away_goals, 0)
    tau = pt.set_subtensor(tau[z00], 1.0 - rho * lambda_h[z00] * lambda_a[z00])
    # (0,1)
    z01 = pt.eq(home_goals, 0) & pt.eq(away_goals, 1)
    tau = pt.set_subtensor(tau[z01], 1.0 + rho * lambda_h[z01])
    # (1,0)
    z10 = pt.eq(home_goals, 1) & pt.eq(away_goals, 0)
    tau = pt.set_subtensor(tau[z10], 1.0 + rho * lambda_a[z10])
    # (1,1)
    z11 = pt.eq(home_goals, 1) & pt.eq(away_goals, 1)
    tau = pt.set_subtensor(tau[z11], 1.0 - rho)
    return pt.clip(tau, 1e-8, 2.0)


def _dc_logp(home_goals, away_goals, lambda_h, lambda_a, rho):
    """Dixon-Coles 对数似然（向量化）。"""
    tau = _dc_tau(home_goals, away_goals, lambda_h, lambda_a, rho)
    logp = (
        pt.log(tau)
        + pm.logp(pm.Poisson.dist(lambda_h), home_goals)
        + pm.logp(pm.Poisson.dist(lambda_a), away_goals)
    )
    return logp


class BayesianDixonColes:
    """贝叶斯 Dixon-Coles 模型（PyMC NUTS）。"""

    def __init__(self, decay_halflife_days: int = 100):
        self.decay_halflife_days = decay_halflife_days
        self.team_mapping: Dict[str, int] = {}
        self.team_names: List[str] = []
        self.n_teams: int = 0
        self.fitted: bool = False

        # 后验样本
        self.trace: Optional[any] = None
        self.idata: Optional[any] = None

        # 后验均值（点估计兼容）
        self.mu: float = 0.0
        self.home_adv: float = 0.0
        self.rho: float = 0.0
        self.attack_params: Dict[str, float] = {}
        self.defence_params: Dict[str, float] = {}
        # 后验标准差（不确定性）
        self.attack_std: Dict[str, float] = {}
        self.defence_std: Dict[str, float] = {}

        # 分层先验的超参数
        self.sigma_attack: float = 1.0
        self.sigma_defense: float = 1.0

    def _compute_weights(self, dates: pd.Series) -> np.ndarray:
        if len(dates) == 0:
            return np.array([])
        most_recent = dates.max()
        days_diff = (most_recent - dates).dt.total_seconds() / (24 * 3600)
        decay_const = np.log(2) / self.decay_halflife_days
        return np.exp(-decay_const * days_diff.values)

    def fit(
        self,
        df: pd.DataFrame,
        draws: int = 800,
        tune: int = 800,
        chains: int = 2,
        target_accept: float = 0.85,
    ):
        """用 MCMC 训练贝叶斯 Dixon-Coles 模型。

        Args:
            df: 必须包含 date, home, away, home_goals, away_goals
            draws: 采样数（默认 800）
            tune: 预热数（默认 800）
            chains: 链数（默认 2）
            target_accept: NUTS target_accept
        """
        if not _PYMC_AVAILABLE:
            raise ImportError("PyMC 未安装: pip install pymc")

        df = df.dropna(subset=["home_goals", "away_goals"]).copy()
        df["date"] = pd.to_datetime(df["date"])

        # 球队编码
        all_teams = sorted(set(df["home"].unique()) | set(df["away"].unique()))
        self.team_mapping = {t: i for i, t in enumerate(all_teams)}
        self.team_names = all_teams
        self.n_teams = len(all_teams)

        home_idx = np.array(df["home"].map(self.team_mapping), dtype=int)
        away_idx = np.array(df["away"].map(self.team_mapping), dtype=int)
        home_goals = df["home_goals"].values.astype(int)
        away_goals = df["away_goals"].values.astype(int)
        n_games = len(df)

        logger.info(
            "  🧠 贝叶斯 Dixon-Coles: %d 支球队, %d 场比赛, %d 采样×%d 链",
            self.n_teams, n_games, draws, chains,
        )

        # ── 构建 PyMC 模型 ──
        with pm.Model() as model:
            # 分层先验
            σ_attack = pm.Exponential("σ_attack", 1.0)
            σ_defense = pm.Exponential("σ_defense", 1.0)
            attack = pm.Normal("attack", mu=0, sigma=σ_attack, shape=self.n_teams)
            defense = pm.Normal("defense", mu=0, sigma=σ_defense, shape=self.n_teams)

            # 基线参数
            mu = pm.Normal("mu", mu=np.log(max(df["home_goals"].mean(), 0.5)), sigma=1.0)
            home_adv = pm.Normal("home_adv", mu=0.2, sigma=0.15)
            rho = pm.Uniform("rho", -0.5, 0.5)

            # 期望进球
            λ_home = pt.exp(mu + home_adv + attack[home_idx] + defense[away_idx])
            λ_away = pt.exp(mu + attack[away_idx] + defense[home_idx])

            # 似然（通过 Potential 实现 DC 调整）
            ll = _dc_logp(home_goals, away_goals, λ_home, λ_away, rho)
            pm.Potential("likelihood", ll)

            # 采样
            self.idata = pm.sample(
                draws=draws,
                tune=tune,
                chains=chains,
                target_accept=target_accept,
                random_seed=42,
                progressbar=True,
                idata_kwargs={"log_likelihood": False},
            )

        self.trace = self.idata.posterior
        self.fitted = True

        # ── 提取后验均值 ──
        self.mu = float(self.trace["mu"].mean().values)
        self.home_adv = float(self.trace["home_adv"].mean().values)
        self.rho = float(self.trace["rho"].mean().values)
        self.sigma_attack = float(self.trace["σ_attack"].mean().values)
        self.sigma_defense = float(self.trace["σ_defense"].mean().values)

        attack_mean = self.trace["attack"].mean(dim=["chain", "draw"]).values
        attack_std = self.trace["attack"].std(dim=["chain", "draw"]).values
        defense_mean = self.trace["defense"].mean(dim=["chain", "draw"]).values
        defense_std = self.trace["defense"].std(dim=["chain", "draw"]).values

        for i, team in enumerate(all_teams):
            self.attack_params[team] = float(attack_mean[i])
            self.attack_std[team] = float(attack_std[i])
            self.defence_params[team] = float(defense_mean[i])
            self.defence_std[team] = float(defense_std[i])

        logger.info(
            "  ✅ 贝叶斯 DC 训练完成: μ=%.3f, HA=%.3f, ρ=%.3f, σ_att=%.3f, σ_def=%.3f",
            self.mu, self.home_adv, self.rho, self.sigma_attack, self.sigma_defense,
        )

    def _get_team_params(self, team: str) -> Tuple[float, float, float, float]:
        """获取球队参数（含未知球队的收缩估计）。"""
        if team in self.attack_params:
            return (
                self.attack_params[team],
                self.attack_std.get(team, self.sigma_attack),
                self.defence_params[team],
                self.defence_std.get(team, self.sigma_defense),
            )
        # 未知球队: 从分层先验获取（均值=0，标准差=σ）
        logger.debug("  未知球队 %s: 使用分层先验收缩估计", team)
        return 0.0, self.sigma_attack, 0.0, self.sigma_defense

    def predict(
        self, home_team: str, away_team: str, max_goals: int = 10,
        return_uncertainty: bool = False,
    ) -> Dict:
        """预测一场比赛的比分概率分布。

        当 return_uncertainty=True 时返回 90% 可信区间。
        """
        if not self.fitted:
            return {"error": "模型尚未训练"}

        ha, ha_std, hd, hd_std = self._get_team_params(home_team)
        aa, aa_std, ad, ad_std = self._get_team_params(away_team)

        λ_h = np.exp(self.mu + self.home_adv + ha + ad)
        λ_a = np.exp(self.mu + aa + hd)

        # 用后验不确定性对 λ 做扰动（蒙特卡洛，向量化加速）
        n_samples = 1000  # 1000 样本已足够稳定（5000 浪费）
        rng = np.random.default_rng(42)
        attack_h_s = rng.normal(ha, max(ha_std, 0.05), n_samples)
        defense_a_s = rng.normal(ad, max(ad_std, 0.05), n_samples)
        attack_a_s = rng.normal(aa, max(aa_std, 0.05), n_samples)
        defense_h_s = rng.normal(hd, max(hd_std, 0.05), n_samples)
        mu_s = rng.normal(self.mu, 0.05, n_samples)
        ha_s = rng.normal(self.home_adv, 0.03, n_samples)
        rho_s = rng.normal(self.rho, 0.03, n_samples)
        rho_s = np.clip(rho_s, -0.5, 0.5)

        λ_h_s = np.exp(mu_s + ha_s + attack_h_s + defense_a_s)
        λ_a_s = np.exp(mu_s + attack_a_s + defense_h_s)

        # 向量化：一次性计算所有样本的 Poisson PMF 矩阵
        # score[i, j, s] = poisson_pmf(i, λ_h_s[s]) * poisson_pmf(j, λ_a_s[s]) * tau
        g = np.arange(max_goals + 1)                      # [0..10]
        # log_pmf[i, s] = -λ_h_s[s] + i*log(λ_h_s[s]) - log(i!)
        log_fact = np.array([np.sum(np.log(np.arange(1, k + 1))) if k > 0 else 0.0 for k in g])
        # shape: (max_goals+1, n_samples)
        log_pmf_h = -λ_h_s[np.newaxis, :] + g[:, np.newaxis] * np.log(λ_h_s[np.newaxis, :]) - log_fact[:, np.newaxis]
        log_pmf_a = -λ_a_s[np.newaxis, :] + g[:, np.newaxis] * np.log(λ_a_s[np.newaxis, :]) - log_fact[:, np.newaxis]
        pmf_h = np.exp(log_pmf_h)  # (11, n)
        pmf_a = np.exp(log_pmf_a)  # (11, n)

        # 构建比分矩阵 (11, 11, n)
        sm_all = pmf_h[:, np.newaxis, :] * pmf_a[np.newaxis, :, :]  # (11, 11, n)

        # Dixon-Coles tau 调整（向量化）
        tau = np.ones_like(sm_all)
        tau[0, 0, :] = 1.0 - rho_s * λ_h_s * λ_a_s
        tau[0, 1, :] = 1.0 + rho_s * λ_h_s
        tau[1, 0, :] = 1.0 + rho_s * λ_a_s
        tau[1, 1, :] = 1.0 - rho_s
        tau = np.clip(tau, 1e-8, 2.0)
        sm_all *= tau

        # 归一化
        sm_sum = sm_all.sum(axis=(0, 1), keepdims=True)
        sm_sum = np.where(sm_sum <= 0, 1.0, sm_sum)
        sm_all /= sm_sum

        # 胜平负概率
        n_g = max_goals + 1
        i_idx, j_idx = np.indices((n_g, n_g))
        home_wins = sm_all[i_idx > j_idx].sum(axis=0)     # i > j → 主场胜
        draws_v = sm_all[i_idx == j_idx].sum(axis=0)       # i == j → 平
        away_wins = sm_all[i_idx < j_idx].sum(axis=0)      # i < j → 客场胜
        over_2_5 = sm_all[(i_idx + j_idx) > 2.5].sum(axis=0)
        btts_v = sm_all[1:, 1:, :].sum(axis=(0, 1))
        score_matrices = sm_all.mean(axis=2)  # 平均比分矩阵

        result = {
            "home_win": float(np.mean(home_wins)),
            "draw": float(np.mean(draws_v)),
            "away_win": float(np.mean(away_wins)),
            "over_2_5": float(np.mean(over_2_5)),
            "under_2_5": float(1.0 - np.mean(over_2_5)),
            "btts": float(np.mean(btts_v)),
            "lambda_home": float(λ_h),
            "lambda_away": float(λ_a),
            "score_matrix": score_matrices.tolist(),
        }

        if return_uncertainty:
            result["uncertainty"] = {
                "home_win_ci": [float(np.percentile(home_wins, 5)), float(np.percentile(home_wins, 95))],
                "draw_ci": [float(np.percentile(draws_v, 5)), float(np.percentile(draws_v, 95))],
                "away_win_ci": [float(np.percentile(away_wins, 5)), float(np.percentile(away_wins, 95))],
            }

        return result

    def predict_asian_handicap(
        self, home_team: str, away_team: str, handicap: float = -0.5
    ) -> Dict:
        """亚洲盘口覆盖概率。"""
        pred = self.predict(home_team, away_team, max_goals=10)
        if "error" in pred:
            return pred

        sm = np.array(pred["score_matrix"])
        mg = sm.shape[0] - 1
        home_cover, away_cover, push = 0.0, 0.0, 0.0
        for i in range(mg + 1):
            for j in range(mg + 1):
                eff = i + handicap
                if eff > j:
                    home_cover += sm[i, j]
                elif eff < j:
                    away_cover += sm[i, j]
                else:
                    push += sm[i, j]

        return {
            "home_cover": float(home_cover),
            "away_cover": float(away_cover),
            "push": float(push),
            "handicap": handicap,
            "lambda_home": pred["lambda_home"],
            "lambda_away": pred["lambda_away"],
        }

    def save(self, path: str):
        """保存模型参数（不含完整 MCMC trace）。"""
        data = {
            "mu": self.mu,
            "home_adv": self.home_adv,
            "rho": self.rho,
            "sigma_attack": self.sigma_attack,
            "sigma_defense": self.sigma_defense,
            "team_names": self.team_names,
            "attack": self.attack_params,
            "defence": self.defence_params,
            "attack_std": self.attack_std,
            "defence_std": self.defence_std,
            "decay_halflife_days": self.decay_halflife_days,
        }
        Path(path).write_text(json.dumps(data, indent=2))

    def load(self, path: str):
        """加载模型参数。"""
        data = json.loads(Path(path).read_text())
        self.mu = data["mu"]
        self.home_adv = data["home_adv"]
        self.rho = data["rho"]
        self.sigma_attack = data["sigma_attack"]
        self.sigma_defense = data["sigma_defense"]
        self.team_names = data["team_names"]
        self.attack_params = data["attack"]
        self.defence_params = data["defence"]
        self.attack_std = data.get("attack_std", {t: self.sigma_attack for t in self.team_names})
        self.defence_std = data.get("defence_std", {t: self.sigma_defense for t in self.team_names})
        self.decay_halflife_days = data["decay_halflife_days"]
        self.fitted = True
        self.n_teams = len(self.team_names)
        self.team_mapping = {t: i for i, t in enumerate(self.team_names)}


def poisson_pmf(k: int, lam: float) -> float:
    """泊松概率质量函数（纯 numpy，无 scipy 依赖）。"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return np.exp(-lam + k * np.log(lam) - np.sum(np.log(np.arange(1, k + 1))))


def train_bayesian_dc(
    save_path: str = "models/bayesian_dc_model.json",
    draws: int = 800,
    tune: int = 800,
    chains: int = 2,
) -> "BayesianDixonColes":
    """从足球历史数据训练贝叶斯 DC 模型并保存。"""
    csv_path = ROOT / "data" / "storage" / "football_history.csv"
    if not csv_path.exists():
        logger.warning("未找到足球历史数据: %s", csv_path)
        return BayesianDixonColes()

    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], format="mixed", utc=True).dt.tz_localize(None)

    model = BayesianDixonColes(decay_halflife_days=100)
    model.fit(
        df[["date", "home", "away", "home_goals", "away_goals"]],
        draws=draws, tune=tune, chains=chains,
    )

    if model.fitted:
        save_path_full = ROOT / save_path
        save_path_full.parent.mkdir(parents=True, exist_ok=True)
        model.save(str(save_path_full))
        logger.info("贝叶斯 DC 模型已保存到 %s", save_path_full)

    return model


if __name__ == "__main__":
    model = train_bayesian_dc()
    if model.fitted:
        print("\n预测示例: Liverpool vs Manchester City")
        result = model.predict("Liverpool", "Manchester City", return_uncertainty=True)
        hw = result["home_win"]
        dr = result["draw"]
        aw = result["away_win"]
        print(f"  主胜: {hw:.1%} (90%CI: {result['uncertainty']['home_win_ci'][0]:.1%}-{result['uncertainty']['home_win_ci'][1]:.1%})")
        print(f"  平局: {dr:.1%} (90%CI: {result['uncertainty']['draw_ci'][0]:.1%}-{result['uncertainty']['draw_ci'][1]:.1%})")
        print(f"  客胜: {aw:.1%} (90%CI: {result['uncertainty']['away_win_ci'][0]:.1%}-{result['uncertainty']['away_win_ci'][1]:.1%})")
        print(f"  大2.5球: {result['over_2_5']:.1%}")
        print(f"  预期进球: {result['lambda_home']:.2f} - {result['lambda_away']:.2f}")

        print("\n未知球队测试: UnknownTeam vs AnotherUnknown")
        result2 = model.predict("UnknownTeam", "AnotherUnknown", return_uncertainty=True)
        print(f"  主胜: {result2['home_win']:.1%} (CI: {result2['uncertainty']['home_win_ci'][0]:.1%}-{result2['uncertainty']['home_win_ci'][1]:.1%})")
        print(f"  平局: {result2['draw']:.1%}")
        print(f"  客胜: {result2['away_win']:.1%}")
