"""系统健康页面 — 模型状态、API连通性、风控检查。"""
import streamlit as st

from src.dashboard.components.data_loader import (
    load_json, load_csv, data_exists, render_empty_state,
)
from src.dashboard.config import SYSTEM_HEALTH_FILE, BACKTEST_FILE, PRED_LOG_FILE


def render():
    st.header("🔋 系统健康")

    health = load_json(SYSTEM_HEALTH_FILE)

    if not health:
        render_empty_state("暂无健康报告", "运行 `python src/monitor/health_check.py` 生成报告。")
        return

    col1, col2, col3, col4 = st.columns(4)

    # ── 模型健康 ──
    with col1:
        st.subheader("🤖 模型")
        model_health = health.get("model_health", {})
        if model_health:
            days_since = model_health.get("days_since_train", "N/A")
            st.metric("距上次训练", f"{days_since:.0f} 天" if isinstance(days_since, (int, float)) else str(days_since))
            needs = model_health.get("needs_retrain", False)
            st.warning("需要重训") if needs else st.success("状态正常")
        else:
            st.caption("模型健康数据不可用。")

        # 回测摘要
        bt = load_json(BACKTEST_FILE)
        if bt and "report" in bt:
            st.caption(f"回测报告: {len(bt['report'])} 个模型")

    # ── 风控健康 ──
    with col2:
        st.subheader("🛡️ 风控")
        risk = health.get("risk_health", {})
        if risk:
            balance = risk.get("balance", 0)
            roi = risk.get("roi", 0)
            drawdown = risk.get("drawdown", 0)
            st.metric("资金", f"¥{balance:.0f}")
            st.metric("ROI", f"{roi:+.2%}" if isinstance(roi, float) else "N/A")
            st.metric("回撤", f"{drawdown:.1%}" if isinstance(drawdown, float) else "N/A")
            consecutive = risk.get("consecutive_losses", 0)
            if consecutive >= 3:
                st.error(f"连败 {consecutive} 场")
            elif consecutive >= 1:
                st.warning(f"连败 {consecutive} 场")
            else:
                st.success("状态正常")
        else:
            st.caption("风控数据不可用。")

    # ── 业绩健康 ──
    with col3:
        st.subheader("📊 业绩")
        perf = health.get("performance_health", {})
        if perf:
            total = perf.get("total_bets", 0)
            win_rate = perf.get("win_rate", 0)
            roi = perf.get("roi", 0)
            st.metric("总投注", str(total))
            st.metric("胜率", f"{win_rate:.1%}" if isinstance(win_rate, float) else "N/A")
            st.metric("ROI", f"{roi:+.2%}" if isinstance(roi, float) else "N/A")
        else:
            st.caption("业绩数据不可用，运行预测流水线后生成。")

        # 待结算
        pred_df = load_csv(PRED_LOG_FILE)
        if not pred_df.empty and "status" in pred_df.columns:
            pending = len(pred_df[pred_df["status"] == "pending"])
            st.caption(f"待结算: {pending} 单")

    # ── API 连通性 ──
    with col4:
        st.subheader("🌐 API")
        api = health.get("api_connectivity", {})
        if api:
            for name, status in api.items():
                if status:
                    st.success(f"✅ {name}")
                else:
                    st.error(f"❌ {name}")
        else:
            st.caption("API 连通性数据不可用。")

    # ── 回测结果 ──
    st.subheader("回测结果")
    bt = load_json(BACKTEST_FILE)
    if bt and "report" in bt:
        for model_result in bt["report"]:
            with st.expander(f"{model_result.get('model', '?')} — {model_result.get('target', '?')}"):
                cols = st.columns(3)
                overall = model_result.get("overall", {})
                cols[0].metric("Brier", f"{overall.get('brier', 0):.4f}")
                cols[1].metric("准确率", f"{overall.get('accuracy', 0):.3f}")
                cols[2].metric("F1", f"{overall.get('f1_score', 0):.3f}")

                train = model_result.get("train", {})
                test = model_result.get("test", {})
                st.caption(f"训练集: Brier={train.get('brier', 0):.4f}, "
                           f"测试集: Brier={test.get('brier', 0):.4f}, "
                           f"时序分割: {model_result.get('chronological_split', False)}")
    else:
        st.info("📭 暂无回测结果。")
