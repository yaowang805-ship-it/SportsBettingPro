"""模拟交易面板 — 就绪评估 + 业绩监控 + 权益曲线。

数据源: data/storage/paper_trading.json (由 PaperTrader.refresh() 生成)
"""
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st
import numpy as np

from src.dashboard.components.data_loader import load_json, render_empty_state
from src.dashboard.config import PAPER_TRADING_FILE


def _render_readiness_banner(rd: dict):
    """GO / NO-GO 醒目横幅。"""
    ready = rd.get("ready", False)
    if ready:
        st.success(
            "✅ **GO: 就绪！** 所有 7 项检查全部通过，可以启用自动线上交易。"
            if rd.get("ready_since") else ""
        )
        if rd.get("ready_since"):
            since = rd["ready_since"][:10]
            st.success(f"✅ **GO: 就绪！** 自 {since} 起持续达标，所有 7 项检查通过。")
    else:
        failed = [k for k, v in rd.get("checks", {}).items() if not v["passed"]]
        msg = f"❌ **NO-GO: 暂不可用** — {len(failed)} 项检查未通过"
        st.error(msg)


def _render_check_table(checks: dict):
    """7 项就绪检查表。"""
    if not checks:
        return
    rows = []
    for ck, cv in checks.items():
        actual = cv.get("actual")
        if isinstance(actual, float):
            actual_str = f"{actual:.4f}" if abs(actual) < 1 else f"{actual:.2f}"
        else:
            actual_str = str(actual) if actual is not None else "N/A"
        rows.append({
            "检查项": {
                "min_bets": "最小样本量",
                "win_rate": "胜率显著性",
                "positive_roi": "正 ROI",
                "positive_clv": "正向 CLV",
                "max_drawdown": "最大回撤",
                "sharpe_ratio": "夏普比率",
                "stability": "稳定期",
            }.get(ck, ck),
            "状态": "✅" if cv["passed"] else "❌",
            "当前值": actual_str,
            "要求": str(cv.get("required", "")),
            "详情": cv.get("detail", ""),
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True,
                 column_config={
                     "检查项": st.column_config.TextColumn(width="small"),
                     "状态": st.column_config.TextColumn(width="small"),
                     "当前值": st.column_config.TextColumn(width="small"),
                     "要求": st.column_config.TextColumn(width="medium"),
                     "详情": st.column_config.TextColumn(width="medium"),
                 })


def render():
    st.header("📜 模拟交易评估")

    state = load_json(PAPER_TRADING_FILE)
    if not state or not state.get("total_bets", 0) > 0:
        render_empty_state("暂无模拟交易数据",
                           "运行每日流水线后系统会自动采集虚拟投注数据。\n"
                           "手动触发: `python3 -c \"from src.betting.paper_trader import PaperTrader; PaperTrader().print_report()\"`")
        return

    rd = state.get("readiness", {})

    # ── GO/NO-GO 横幅 ──
    _render_readiness_banner(rd)

    # ── KPI 行 ──
    cols = st.columns(8)
    profit = state.get("total_profit", 0)
    cols[0].metric("💰 余额", f"¥{state.get('current_bankroll', 0):,.0f}")
    cols[1].metric("📈 总盈亏", f"¥{profit:+,.0f}", delta=profit)
    cols[2].metric("🎯 胜率", f"{state.get('win_rate', 0):.1%}" if state.get("settled_bets", 0) > 0 else "N/A")
    cols[3].metric("📊 ROI", f"{state.get('roi', 0):+.2%}")
    cols[4].metric("📉 最大回撤", f"{state.get('max_drawdown', 0):.1%}")
    sharpe = state.get("sharpe_ratio")
    cols[5].metric("夏普比率", f"{sharpe:.2f}" if sharpe is not None else "N/A",
                   delta="达标" if sharpe and sharpe > 0.5 else None)
    cols[6].metric("已结算", str(state.get("settled_bets", 0)))
    cols[7].metric("⏳ 待结算", str(state.get("pending_bets", 0)))

    # ── 就绪检查表 ──
    st.divider()
    st.subheader("就绪检查 (7 项)")
    _render_check_table(rd.get("checks", {}))

    # ── 中文建议 ──
    rec = rd.get("recommendation_cn", "")
    if rec:
        st.caption(f"📋 评估建议: {rec}")

    # ── 权益曲线 + 快照历史 ──
    col_l, col_r = st.columns([3, 2])

    with col_l:
        st.subheader("权益曲线")
        equity = state.get("equity_curve", [])
        if len(equity) >= 2:
            eq_df = pd.DataFrame(equity)
            eq_df["date"] = pd.to_datetime(eq_df["date"], errors="coerce")
            eq_df["drawdown"] = _compute_drawdown(eq_df["balance"])
            max_dd = eq_df["drawdown"].min()

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

            chart = alt.layer(area, dd_area).properties(height=300)
            st.altair_chart(chart, use_container_width=True)

            st.caption(f"📉 最大回撤: {max_dd:.1%}  "
                       f"(从 ¥{eq_df['balance'].max():,.0f} 至 ¥{eq_df['balance'].min():,.0f})")
        else:
            st.caption("权益曲线需要至少 2 个数据点。")

    with col_r:
        st.subheader("快照历史趋势")
        snapshots = state.get("snapshot_history", [])
        if len(snapshots) >= 2:
            snap_df = pd.DataFrame(snapshots)
            snap_df["date"] = pd.to_datetime(snap_df["date"], errors="coerce")

            base = alt.Chart(snap_df).encode(x=alt.X("date:T", title=""))

            line_roi = base.mark_line(color="#FFB300", point=False).encode(
                y=alt.Y("roi:Q", title="ROI", scale=alt.Scale(zero=False))
            )
            line_settled = base.mark_line(color="#42A5F5", strokeDash=[4, 4]).encode(
                y=alt.Y("settled:Q", title="已结算", scale=alt.Scale(zero=False))
            )
            chart = alt.layer(line_roi, line_settled).resolve_scale(
                y="independent"
            ).properties(height=200)
            st.altair_chart(chart, use_container_width=True)
        else:
            st.caption("快照需累计至少 2 个周期。")

    # ── 按运动拆分 ──
    st.divider()
    st.subheader("按运动拆分")
    by_sport = state.get("by_sport", {})
    if by_sport:
        rows = []
        for sk, sv in sorted(by_sport.items()):
            rows.append({
                "运动": sk,
                "总注": sv["bets"],
                "已结算": sv["settled"],
                "胜/负": f"{sv.get('win_count',0)}W/{sv.get('loss_count',0)}L",
                "胜率": f"{sv['win_rate']:.1%}" if sv.get("win_rate") is not None else "N/A",
                "利润": f"¥{sv['total_profit']:+,.0f}" if abs(sv.get("total_profit", 0)) >= 0.5 else "¥0",
                "ROI": f"{sv['roi']:.1%}" if sv.get("roi") is not None else "N/A",
                "CLV": f"{sv['avg_clv']:.1%}" if sv.get("avg_clv") is not None else "N/A",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # 运动胜率柱状图
        chart_rows = [r for r in rows if r["胜率"] != "N/A"]
        if len(chart_rows) > 1:
            chart_df = pd.DataFrame(chart_rows)
            chart_df["胜率_val"] = chart_df["胜率"].str.rstrip("%").astype(float) / 100
            c = alt.Chart(chart_df).mark_bar().encode(
                x=alt.X("运动:N", sort=None),
                y=alt.Y("胜率_val:Q", scale=alt.Scale(domain=[0, 1])),
                color=alt.Color("胜率_val:Q", scale=alt.Scale(scheme="greenorange")),
            ).properties(height=200)
            st.altair_chart(c, use_container_width=True)
    else:
        st.info("暂无按运动拆分的数据。")

    # ── 时间窗口表现 ──
    st.divider()
    st.subheader("时间窗口表现")
    tiers = state.get("metrics_by_tier", {})
    if tiers:
        tier_rows = []
        for label_key, data_key in [("最近 7 天", "last_7_days"),
                                     ("最近 30 天", "last_30_days"),
                                     ("全部", "all_time")]:
            td = tiers.get(data_key, {})
            tier_rows.append({
                "周期": label_key,
                "注单数": td.get("bets", 0),
                "胜率": f"{td['win_rate']:.1%}" if td.get("win_rate") is not None else "N/A",
                "ROI": f"{td['roi']:.1%}" if td.get("roi") is not None else "N/A",
                "利润": f"¥{td['profit']:+,.0f}" if td.get("profit") is not None else "N/A",
            })
        st.dataframe(pd.DataFrame(tier_rows), use_container_width=True, hide_index=True)
    else:
        st.info("暂无时间窗口数据。")

    # ── CLV 分析 ──
    st.divider()
    st.subheader("CLV 分析")
    avg_clv = state.get("avg_clv")
    pos_clv_rate = state.get("positive_clv_rate")
    if avg_clv is not None:
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("平均 CLV", f"{avg_clv:+.4f}")
        col2.metric("正向 CLV 率", f"{pos_clv_rate:.0%}" if pos_clv_rate else "N/A")
        col3.metric("平均赔率", f"{state.get('avg_odds', 0):.2f}")
        col4.metric("平均 EV", f"{state.get('avg_ev', 0):+.2%}" if state.get("avg_ev") else "N/A")
        tag = "✅ 系统具有真实优势" if avg_clv > 0 else "⚠️ 系统可能靠运气"
        st.caption(f"解读: {tag}")
    else:
        st.info("暂无 CLV 数据 — 投注结算后系统会自动追踪收盘价。")

    # ── 风险指标 ──
    st.divider()
    st.subheader("风险指标")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("最大回撤", f"{state.get('max_drawdown', 0):.1%}")
    col2.metric("夏普(年化)", f"{state.get('sharpe_ratio', 0):.2f}" if state.get("sharpe_ratio") else "N/A")
    col3.metric("Sortino(年化)", f"{state.get('sortino_ratio', 0):.2f}" if state.get("sortino_ratio") else "N/A")
    col4.metric("最大连败", str(state.get("max_consecutive_losses", 0)))

    var_95 = state.get("var_95")
    cvar_95 = state.get("cvar_95")
    if var_95 is not None:
        st.caption(f"VaR(95%): ¥{var_95:,.0f}  |  CVaR(95%): ¥{cvar_95:,.0f}" if cvar_95 else f"VaR(95%): ¥{var_95:,.0f}")

    # ── 底部摘要 ──
    st.divider()
    st.caption(
        f"更新: {state.get('last_updated', 'N/A')[:19]}  |  "
        f"初始资金: ¥{state.get('initial_bankroll', 0):,.0f}  |  "
        f"当前资金: ¥{state.get('current_bankroll', 0):,.0f}  |  "
        f"总投注: {state.get('total_bets', 0)}  |  "
        f"活跃天数: {state.get('total_days_active', 0)}"
    )


def _compute_drawdown(equity: pd.Series) -> pd.Series:
    running_max = equity.cummax()
    return (equity - running_max) / running_max
