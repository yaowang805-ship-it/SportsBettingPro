"""模型可解释性看板 — SHAP 特征重要性、特征漂移检测。"""
from pathlib import Path

import streamlit as st
import pandas as pd

from config.logging_config import get_logger
logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent.parent
MODEL_DIR = ROOT / "models"
SHAP_DIR = MODEL_DIR / "shap"


def render():
    st.title("🔬 模型可解释性")
    st.caption("特征重要性分析 · SHAP 值 · 漂移检测")

    sport = st.sidebar.selectbox(
        "选择运动", ["bb", "fb"],
        format_func=lambda x: {"bb": "🏀 NBA", "fb": "⚽ 足球"}[x],
    )

    tab1, tab2, tab3 = st.tabs(["📊 特征重要性", "📈 SHAP 分析", "⚠️ 漂移检测"])

    with tab1:
        _render_feature_importance(sport)
    with tab2:
        _render_shap(sport)
    with tab3:
        _render_drift(sport)


def _load_feature_importance(sport="bb"):
    paths = list(SHAP_DIR.glob(f"**/*{sport}*feature_importance*"))
    if not paths:
        feat_json = MODEL_DIR / f"model_{sport}_features.json"
        if feat_json.exists():
            return None, "特征文件存在但未运行 SHAP 分析。运行 `python3 src/models/ensemble_trainer.py {sport}` 后重试。"
        return None, f"未找到 {sport.upper()} 的特征数据"
    df = pd.read_csv(paths[0])
    return df, None


def _load_shap_summary(sport="bb"):
    paths = list(SHAP_DIR.glob(f"**/*{sport}*shap_summary*"))
    if paths:
        return pd.read_csv(paths[0]), None
    return None, None


def _render_feature_importance(sport):
    st.subheader("特征重要性排名")

    imp_df, error = _load_feature_importance(sport)
    if error:
        st.info(error)
    elif imp_df is not None and not imp_df.empty:
        col1, col2 = st.columns([1, 2])

        with col1:
            n_show = st.slider("显示特征数", 5, min(50, len(imp_df)), 15)
            st.dataframe(
                imp_df[["feature", "importance", "importance_pct"]].head(n_show).style
                .format({"importance": "{:.4f}", "importance_pct": "{:.1%}"}),
                hide_index=True, use_container_width=True,
            )

        with col2:
            chart_df = imp_df.head(n_show).copy()
            chart_df["color"] = chart_df["importance_pct"]
            st.bar_chart(chart_df, x="feature", y="importance_pct", horizontal=True,
                         use_container_width=True)

        st.caption(f"计算方法: {imp_df['method'].iloc[0] if 'method' in imp_df.columns else 'feature_importances_'}")
    else:
        st.info("暂无特征重要性数据。请先训练模型。")


def _render_shap(sport):
    st.subheader("SHAP 特征贡献")

    shap_df, error = _load_shap_summary(sport)
    if shap_df is not None and not shap_df.empty:
        col1, col2 = st.columns([1, 2])

        with col1:
            n_show = st.slider("显示特征数", 5, min(50, len(shap_df)), 15, key="shap_n")
            st.dataframe(
                shap_df.head(n_show).style.format({"mean_abs_shap": "{:.4f}"}),
                hide_index=True, use_container_width=True,
            )

        with col2:
            chart = shap_df.head(n_show).copy()
            chart["color"] = chart["mean_abs_shap"]
            st.bar_chart(chart, x="feature", y="mean_abs_shap", horizontal=True,
                         use_container_width=True)
    else:
        st.info(
            "SHAP 分析需要更多计算资源，在模型训练时自动生成。\n\n"
            f"当前 {sport.upper()} 尚未生成 SHAP 数据。"
        )


def _render_drift(sport):
    st.subheader("特征漂移检测")

    baseline_files = list(SHAP_DIR.glob(f"**/*{sport}*baseline*feature_importance*"))
    if baseline_files:
        baseline = pd.read_csv(baseline_files[0])
        current, _ = _load_feature_importance(sport)

        if current is not None and not current.empty:
            base_features = set(baseline["feature"])
            curr_features = set(current["feature"])

            new_feats = curr_features - base_features
            missing_feats = base_features - curr_features

            if new_feats:
                st.warning(f"**新增 {len(new_feats)} 个特征**: {', '.join(list(new_feats)[:10])}")
            if missing_feats:
                st.warning(f"**缺失 {len(missing_feats)} 个特征**: {', '.join(list(missing_feats)[:10])}")

            if not new_feats and not missing_feats:
                st.success("✅ 特征集未变化")

            merged = baseline[["feature", "importance_pct"]].rename(
                columns={"importance_pct": "baseline"})
            merged = merged.merge(
                current[["feature", "importance_pct"]].rename(
                    columns={"importance_pct": "current"}),
                on="feature", how="inner",
            )
            merged["change"] = abs(merged["current"] - merged["baseline"])
            merged = merged.sort_values("change", ascending=False)

            st.dataframe(
                merged.head(20).style.format(
                    {"baseline": "{:.2%}", "current": "{:.2%}", "change": "{:.2%}"}
                ),
                hide_index=True, use_container_width=True,
            )
    else:
        st.info("需要先运行两轮模型训练（产生基线和当前特征重要性），才能进行漂移检测。")
