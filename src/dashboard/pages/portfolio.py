"""投资组合页面 — P&L分解、投注分布、CLV分析、权益曲线。"""
import json
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import numpy as np

from src.dashboard.components.data_loader import load_json, load_csv, data_exists, render_empty_state
from src.dashboard.config import PORTFOLIO_FILE, BET_HISTORY_FILE, EDGE_ATTRIBUTION_FILE, TEAM_EDGE_FILE


def render():
    st.header("💰 投资组合")

    portfolio_state = load_json(PORTFOLIO_FILE)
    bet_history = load_csv(BET_HISTORY_FILE)
    edge_attr = load_json(EDGE_ATTRIBUTION_FILE)

    if not portfolio_state:
        render_empty_state("暂无组合数据", "运行每日流水线生成投注记录。")
        return

    balance = portfolio_state.get("balance", 10000)
    settled = portfolio_state.get("settled", {})
    pending_bets = portfolio_state.get("pending_bets", [])
    history = portfolio_state.get("history", [])

    n_settled = len(settled)
    n_won = sum(1 for v in settled.values() if v == "won")
    n_pending = len(pending_bets)
    win_rate = n_won / n_settled if n_settled > 0 else 0

    # 从 bet_history.csv 算盈亏
    if not bet_history.empty and "profit" in bet_history.columns:
        total_profit = bet_history["profit"].fillna(0).sum()
        total_stake = bet_history["stake"].fillna(0).sum()
        roi = total_profit / total_stake if total_stake > 0 else 0
    else:
        total_profit = 0
        roi = 0

    # ── KPI ──
    st.subheader("组合概览")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("💰 余额", f"¥{balance:,.0f}")
    col2.metric("📈 总盈亏", f"¥{total_profit:+,.0f}", delta=total_profit)
    col3.metric("🎯 胜率", f"{win_rate:.1%}" if n_settled > 0 else "N/A")
    col4.metric("📊 ROI", f"{roi:+.2%}" if roi != 0 else "0.00%")
    col5.metric("🏆 已结算", str(n_settled))
    col6.metric("⏳ 待结算", str(n_pending))

    # ── 双列: 权益曲线 + 盈亏分布 ──
    left, right = st.columns([3, 2])

    with left:
        st.subheader("权益曲线")

        # 用 history + bet_history 构建曲线
        equity_points = []

        # 从 portfolio history 中取
        initial_balance = 10000
        running = initial_balance
        equity_points.append({"date": "起始", "balance": running})

        hist_records = portfolio_state.get("history", [])
        for h in hist_records:
            profit = h.get("profit", 0)
            running += profit
            equity_points.append({
                "date": h.get("date", datetime.now().isoformat()),
                "balance": running,
            })

        if len(equity_points) > 1:
            eq_df = pd.DataFrame(equity_points)
            eq_df["date"] = pd.to_datetime(eq_df["date"], errors="coerce")

            chart = (
                alt.Chart(eq_df)
                .mark_area(
                    color=alt.Gradient(
                        gradient="linear",
                        stops=[
                            alt.GradientStop(color="#00BFA5", offset=0),
                            alt.GradientStop(color="#00BFA500", offset=1),
                        ],
                    ),
                    line={"color": "#00BFA5", "width": 2},
                )
                .encode(
                    x=alt.X("date:T", title=""),
                    y=alt.Y("balance:Q", title="资金 (¥)", scale=alt.Scale(zero=False)),
                    tooltip=["date:T", alt.Tooltip("balance:Q", format=".0f")],
                )
                .properties(height=300)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            render_empty_state("暂无权益数据", "投注结算后将显示权益曲线。")

    with right:
        st.subheader("盈亏分布")
        if not bet_history.empty and "profit" in bet_history.columns:
            profits = bet_history["profit"].dropna()
            if len(profits) > 0:
                pnl_data = pd.DataFrame({
                    "盈亏": profits,
                    "类型": profits.apply(lambda x: "盈利" if x > 0 else "亏损"),
                })
                chart = (
                    alt.Chart(pnl_data)
                    .mark_bar(opacity=0.8)
                    .encode(
                        x=alt.X("盈亏:Q", bin=alt.Bin(maxbins=20)),
                        y=alt.Y("count()", title="次数"),
                        color=alt.Color("类型:N", scale=alt.Scale(
                            domain=["盈利", "亏损"],
                            range=["#00BFA5", "#FF6B6B"],
                        )),
                    )
                    .properties(height=200)
                )
                st.altair_chart(chart, use_container_width=True)

                avg_win = profits[profits > 0].mean() if (profits > 0).any() else 0
                avg_loss = abs(profits[profits < 0].mean()) if (profits < 0).any() else 0
                if avg_win > 0 and avg_loss > 0:
                    st.metric("盈亏比", f"{avg_win / avg_loss:.2f}")
            else:
                st.caption("暂无盈亏数据")
        else:
            render_empty_state("暂无数据", "")

    # ── 待结算投注详情 ──
    st.divider()
    st.subheader(f"待结算投注 ({n_pending})")
    if pending_bets:
        rows = []
        for b in pending_bets:
            model_prob = b.get("model_prob", 0)
            odds = b.get("odds", 1)
            ev = model_prob * odds - 1
            rows.append({
                "比赛": f"{b.get('home_cn', '?')} vs {b.get('away_cn', '?')}",
                "联赛": b.get("league", ""),
                "类型": b.get("market_type", ""),
                "赔率": f"{odds:.2f}",
                "模型概率": f"{model_prob:.1%}",
                "注额": f"¥{b.get('stake', 0):.0f}",
                "EV": f"{ev:+.1%}",
                "时间": b.get("created_at", "")[:16].replace("T", " "),
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True,
                     column_config={"比赛": st.column_config.TextColumn(width="medium")})
    else:
        st.info("无待结算投注 — 预测流水线运行后将自动生成。")

    # ── CLV 分析 ──
    st.divider()
    st.subheader("CLV 分析（收盘价价值）")
    clv_values = []
    for b in pending_bets:
        clv = b.get("clv")
        if clv is not None:
            clv_values.append(clv)
    for h in portfolio_state.get("history", []):
        clv = h.get("clv")
        if clv is not None:
            clv_values.append(clv)

    if clv_values:
        clv_s = pd.Series(clv_values)
        avg_clv = clv_s.mean()
        pos_clv = (clv_s > 0).sum()

        col1, col2, col3 = st.columns(3)
        col1.metric("平均 CLV", f"{avg_clv:+.4f}")
        col2.metric("正向 CLV", f"{pos_clv}/{len(clv_s)} ({pos_clv/len(clv_s):.0%})")
        col3.metric("最佳 CLV", f"{clv_s.max():+.4f}")

        clv_df = pd.DataFrame({"clv": clv_values, "idx": range(len(clv_values))})
        chart = (
            alt.Chart(clv_df)
            .mark_bar(opacity=0.7)
            .encode(
                x=alt.X("idx:O", title="投注 #"),
                y=alt.Y("clv:Q", title="CLV"),
                color=alt.condition(
                    alt.datum.clv > 0,
                    alt.value("#00BFA5"),
                    alt.value("#FF6B6B"),
                ),
                tooltip=["clv:Q"],
            )
            .properties(height=200)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("暂无 CLV 数据 — 投注结算后系统会自动追踪收盘价。")

    # ── Edge Attribution ──
    if edge_attr:
        st.divider()
        st.subheader("收益来源归因")
        summary = edge_attr.get("summary", {})
        if summary:
            cols = st.columns(3)
            cols[0].metric("模型贡献", f"{summary.get('model_pct', 0):.1%}")
            cols[1].metric("选品贡献", f"{summary.get('line_shopping_pct', 0):.1%}")
            cols[2].metric("择时贡献", f"{summary.get('timing_pct', 0):.1%}")
