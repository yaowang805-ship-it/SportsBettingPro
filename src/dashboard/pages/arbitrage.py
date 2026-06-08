"""套利监控页面 — 实时套利机会、赔率分歧、市场效率。"""
import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from src.dashboard.components.data_loader import load_json, load_csv, render_empty_state
from src.dashboard.config import ARBITRAGE_FILE, EDGE_ATTRIBUTION_FILE
from src.dashboard.components.data_loader import st


def _load_odds_snapshots():
    """加载赔率快照数据用于分歧分析。"""
    from src.dashboard.config import SNAPSHOT_DIR
    if not SNAPSHOT_DIR.exists():
        return {}
    snapshots = {}
    for f in sorted(SNAPSHOT_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            snapshots[f.stem] = data
        except Exception:
            pass
    return snapshots


def render():
    st.header("🔄 套利与市场效率")

    arb_data = load_json(ARBITRAGE_FILE)
    edge_attr = load_json(EDGE_ATTRIBUTION_FILE)
    snapshots = _load_odds_snapshots()

    # ── KPI ──
    st.subheader("套利概览")
    if arb_data:
        n_opportunities = arb_data.get("n_total", len(arb_data.get("opportunities", [])))
        col1, col2, col3 = st.columns(3)
        col1.metric("套利机会", str(n_opportunities))
        col2.metric("H2H 机会", str(arb_data.get("n_h2h", 0)))
        col3.metric("让分机会", str(arb_data.get("n_spread", 0)))
    else:
        st.info("暂无套利数据 — 盘口快照采集后自动分析。")

    # ── 当前套利机会列表 ──
    st.subheader("当前套利机会")
    opportunities = []
    if arb_data:
        opportunities = arb_data.get("opportunities", [])
        if isinstance(opportunities, dict):
            opportunities = [opportunities]

    if opportunities:
        rows = []
        for opp in opportunities:
            rows.append({
                "比赛": opp.get("match", opp.get("game", "?")),
                "类型": opp.get("type", opp.get("market", "")),
                "收益率": f"{opp.get('yield', opp.get('return', 0)):.2%}",
                "主队赔率": f"{opp.get('home_odds', 0):.2f}",
                "客队赔率": f"{opp.get('away_odds', 0):.2f}",
                "平台": opp.get("bookmaker", opp.get("platform", "")),
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

        # 可视化
        if len(rows) > 1:
            chart_df = pd.DataFrame(rows)
            yields = [float(r["收益率"].strip("%")) / 100 for r in rows]
            chart_df["收益率"] = yields
            chart = (
                alt.Chart(chart_df)
                .mark_bar(color="#FFD700", opacity=0.8)
                .encode(
                    x=alt.X("比赛:N", sort="-y"),
                    y=alt.Y("收益率:Q", title="收益率", axis=alt.Axis(format="%")),
                    tooltip=["比赛:N", "收益率:Q"],
                )
                .properties(height=250)
            )
            st.altair_chart(chart, use_container_width=True)
    else:
        render_empty_state("暂无套利机会", "市场处于均衡状态，或盘口数据不足。")

    # ── 赔率分歧分析 ──
    st.divider()
    st.subheader("赔率分歧分析（市场低效信号）")
    if snapshots:
        # 分析各博彩公司间的赔率标准差
        all_prices = []
        for name, data in snapshots.items():
            if isinstance(data, dict) and "bookmakers" in data:
                for bm in data["bookmakers"]:
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") == "h2h":
                            for outcome in mkt.get("outcomes", []):
                                all_prices.append({
                                    "比赛": name,
                                    "博彩公司": bm.get("title", "?"),
                                    "选项": outcome.get("name", "?"),
                                    "赔率": outcome.get("price", 0),
                                })

        if all_prices:
            price_df = pd.DataFrame(all_prices)
            # 按比赛+选项分组算标准差
            divergence = (
                price_df.groupby(["比赛", "选项"])["赔率"]
                .agg(["mean", "std", "min", "max", "count"])
                .reset_index()
            )
            divergence["分歧度"] = divergence["std"] / divergence["mean"]
            divergence = divergence.sort_values("分歧度", ascending=False).head(10)

            if not divergence.empty:
                chart = (
                    alt.Chart(divergence)
                    .mark_bar(opacity=0.8)
                    .encode(
                        x=alt.X("分歧度:Q", title="分歧度 (CV)"),
                        y=alt.Y("比赛:N", sort="-x"),
                        color=alt.Color("分歧度:Q", scale=alt.Scale(scheme="yellowgreenblue")),
                        tooltip=["比赛:N", "选项:N", "分歧度:Q", "count:Q"],
                    )
                    .properties(height=300)
                )
                st.altair_chart(chart, use_container_width=True)
                st.caption("分歧度 = 赔率标准差/均值 — 越大表示博彩公司间分歧越大，潜在套利/价值空间越大")
            else:
                st.caption("无足够数据计算分歧度")
        else:
            st.caption("无赔率快照数据")
    else:
        st.info("暂无赔率快照 — 盘口采集运行后自动生成。")

    # ── 收益归因摘要 ──
    st.divider()
    st.subheader("收益来源分解")
    if edge_attr:
        summary = edge_attr.get("summary", {})
        by_sport = edge_attr.get("by_sport", {})

        if summary:
            cols = st.columns(3)
            cols[0].metric("🧠 模型预测", f"{summary.get('model_pct', 0):.1%}")
            cols[1].metric("🔍 选品 (Line Shopping)", f"{summary.get('line_shopping_pct', 0):.1%}")
            cols[2].metric("⏱️ 择时 (CLV)", f"{summary.get('timing_pct', 0):.1%}")

        if by_sport:
            rows = []
            for sport, data in by_sport.items():
                rows.append({
                    "运动": sport,
                    "样本量": data.get("count", 0),
                    "总收益": f"{data.get('avg_total_edge', 0):+.2%}",
                    "模型贡献": f"{data.get('avg_model_edge', 0):+.2%}",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("暂无收益归因数据。")
