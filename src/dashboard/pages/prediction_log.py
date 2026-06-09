#!/usr/bin/env python3
"""预测记录浏览页面 — 查询、筛选、统计 prediction_log.csv 历史记录。"""
from pathlib import Path

import pandas as pd
import streamlit as st

from src.dashboard.components.data_loader import render_empty_state

_LOG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "storage" / "prediction_log.csv"


def render():
    st.header("📋 预测记录浏览")

    if not _LOG_PATH.exists():
        render_empty_state("暂无预测记录。记录将在预测生成后自动写入 prediction_log.csv。")
        return

    df = pd.read_csv(_LOG_PATH)
    if df.empty:
        render_empty_state("预测记录为空。")
        return

    # 筛选条件
    cols = st.columns(4)
    with cols[0]:
        status_filter = st.multiselect("状态", options=df["status"].unique(), default=[])
    with cols[1]:
        sport_filter = st.multiselect("运动", options=df["sport"].unique(), default=[])
    with cols[2]:
        league_filter = st.multiselect("联赛", options=df["league"].unique(), default=[])
    with cols[3]:
        market_filter = st.multiselect("盘口类型", options=df["market_type"].unique(), default=[])

    if status_filter:
        df = df[df["status"].isin(status_filter)]
    if sport_filter:
        df = df[df["sport"].isin(sport_filter)]
    if league_filter:
        df = df[df["league"].isin(league_filter)]
    if market_filter:
        df = df[df["market_type"].isin(market_filter)]

    st.caption(f"显示 {len(df)} 条记录")

    # 摘要统计
    if not df.empty and "status" in df.columns:
        settled = df[df["status"].isin(["won", "lost"])]
        if not settled.empty:
            wins = (settled["status"] == "won").sum()
            total = len(settled)
            wr = wins / total if total > 0 else 0
            col1, col2, col3 = st.columns(3)
            col1.metric("已结算", total)
            col2.metric("胜率", f"{wr:.1%}")
            col3.metric("待结算", len(df[df["status"] == "pending"]))

    # 显示表格
    show_cols = ["date", "sport", "league", "market_type", "home_team", "away_team",
                 "model_prob", "odds", "ev", "stake", "status"]
    show_cols = [c for c in show_cols if c in df.columns]
    sort_col = "date" if "date" in df.columns else None
    display_df = df[show_cols].sort_values(sort_col, ascending=False) if sort_col else df[show_cols]
    st.dataframe(display_df, hide_index=True, use_container_width=True)

    # 导出
    csv = display_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("📥 导出 CSV", data=csv, file_name="prediction_log_export.csv", mime="text/csv")
