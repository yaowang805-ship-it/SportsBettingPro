"""自动结算报告 — 结算成功率、时效性、按维度分解。"""
import streamlit as st
import pandas as pd

from src.dashboard.components.data_loader import load_csv, load_json, render_empty_state
from src.dashboard.config import PRED_LOG_FILE, PORTFOLIO_FILE
from src.dashboard.config import DATA_DIR

PERF_SUMMARY_FILE = DATA_DIR / "performance_summary.json"


def _load_perf_summary():
    """加载预计算绩效汇总。"""
    d = load_json(PERF_SUMMARY_FILE)
    if not d:
        return None
    return d


def render():
    st.header("📋 自动结算报告")

    df = load_csv(PRED_LOG_FILE)
    perf = _load_perf_summary()
    portfolio = load_json(PORTFOLIO_FILE)

    if df.empty and perf is None:
        render_empty_state("暂无结算数据",
                           "运行预测流水线 (`python main.py`) 后查看。")
        return

    # ── KPI 行 ──
    if perf:
        total = perf.get("total_predictions", 0)
        settled = perf.get("settled", 0)
        won = perf.get("won", 0)
        lost = perf.get("lost", 0)
        win_rate = perf.get("win_rate", 0)
        roi = perf.get("roi", 0)
        total_profit = perf.get("total_profit", 0)
    else:
        total = len(df)
        settled = len(df[df["status"].isin(["won", "lost"])]) if "status" in df.columns else 0
        won = len(df[df["status"] == "won"]) if "status" in df.columns else 0
        lost = len(df[df["status"] == "lost"]) if "status" in df.columns else 0
        win_rate = won / (won + lost) if (won + lost) > 0 else 0
        total_profit = 0
        roi = 0

    pending = total - settled

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("总预测", total)
    col2.metric("已结算", settled)
    col3.metric("⏳ 待结算", pending)
    col4.metric("✅ 胜率", f"{win_rate:.1%}" if isinstance(win_rate, (int, float)) else "—")
    col5.metric("💰 ROI", f"{roi:+.1%}" if isinstance(roi, (int, float)) else "—")
    col6.metric("📈 盈利", f"¥{total_profit:+.0f}" if total_profit else "—")

    st.divider()

    # ── 结算时间线 ──
    if not df.empty and "settled_at" in df.columns:
        settled_df = df[df["settled_at"].notna() & (df["settled_at"] != "")].copy()
        if not settled_df.empty:
            settled_df["settled_dt"] = pd.to_datetime(settled_df["settled_at"], errors="coerce")
            settled_df = settled_df.dropna(subset=["settled_dt"])

            st.subheader("⏱️ 结算时间线")
            timeline = (
                settled_df.set_index("settled_dt")
                .resample("D")
                .size()
                .reset_index(name="count")
            )
            import altair as alt
            chart = alt.Chart(timeline).mark_bar(color="#00BFA5").encode(
                x=alt.X("settled_dt:T", title="日期"),
                y=alt.Y("count:Q", title="结算数"),
                tooltip=["settled_dt", "count"],
            ).properties(height=200)
            st.altair_chart(chart, use_container_width=True)
            st.caption("")

    # ── 按联赛分解 ──
    st.subheader("🏆 按联赛分解")
    if perf and "by_league" in perf and perf["by_league"]:
        league_rows = []
        for league, stats in perf["by_league"].items():
            league_rows.append({
                "联赛": league,
                "总数": stats.get("total", 0),
                "胜": stats.get("won", 0),
                "胜率": f"{stats.get('win_rate', 0):.1%}",
                "投注额": f"¥{stats.get('stake', 0):.0f}",
                "盈利": f"¥{stats.get('profit', 0):+.0f}",
                "ROI": f"{stats.get('roi', 0):+.1%}",
            })
        st.dataframe(pd.DataFrame(league_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("暂无联赛分解数据。")

    # ── 按市场类型分解 ──
    st.subheader("🎯 按盘口类型分解")
    if perf and "by_market" in perf and perf["by_market"]:
        market_rows = []
        for market, stats in perf["by_market"].items():
            market_rows.append({
                "盘口类型": market,
                "总数": stats.get("total", 0),
                "胜": stats.get("won", 0),
                "胜率": f"{stats.get('win_rate', 0):.1%}",
                "投注额": f"¥{stats.get('stake', 0):.0f}",
                "盈利": f"¥{stats.get('profit', 0):+.0f}",
                "ROI": f"{stats.get('roi', 0):+.1%}",
            })
        st.dataframe(pd.DataFrame(market_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("暂无盘口类型分解数据。")

    # ── 按运动分解 ──
    st.subheader("⚽ 按运动分解")
    if perf and "by_sport" in perf and perf["by_sport"]:
        sport_rows = []
        for sport, stats in perf["by_sport"].items():
            sport_rows.append({
                "运动": sport,
                "总数": stats.get("total", 0),
                "胜": stats.get("won", 0),
                "胜率": f"{stats.get('win_rate', 0):.1%}",
                "投注额": f"¥{stats.get('stake', 0):.0f}",
                "盈利": f"¥{stats.get('profit', 0):+.0f}",
                "ROI": f"{stats.get('roi', 0):+.1%}",
            })
        st.dataframe(pd.DataFrame(sport_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("暂无运动分解数据。")

    st.divider()

    # ── 待结算分析 ──
    if not df.empty and "status" in df.columns:
        pending_df = df[df["status"] == "pending"].copy()
        if not pending_df.empty and "date" in pending_df.columns:
            st.subheader("⏳ 待结算逾期分析")
            pending_df["pred_date"] = pd.to_datetime(pending_df["date"], errors="coerce")
            now = pd.Timestamp.now(tz="UTC")
            pending_df["wait_days"] = (now - pending_df["pred_date"]).dt.total_seconds() / 86400
            avg_wait = pending_df["wait_days"].mean()
            max_wait = pending_df["wait_days"].max()

            col1, col2, col3 = st.columns(3)
            col1.metric("待结算数", len(pending_df))
            col2.metric("平均等待", f"{avg_wait:.1f} 天" if pd.notna(avg_wait) else "—")
            col3.metric("最长等待", f"{max_wait:.1f} 天" if pd.notna(max_wait) else "—")

            # 按联赛分布
            if "league" in pending_df.columns:
                pending_by_league = (
                    pending_df["league"].value_counts().reset_index()
                )
                pending_by_league.columns = ["联赛", "待结算数"]
                st.dataframe(pending_by_league, use_container_width=True, hide_index=True)

    # ── 最近结算记录 ──
    if not df.empty and "status" in df.columns:
        settled_df = df[df["status"].isin(["won", "lost", "void"])].copy()
        if not settled_df.empty:
            st.divider()
            st.subheader(f"📄 最近结算记录 ({len(settled_df)} 条)")

            cols = [c for c in ["date", "sport", "league", "home_team_cn", "away_team_cn",
                                "market_type", "market_detail", "odds", "stake", "status",
                                "model_prob", "ev", "settled_at"]
                    if c in settled_df.columns]

            display = settled_df[cols].copy().sort_values("settled_at", ascending=False) if "settled_at" in cols else settled_df[cols].copy()

            # Format
            if "odds" in display.columns:
                display["odds"] = display["odds"].apply(lambda x: f"{float(x):.2f}" if pd.notna(x) else "")
            if "stake" in display.columns:
                display["stake"] = display["stake"].apply(lambda x: f"¥{float(x):.2f}" if pd.notna(x) else "")
            if "model_prob" in display.columns:
                display["model_prob"] = display["model_prob"].apply(lambda x: f"{float(x):.1%}" if pd.notna(x) else "")
            if "ev" in display.columns:
                display["ev"] = display["ev"].apply(lambda x: f"{float(x):.2%}" if pd.notna(x) else "")

            def _style_status(val):
                if val == "won":
                    return "color: #00BFA5; font-weight: bold"
                if val == "lost":
                    return "color: #FF5252; font-weight: bold"
                return ""

            styled = display.style.map(_style_status, subset=["status"])
            st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── 组合结算历史 ──
    if portfolio and "history" in portfolio and portfolio["history"]:
        st.divider()
        st.subheader("💰 投资组合结算历史")

        hist = pd.DataFrame(portfolio["history"])
        if not hist.empty:
            cols = [c for c in ["date", "match", "stake", "odds", "profit", "status"]
                    if c in hist.columns]
            display = hist[cols].copy()
            if "profit" in display.columns:
                display["profit"] = display["profit"].apply(
                    lambda x: f"¥{float(x):+.0f}" if pd.notna(x) else "")
                # Color profit column
                def _color_profit(val):
                    try:
                        v = float(val.replace("¥", "").replace("+", ""))
                        if v > 0:
                            return "color: #00BFA5"
                        if v < 0:
                            return "color: #FF5252"
                    except (ValueError, AttributeError):
                        pass
                    return ""
                display = display.style.map(_color_profit, subset=["profit"])

            st.dataframe(display, use_container_width=True, hide_index=True)

    st.divider()
    st.caption("数据来源: prediction_log.csv + virtual_portfolio.json + performance_summary.json")
