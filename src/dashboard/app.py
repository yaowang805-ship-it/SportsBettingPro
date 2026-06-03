"""SportsBettingPro Dashboard — Streamlit monitoring panel.

Usage:
    cd SportsBettingPro && streamlit run src/dashboard/app.py
"""
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，支持从任意目录启动
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from src.dashboard.config import config
from src.dashboard.pages import (
    overview, recommendations, clv_analysis,
    line_movement, power_ratings, system_health,
    model_interpret,
)

st.set_page_config(
    page_title=config.page_title,
    page_icon=config.page_icon,
    layout=config.layout,
)

# ── 侧边栏导航 ──
PAGES = {
    "📊 总览": overview,
    "📋 推荐": recommendations,
    "🎯 CLV 分析": clv_analysis,
    "📈 盘口变动": line_movement,
    "🏋️ 实力评分": power_ratings,
    "🔬 模型解释": model_interpret,
    "🔋 系统健康": system_health,
}

st.sidebar.title("SportsBettingPro")
selected = st.sidebar.radio("导航", list(PAGES.keys()))

# 自动刷新开关
auto_refresh = st.sidebar.checkbox("自动刷新", value=False,
                                   help=f"每 {config.refresh_interval} 秒刷新一次")
if auto_refresh:
    st.sidebar.info(f"🔄 自动刷新中 ({config.refresh_interval}s)")
    st.rerun()

# 版本信息
st.sidebar.divider()
st.sidebar.caption("v2.0 | 数据来源: The Odds API")
st.sidebar.caption("预测 + 风控 + 监控 一体化")

# ── 渲染选中页面 ──
page = PAGES[selected]
# 确保模块加载最新代码（解决 Streamlit 热重载缓存问题）
if not hasattr(page, 'render'):
    import importlib
    page = importlib.reload(page)
page.render()
