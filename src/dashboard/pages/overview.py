"""总览页面 — 权益曲线、KPI指标、按项目胜率。"""
import altair as alt
import pandas as pd
import streamlit as st

from src.dashboard.components.data_loader import (
    load_csv, load_json, data_exists, render_empty_state,
)
from src.dashboard.components.team_cn import sport_cn
from src.dashboard.config import PERF_FILE, PRED_LOG_FILE, SYSTEM_HEALTH_FILE, RISK_STATE_FILE


def render():
    st.header("📊 总览")

    # ── KPI 指标 ──
    health = load_json(SYSTEM_HEALTH_FILE)
    risk_state = load_json(RISK_STATE_FILE)
    perf_df = load_csv(PERF_FILE)
    pred_df = load_csv(PRED_LOG_FILE)

    col1, col2, col3, col4, col5 = st.columns(5)

    # 资金
    balance = risk_state.get("current_balance", health.get("balance", 0))
    col1.metric("💰 当前资金", f"¥{balance:.0f}")

    # ROI
    perf_health = health.get("performance_health", {})
    roi = perf_health.get("roi", risk_state.get("roi", 0))
    col2.metric("📈 ROI", f"{roi:+.2%}" if isinstance(roi, float) else "N/A")

    # 胜率
    win_rate = perf_health.get("win_rate", risk_state.get("win_rate", 0))
    col3.metric("🎯 胜率", f"{win_rate:.1%}" if isinstance(win_rate, float) else "N/A")

    # 最大回撤
    dd = perf_health.get("max_drawdown", risk_state.get("drawdown", 0))
    col4.metric("📉 最大回撤", f"{dd:.1%}" if isinstance(dd, float) else "N/A")

    # 待结算
    if not pred_df.empty and "status" in pred_df.columns:
        open_bets = len(pred_df[pred_df["status"] == "pending"])
    else:
        open_bets = 0
    col5.metric("⏳ 待结算", str(open_bets))

    # ── 权益曲线 ──
    st.subheader("权益曲线")
    if not perf_df.empty and "cumulative_balance" in perf_df.columns:
        chart_data = perf_df[["date", "cumulative_balance"]].copy()
        chart_data["date"] = pd.to_datetime(chart_data["date"])
        chart = (
            alt.Chart(chart_data)
            .mark_line(color="#00BFA5", strokeWidth=2)
            .encode(
                x=alt.X("date:T", title="日期"),
                y=alt.Y("cumulative_balance:Q", title="资金 (¥)"),
                tooltip=["date:T", "cumulative_balance:Q"],
            )
            .properties(height=350)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        render_empty_state("暂无权益数据", "完成首次投注并结算后将显示权益曲线。")

    # ── 双列布局：按项目胜率 + 近期投注 ──
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("按项目胜率")
        if not pred_df.empty and {"sport", "status"}.issubset(pred_df.columns):
            settled = pred_df[pred_df["status"].isin(["won", "lost"])].copy()
            if not settled.empty:
                settled["sport_cn"] = settled["sport"].map(sport_cn)
                sport_stats = (
                    settled.groupby("sport_cn")
                    .agg(total=("status", "count"), won=("status", lambda x: (x == "won").sum()))
                    .reset_index()
                )
                sport_stats["win_rate"] = sport_stats["won"] / sport_stats["total"]
                chart = (
                    alt.Chart(sport_stats)
                    .mark_bar(color="#5B8FF9")
                    .encode(
                        x=alt.X("sport_cn:N", title="项目"),
                        y=alt.Y("win_rate:Q", title="胜率", scale=alt.Scale(domain=[0, 1])),
                        tooltip=["sport_cn:N", "total:Q", "won:Q", "win_rate:Q"],
                    )
                    .properties(height=250)
                )
                st.altair_chart(chart, use_container_width=True)
            else:
                st.caption("暂无已结算投注。")
        else:
            render_empty_state("暂无数据", "运行预测流水线后显示。")

    with col_right:
        st.subheader("近期投注")
        if not perf_df.empty:
            recent = perf_df.tail(10)[["date", "game", "result", "profit"]].copy()
            recent["date"] = pd.to_datetime(recent["date"]).dt.strftime("%m-%d")
            recent["profit"] = recent["profit"].fillna(0)
            recent = recent.rename(columns={"game": "比赛", "result": "结果", "profit": "利润"})
            st.dataframe(recent, use_container_width=True, hide_index=True)
        else:
            render_empty_state("暂无记录", "投注结算后将显示近期记录。")
