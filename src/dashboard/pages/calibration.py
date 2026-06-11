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


def _calibration_stats(grp: pd.DataFrame) -> dict:
    """计算单组的校准统计。"""
    n = len(grp)
    if n < 5:
        return {"样本量": n, "Brier": None, "ECE": None, "胜率": None, "平均概率": None}
    gt = grp["y_true"].values
    gp = grp["y_prob"].values
    return {
        "样本量": n,
        "Brier": round(brier_score(gt, gp), 4),
        "ECE": round(_ece(gt, gp), 4),
        "胜率": f"{gt.mean():.1%}",
        "平均概率": f"{gp.mean():.1%}",
    }


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

    # 偏差 = 实际 - 预测
    cal_df["偏差"] = cal_df["实际胜率"] - cal_df["预测概率"]

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

    st.altair_chart(reliability_chart + perfect, use_container_width=True)

    # ── 校准偏差柱状图 ──
    st.subheader("校准偏差（实际 - 预测）")
    bias_chart = (
        alt.Chart(cal_df)
        .mark_bar(cornerRadius=2)
        .encode(
            x=alt.X("预测概率:Q", scale=alt.Scale(domain=(0, 1)), title="预测概率桶", axis=alt.Axis(format="%")),
            y=alt.Y("偏差:Q", title="偏差 (实际-预测)"),
            color=alt.condition(
                alt.datum.偏差 > 0, alt.value("#00BFA5"), alt.value("#FF5252")
            ),
        )
        .properties(height=200)
    )
    st.altair_chart(bias_chart, use_container_width=True)

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

    # ── 按运动项目细分 ──
    st.subheader("按运动项目细分")
    sport_rows = []
    for sport, grp in df.groupby("sport"):
        stats = _calibration_stats(grp)
        if stats["Brier"] is not None:
            stats["运动"] = sport
            sport_rows.append(stats)
    if sport_rows:
        st.dataframe(pd.DataFrame(sport_rows).sort_values("Brier"), hide_index=True, use_container_width=True)

    # ── 按联赛细分 ──
    if "league" in df.columns:
        st.subheader("按联赛细分")
        # Filter to leagues with enough data
        league_rows = []
        for league, grp in df.groupby("league"):
            stats = _calibration_stats(grp)
            if stats["Brier"] is not None:
                stats["联赛"] = league
                league_rows.append(stats)
        if league_rows:
            league_df = pd.DataFrame(league_rows).sort_values("Brier")
            st.dataframe(league_df, hide_index=True, use_container_width=True)

            # 联赛 Brier 柱状图
            chart = (
                alt.Chart(league_df)
                .mark_bar(cornerRadius=2)
                .encode(
                    x=alt.X("Brier:Q", title="Brier分数"),
                    y=alt.Y("联赛:N", sort="-x", title=""),
                    tooltip=["联赛", "样本量", "Brier", "ECE"],
                )
                .properties(height=max(120, len(league_df) * 25))
            )
            st.altair_chart(chart, use_container_width=True)

    # ── 按市场类型细分 ──
    if "market_type" in df.columns:
        st.subheader("按市场类型细分")
        market_rows = []
        for market, grp in df.groupby("market_type"):
            stats = _calibration_stats(grp)
            if stats["Brier"] is not None:
                stats["市场类型"] = market
                market_rows.append(stats)
        if market_rows:
            st.dataframe(pd.DataFrame(market_rows).sort_values("Brier"), hide_index=True, use_container_width=True)

    # ── 校准漂移（按时间） ──
    if "settled_at" in df.columns:
        st.subheader("校准漂移（时间趋势）")
        time_df = df.dropna(subset=["settled_at"]).copy()
        time_df["settled_at"] = pd.to_datetime(time_df["settled_at"])
        time_df["_week"] = time_df["settled_at"].dt.isocalendar().week.astype(str) + "周"
        time_df["_month"] = time_df["settled_at"].dt.to_period("M").astype(str)

        drift_rows = []
        for period, grp in time_df.groupby("_month"):
            if len(grp) < 10:
                continue
            gt = grp["y_true"].values
            gp = grp["y_prob"].values
            drift_rows.append({
                "月份": period,
                "样本量": len(grp),
                "Brier": round(brier_score(gt, gp), 4),
                "ECE": round(_ece(gt, gp), 4),
                "胜率": f"{gt.mean():.1%}",
            })
        if len(drift_rows) >= 2:
            drift_df = pd.DataFrame(drift_rows).sort_values("月份")
            drift_chart = (
                alt.Chart(drift_df)
                .mark_line(point=True)
                .encode(
                    x=alt.X("月份:N", title="月份"),
                    y=alt.Y("Brier:Q", title="Brier分数", scale=alt.Scale(zero=False)),
                    tooltip=["月份", "样本量", "Brier", "ECE"],
                )
                .properties(height=200)
            )
            st.altair_chart(drift_chart, use_container_width=True)

    # ── 按运动×市场交叉校准 ──
    if "sport" in df.columns and "market_type" in df.columns:
        st.subheader("交叉校准： 运动 × 市场")
        cross_rows = []
        for (sport, market), grp in df.groupby(["sport", "market_type"]):
            stats = _calibration_stats(grp)
            if stats["Brier"] is not None:
                stats["运动"] = sport
                stats["市场"] = market
                cross_rows.append(stats)
        if cross_rows:
            cross_df = pd.DataFrame(cross_rows).sort_values("Brier")
            st.dataframe(cross_df, hide_index=True, use_container_width=True)

    # ── 原始数据 ──
    with st.expander("📄 原始结算记录"):
        show_cols = ["sport", "league", "market_type", "model_prob", "status", "odds",
                     "home_team", "away_team", "settled_at"]
        show_cols = [c for c in show_cols if c in df.columns]
        st.dataframe(df[show_cols].sort_values("settled_at") if "settled_at" in df.columns else df[show_cols],
                     hide_index=True, use_container_width=True)
