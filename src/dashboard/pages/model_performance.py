"""模型表现页面 — 准确率趋势、各模型对比、校准曲线、衰减检测。"""
import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import numpy as np

from src.dashboard.components.data_loader import load_json, load_csv, load_recommendations, render_empty_state
from src.dashboard.config import (
    MODEL_ACCURACY_FILE, BACKTEST_FILE, CALIBRATION_FILE,
    DECAY_REPORT_FILE, MODEL_DIR, DATA_DIR,
)


def _load_model_metas():
    """加载所有模型 meta 文件中的性能指标。"""
    results = []
    for f in sorted(MODEL_DIR.glob("model_*_ensemble_meta.json")):
        try:
            meta = json.loads(f.read_text())
            prefix = f.stem.replace("_ensemble_meta", "")
            parts = prefix.split("_")
            sport = "NBA" if "bb" in prefix else "足球"
            target = meta.get("target", parts[-1] if len(parts) > 1 else "unknown")

            metrics = meta.get("metrics", {})
            weights = meta.get("ensemble_weights", {})
            ot = meta.get("optimal_threshold", {})
            n_samples = meta.get("n_samples", 0)
            n_features = meta.get("n_features", 0)
            base_models = meta.get("base_models", [])

            results.append({
                "运动": sport,
                "目标": {"win": "主胜", "spread_result": "让分", "total_result": "大小球"}.get(target, target),
                "Brier": metrics.get("brier", 0),
                "LogLoss": metrics.get("log_loss", 0),
                "最优阈值": ot.get("threshold", 0.5),
                "测试准确率": ot.get("test_accuracy", 0),
                "样本量": n_samples,
                "特征数": n_features,
                "基模型": ", ".join(base_models),
                "权重": ", ".join(f"{k}:{v:.2f}" for k, v in sorted(weights.items())),
            })
        except Exception:
            pass
    return pd.DataFrame(results)


def _load_calibration_data():
    """加载所有可用的校准数据集。"""
    cal_path = CALIBRATION_FILE
    if cal_path.exists():
        return pd.read_csv(cal_path)
    return pd.DataFrame()


def render():
    st.header("🧠 模型表现")

    model_df = _load_model_metas()
    acc_df = load_csv(MODEL_ACCURACY_FILE)
    decay_report = load_json(DECAY_REPORT_FILE)
    bt = load_json(BACKTEST_FILE)

    # ── KPI 行 ──
    st.subheader("模型概要")
    col1, col2, col3, col4 = st.columns(4)

    n_models = len(model_df)
    col1.metric("已训练模型", str(n_models))

    if not model_df.empty and "测试准确率" in model_df.columns:
        avg_acc = model_df["测试准确率"].mean()
        col2.metric("平均准确率", f"{avg_acc:.1%}")

        passing = (model_df["测试准确率"] >= 0.52).sum()
        col3.metric("通过阈值 (≥52%)", f"{passing}/{n_models}")

    if not model_df.empty and "Brier" in model_df.columns:
        avg_brier = model_df["Brier"].mean()
        col4.metric("平均 Brier", f"{avg_brier:.4f}")

    # ── 各模型性能表 ──
    if not model_df.empty:
        st.subheader("各目标模型性能")
        display_cols = ["运动", "目标", "测试准确率", "最优阈值", "Brier", "LogLoss", "样本量", "特征数"]
        display_df = model_df[display_cols].copy()
        display_df["测试准确率"] = display_df["测试准确率"].apply(lambda x: f"{x:.1%}")
        display_df["Brier"] = display_df["Brier"].apply(lambda x: f"{x:.4f}")
        display_df["LogLoss"] = display_df["LogLoss"].apply(lambda x: f"{x:.4f}")

        # 颜色标注
        def _color_row(row):
            try:
                acc = float(row["测试准确率"].strip("%")) / 100
                if acc >= 0.65:
                    return ["background-color: #1a3a2a"] * len(row)
                elif acc >= 0.52:
                    return ["background-color: #1a2a3a"] * len(row)
                else:
                    return ["background-color: #3a1a1a"] * len(row)
            except Exception:
                return [""] * len(row)

        st.dataframe(
            display_df.style.apply(_color_row, axis=1),
            use_container_width=True, hide_index=True,
        )

    # ── 准确率趋势 ──
    st.divider()
    st.subheader("准确率趋势（滚动窗口）")
    if not acc_df.empty and "date" in acc_df.columns:
        acc_df["date"] = pd.to_datetime(acc_df["date"], errors="coerce")
        acc_df = acc_df.dropna(subset=["date"]).sort_values("date")

        # 选择要显示的列
        roll_cols = [c for c in ["rolling_7d", "rolling_14d", "rolling_30d"] if c in acc_df.columns]
        if roll_cols:
            chart_data = acc_df[["date"] + roll_cols].melt(
                id_vars=["date"], var_name="窗口", value_name="准确率"
            )
            chart_data["窗口"] = chart_data["窗口"].map({
                "rolling_7d": "7天", "rolling_14d": "14天", "rolling_30d": "30天",
            })

            chart = (
                alt.Chart(chart_data.dropna())
                .mark_line(strokeWidth=2, point=False)
                .encode(
                    x=alt.X("date:T", title="日期"),
                    y=alt.Y("准确率:Q", title="准确率", scale=alt.Scale(domain=[0, 1])),
                    color=alt.Color("窗口:N", scale=alt.Scale(
                        scheme="category10",
                    )),
                    tooltip=["date:T", "准确率:Q"],
                )
                .properties(height=300)
            )
            ref = pd.DataFrame({"y": [0.52]})
            ref_line = (
                alt.Chart(ref)
                .mark_rule(color="red", strokeDash=[5, 5], opacity=0.4)
                .encode(y="y:Q")
            )
            st.altair_chart(chart + ref_line, use_container_width=True)
            st.caption("红色虚线: 52% 可靠阈值。低于此线的模型不生成推荐。")
    else:
        render_empty_state("暂无准确率历史", "每日流水线运行后会持续收集。")

    # ── 模型衰减检测 ──
    st.divider()
    st.subheader("模型衰减检测")
    if decay_report:
        col1, col2, col3 = st.columns(3)
        col1.metric("基线准确率", f"{decay_report.get('baseline', 0):.1%}")
        col2.metric("14天滚动", f"{decay_report.get('rolling_14d', 0):.1%}"
                     if decay_report.get('rolling_14d') else "N/A")
        col3.metric("总样本", str(decay_report.get('n_total', 0)))

        if decay_report.get("is_decaying"):
            st.error("⚠️ 检测到模型衰减 — 建议重新训练！")
        else:
            st.success("✅ 模型性能正常，无衰减信号")

        # 按运动分解
        by_sport = decay_report.get("by_sport", {})
        if by_sport:
            sport_rows = []
            for sport, data in by_sport.items():
                sport_rows.append({
                    "运动": sport,
                    "样本量": data.get("n", 0),
                    "胜场": data.get("wins", 0),
                    "胜率": f"{data.get('wins', 0) / max(data.get('n', 1), 1):.1%}",
                })
            st.dataframe(pd.DataFrame(sport_rows), use_container_width=True, hide_index=True)
    else:
        st.info("暂无衰减检测数据 — 有足够结算样本后自动生成。")

    # ── 回测摘要 ──
    st.divider()
    st.subheader("回测摘要")
    if bt:
        report = bt.get("report", [])
        if report:
            rows = []
            for r in report:
                rows.append({
                    "模型": r.get("model", "?"),
                    "总收益": f"{r.get('total_return', 0):+.1%}",
                    "胜率": f"{r.get('win_rate', 0):.1%}",
                    "Sharpe": f"{r.get('sharpe', 0):.2f}",
                    "最大回撤": f"{r.get('max_drawdown', 0):.1%}",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        backtest_date = bt.get("generated_at", bt.get("date", ""))
        if backtest_date:
            st.caption(f"回测时间: {backtest_date[:19]}")
    else:
        st.info("暂无回测数据。")
