"""投注记录检索 — 搜索和筛选历史投注。"""
import streamlit as st
import pandas as pd

from src.dashboard.components.data_loader import load_csv, load_json, render_empty_state
from src.dashboard.config import PRED_LOG_FILE, PORTFOLIO_FILE


def _load_records() -> pd.DataFrame:
    """加载并合并预测记录和组合历史。"""
    df = load_csv(PRED_LOG_FILE)
    if df.empty:
        return df

    # 补齐缺失列
    for col in ["home_team_cn", "away_team_cn", "quality_score", "quality_tier",
                 "home_team_en", "away_team_en"]:
        if col not in df.columns:
            df[col] = ""

    return df


def _style_status(val: str) -> str:
    if val == "won":
        return "color: #00BFA5; font-weight: bold"
    if val == "lost":
        return "color: #FF5252; font-weight: bold"
    if val == "pending":
        return "color: #FFA726; font-weight: bold"
    return ""


def render():
    st.header("🔍 投注记录检索")

    df = _load_records()

    if df.empty:
        render_empty_state("暂无投注记录",
                           "运行预测流水线并在`虚拟投资组合`中投注后生成。")
        return

    # 日期列处理
    date_col = "date" if "date" in df.columns else "timestamp"
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")

    # ── 过滤器 ──
    st.subheader("筛选条件")

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        date_min = df[date_col].min().date() if date_col in df.columns and not df[date_col].isna().all() else None
        date_max = df[date_col].max().date() if date_col in df.columns and not df[date_col].isna().all() else None
        date_from = st.date_input("开始日期", date_min or pd.Timestamp.today().date(),
                                  min_value=date_min, max_value=date_max)

    with col2:
        date_to = st.date_input("结束日期", date_max or pd.Timestamp.today().date(),
                                min_value=date_min, max_value=date_max)

    with col3:
        sports = ["全部"] + sorted(df["sport"].dropna().unique().tolist()) if "sport" in df.columns else ["全部"]
        sport_filter = st.selectbox("运动", sports)

    with col4:
        leagues = ["全部"] + sorted(df["league"].dropna().unique().tolist()) if "league" in df.columns else ["全部"]
        league_filter = st.selectbox("联赛", leagues)

    with col5:
        market_types = ["全部"] + sorted(df["market_type"].dropna().unique().tolist()) if "market_type" in df.columns else ["全部"]
        market_filter = st.selectbox("盘口类型", market_types)

    col6, col7, col8 = st.columns(3)
    with col6:
        statuses = ["全部", "won", "lost", "pending"]
        status_filter = st.selectbox("状态", statuses)

    with col7:
        search_term = st.text_input("球队搜索（中文/英文）", placeholder="如: 湖人, Arsenal")

    with col8:
        st.caption("")  # spacer
        show_all_cols = st.checkbox("显示所有列", value=False)

    # ── 应用过滤 ──
    filtered = df.copy()

    if date_col in filtered.columns:
        filtered = filtered[filtered[date_col].dt.date >= date_from]
        filtered = filtered[filtered[date_col].dt.date <= date_to]

    if sport_filter != "全部":
        filtered = filtered[filtered["sport"] == sport_filter]
    if league_filter != "全部":
        filtered = filtered[filtered["league"] == league_filter]
    if market_filter != "全部":
        filtered = filtered[filtered["market_type"] == market_filter]
    if status_filter != "全部":
        filtered = filtered[filtered["status"] == status_filter]
    if search_term:
        mask = pd.Series([False] * len(filtered))
        for col in ["home_team", "away_team", "home_team_cn", "away_team_cn",
                     "home_team_en", "away_team_en", "team"]:
            if col in filtered.columns:
                mask |= filtered[col].astype(str).str.contains(search_term, case=False, na=False)
        filtered = filtered[mask]

    # ── 汇总统计 ──
    st.divider()
    n_total = len(filtered)
    n_won = len(filtered[filtered["status"] == "won"]) if "status" in filtered.columns else 0
    n_lost = len(filtered[filtered["status"] == "lost"]) if "status" in filtered.columns else 0
    n_pending = len(filtered[filtered["status"] == "pending"]) if "status" in filtered.columns else 0

    if "stake" in filtered.columns:
        filtered["stake"] = pd.to_numeric(filtered["stake"], errors="coerce")
        total_stake = filtered["stake"].sum()
    else:
        total_stake = 0

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("总记录", n_total)
    col2.metric("✅ 赢", n_won)
    col3.metric("❌ 输", n_lost)
    col4.metric("⏳ 待结算", n_pending)
    col5.metric("总投注额", f"¥{total_stake:.0f}" if total_stake else "—")

    if n_won + n_lost > 0:
        win_rate = n_won / (n_won + n_lost)
        st.caption(f"胜率: {win_rate:.1%}")
        # 盈利估算
        est_profit = (
            filtered.loc[filtered["status"] == "won", "stake"].sum() * 0.9
            - filtered.loc[filtered["status"] == "lost", "stake"].sum()
            if "stake" in filtered.columns else 0
        )
        st.caption(f"预估盈利: ¥{est_profit:.0f}")

    # ── 结果表格 ──
    st.divider()
    st.subheader(f"共 {len(filtered)} 条记录")

    if not filtered.empty:
        # 选择展示列
        base_cols = ["date", "sport", "league"]
        info_cols = ["home_team_cn", "away_team_cn", "market_type", "market_detail"]
        bet_cols = ["odds", "stake", "status"]
        extra_cols = ["model_prob", "ev", "quality_score", "quality_tier",
                       "home_team_en", "away_team_en", "model_version"]

        if show_all_cols:
            display_cols = [c for c in base_cols + info_cols + bet_cols + extra_cols
                            if c in filtered.columns]
        else:
            display_cols = [c for c in base_cols + info_cols + bet_cols
                            if c in filtered.columns]

        # 确保关键列存在
        display_cols = [c for c in display_cols if c in filtered.columns]

        display_df = filtered[display_cols].copy()

        # 格式化
        if "odds" in display_df.columns:
            display_df["odds"] = display_df["odds"].apply(
                lambda x: f"{x:.2f}" if pd.notna(x) else "")
        if "stake" in display_df.columns:
            display_df["stake"] = display_df["stake"].apply(
                lambda x: f"¥{x:.2f}" if pd.notna(x) else "")
        if "model_prob" in display_df.columns:
            display_df["model_prob"] = display_df["model_prob"].apply(
                lambda x: f"{float(x):.1%}" if pd.notna(x) else "")
        if "ev" in display_df.columns:
            display_df["ev"] = display_df["ev"].apply(
                lambda x: f"{float(x):.2%}" if pd.notna(x) else "")

        styled = display_df.style.map(_style_status, subset=["status"])

        st.dataframe(styled, use_container_width=True, hide_index=True)

        # ── 导出 ──
        csv = filtered.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 导出 CSV",
            data=csv,
            file_name="bet_records.csv",
            mime="text/csv",
        )
    else:
        st.info("没有匹配的记录。")
