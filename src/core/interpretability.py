"""模型可解释性模块 — SHAP 特征重要性分析。

用法:
    from src.core.interpretability import compute_shap_values, report_feature_importance
    importance = report_feature_importance(model, X, feature_names)
"""
import json
import warnings
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd

from config.logging_config import get_logger
logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
SHAP_DIR = ROOT / "models" / "shap"
PERM_SAMPLE_MAX = 500  # permutation importance 最大采样行数


def compute_shap_values(model, X: pd.DataFrame, n_samples: int = 200) -> Tuple[np.ndarray, np.ndarray]:
    """计算 SHAP 值（用 KernelExplainer 兼容任意模型）。

    Args:
        model: 已训练的模型（需有 predict_proba）
        X: 特征 DataFrame
        n_samples: 采样子数（加速计算）

    Returns:
        (shap_values, expected_value)
    """
    import shap

    # 采样背景数据集（加速 KernelSHAP）
    if len(X) > n_samples:
        X_background = X.sample(n_samples, random_state=42)
    else:
        X_background = X

    # 对集成模型（VotingClassifier/CalibratedClassifierCV），用 predict_proba 封装
    def model_predict(x):
        proba = model.predict_proba(x)
        return proba[:, 1] if proba.ndim > 1 and proba.shape[1] > 1 else proba.ravel()

    # KernelExplainer 兼容所有模型类型
    explainer = shap.KernelExplainer(model_predict, X_background)
    shap_values = explainer.shap_values(X_background, nsamples=100)

    return shap_values, explainer.expected_value


def report_feature_importance(model, X: pd.DataFrame, feature_names: List[str] = None,
                               save_dir: str = None) -> pd.DataFrame:
    """计算并输出特征重要性排名。

    优先使用模型内置 feature_importances_（TreeSHAP 兼容），
    回退到 permutation importance。

    Args:
        model: 已训练的模型
        X: 特征 DataFrame
        feature_names: 特征名列表
        save_dir: 保存目录（可选）

    Returns:
        特征重要性 DataFrame，按重要性降序
    """
    if feature_names is None:
        feature_names = X.columns.tolist()

    X_array = X.values if hasattr(X, 'values') else X

    # 优先取模型内置 feature_importances_
    importance = None
    method = "unknown"

    if hasattr(model, 'feature_importances_'):
        importance = model.feature_importances_
        method = "feature_importances_"
    elif hasattr(model, 'coef_'):
        importance = np.abs(model.coef_).flatten()
        method = "coef_"
    elif hasattr(model, 'estimators_'):
        # VotingClassifier: estimators_ = [(name, estimator), ...]
        for name, est in model.estimators_:
            if hasattr(est, 'feature_importances_'):
                importance = est.feature_importances_
                method = f"estimators_.{name}.feature_importances_"
                break
    elif hasattr(model, 'estimator'):
        est = model.estimator
        if hasattr(est, 'feature_importances_'):
            importance = est.feature_importances_
            method = "estimator.feature_importances_"
        elif hasattr(est, 'coef_'):
            importance = np.abs(est.coef_).flatten()
            method = "estimator.coef_"
        elif hasattr(est, 'estimators_'):
            for name, e in est.estimators_:
                if hasattr(e, 'feature_importances_'):
                    importance = e.feature_importances_
                    method = f"estimator.estimators_.{name}.feature_importances_"
                    break

    # 如果以上都无法提取，用 permutation importance（采样加速）
    if importance is None:
        method = "permutation"
        try:
            n = min(len(X_array), PERM_SAMPLE_MAX)
            if n < len(X_array):
                idx = np.random.RandomState(42).choice(len(X_array), n, replace=False)
                X_samp = X_array[idx]
            else:
                X_samp = X_array
            probas = model.predict_proba(X_samp)
            y_dummy = (probas[:, 1] >= 0.5).astype(int)
            base_score = np.mean(y_dummy == (probas[:, 1] >= 0.5))
            importance = np.zeros(len(feature_names))
            for i in range(len(feature_names)):
                X_perm = X_samp.copy()
                np.random.shuffle(X_perm[:, i])
                perm_probas = model.predict_proba(X_perm)
                perm_score = np.mean(y_dummy == (perm_probas[:, 1] >= 0.5))
                importance[i] = max(0, base_score - perm_score)
        except Exception as e:
            warnings.warn(f"Permutation importance failed: {e}")
            importance = np.ones(len(feature_names)) / len(feature_names)

    df = pd.DataFrame({
        "feature": feature_names,
        "importance": importance,
        "method": method,
    }).sort_values("importance", ascending=False).reset_index(drop=True)

    # 归一化
    if df["importance"].sum() > 0:
        df["importance_pct"] = df["importance"] / df["importance"].sum()
    else:
        df["importance_pct"] = 1.0 / len(df)

    # 输出排名
    logger.info("\n  📊 特征重要性排名 (method=%s):", method)
    logger.info("  %-35s %10s %8s", "特征名", "重要性", "占比")
    logger.info("  %s", "-" * 55)
    for _, row in df.head(15).iterrows():
        logger.info("  %-35s %10.4f %7.1f%%",
                   row["feature"][:34], row["importance"], row["importance_pct"] * 100)

    # 保存 CSV
    if save_dir:
        save_path = Path(save_dir)
        save_path.mkdir(parents=True, exist_ok=True)
        df.to_csv(save_path / "feature_importance.csv", index=False)
        logger.info("  已保存到 %s", save_path / "feature_importance.csv")

    return df


def detect_feature_drift(baseline_path: str, new_importance: pd.DataFrame,
                          threshold: float = 0.15) -> List[dict]:
    """检测特征重要性漂移。

    Args:
        baseline_path: 基线特征重要性 CSV 路径
        new_importance: 当前特征重要性 DataFrame
        threshold: 漂移阈值（特征排名变化超过此值视为漂移）

    Returns:
        漂移特征列表 [{feature, rank_change, importance_change}]
    """
    if not Path(baseline_path).exists():
        logger.warning("基线文件不存在: %s", baseline_path)
        return []

    baseline = pd.read_csv(baseline_path)
    if "feature" not in baseline.columns or "importance_pct" not in baseline.columns:
        return []

    merged = baseline[["feature", "importance_pct"]].rename(
        columns={"importance_pct": "baseline"})
    merged = merged.merge(
        new_importance[["feature", "importance_pct"]].rename(
            columns={"importance_pct": "current"}),
        on="feature", how="outer"
    ).fillna(0)

    merged["rank_base"] = merged["baseline"].rank(ascending=False)
    merged["rank_curr"] = merged["current"].rank(ascending=False)
    merged["rank_change"] = abs(merged["rank_base"] - merged["rank_curr"])
    merged["importance_change"] = abs(merged["current"] - merged["baseline"])

    drifted = merged[merged["rank_change"] > threshold * len(merged)].copy()
    result = drifted.sort_values("rank_change", ascending=False)
    result = result.to_dict("records")

    if result:
        logger.info("\n  ⚠️ 特征漂移检测到 %d 个特征:", len(result))
        for r in result[:10]:
            logger.info("    %s: 排名变化 %.0f, 重要性变化 %+.1f%%",
                       r["feature"], r["rank_change"],
                       (r["current"] - r["baseline"]) * 100)
    else:
        logger.info("  ✅ 未检测到显著特征漂移")

    return result


def save_shap_report(model, X: pd.DataFrame, feature_names: List[str],
                      save_dir: str, model_name: str = "model") -> dict:
    """完整的 SHAP 分析流程：计算 + 特征重要性 + 保存。

    Returns:
        {mean_shap: dict, top_features: list}
    """
    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    # 特征重要性
    report_feature_importance(model, X, feature_names, save_dir=str(save_path))

    # SHAP 值（如果模型兼容）
    top_features = []

    try:
        shap_values, expected_value = compute_shap_values(model, X)
        if shap_values is not None:
            # 计算平均绝对值 SHAP
            mean_shap_values = np.abs(shap_values).mean(axis=0)

            # 排序
            sorted_idx = np.argsort(mean_shap_values)[::-1]
            top_features = [feature_names[i] for i in sorted_idx[:20]]

            # 保存 SHAP 值汇总
            shap_df = pd.DataFrame({
                "feature": feature_names,
                "mean_abs_shap": mean_shap_values,
            }).sort_values("mean_abs_shap", ascending=False)
            shap_df.to_csv(save_path / f"shap_summary_{model_name}.csv", index=False)
            logger.info("  SHAP 汇总已保存到 %s", save_path / f"shap_summary_{model_name}.csv")
    except Exception as e:
        logger.warning("  SHAP 计算跳过: %s", e)

    # 保存完整报告
    report = {
        "model_name": model_name,
        "n_features": len(feature_names),
        "top_20_features": top_features,
        "feature_importance_file": str(save_path / "feature_importance.csv"),
    }
    with open(save_path / f"shap_report_{model_name}.json", "w") as f:
        json.dump(report, f, indent=2)

    return report


if __name__ == "__main__":
    # 测试: 从训练好的模型加载并分析
    import joblib
    from config.settings import MODEL_DIR
    model_dir = Path(MODEL_DIR)

    # 找最近训练的模型
    pkl_files = list(model_dir.glob("*_ensemble.pkl"))
    if pkl_files:
        model_path = pkl_files[0]
        print(f"加载模型: {model_path}")
        model = joblib.load(model_path)

        # 加载特征
        prefix = model_path.stem.replace("_ensemble", "")
        feat_json = model_dir / f"{prefix}_features.json"
        if feat_json.exists():
            with open(feat_json) as f:
                feat_cols = json.load(f)

            # 加载样本数据
            sport = "bb" if "bb" in prefix else "fb"
            csv_path = f"data/processed/{sport}_features.csv"
            df = pd.read_csv(csv_path)
            feat_cols = [c for c in feat_cols if c in df.columns]
            X = df[feat_cols].fillna(0).head(500)

            report_feature_importance(model, X, feat_cols)
