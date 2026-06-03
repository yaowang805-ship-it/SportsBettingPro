"""CLV 分析页面 — 收盘价偏差追踪与图表。"""
import altair as alt
import pandas as pd
import streamlit as st

from src.dashboard.components.data_loader import (
    load_json, load_csv, data_exists, render_empty_state,
)
from src.dashboard.config import CLV_REPORT_FILE, PRED_LOG_FILE


def render():
    st.header("🎯 CLV 收盘价分析")

    report = load_json(CLV_REPORT_FILE)

    if not report or report.get("total_settled", 0) == 0:
        # 降级：从预测日志计算
        pred_df = load_csv(PRED_LOG_FILE)
        if pred_df.empty:
            render_empty_state("暂无CLV数据", "运行 `python src/monitor/clv_tracker.py` 生成CLV报告。")
            return

        if {"odds", "result_odds"}.issubset(pred_df.columns):
            valid = pred_df[pred_df["result_odds"].notna() & (pred_df["result_odds"] > 0)].copy()
            if not valid.empty:
                valid["clv"] = (valid["result_odds"] - valid["odds"]) / valid["odds"]
                _render_from_df(valid)
                return
        render_empty_state("暂无CLV数据", "先运行 `python src/monitor/clv_tracker.py`。")
        return

    # ── KPI 行 ──
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("已结算投注", report.get("total_settled", 0))
    col2.metric("有CLV记录", report.get("with_clv", 0))
    col3.metric("正CLV", report.get("positive_clv", 0))
    avg_clv = report.get("avg_clv", 0)
    col4.metric("平均CLV", f"{avg_clv:+.2%}", delta=avg_clv)

    # ── 按联赛 CLV ──
    by_league = report.get("avg_clv_by_league", {})
    if by_league:
        st.subheader("按联赛 CLV")
        league_df = pd.DataFrame([
            {"league": k, "avg_clv": v} for k, v in by_league.items()
        ])
        chart = (
            alt.Chart(league_df)
            .mark_bar()
            .encode(
                x=alt.X("league:N", title="联赛"),
                y=alt.Y("avg_clv:Q", title="平均CLV"),
                color=alt.condition(
                    alt.datum.avg_clv > 0, alt.value("#00BFA5"), alt.value("#FF5252")
                ),
            )
            .properties(height=250)
        )
        st.altair_chart(chart, use_container_width=True)

    # ── 按市场 CLV ──
    by_market = report.get("avg_clv_by_market", {})
    if by_market:
        st.subheader("按市场 CLV")
        market_df = pd.DataFrame([
            {"market": k, "avg_clv": v} for k, v in by_market.items()
        ])
        chart = (
            alt.Chart(market_df)
            .mark_bar()
            .encode(
                x=alt.X("market:N", title="市场"),
                y=alt.Y("avg_clv:Q", title="平均CLV"),
                color=alt.condition(
                    alt.datum.avg_clv > 0, alt.value("#00BFA5"), alt.value("#FF5252")
                ),
            )
            .properties(height=200)
        )
        st.altair_chart(chart, use_container_width=True)


def _render_from_df(df: pd.DataFrame):
    """从包含 clv 列的 DataFrame 渲染 CLV 图表。"""
    avg_clv = df["clv"].mean()
    st.metric("平均CLV", f"{avg_clv:+.2%}")

    # 直方图
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X("clv:Q", bin=alt.Bin(maxbins=20), title="CLV"),
            y=alt.Y("count()", title="次数"),
        )
        .properties(height=250)
    )
    st.altair_chart(chart, use_container_width=True)
