"""推荐页面 — 自动虚拟投注组合 + 今日推荐。"""
import altair as alt
import pandas as pd
import streamlit as st

from src.dashboard.components.data_loader import (
    load_csv, render_empty_state, load_recommendations,
)
from src.dashboard.components.virtual_portfolio import (
    compute_portfolio, settle_bet, auto_place_bets, reset_portfolio, update_clv_for_pending,
)
from src.dashboard.components.team_cn import team_cn
from src.dashboard.config import PRED_LOG_FILE


def render():
    st.header("📋 今日推荐")

    pred_df = load_csv(PRED_LOG_FILE)

    # ── 加载推荐（合并多个来源，优先使用每日新鲜数据）──
    rec_list = load_recommendations()
    if rec_list:
        auto_place_bets(rec_list)

    # ── 虚拟投注组合 ──
    # 先更新 CLV（收盘价价值）
    updated_clv = update_clv_for_pending()
    portfolio = compute_portfolio(pred_df)
    clv = portfolio.get("clv_metrics", {})

    if st.button("🔄 重置虚拟组合"):
        reset_portfolio()
        st.rerun()

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("💰 虚拟余额", f"¥{portfolio['balance']:.0f}")
    col2.metric("📈 总ROI", f"{portfolio['total_roi']:+.2%}")
    col3.metric("🎯 胜率", f"{portfolio['win_rate']:.1%}" if portfolio['total_settled'] > 0 else "N/A")
    col4.metric("📊 总投注", str(portfolio['total_bets']))
    col5.metric("⏳ 待结算", str(portfolio['pending_count']))
    if clv.get("avg_clv") is not None:
        clv_str = f"{clv['avg_clv']:+.2%}"
        clv_color = "normal" if clv['avg_clv'] > 0 else "inverse"
        col6.metric("🎯 平均 CLV", clv_str, delta=f"{clv['positive_clv']}/{clv['negative_clv']} 正/负",
                    delta_color=clv_color)
    else:
        col6.metric("🎯 CLV", "暂无数据")

    if updated_clv > 0:
        st.caption(f"已更新 {updated_clv} 笔投注的 CLV 数据")

    # ── 权益曲线 ──
    if portfolio['equity_curve']:
        eq_df = pd.DataFrame(portfolio['equity_curve'])
        eq_df['date'] = pd.to_datetime(eq_df['date'])
        chart = (
            alt.Chart(eq_df)
            .mark_line(color="#00BFA5", strokeWidth=2)
            .encode(
                x=alt.X("date:T", title="日期"),
                y=alt.Y("balance:Q", title="资金 (¥)"),
                tooltip=["date:T", "balance:Q"],
            )
            .properties(height=250)
        )
        st.altair_chart(chart, use_container_width=True)

    st.divider()

    # ── 待结算投注（自动创建，手动标记结果） ──
    pending_list = portfolio.get('pending_bets', [])
    if pending_list:
        st.subheader(f"待结算投注（{len(pending_list)} 笔）")
        for bet in pending_list:
            home = bet.get('home_cn', bet.get('home_team', '?'))
            away = bet.get('away_cn', bet.get('away_team', '?'))
            league = bet.get('league', '')
            market = bet.get('market_detail', bet.get('market_type', ''))
            odds = bet.get('odds', 0)
            stake = bet.get('stake', 0)
            bid = bet.get('id', '')

            c1, c2, c3, c4, c5 = st.columns([3, 1, 1.5, 1, 1])
            c1.caption(f"{league} {home} vs {away} | {market}")
            c2.caption(f"赔率 {odds:.2f}")
            c3.caption(f"注额 ¥{stake:.0f}")
            if c4.button("✅ 赢", key=f"win_{bid}"):
                settle_bet(bid, "won", stake, odds)
                st.rerun()
            if c5.button("❌ 输", key=f"lose_{bid}"):
                settle_bet(bid, "lost", stake, odds)
                st.rerun()

    # ── 已结算记录 ──
    if portfolio['history']:
        st.subheader("已结算记录")
        hist_df = pd.DataFrame(portfolio['history'][-20:][::-1])
        display_cols = [c for c in ['match', 'stake', 'odds', 'profit', 'status'] if c in hist_df.columns]
        col_map = {'match': '比赛', 'stake': '注额', 'odds': '赔率', 'profit': '盈亏', 'status': '结果'}
        if display_cols:
            d = hist_df[display_cols].rename(columns={k: v for k, v in col_map.items() if k in display_cols})
            st.dataframe(d, use_container_width=True, hide_index=True)

    st.divider()

    # ── 今日推荐详情 ──
    if rec_list:
        st.subheader(f"今日推荐（{len(rec_list)} 条）")
        df = pd.DataFrame(rec_list)

        # 队名中文化（按项目区分）
        def _translate(row, col):
            name = str(row.get(col, ""))
            sport_type = 'nba' if str(row.get('sport', '')).upper() == 'NBA' else 'football'
            return team_cn(name, sport_type)

        if 'home_cn' in df.columns:
            df['home_cn'] = df.apply(lambda r: _translate(r, 'home_cn'), axis=1)
        if 'away_cn' in df.columns:
            df['away_cn'] = df.apply(lambda r: _translate(r, 'away_cn'), axis=1)

        # EV 条形图
        if "ev" in df.columns or "edge" in df.columns:
            ev_col = "ev" if "ev" in df.columns else "edge"
            chart = (
                alt.Chart(df)
                .mark_bar()
                .encode(
                    x=alt.X(f"{ev_col}:Q", title="期望值 (EV)"),
                    y=alt.Y("home_cn:N", sort="-x", title="比赛"),
                    color=alt.condition(
                        alt.datum[ev_col] > 0, alt.value("#00BFA5"), alt.value("#FF5252")
                    ),
                    tooltip=[
                        alt.Tooltip("home_cn:N", title="主队"),
                        alt.Tooltip("away_cn:N", title="客队"),
                        alt.Tooltip("market:N", title="市场"),
                        alt.Tooltip("odds:Q", title="赔率", format=".2f"),
                        alt.Tooltip("model_prob:Q", title="模型概率", format=".1%"),
                        alt.Tooltip("market_prob:Q", title="市场概率", format=".1%"),
                        alt.Tooltip(f"{ev_col}:Q", title="EV", format=".2%"),
                        alt.Tooltip("stake:Q", title="建议注额", format=".0f"),
                    ],
                )
                .properties(height=max(40 * len(df), 100))
            )
            st.altair_chart(chart, use_container_width=True)

        # 详情表格
        display_cols = [c for c in ["home_cn", "away_cn", "market", "odds", "model_prob",
                                     "market_prob", "edge", "stake"]
                        if c in df.columns]
        if display_cols:
            st.subheader("推荐详情")
            styled = df[display_cols].copy()
            col_map = {
                "home_cn": "主队", "away_cn": "客队", "market": "市场",
                "odds": "赔率", "model_prob": "模型概率", "market_prob": "市场概率",
                "edge": "EV", "stake": "建议注额",
            }
            styled = styled.rename(columns={k: v for k, v in col_map.items() if k in styled.columns})
            st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        render_empty_state("暂无推荐", "今日尚未生成推荐，请运行 `python main.py`。")
