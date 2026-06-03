"""盘口变动页面 — 赔率快照与 Steam Move 检测。"""
import altair as alt
import pandas as pd
import streamlit as st

from src.dashboard.components.data_loader import (
    load_json, load_csv, data_exists, render_empty_state,
)
from src.dashboard.components.team_cn import team_cn
from src.dashboard.config import SNAPSHOT_FILE, MOVEMENTS_FILE


def _cn_match(match_key: str) -> str:
    """将 'TeamA @ TeamB' 格式转为中文。"""
    if " @" in match_key:
        parts = match_key.split(" @ ")
        if len(parts) == 2:
            return f"{team_cn(parts[0], 'basketball')} vs {team_cn(parts[1], 'basketball')}"
    return match_key


def render():
    st.header("📈 盘口变动监测")

    movements = load_json(MOVEMENTS_FILE)
    snapshot = load_json(SNAPSHOT_FILE)

    # ── 变动汇总 ──
    mov_list = movements if isinstance(movements, list) else []
    if mov_list:
        st.subheader(f"最近 {len(movements)} 次盘口变动")

        mov_df = pd.DataFrame(mov_list)
        col1, col2, col3 = st.columns(3)
        col1.metric("H2H 变动", len(mov_df[mov_df["type"] == "h2h"]))
        col2.metric("让分盘变动", len(mov_df[mov_df["type"] == "spread"]))
        col3.metric("大小球变动", len(mov_df[mov_df["type"] == "total"]))

        # 变动列表（队名中文化）
        if "match" in mov_df.columns:
            mov_df["match_cn"] = mov_df["match"].apply(_cn_match)
        display_cols = [c for c in ["match_cn", "type", "previous", "current", "change_pct", "timestamp"]
                        if c in mov_df.columns]
        col_map = {"match_cn": "比赛", "type": "类型", "previous": "前值", "current": "现值", "change_pct": "变动%", "timestamp": "时间"}
        if display_cols:
            display_df = mov_df[display_cols].tail(20).rename(columns={k: v for k, v in col_map.items() if k in display_cols})
            st.dataframe(display_df, use_container_width=True, hide_index=True)

        # 变动类型分布
        if "type" in mov_df.columns:
            type_counts = mov_df["type"].value_counts().reset_index()
            type_counts.columns = ["type", "count"]
            chart = (
                alt.Chart(type_counts)
                .mark_arc(innerRadius=60)
                .encode(
                    theta=alt.Theta(field="count", type="quantitative"),
                    color=alt.Color(field="type", type="nominal", title="类型"),
                    tooltip=["type", "count"],
                )
                .properties(height=300)
            )
            st.altair_chart(chart, use_container_width=True)
    else:
        st.info("📭 尚无盘口变动记录。盘口变动需要至少两次快照才能检测。")

    # ── 当前盘口 ──
    st.subheader("当前盘口")
    if snapshot and isinstance(snapshot, dict):
        rows = []
        for match_key, data in snapshot.items():
            if isinstance(data, dict):
                cn_key = _cn_match(match_key)
                row = {"比赛": cn_key}
                row.update(data)
                rows.append(row)

        if rows:
            snap_df = pd.DataFrame(rows)
            display_cols = [c for c in ["比赛", "h2h_home", "spread_point", "spread_odds",
                                        "total_point", "over_odds", "timestamp"]
                            if c in snap_df.columns]
            col_map = {
                "比赛": "比赛", "h2h_home": "主胜赔率", "spread_point": "让分",
                "spread_odds": "让分赔率", "total_point": "总分", "over_odds": "大分赔率",
                "timestamp": "快照时间",
            }
            display_df = snap_df[display_cols].rename(columns={k: v for k, v in col_map.items() if k in display_cols})
            st.dataframe(display_df, use_container_width=True, hide_index=True)
    else:
        st.info("📭 当前无盘口数据。运行盘口快照后显示。")
