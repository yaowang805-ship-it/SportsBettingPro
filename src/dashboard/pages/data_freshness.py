"""数据新鲜度面板 — 数据源更新状态与时效性监控。"""
import os
import time
from datetime import datetime
from pathlib import Path

import streamlit as st
import pandas as pd

from src.dashboard.config import DATA_DIR, SNAPSHOT_DIR, MODEL_DIR
from src.dashboard.components.data_loader import render_empty_state

ROOT = DATA_DIR.parent


def _file_age(path: Path) -> float:
    """文件距今秒数。"""
    if not path.exists():
        return -1
    return time.time() - os.path.getmtime(str(path))


def _fmt_age(seconds: float) -> str:
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _fmt_time(path: Path) -> str:
    if not path.exists():
        return "—"
    ts = os.path.getmtime(str(path))
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _status(age_seconds: float, warn_h: float, stale_h: float):
    """根据时效区间返回状态标签与颜色。"""
    if age_seconds < 0:
        return "❓ 未知", "#888"
    if age_seconds < warn_h * 3600:
        return "✅ 正常", "#00BFA5"
    if age_seconds < stale_h * 3600:
        return "⚠️ 偏旧", "#FFA726"
    return "🔴 过期", "#FF5252"


# ── 数据源定义 ─────────────────────────────────────────────────
# (label, path, warn_hours, stale_hours, category)
SOURCES = [
    # ── 行情数据 ──
    ("NBA赔率", DATA_DIR / "odds" / "live_basketball_nba_odds.json", 4, 12, "📡 行情数据"),
    ("足球赔率", DATA_DIR / "odds" / "live_football_all_odds.json", 4, 12, "📡 行情数据"),
    ("NFL赔率", DATA_DIR / "odds" / "live_americanfootball_nfl_odds.json", 6, 24, "📡 行情数据"),

    # ── 预测输出 ──
    ("BB推荐", DATA_DIR / "daily_bb_recommendations.json", 6, 24, "🤖 模型输出"),
    ("FB推荐", DATA_DIR / "daily_fb_recommendations.json", 6, 24, "🤖 模型输出"),
    ("NFL推荐", DATA_DIR / "daily_nfl_recommendations.json", 12, 48, "🤖 模型输出"),
    ("市场效率", DATA_DIR / "market_efficiency.json", 12, 48, "🤖 模型输出"),
    ("校准数据", DATA_DIR / "calibration_data.csv", 12, 48, "🤖 模型输出"),

    # ── 系统文件 ──
    ("系统健康", DATA_DIR / "system_health.json", 12, 36, "⚙️ 系统状态"),
    ("CLV报告", DATA_DIR / "clv_report.json", 12, 48, "⚙️ 系统状态"),
    ("NBA实力评分", DATA_DIR / "nba_power_ratings.json", 48, 168, "⚙️ 系统状态"),
    ("足球实力评分", DATA_DIR / "fb_power_ratings.json", 48, 168, "⚙️ 系统状态"),
    ("模型元数据", MODEL_DIR / "model_metadata.json", 72, 240, "⚙️ 系统状态"),
    ("模型衰减报告", DATA_DIR / "model_decay_report.json", 24, 72, "⚙️ 系统状态"),

    # ── 投注数据 ──
    ("投资组合", DATA_DIR / "virtual_portfolio.json", 6, 24, "💰 投注数据"),
    ("预测记录", DATA_DIR / "prediction_log.csv", 6, 24, "💰 投注数据"),
    ("绩效归因", DATA_DIR / "performance_attribution.json", 24, 72, "💰 投注数据"),
    ("CLV追踪", DATA_DIR / "clv_report.json", 12, 48, "💰 投注数据"),

    # ── 结算数据 ──
    ("ESPN同步", DATA_DIR / ".espn_last_sync", 12, 36, "🏁 结算数据"),
    ("盘口快照", SNAPSHOT_DIR / "last_snapshot.json", 6, 24, "🏁 结算数据"),
]


def render():
    st.header("📅 数据新鲜度")

    # 构建 DataFrame
    rows = []
    for label, path, warn_h, stale_h, category in SOURCES:
        age = _file_age(path)
        status_icon, color = _status(age, warn_h, stale_h)
        rows.append({
            "类别": category,
            "数据源": label,
            "文件": path.name,
            "修改时间": _fmt_time(path),
            "距今": _fmt_age(age),
            "状态": status_icon,
        })

    df = pd.DataFrame(rows)

    if df.empty:
        render_empty_state("暂无数据", "运行数据流水线后生成新鲜度报告。")
        return

    # ── 汇总 KPI ──
    total = len(df)
    ok = df["状态"].str.startswith("✅").sum()
    warn = df["状态"].str.startswith("⚠️").sum()
    stale = df["状态"].str.startswith("🔴").sum()
    unknown = df["状态"].str.startswith("❓").sum()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("数据源总数", total)
    col2.metric("✅ 正常", ok)
    col3.metric("⚠️ 偏旧", warn)
    col4.metric("🔴 过期", stale)
    col5.metric("❓ 未知", unknown)

    st.divider()

    # ── 按类别分组 ──
    for cat in df["类别"].unique():
        sub = df[df["类别"] == cat].drop(columns=["类别"])
        st.subheader(cat)
        # 高亮过期行
        def _row_style(row):
            if "🔴" in str(row["状态"]):
                return ["background-color: #3d0000"] * len(row)
            if "⚠️" in str(row["状态"]):
                return ["background-color: #3d2e00"] * len(row)
            return [""] * len(row)

        styled = sub.style.apply(_row_style, axis=1)
        st.dataframe(styled, use_container_width=True, hide_index=True,
                     column_config={
                         "数据源": st.column_config.TextColumn("数据源", width="small"),
                         "文件": st.column_config.TextColumn("文件", width="medium"),
                         "修改时间": st.column_config.TextColumn("修改时间", width="small"),
                         "距今": st.column_config.TextColumn("距今", width="small"),
                         "状态": st.column_config.TextColumn("状态", width="small"),
                     })
        st.caption("")

    st.divider()

    # ── 数据流水线状态 ──
    st.subheader("⏱️ 流水线概览")
    st.markdown("""
    | 流水线 | 周期 | 说明 |
    |---|---|---|
    | 行情同步 | 每 4h | 通过 Odds API / BSD 获取最新赔率 |
    | 每日预测 | 每 24h | 运行特征工程 → 模型推理 → 推荐输出 |
    | 自动结算 | 每 30min | 匹配 ESPN 赛果并结算待定投注 |
    | CLV追踪 | 随推荐 | 记录开仓/收盘赔率偏差 |
    | 市场效率 | 每日 | 计算各联赛盘口的历史 Sharpe / 校准误差 |
    | 模型衰减 | 每日 | 监测模型性能是否显著下降 |
    """)
