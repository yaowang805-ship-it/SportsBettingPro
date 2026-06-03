"""实力评分页面 — NBA与足球球队实力排名。"""
import altair as alt
import pandas as pd
import streamlit as st

from src.dashboard.components.data_loader import (
    load_json, load_csv, data_exists, render_empty_state,
)
from src.dashboard.components.team_cn import team_cn, sport_cn
from src.dashboard.config import NBA_RATINGS_FILE, FB_RATINGS_FILE


def render():
    st.header("🏋️ 球队实力评分 (Power Ratings)")

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("🏀 NBA")
        nba_ratings = load_json(NBA_RATINGS_FILE)
        if nba_ratings and isinstance(nba_ratings, dict):
            _render_ratings_chart(nba_ratings, "nba")
        else:
            render_empty_state("暂无NBA评分", "运行Power Rating计算后显示。")

    with col_right:
        st.subheader("⚽ 足球")
        fb_ratings = load_json(FB_RATINGS_FILE)
        if fb_ratings and isinstance(fb_ratings, dict):
            _render_ratings_chart(fb_ratings, "football")
        else:
            render_empty_state("暂无足球评分", "运行Power Rating计算后显示。")


def _render_ratings_chart(ratings: dict, sport: str):
    """渲染球队评分的水平条形图（队名已中文化）。"""
    df = pd.DataFrame([
        {"team": team_cn(team, sport), "rating": rating}
        for team, rating in ratings.items()
    ]).sort_values("rating", ascending=True)

    if df.empty:
        st.caption("暂无评分数据。")
        return

    # 显示前15名
    top_n = min(15, len(df))
    chart_df = df.tail(top_n)

    chart = (
        alt.Chart(chart_df)
        .mark_bar()
        .encode(
            x=alt.X("rating:Q", title="评分"),
            y=alt.Y("team:N", sort="-x", title="球队"),
            color=alt.condition(
                alt.datum.rating > 0, alt.value("#5B8FF9"), alt.value("#FF5252")
            ),
            tooltip=["team:N", "rating:Q"],
        )
        .properties(height=max(30 * top_n, 150))
    )
    st.altair_chart(chart, use_container_width=True)

    # 全部球队表格
    st.caption(f"全部 {len(df)} 支球队")
    st.dataframe(
        df.sort_values("rating", ascending=False),
        use_container_width=True,
        hide_index=True,
    )
