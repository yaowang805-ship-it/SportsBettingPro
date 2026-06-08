"""总览页面 — 系统核心指标、组合概览、实时状态。"""
import json
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import numpy as np

from src.dashboard.components.data_loader import load_json, load_csv, data_exists, render_empty_state
from src.dashboard.config import (
    SYSTEM_HEALTH_FILE, RISK_STATE_FILE, PORTFOLIO_FILE,
    BET_HISTORY_FILE, MODEL_ACCURACY_FILE,
)


def _safe_metric(value, fmt=".2f", fallback="N/A"):
    """Format a metric with safe fallback."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return fallback
    if isinstance(value, str):
        return value
    return f"{value:{fmt}}"


def render():
    st.header("📊 系统总览")

    # ── 加载数据 ──
    health = load_json(SYSTEM_HEALTH_FILE)
    portfolio_state = load_json(PORTFOLIO_FILE)
    risk_state = load_json(RISK_STATE_FILE)
    bet_history = load_csv(BET_HISTORY_FILE)
    model_acc = load_csv(MODEL_ACCURACY_FILE)

    balance = portfolio_state.get("balance", risk_state.get("balance", 10000))

    # ── 待结算投注 ──
    pending_bets = portfolio_state.get("pending_bets", [])
    settled = portfolio_state.get("settled", {})
    n_settled = len(settled)
    n_won = sum(1 for v in settled.values() if v == "won")
    n_pending = len(pending_bets)
    win_rate = n_won / n_settled if n_settled > 0 else 0

    # ── 从 bet_history 计算盈亏 ──
    if not bet_history.empty and "profit" in bet_history.columns and "stake" in bet_history.columns:
        total_profit = bet_history["profit"].fillna(0).sum()
        total_stake = bet_history["stake"].fillna(0).sum()
        roi = total_profit / total_stake if total_stake > 0 else 0
    else:
        total_profit = 0
        roi = 0

    # ── 顶部 KPI 行 ──
    st.subheader("核心指标")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("💰 资金余额", f"¥{balance:,.0f}")
    col2.metric("📈 总盈亏", f"¥{total_profit:+,.0f}", delta=total_profit)
    col3.metric("🎯 胜率", f"{win_rate:.1%}" if n_settled > 0 else "N/A")
    col4.metric("📊 ROI", f"{roi:+.2%}" if roi != 0 else "0.00%")
    col5.metric("⏳ 待结算", str(n_pending))
    col6.metric("🏆 已结算", str(n_settled))

    # ── 第二行: 模型/系统状态 ──
    st.caption("")
    col1, col2, col3, col4 = st.columns(4)
    if health:
        mh = health.get("model_health", {})
        days_since = mh.get("days_since_train", "N/A")
        col1.metric("🧠 模型距上次训练", f"{days_since:.0f}天" if isinstance(days_since, (int, float)) else days_since)
        col2.metric("🔧 需要重训", "是 ⚠️" if mh.get("needs_retrain") else "否 ✅")

        rh = health.get("risk_health", {})
        dd = rh.get("drawdown", 0)
        col3.metric("📉 最大回撤", _safe_metric(dd, ".1%"))

        ph = health.get("performance_health", {})
        total_bets = ph.get("total_bets", n_settled)
        col4.metric("📋 总投注数", str(total_bets))
    else:
        for c in [col1, col2, col3, col4]:
            c.metric("—", "N/A")

    # ── 双列布局: 权益曲线 + 近期推荐 ──
    left_col, right_col = st.columns([3, 2])

    with left_col:
        st.subheader("权益曲线")

        # 从 bet_history 构建权益曲线
        if not bet_history.empty and "profit" in bet_history.columns:
            df = bet_history.copy()
            df["date"] = pd.to_datetime(df.get("date", df.index), errors="coerce")
            df = df.sort_values("date")
            df["cumulative"] = df["profit"].fillna(0).cumsum() + balance - df["profit"].fillna(0).sum()
            # 安全倒退初始余额
            start_balance = balance - df["profit"].fillna(0).sum()
            df["cumulative"] = df["profit"].fillna(0).cumsum() + start_balance
            # 加起始点
            if len(df) > 0:
                first = df.iloc[0]
                points = pd.DataFrame([
                    {"date": df["date"].min() - pd.Timedelta(days=1), "cumulative": start_balance},
                    *[{"date": r["date"], "cumulative": r["cumulative"]} for _, r in df.iterrows()],
                ])
                chart = (
                    alt.Chart(points)
                    .mark_line(color="#00BFA5", strokeWidth=2, point=False)
                    .encode(
                        x=alt.X("date:T", title="日期"),
                        y=alt.Y("cumulative:Q", title="资金 (¥)", scale=alt.Scale(zero=False)),
                        tooltip=["date:T", alt.Tooltip("cumulative:Q", format=".0f")],
                    )
                    .properties(height=300)
                )
                # 添加参考线
                reference = pd.DataFrame({"y": [balance]})
                ref_line = (
                    alt.Chart(reference)
                    .mark_rule(color="gray", strokeDash=[5, 5], opacity=0.5)
                    .encode(y="y:Q")
                )
                st.altair_chart(chart + ref_line, use_container_width=True)
            else:
                render_empty_state("暂无权益数据", "完成投注并结算后将显示权益曲线。")
        else:
            render_empty_state("暂无权益数据", "完成投注并结算后将显示权益曲线。")

    with right_col:
        st.subheader("待结算投注")
        if pending_bets:
            rows = []
            for b in pending_bets:
                rows.append({
                    "比赛": f"{b.get('home_cn', '?')} vs {b.get('away_cn', '?')}",
                    "类型": b.get("market_type", ""),
                    "赔率": f"{b.get('odds', 0):.2f}",
                    "注额": f"¥{b.get('stake', 0):.0f}",
                    "EV": f"{b.get('model_prob', 0) * b.get('odds', 1) - 1:+.1%}",
                })
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
                column_config={"比赛": st.column_config.TextColumn(width="large")},
            )
            st.caption(f"共 {n_pending} 笔待结算投注")
        else:
            render_empty_state("暂无待结算投注", "推荐生成后虚拟组合会自动同步。")

    # ── 底部: 模型准确率趋势 ──
    st.divider()
    st.subheader("模型准确率趋势")
    if not model_acc.empty and "date" in model_acc.columns and "rolling_14d" in model_acc.columns:
        acc_df = model_acc.copy()
        acc_df["date"] = pd.to_datetime(acc_df["date"], errors="coerce")
        acc_df = acc_df.dropna(subset=["date"]).sort_values("date")

        chart = (
            alt.Chart(acc_df)
            .transform_fold(
                ["rolling_7d", "rolling_14d", "rolling_30d"],
                as_=["窗口", "准确率"],
            )
            .mark_line(strokeWidth=2)
            .encode(
                x=alt.X("date:T", title="日期"),
                y=alt.Y("准确率:Q", title="准确率", scale=alt.Scale(domain=[0, 1])),
                color=alt.Color("窗口:N", scale=alt.Scale(
                    domain=["rolling_7d", "rolling_14d", "rolling_30d"],
                    range=["#FF6B6B", "#4ECDC4", "#45B7D1"],
                )),
                tooltip=["date:T", "准确率:Q"],
            )
            .properties(height=250)
        )
        # 52% 参考线（最小可靠阈值）
        ref = pd.DataFrame({"y": [0.52]})
        ref_line = (
            alt.Chart(ref)
            .mark_rule(color="red", strokeDash=[5, 5], opacity=0.4)
            .encode(y="y:Q")
        )
        st.altair_chart(chart + ref_line, use_container_width=True)
        st.caption("红色虚线 = 最小可靠阈值 (52%) — 低于此线的模型禁用推荐")
    else:
        render_empty_state("暂无模型准确率数据", "模型训练完成后将自动生成。")

    # ── 最近结算记录 ──
    if not bet_history.empty:
        st.divider()
        st.subheader("最近投注记录")
        cols = [c for c in ["date", "home_team", "away_team", "bet_type", "odds", "stake", "profit", "result"] if c in bet_history.columns]
        if cols:
            recent = bet_history[cols].tail(10).copy()
            if "date" in recent.columns:
                recent["date"] = pd.to_datetime(recent["date"], errors="coerce").dt.strftime("%m-%d %H:%M")
            st.dataframe(recent, use_container_width=True, hide_index=True)
