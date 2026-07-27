"""ML 驱动的动态仓位管理 — 用历史投注结果训练最优凯利乘数预测。

替代 RiskManager 中阈值式的 _get_confidence_tier / _get_drawdown_multiplier /
_get_streak_multiplier，用 GradientBoostingRegressor 根据真实特征预测
每笔投注应使用的凯利分数乘数。

训练目标：最优凯利分数（opt_kelly_frac）
  - 若投注赢了：opt_kelly_frac = 1.0（全额凯利是正确的）
  - 若投注输了：opt_kelly_frac = 0.0（不应下注 — 惩罚）
  - 更精细的目标：实际 payout 与凯利全额的比率

用法:
    staker = DynamicStakingModel()
    staker.train()            # 从 bet_history.csv 训练
    mult = staker.predict_multiplier({
        "edge": 0.12, "model_prob": 0.62, "odds": 2.0,
        "drawdown_pct": 0.03, "consecutive_losses": 0,
        ...
    })  # → 0.75 等
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

MODEL_FILE = DATA_DIR / "dynamic_staking_model.json"
BET_LOG_FILE = DATA_DIR / "bet_history.csv"
MIN_TRAIN_SAMPLES = 50

# 基于文献和经验的先验乘数（当 ML 模型不可用时回退）
PRIOR_MULTIPLIERS = {
    "edge_high": 1.0,
    "edge_medium_high": 0.8,
    "edge_medium": 0.6,
    "edge_low": 0.3,
}


class DynamicStakingModel:
    """ML 驱动的动态仓位模型。

    用 GradientBoostingRegressor 学习特征与最优凯利乘数的关系。
    当训练数据不足时，回退到阈值式规则。
    """

    def __init__(self):
        self.model = None  # 训练后为 dict: {"trees": [...], "params": {...}}
        self.feature_cols = [
            "edge", "model_prob", "odds",
            "drawdown_pct", "consecutive_losses",
            "adaptive_kelly_frac", "n_active_bets",
            "win_rate", "total_bets",
        ]
        self.is_trained = False
        self._load()

    # ── 训练 ─────────────────────────────────────

    def collect_training_data(self, bet_log_path: Optional[Path] = None
                              ) -> Optional[pd.DataFrame]:
        """从 bet_history.csv 提取特征与训练目标。

        Returns:
            DataFrame 含 feature_cols + "target" 列，或 None（数据不足）
        """
        path = bet_log_path or BET_LOG_FILE
        if not path.exists():
            logger.info("  bet_history.csv 不存在，跳过 ML 训练")
            return None

        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning("  bet_log 读取失败: %s", e)
            return None

        if df.empty or len(df) < MIN_TRAIN_SAMPLES:
            logger.info("  bet_log 记录不足 %d 条 (%d)，跳过 ML 训练",
                        MIN_TRAIN_SAMPLES, len(df) if not df.empty else 0)
            return None

        # 确保必要列存在
        required = ["win", "stake", "odds", "model_prob"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            logger.info("  bet_log 缺少必要列: %s，跳过", missing)
            return None

        df = df.dropna(subset=required).copy()
        df["win"] = df["win"].astype(int)
        df["odds"] = df["odds"].astype(float)
        df["model_prob"] = df["model_prob"].astype(float)
        df["stake"] = df["stake"].astype(float)

        # 特征工程
        df["edge"] = df["model_prob"] - 1.0 / df["odds"]
        df["edge"] = df["edge"].clip(lower=0)

        # 滚动资金回撤（若无 balance_after 列则跳过）
        if "balance_after" in df.columns:
            df["balance_after"] = df["balance_after"].astype(float)
            initial = df["balance_after"].iloc[0] + df["stake"].iloc[0] * (
                1 - df["win"].iloc[0]
            )
            df["drawdown_pct"] = 1.0 - df["balance_after"] / max(initial, 1)
            df["drawdown_pct"] = df["drawdown_pct"].clip(lower=0)
        else:
            df["drawdown_pct"] = 0.0

        # 滚动连败
        df["consecutive_losses"] = (
            (~df["win"].astype(bool)).astype(int)
            .groupby((df["win"] == 1).cumsum())
            .cumsum()
        )

        # 滚动胜率
        df["win_rate"] = df["win"].rolling(50, min_periods=10).mean().fillna(0.5)
        df["total_bets"] = range(1, len(df) + 1)

        # 自适应凯利分数近似（从 stake/odds/prob 反推）
        df["implied_kelly"] = df.apply(
            lambda r: (
                (r["model_prob"] * (r["odds"] - 1) - (1 - r["model_prob"]))
                / (r["odds"] - 1)
                if r["odds"] > 1 else 0
            ), axis=1
        )
        df["implied_kelly"] = df["implied_kelly"].clip(lower=0)
        # 注意: 不再从 balance_after 反推 kelly_frac（含未来信息泄漏），改用常数
        df["adaptive_kelly_frac"] = 0.25

        df["n_active_bets"] = 1  # 简化：历史数据无反查组合规模

        # 训练目标：赢=1.0, 输=0.0
        df["target"] = df["win"].astype(float)

        # 确保所有特征列存在
        for col in self.feature_cols:
            if col not in df.columns:
                df[col] = 0.0

        result = df[self.feature_cols + ["target"]].dropna()
        logger.info("  ML 训练数据: %d 条记录, %d 特征",
                    len(result), len(self.feature_cols))
        return result

    def _train_sklearn(self, X: np.ndarray, y: np.ndarray):
        """用 sklearn GradientBoostingRegressor 训练。"""
        try:
            from sklearn.ensemble import GradientBoostingRegressor
            self._sk_model = GradientBoostingRegressor(
                n_estimators=120,
                max_depth=3,
                min_samples_leaf=10,
                learning_rate=0.08,
                subsample=0.8,
                random_state=42,
            )
            self._sk_model.fit(X, y)
            self.is_trained = True
            logger.info("  ✅ ML 动态仓位模型训练完成")
            return True
        except ImportError:
            logger.warning("  sklearn 未安装，回退到阈值式规则")
            return False

    def train(self, bet_log_path: Optional[Path] = None) -> bool:
        """训练动态仓位模型。

        Returns:
            True 表示训练成功，False 表示数据不足或回退到规则
        """
        data = self.collect_training_data(bet_log_path)
        if data is None or len(data) < MIN_TRAIN_SAMPLES:
            logger.info("  数据不足，使用阈值式规则回退")
            return False

        X = data[self.feature_cols].values
        y = data["target"].values

        success = self._train_sklearn(X, y)
        if success:
            self._save()
        return success

    # ── 预测 ─────────────────────────────────────

    def predict_multiplier(self, features: Dict[str, float]) -> float:
        """预测给定特征下的最优凯利分数乘数。

        Args:
            features: 至少含 self.feature_cols 中字段的 dict

        Returns:
            乘数 (0.1~1.0)，乘以 KELLY_FRACTION 得到最终分数
        """
        if not self.is_trained or not hasattr(self, "_sk_model") or self._sk_model is None:
            return self._rule_based_multiplier(features)

        try:
            X = pd.DataFrame([features])[self.feature_cols].values
            pred = self._sk_model.predict(X)[0]
            return float(np.clip(pred, 0.1, 1.0))
        except Exception as e:
            logger.warning("ML 预测失败 (%s)，回退到规则", e)
            return self._rule_based_multiplier(features)

    def _rule_based_multiplier(self, features: Dict[str, float]) -> float:
        """阈值式回退：与 RiskManager._get_confidence_tier 一致。"""
        edge = features.get("edge", 0)
        drawdown = features.get("drawdown_pct", 0)
        consecutive = features.get("consecutive_losses", 0)

        # 置信度分档
        if edge >= 0.15:
            conf = 1.0
        elif edge >= 0.10:
            conf = 0.8
        elif edge >= 0.06:
            conf = 0.6
        else:
            conf = 0.3

        # 回撤调整
        if drawdown <= 0.0:
            dd_mult = 1.0
        elif drawdown <= 0.05:
            dd_mult = 0.9
        elif drawdown <= 0.10:
            dd_mult = 0.7
        elif drawdown <= 0.20:
            dd_mult = 0.4
        else:
            dd_mult = 0.0

        # 连败调整
        if consecutive <= 1:
            streak_mult = 1.0
        elif consecutive <= 3:
            streak_mult = 0.7
        elif consecutive <= 5:
            streak_mult = 0.4
        else:
            streak_mult = 0.0

        return conf * dd_mult * streak_mult

    # ── 持久化 ─────────────────────────────────────

    def _save(self):
        """保存模型参数到 JSON（仅保存特征重要性等元数据 + sklearn pickle）。"""
        if not self.is_trained or self._sk_model is None:
            return
        try:
            import joblib
            MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
            pkl_path = MODEL_FILE.with_suffix(".pkl")
            joblib.dump(self._sk_model, pkl_path)

            # 保存元数据
            meta = {
                "feature_cols": self.feature_cols,
                "n_features": len(self.feature_cols),
                "trained_at": datetime.now().isoformat(),
                "feature_importances": (
                    self._sk_model.feature_importances_.tolist()
                    if hasattr(self._sk_model, "feature_importances_")
                    else None
                ),
            }
            MODEL_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
            logger.info("  💾 动态仓位模型已保存 (%s)", pkl_path)
        except Exception as e:
            logger.warning("模型保存失败: %s", e)

    def _load(self):
        """加载已训练的 sklearn 模型。"""
        pkl_path = MODEL_FILE.with_suffix(".pkl")
        if not pkl_path.exists():
            return
        try:
            import joblib
            self._sk_model = joblib.load(pkl_path)
            self.is_trained = True
            logger.info("  📂 动态仓位模型已加载 (%s)", pkl_path)
        except Exception as e:
            logger.warning("模型加载失败: %s", e)

    def get_feature_importance(self) -> Optional[Dict[str, float]]:
        """返回特征重要性（用于仪表盘展示）。"""
        if not self.is_trained or not hasattr(self, "_sk_model") or self._sk_model is None:
            return None
        if not hasattr(self._sk_model, "feature_importances_"):
            return None
        return dict(zip(
            self.feature_cols,
            [round(v, 4) for v in self._sk_model.feature_importances_.tolist()]
        ))


# ── 快捷集成 ─────────────────────────────────────

def maybe_train_dynamic_staking() -> bool:
    """在 RiskManager 初始化时调用，自动训练（如果有足够数据）。"""
    model = DynamicStakingModel()
    if model.is_trained:
        return True
    return model.train()


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    model = DynamicStakingModel()
    if model.train():
        imp = model.get_feature_importance()
        if imp:
            logger.info("特征重要性: %s", imp)
    else:
        logger.info("使用阈值式规则回退")
