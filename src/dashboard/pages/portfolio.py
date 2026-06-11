"""投资组合页面 — 权益曲线+最大回撤、利润因子、滚动Sharpe、冷启动模式。"""
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st
import numpy as np

from src.dashboard.components.data_loader import load_json, load_csv, render_empty_state
from src.dashboard.config import (
    PORTFOLIO_FILE, BET_HISTORY_FILE, EDGE_ATTRIBUTION_FILE,
    BANKROLL_SIM_FILE, ATTRIBUTION_FILE,
)

_COLD_START_THRESHOLD = 5
_WARMUP_THRESHOLD = 20


def _compute_drawdown(equity: pd.Series) -> pd.Series:
    """计算从峰值的回撤。"""
    running_max = equity.cummax()
    return (equity - running_max) / running_max


def _compute_sharpe_rolling(profits: pd.Series, stakes: pd.Series, window: int = 20) -> float:
    """滚动 Sharpe ratio — 最近 window 笔投注的收益风险比。"""
    valid = stakes > 0
    if valid.sum() < 3:
        return 0.0
    returns = profits[valid] / stakes[valid]
    recent = returns.tail(window)
    if len(recent) < 3:
        return 0.0
    std = recent.std()
    if std == 0:
        return 0.0
    return float(recent.mean() / std * np.sqrt(len(recent)))


def _compute_streaks(df: pd.DataFrame) -> dict:
    """计算连败和连胜。"""
    if df.empty or "win" not in df.columns:
        return {"current_streak": 0, "max_win_streak": 0, "max_loss_streak": 0, "streak_type": "none"}
    wins = df["win"].astype(int).values
    current = 0
    max_win = 0
    max_loss = 0
    for w in reversed(wins):
        if (w == 1 and current >= 0) or (w == 0 and current <= 0):
            current += 1 if w == 1 else -1
        else:
            break
    curr_streak = current

    run = 0
    for w in wins:
        if w == 1:
            run = run + 1 if run >= 0 else 1
        else:
            run = run - 1 if run <= 0 else -1
        max_win = max(max_win, run) if run > 0 else max_win
        max_loss = min(max_loss, run) if run < 0 else max_loss

    streak_type = "win" if curr_streak > 0 else ("loss" if curr_streak < 0 else "none")
    return {
        "current_streak": abs(curr_streak),
        "streak_type": streak_type,
        "max_win_streak": max_win,
        "max_loss_streak": abs(max_loss),
    }


def _compute_profit_factor(df: pd.DataFrame) -> float:
    """利润因子 = 总盈利 / 总亏损。"""
    if df.empty or "profit" not in df.columns:
        return 0.0
    gross_win = df[df["profit"] > 0]["profit"].sum()
    gross_loss = abs(df[df["profit"] < 0]["profit"].sum())
    return gross_win / gross_loss if gross_loss > 0 else 0.0


def _render_cold_start_banner(n_settled: int):
    """根据结算量显示冷启动/热身模式提示。"""
    if n_settled < _COLD_START_THRESHOLD:
        st.warning(
            "🔴 **冷启动模式** — 仅 %d 条已结算记录，数据不足以验证模型有效性。"
            " 所有推荐仅供观察，建议 1/10 虚拟资金。"
            " 当积累 20+ 条结算后自动退出冷启动。" % n_settled
        )
    elif n_settled < _WARMUP_THRESHOLD:
        st.info(
            "🟡 **热身模式** — %d 条已结算记录，模型正在积累表现数据。"
            " 建议使用 1/4 凯利，待 20+ 条后恢复正常仓位。" % n_settled
        )


def _render_bankroll_simulation():
    """展示 Bankroll Monte Carlo 模拟结果。"""
    sim = load_json(BANKROLL_SIM_FILE)
    if not sim or "results" not in sim:
        return

    results = sim.get("results", {})
    if not results:
        return

    st.divider()
    st.subheader("📊 资金 Monte Carlo 模拟")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("模拟次数", f"{sim.get('n_simulations', 0):,}")
    col2.metric("历史注单", str(sim.get("n_bets", 0)))
    col3.metric("最优凯利", f"{sim.get('optimal_kelly', 0):.2f}")

    # 当前凯利 (0.25) 的表现
    default_kf = "0.25"
    if default_kf in results:
        cur = results[default_kf]
        col4.metric("破产概率 (1/4凯利)", f"{cur.get('ruin_prob', 0):.1%}")

    # 各凯利分数对比表
    rows = []
    for kf_str in sorted(results.keys(), key=float):
        r = results[kf_str]
        rows.append({
            "凯利分数": float(kf_str),
            "期末中位": f"¥{r['median_final']:,.0f}",
            "破产概率": f"{r['ruin_prob']:.1%}",
            "增长率": f"{r['growth_rate']:+.1%}",
        })

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # 资金曲线百分位图
    if default_kf in results:
        pcts = results[default_kf].get("percentiles", {})
        if pcts:
            sim_df = pd.DataFrame({
                "step": range(len(pcts.get("50", []))),
                "P50": pcts.get("50", []),
                "P10": pcts.get("10", []),
                "P90": pcts.get("90", []),
            })
            if not sim_df.empty:
                base = alt.Chart(sim_df).encode(x=alt.X("step:Q", title="注单 #"))
                line = base.mark_line(color="#00BFA5").encode(
                    y=alt.Y("P50:Q", title="资金 (¥)", scale=alt.Scale(zero=False)))
                band = base.mark_area(opacity=0.15, color="#00BFA5").encode(
                    y=alt.Y("P10:Q"), y2=alt.Y2("P90:Q"))
                chart = (band + line).properties(height=250)
                st.altair_chart(chart, use_container_width=True)


def _render_attr_table(data: dict, dim_label: str):
    """渲染归因维度表。"""
    if not data:
        st.caption(f"暂无{dim_label}维度的结算数据")
        return
    df = pd.DataFrame([
        {dim_label: k, "场次": v["bets"], "胜": v["wins"],
         "负": v["losses"], "胜率": f"{v['win_rate']:.1%}"}
        for k, v in sorted(data.items(), key=lambda x: x[1]["bets"], reverse=True)
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)
    chart_df = pd.DataFrame([
        {dim_label: k, "胜率": v["win_rate"]}
        for k, v in data.items() if v["bets"] >= 3
    ])
    if not chart_df.empty and len(chart_df) > 1:
        c = alt.Chart(chart_df).mark_bar().encode(
            x=alt.X(f"{dim_label}:N", sort=None),
            y=alt.Y("胜率:Q", scale=alt.Scale(domain=[0, 1])),
            color=alt.Color("胜率:Q", scale=alt.Scale(scheme="greenorange")),
        ).properties(height=200)
        st.altair_chart(c, use_container_width=True)


def _render_attr_cross(data: dict):
    """渲染交叉维度。"""
    if not data:
        st.caption("暂无交叉维度数据")
        return
    for sport, markets in sorted(data.items()):
        rows = []
        for mkt, v in sorted(markets.items()):
            rows.append({"市场": mkt, "场次": v["bets"],
                         "胜率": f"{v['win_rate']:.1%}"})
        if rows:
            st.caption(f"**{sport}**")
            st.dataframe(pd.DataFrame(rows),
                         use_container_width=True, hide_index=True)


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

    n_settled = len(settled)
    n_won = sum(1 for v in settled.values() if v == "won")
    n_pending = len(pending_bets)
    win_rate = n_won / n_settled if n_settled > 0 else 0

    # 冷启动/热身提示
    _render_cold_start_banner(n_settled)

    # 从 bet_history.csv 算盈亏
    if not bet_history.empty and "profit" in bet_history.columns:
        total_profit = bet_history["profit"].fillna(0).sum()
        total_stake = bet_history["stake"].fillna(0).sum()
        roi = total_profit / total_stake if total_stake > 0 else 0
        profit_factor = _compute_profit_factor(bet_history)
        rolling_sharpe = _compute_sharpe_rolling(
            bet_history["profit"].fillna(0),
            bet_history["stake"].fillna(0),
        )
        streaks = _compute_streaks(bet_history)
    else:
        total_profit = 0
        roi = 0
        profit_factor = 0.0
        rolling_sharpe = 0.0
        streaks = {"current_streak": 0, "streak_type": "none", "max_win_streak": 0, "max_loss_streak": 0}

    # ── KPI 行 ──
    st.subheader("组合概览")
    cols = st.columns(8)
    cols[0].metric("💰 余额", f"¥{balance:,.0f}")
    cols[1].metric("📈 总盈亏", f"¥{total_profit:+,.0f}", delta=total_profit)
    cols[2].metric("🎯 胜率", f"{win_rate:.1%}" if n_settled > 0 else "N/A")
    cols[3].metric("📊 ROI", f"{roi:+.2%}" if roi != 0 else "0.00%")
    cols[4].metric("利润因子", f"{profit_factor:.2f}" if profit_factor > 0 else "N/A")
    cols[5].metric("滚动 Sharpe", f"{rolling_sharpe:.2f}" if rolling_sharpe != 0 else "N/A",
                   delta="可靠" if rolling_sharpe > 0.5 else ("观察" if rolling_sharpe > 0 else None))
    # 连败/连胜
    if streaks["current_streak"] > 1:
        emoji = "🔥" if streaks["streak_type"] == "win" else "🥶"
        label = f"{emoji} {'连胜' if streaks['streak_type'] == 'win' else '连败'}"
        cols[6].metric(label, f"{streaks['current_streak']} 次")
    else:
        cols[6].metric("🏆 已结算", str(n_settled))
    cols[7].metric("⏳ 待结算", str(n_pending))

    # ── 双列: 权益曲线 + 盈亏分布 ──
    left, right = st.columns([3, 2])

    with left:
        st.subheader("权益曲线")

        # 用 history + bet_history 构建曲线
        equity_points = []
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
            eq_df["drawdown"] = _compute_drawdown(eq_df["balance"])
            max_dd = eq_df["drawdown"].min()

            # 权益曲线（面积图）
            area = (
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
            )

            # 最大回撤标注：红色阴影区域（从峰值到当前值的差值区域）
            dd_area = (
                alt.Chart(eq_df)
                .mark_area(opacity=0.12, color="#FF5252")
                .encode(
                    x=alt.X("date:T"),
                    y=alt.Y("balance:Q"),
                    y2="peak:Q",
                )
                .transform_calculate(
                    peak="datum.balance / (1 + datum.drawdown)"
                )
            )

            # 回撤标签线（未使用，保留注释）
            # rule = alt.Chart(eq_df).mark_rule(...)

            chart = alt.layer(area, dd_area).properties(height=300)
            st.altair_chart(chart, use_container_width=True)

            # 最大回撤指标
            st.caption(f"📉 最大回撤: {max_dd:.1%}  "
                       f"(从 ¥{eq_df['balance'].max():,.0f} 至 ¥{eq_df['balance'].min():,.0f})")
        else:
            render_empty_state("暂无权益数据", "投注结算后将显示权益曲线。")

        # 最大连胜/连败
        if streaks["max_win_streak"] > 1 or streaks["max_loss_streak"] > 1:
            cols = st.columns(4)
            cols[0].metric("最长连胜", f"{streaks['max_win_streak']}")
            cols[1].metric("最长连败", f"{streaks['max_loss_streak']}")
            cols[2].metric("当前", f"{streaks['current_streak']} {'连胜' if streaks['streak_type']=='win' else '连败'}" if streaks['current_streak'] > 1 else "—")

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

        # 日/周/月 P&L
        if not bet_history.empty and "profit" in bet_history.columns and "date" in bet_history.columns:
            try:
                bet_history["_date"] = pd.to_datetime(bet_history["date"], errors="coerce")
                today = pd.Timestamp.now()
                pnl_daily = bet_history[bet_history["_date"].dt.date == today.date()]["profit"].sum()
                pnl_weekly = bet_history[bet_history["_date"] >= today - pd.Timedelta(days=7)]["profit"].sum()
                pnl_monthly = bet_history[bet_history["_date"] >= today - pd.Timedelta(days=30)]["profit"].sum()
                st.caption(f"📅 日P&L: ¥{pnl_daily:+,.0f}  |  周P&L: ¥{pnl_weekly:+,.0f}  |  月P&L: ¥{pnl_monthly:+,.0f}")
            except Exception:
                pass

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

    # ── 组合业绩归因 ──
    attr = load_json(ATTRIBUTION_FILE)
    if attr and attr.get("n_settled", 0) > 0:
        st.divider()
        st.subheader("📊 业绩归因")
        tabs = st.tabs(["按运动", "按市场", "按联赛", "运动×市场"])

        with tabs[0]:
            _render_attr_table(attr.get("by_sport", {}), "运动")

        with tabs[1]:
            _render_attr_table(attr.get("by_market", {}), "市场")

        with tabs[2]:
            _render_attr_table(attr.get("by_league", {}), "联赛")

        with tabs[3]:
            _render_attr_cross(attr.get("by_sport_market", {}))

        overall = attr.get("overall", {})
        st.caption(f"全局: {overall.get('bets',0)} 场 | "
                   f"胜 {overall.get('wins',0)} / 负 {overall.get('losses',0)} | "
                   f"胜率 {overall.get('win_rate',0):.1%}")

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

    # ── Bankroll Simulation ──
    _render_bankroll_simulation()
