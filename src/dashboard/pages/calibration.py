#!/usr/bin/env python3
"""校准可靠性图页面 — 模型概率校准质量可视化。"""
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import numpy as np

from src.core.calibration import calibration_curve, brier_score
from src.dashboard.components.data_loader import render_empty_state

_LOG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "storage" / "prediction_log.csv"
_MODEL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "models"


def _load_settled() -> pd.DataFrame:
    if not _LOG_PATH.exists():
        return pd.DataFrame()
    df = pd.read_csv(_LOG_PATH)
    df = df[df["status"].isin(["won", "lost"])].copy()
    if df.empty:
        return df
    df["y_true"] = (df["status"] == "won").astype(int)
    df["y_prob"] = pd.to_numeric(df["model_prob"], errors="coerce")
    df = df.dropna(subset=["y_prob", "y_true"])
    return df


def _ece(y_true, y_prob, bins=10):
    """Expected Calibration Error."""
    centers, fracs = calibration_curve(y_true, y_prob, bins)
    edges = np.linspace(0, 1, bins + 1)
    weights = np.zeros(bins)
    for i in range(bins):
        mask = (y_prob >= edges[i]) & (y_prob < edges[i + 1])
        weights[i] = mask.sum()
    valid = ~np.isnan(fracs)
    if valid.sum() == 0:
        return 0.0
    return float(np.average(np.abs(fracs[valid] - centers[valid]), weights=weights[valid]))


def _mce(y_true, y_prob, bins=10):
    """Maximum Calibration Error."""
    centers, fracs = calibration_curve(y_true, y_prob, bins)
    valid = ~np.isnan(fracs)
    if valid.sum() == 0:
        return 0.0
    return float(np.max(np.abs(fracs[valid] - centers[valid])))


def render():
    st.header("🎯 校准可靠性分析")

    df = _load_settled()
    if df.empty:
        render_empty_state("暂无已结算预测记录。系统将在产生投注记录后自动生成校准报告。")
        return

    sport_filter = st.multiselect("运动项目", options=df["sport"].unique(), default=df["sport"].unique())
    if sport_filter:
        df = df[df["sport"].isin(sport_filter)]

    if df.empty:
        render_empty_state("筛选后无已结算记录。")
        return

    y_true = df["y_true"].values
    y_prob = df["y_prob"].values
    bs = brier_score(y_true, y_prob)
    ece_val = _ece(y_true, y_prob)
    mce_val = _mce(y_true, y_prob)

    # ── 指标卡片 ──
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("样本量", len(df))
    col2.metric("Brier 分数", f"{bs:.4f}", help="0=完美, 0.25=随机")
    col3.metric("ECE", f"{ece_val:.4f}", help="Expected Calibration Error（越低越好）")
    col4.metric("MCE", f"{mce_val:.4f}", help="Maximum Calibration Error")

    # ── 可靠性图 ──
    centers, fracs = calibration_curve(y_true, y_prob)
    cal_df = pd.DataFrame({
        "预测概率": centers,
        "实际胜率": fracs,
        "完美校准": centers,
    }).dropna()

    perfect_line = pd.DataFrame({"预测概率": np.linspace(0, 1, 100), "实际胜率": np.linspace(0, 1, 100)})

    reliability_chart = (
        alt.Chart(cal_df)
        .mark_bar(opacity=0.7, cornerRadius=2)
        .encode(
            x=alt.X("预测概率:Q", scale=alt.Scale(domain=(0, 1)), title="预测概率", axis=alt.Axis(format="%")),
            y=alt.Y("实际胜率:Q", scale=alt.Scale(domain=(0, 1)), title="实际胜率", axis=alt.Axis(format="%")),
        )
    )

    perfect = (
        alt.Chart(perfect_line)
        .mark_line(color="red", strokeDash=[5, 5], opacity=0.6)
        .encode(x="预测概率:Q", y="实际胜率:Q")
    )

    error_bars = (
        alt.Chart(cal_df)
        .mark_errorbar(color="gray", opacity=0.4)
        .encode(
            x=alt.X("预测概率:Q"),
            y=alt.Y("lower:Q", title="实际胜率"),
            y2=alt.Y2("upper:Q"),
        )
    )

    st.altair_chart(reliability_chart + perfect, use_container_width=True)

    # ── 概率分布直方图 ──
    hist_df = pd.DataFrame({"预测概率": y_prob})
    hist = (
        alt.Chart(hist_df)
        .mark_bar(opacity=0.6, color="steelblue")
        .encode(
            x=alt.X("预测概率:Q", bin=alt.Bin(maxbins=20), title="预测概率"),
            y=alt.Y("count():Q", title="频次"),
        )
    )
    st.altair_chart(hist, use_container_width=True)

    # ── 按运动/联赛细分 ──
    st.subheader("按运动项目细分")
    sport_stats = []
    for sport, grp in df.groupby("sport"):
        n = len(grp)
        if n < 5:
            continue
        gt = grp["y_true"].values
        gp = grp["y_prob"].values
        sport_stats.append({
            "运动": sport,
            "样本量": n,
            "Brier": round(brier_score(gt, gp), 4),
            "ECE": round(_ece(gt, gp), 4),
            "胜率": f"{gt.mean():.1%}",
            "平均概率": f"{gp.mean():.1%}",
        })
    if sport_stats:
        st.dataframe(pd.DataFrame(sport_stats).sort_values("Brier"), hide_index=True, use_container_width=True)

    # ── 原始数据 ──
    with st.expander("📄 原始结算记录"):
        show_cols = ["sport", "league", "market_type", "model_prob", "status", "odds", "ev", "home_team", "away_team"]
        show_cols = [c for c in show_cols if c in df.columns]
        st.dataframe(df[show_cols].sort_values("date") if "date" in df.columns else df[show_cols],
                     hide_index=True, use_container_width=True)
