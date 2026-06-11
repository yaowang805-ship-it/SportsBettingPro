"""CLV 分析页面 — 收盘价偏差追踪与图表。"""
import altair as alt
import pandas as pd
import streamlit as st

from src.dashboard.components.data_loader import (
    load_json, load_csv, render_empty_state,
)
from src.dashboard.config import CLV_REPORT_FILE, PRED_LOG_FILE


def render():
    st.header("🎯 CLV 收盘价分析")

    report = load_json(CLV_REPORT_FILE)
    has_report = report and report.get("total_settled", 0) > 0

    # 从预测日志计算 CLV（兼容两种来源）
    pred_df = load_csv(PRED_LOG_FILE)
    clv_df = _build_clv_df(pred_df)

    if clv_df.empty and not has_report:
        render_empty_state("暂无CLV数据", "运行 `python src/monitor/clv_tracker.py` 生成CLV报告。")
        return

    # ── KPI 行 ──
    kpi_data = report if has_report else _kpi_from_df(clv_df)
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("已结算", kpi_data.get("total_settled", len(clv_df)))
    col2.metric("有CLV记录", kpi_data.get("with_clv", clv_df["clv"].notna().sum()))
    avg_clv = kpi_data.get("avg_clv", clv_df["clv"].mean() if not clv_df.empty else 0)
    col3.metric("平均CLV", f"{avg_clv:+.2%}", delta=avg_clv)
    pct_pos = kpi_data.get("positive_pct", (clv_df["clv"] > 0).mean() if not clv_df.empty else 0)
    col4.metric("正CLV率", f"{pct_pos:.1%}")
    clv_std = kpi_data.get("clv_std", clv_df["clv"].std() if not clv_df.empty else 0)
    col5.metric("CLV标准差", f"{clv_std:.2%}")

    # ── CLV 分布直方图 ──
    if not clv_df.empty:
        st.subheader("CLV 分布")
        n_bins = max(10, min(30, len(clv_df) // 3))
        mu = clv_df["clv"].mean()
        hist = (
            alt.Chart(clv_df)
            .mark_bar(opacity=0.7, color="steelblue", cornerRadius=2)
            .encode(
                x=alt.X("clv:Q", bin=alt.Bin(maxbins=n_bins), title="CLV"),
                y=alt.Y("count()", title="频次"),
            )
            .properties(height=200)
        )
        rule = (
            alt.Chart(pd.DataFrame({"mu": [mu]}))
            .mark_rule(color="red", strokeDash=[5, 5])
            .encode(x="mu:Q")
        )
        st.altair_chart(hist + rule, use_container_width=True)

        # ── CLV 时间趋势 ──
        if "settled_at" in clv_df.columns:
            st.subheader("CLV 时间趋势")
            trend = (
                clv_df.dropna(subset=["settled_at", "clv"])
                .sort_values("settled_at")
                .assign(
                    settled_at=lambda d: pd.to_datetime(d["settled_at"]),
                    cum_avg_clv=lambda d: d["clv"].expanding().mean(),
                )
            )
            if len(trend) >= 3:
                base = (
                    alt.Chart(trend)
                    .mark_line(point=True, color="steelblue")
                    .encode(
                        x=alt.X("settled_at:T", title="结算时间"),
                        y=alt.Y("clv:Q", title="CLV"),
                    )
                )
                avg_line = (
                    alt.Chart(trend)
                    .mark_line(color="red", strokeDash=[5, 5], opacity=0.6)
                    .encode(
                        x=alt.X("settled_at:T"),
                        y=alt.Y("cum_avg_clv:Q", title="累积平均CLV"),
                    )
                )
                st.altair_chart(base + avg_line, use_container_width=True)

    # ── 按联赛 CLV ──
    by_league = report.get("avg_clv_by_league", {}) if has_report else (
        clv_df.groupby("league")["clv"].agg(["mean", "count", "std"]).to_dict("index")
        if "league" in clv_df.columns and not clv_df.empty else {}
    )
    if by_league and isinstance(by_league, dict):
        st.subheader("按联赛 CLV")
        league_items = [{"league": k, "avg_clv": v if isinstance(v, (int, float)) else v.get("mean", 0),
                         "count": 1 if isinstance(v, (int, float)) else int(v.get("count", 0))}
                        for k, v in by_league.items()]
        league_df = pd.DataFrame(league_items).sort_values("avg_clv")
        chart = (
            alt.Chart(league_df)
            .mark_bar(cornerRadius=2)
            .encode(
                x=alt.X("avg_clv:Q", title="平均CLV"),
                y=alt.Y("league:N", sort="-x", title=""),
                color=alt.condition(
                    alt.datum.avg_clv > 0, alt.value("#00BFA5"), alt.value("#FF5252")
                ),
                tooltip=["league", "avg_clv", "count"],
            )
            .properties(height=max(150, len(league_df) * 25))
        )
        st.altair_chart(chart, use_container_width=True)

    # ── 按市场 CLV ──
    by_market = report.get("avg_clv_by_market", {}) if has_report else (
        clv_df.groupby("market_type")["clv"].mean().to_dict()
        if "market_type" in clv_df.columns and not clv_df.empty else {}
    )
    if by_market:
        st.subheader("按市场 CLV")
        market_df = pd.DataFrame([
            {"market": k, "avg_clv": v} for k, v in by_market.items()
        ]).sort_values("avg_clv")
        chart = (
            alt.Chart(market_df)
            .mark_bar(cornerRadius=2)
            .encode(
                x=alt.X("avg_clv:Q", title="平均CLV"),
                y=alt.Y("market:N", sort="-x", title=""),
                color=alt.condition(
                    alt.datum.avg_clv > 0, alt.value("#00BFA5"), alt.value("#FF5252")
                ),
            )
            .properties(height=150)
        )
        st.altair_chart(chart, use_container_width=True)

    # ── 按运动项目 CLV ──
    if "sport" in clv_df.columns and not clv_df.empty:
        st.subheader("按运动项目 CLV")
        sport_stats = clv_df.groupby("sport").agg(
            样本量=("clv", "count"),
            平均CLV=("clv", "mean"),
            正CLV率=("clv", lambda x: (x > 0).mean()),
            CLV标准差=("clv", "std"),
        ).round(4)
        sport_stats["平均CLV"] = sport_stats["平均CLV"].map("{:+.2%}".format)
        sport_stats["正CLV率"] = sport_stats["正CLV率"].map("{:.1%}".format)
        sport_stats["CLV标准差"] = sport_stats["CLV标准差"].map("{:.2%}".format)
        st.dataframe(sport_stats, use_container_width=True)

    # ── CLV 最佳/最差记录 ──
    if not clv_df.empty and len(clv_df) >= 3:
        st.subheader("CLV 极端值")
        col_a, col_b = st.columns(2)
        best = clv_df.nlargest(3, "clv")[["sport", "league", "home_team", "away_team", "clv"]] \
            if all(c in clv_df.columns for c in ["sport", "league", "home_team", "clv"]) else clv_df.nlargest(3, "clv")
        worst = clv_df.nsmallest(3, "clv")[["sport", "league", "home_team", "away_team", "clv"]] \
            if all(c in clv_df.columns for c in ["sport", "league", "home_team", "clv"]) else clv_df.nsmallest(3, "clv")
        col_a.dataframe(best.assign(clv=best["clv"].map("{:+.2%}".format)), hide_index=True, use_container_width=True)
        col_b.dataframe(worst.assign(clv=worst["clv"].map("{:+.2%}".format)), hide_index=True, use_container_width=True)


def _build_clv_df(pred_df: pd.DataFrame) -> pd.DataFrame:
    """从预测日志提取 CLV 数据。"""
    if pred_df.empty:
        return pd.DataFrame()
    if not {"odds", "result_odds"}.issubset(pred_df.columns):
        return pd.DataFrame()
    valid = pred_df[pred_df["result_odds"].notna() & (pred_df["result_odds"] > 0)].copy()
    if valid.empty:
        return pd.DataFrame()
    valid["clv"] = (valid["odds"] - valid["result_odds"]) / valid["result_odds"]
    return valid


def _kpi_from_df(df: pd.DataFrame) -> dict:
    """从 DataFrame 计算 KPI。"""
    clv = df["clv"]
    return {
        "total_settled": len(df),
        "with_clv": clv.notna().sum(),
        "avg_clv": clv.mean(),
        "clv_std": clv.std(),
        "positive_pct": (clv > 0).mean(),
    }
