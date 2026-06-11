"""回测报告 — 模型级回测指标、稳定性分析、阈值优化。"""
import streamlit as st
import pandas as pd

from src.dashboard.components.data_loader import load_json, render_empty_state
from src.dashboard.config import BACKTEST_FILE


def _format_pct(v):
    return f"{v:.1%}" if isinstance(v, (int, float)) else "—"


def _format_num(v, decimals=4):
    return f"{v:.{decimals}f}" if isinstance(v, (int, float)) else "—"


def render():
    st.header("📊 回测报告")

    data = load_json(BACKTEST_FILE)

    if not data or "report" not in data or not data["report"]:
        render_empty_state("暂无回测数据",
                           "运行 `python src/backtest/backtest_runner.py` 生成回测报告。")
        return

    report = data["report"]
    updated = data.get("updated", "")
    if updated:
        st.caption(f"🕐 上次回测: {updated[:19].replace('T', ' ')}")

    # ── KPI 行 ──
    n_models = len(report)
    avg_acc = sum(r.get("test", {}).get("accuracy", 0) for r in report) / n_models if n_models else 0
    avg_brier = sum(r.get("test", {}).get("brier", 0) for r in report) / n_models if n_models else 0
    best_idx = max(range(n_models), key=lambda i: report[i].get("test", {}).get("accuracy", 0))
    best_name = f"{report[best_idx]['model']} ({report[best_idx]['target']})"

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("模型数", n_models)
    col2.metric("平均测试准确率", _format_pct(avg_acc))
    col3.metric("平均 Brier", _format_num(avg_brier))
    col4.metric("最佳模型", best_name)

    st.divider()

    # ── 模型对比表 ──
    st.subheader("模型对比")

    rows = []
    for r in report:
        test = r.get("test", {})
        bt = r.get("bootstrap_ci", {})
        acc_ci = bt.get("accuracy", {}) if bt else {}
        rows.append({
            "模型": f"{r['model'].replace('model_', '').replace('_ensemble.pkl', '')} ({r['target']})",
            "运动": r.get("dataset", ""),
            "测试样本": test.get("samples", 0),
            "准确率": _format_pct(test.get("accuracy")),
            "Brier": _format_num(test.get("brier")),
            "LogLoss": _format_num(test.get("logloss")),
            "精确率": _format_pct(test.get("precision")),
            "召回率": _format_pct(test.get("recall")),
            "F1": _format_num(test.get("f1_score")),
            "CI下限": _format_pct(acc_ci.get("ci_lower")),
            "CI上限": _format_pct(acc_ci.get("ci_upper")),
            "最优阈值": _format_num(r.get("optimal_threshold", {}).get("threshold"), 3),
        })

    df = pd.DataFrame(rows)

    def _highlight_best(val):
        """高亮最优值。"""
        return "color: #00BFA5; font-weight: bold"

    def _highlight_col(col):
        if col.name == "准确率":
            best = col.max()
            return ["background-color: #0a3d2e" if v == best else "" for v in col]
        if col.name == "Brier":
            best = col.min()
            return ["background-color: #0a3d2e" if v == best else "" for v in col]
        return [""] * len(col)

    styled = df.style.apply(_highlight_col)
    st.dataframe(styled, use_container_width=True, hide_index=True)

    st.divider()

    # ── Walk-Forward 稳定性 ──
    walk_reports = [r for r in report if r.get("walk_forward")]
    if walk_reports:
        st.subheader("🔄 Walk-Forward 稳定性")

        for r in walk_reports:
            wf = r["walk_forward"]
            avg_wf = next((w for w in wf if w.get("window") == "avg"), None)
            if avg_wf:
                label = f"{r['model'].replace('_ensemble.pkl', '')} ({r['target']})"
                cols = st.columns(4)
                cols[0].metric(f"{label} 平均准确率",
                               _format_pct(avg_wf.get("avg_accuracy")))
                with cols[1]:
                    st.metric("标准差",
                              _format_pct(avg_wf.get("std_accuracy")))
                with cols[2]:
                    st.metric("最低",
                              _format_pct(avg_wf.get("min_accuracy")))
                with cols[3]:
                    st.metric("最高",
                              _format_pct(avg_wf.get("max_accuracy")))

                # 窗口趋势折线图
                windows = [w for w in wf if w.get("window") != "avg"]
                if windows:
                    wf_df = pd.DataFrame(windows)
                    if "accuracy" in wf_df.columns:
                        import altair as alt
                        wf_df["window_label"] = wf_df["window"].astype(str)
                        chart = alt.Chart(wf_df).mark_line(point=True, color="#00BFA5").encode(
                            x=alt.X("window_label:O", title="窗口"),
                            y=alt.Y("accuracy:Q", scale=alt.Scale(zero=False), title="准确率"),
                            tooltip=["window_label", "accuracy", "brier"],
                        ).properties(height=150)
                        st.altair_chart(chart, use_container_width=True)

    st.divider()

    # ── 最优阈值分析 ──
    st.subheader("🎯 最优阈值分析")
    thresh_rows = []
    for r in report:
        ot = r.get("optimal_threshold", {})
        if ot and "threshold" in ot:
            thresh_rows.append({
                "模型": f"{r['model'].replace('_ensemble.pkl', '')} ({r['target']})",
                "最优阈值": _format_num(ot.get("threshold"), 3),
                "测试准确率": _format_pct(ot.get("test_accuracy")),
                "净利润": _format_num(ot.get("net_profit", 0), 1),
            })

    if thresh_rows:
        st.dataframe(pd.DataFrame(thresh_rows), use_container_width=True, hide_index=True)
        st.caption("阈值搜索范围 [0.35, 0.75]，以训练集等额定注利润最大化为目标（赔率 1.91）。")
    else:
        st.caption("暂无阈值分析数据。")

    st.divider()

    # ── Bootstrap 置信区间 ──
    st.subheader("📈 Bootstrap 置信区间 (1000次)")
    ci_rows = []
    for r in report:
        bt = r.get("bootstrap_ci", {})
        if bt and bt.get("accuracy"):
            ci_rows.append({
                "模型": f"{r['model'].replace('_ensemble.pkl', '')} ({r['target']})",
                "准确率均值": _format_pct(bt["accuracy"].get("mean")),
                "标准差": _format_num(bt["accuracy"].get("std")),
                "CI 95% 下限": _format_pct(bt["accuracy"].get("ci_lower")),
                "CI 95% 上限": _format_pct(bt["accuracy"].get("ci_upper")),
                "Brier均值": _format_num(bt.get("brier", {}).get("mean")),
            })

    if ci_rows:
        st.dataframe(pd.DataFrame(ci_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("暂无 Bootstrap 数据。")

    st.divider()

    # ── 交易成本分析 ──
    st.subheader("💰 交易成本模拟")
    cost_rows = []
    for r in report:
        tc = r.get("transaction_cost", {})
        if tc:
            cost_rows.append({
                "模型": f"{r['model'].replace('_ensemble.pkl', '')} ({r['target']})",
                "盘口类型": r.get("market_type", ""),
                "平均Edge损失": _format_pct(tc.get("avg_edge_loss")),
                "滑点费率": _format_pct(tc.get("base_slippage")),
                "成本率": _format_pct(tc.get("cost_rate")),
            })

    if cost_rows:
        st.dataframe(pd.DataFrame(cost_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("暂无交易成本数据。")

    st.divider()

    # ── 投资组合回测（如有） ──
    if "portfolio_backtest" in data:
        st.subheader("💰 投资组合回测")
        pb = data["portfolio_backtest"]
        cols = st.columns(4)
        cols[0].metric("Sharpe", _format_num(pb.get("sharpe"), 3))
        cols[1].metric("Sortino", _format_num(pb.get("sortino"), 3))
        cols[2].metric("最大回撤", _format_pct(pb.get("max_drawdown")))
        cols[3].metric("总收益", _format_pct(pb.get("total_return")))

        strategy_a = pb.get("strategy_a_sequential", {})
        strategy_b = pb.get("strategy_b_portfolio_optimized", {})
        if strategy_a or strategy_b:
            cmp = pd.DataFrame([
                {"策略": "顺序Kelly", **strategy_a} if strategy_a else {},
                {"策略": "组合Kelly优化", **strategy_b} if strategy_b else {},
            ])
            if not cmp.empty:
                st.dataframe(cmp, use_container_width=True, hide_index=True)

    st.divider()
    st.caption("数据来源: model_backtest_summary.json，由 backtest_runner.py 生成。")
