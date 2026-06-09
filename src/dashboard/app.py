"""SportsBettingPro Dashboard — Streamlit monitoring panel.

Usage:
    cd SportsBettingPro && streamlit run src/dashboard/app.py
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from src.dashboard.config import config
from src.dashboard.pages import (
    overview, recommendations, portfolio, model_performance,
    arbitrage, clv_analysis,
    line_movement, power_ratings, system_health,
    model_interpret, calibration, prediction_log,
)

st.set_page_config(
    page_title=config.page_title,
    page_icon=config.page_icon,
    layout=config.layout,
)

# ── 侧边栏导航 ──
PAGES = {
    "📊 总览": overview,
    "💰 投资组合": portfolio,
    "📋 推荐": recommendations,
    "🧠 模型表现": model_performance,
    "🎯 校准可靠性": calibration,
    "🔄 套利监控": arbitrage,
    "🎯 CLV 分析": clv_analysis,
    "📈 盘口变动": line_movement,
    "🏋️ 实力评分": power_ratings,
    "🔬 模型解释": model_interpret,
    "🔋 系统健康": system_health,
    "📄 预测记录": prediction_log,
}

st.sidebar.title("SportsBettingPro")

# 上次更新提示
last_update = "—"
for p in [_ROOT / "data/storage/daily_bb_recommendations.json",
          _ROOT / "data/storage/daily_fb_recommendations.json"]:
    if p.exists():
        try:
            import json
            data = json.loads(p.read_text())
            lu = data.get("date", "")
            if lu:
                last_update = lu[:19].replace("T", " ")
        except Exception:
            pass

st.sidebar.caption(f"📅 数据更新: {last_update}")

selected = st.sidebar.radio("导航", list(PAGES.keys()))

# 自动刷新
auto_refresh = st.sidebar.checkbox("自动刷新", value=False,
                                   help=f"每 {config.refresh_interval} 秒刷新一次")
if auto_refresh:
    st.sidebar.info(f"🔄 {config.refresh_interval}s 后自动刷新")
    st.rerun()

st.sidebar.divider()
st.sidebar.caption("v2.1 | 数据: The Odds API + BSD + ESPN")
st.sidebar.caption("预测 + 风控 + 监控 一体化")

# ── 渲染选中页面 ──
page = PAGES[selected]
if not hasattr(page, 'render'):
    import importlib
    page = importlib.reload(page)
page.render()
