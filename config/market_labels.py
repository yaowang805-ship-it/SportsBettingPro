"""盘口中英文标签统一映射。

2026-08-30 建: 之前中文映射分散在 bb_ev_push._MARKET_CN / clv_collector 的 label /
pinnacle_opportunities 的 SPECIAL_KEY_TO_MKT 三处, 口径不一且缺 ht_dc 等盘口。
统一收口到 SUB_MARKET_CN, 各处只引用不重复定义。新增盘口时只改这里。
"""

# 全量盘口 → 中文标签 (推送显示 / 日志 / 报告统一口径)
SUB_MARKET_CN = {
    # 主盘口
    "1x2": "独赢",
    "hc": "让球",
    "ou": "大小球",
    "ht": "上半场",
    "ht_hc": "上半场让球",
    "ht_ou": "上半场大小球",
    "ht_dc": "上半场双重机会",
    "dc": "双重机会",
    "dnb": "平局退款",
    "btts": "双边进球",
    "oe": "单/双",
    "corner": "角球",
    # 特殊盘口
    "htft": "半全场",
    "correct_score": "正确比分",
    "correct_score_ht": "上半场正确比分",
    "exact_goals_ht": "上半场精确进球",
    "total_goals_range": "总进球区间",
    "first_to_score": "先进球",
    "winning_margin": "净胜球",
}


def market_cn(sub_market: str) -> str:
    """盘口英文标识 → 中文标签 (未知返回原值)。"""
    return SUB_MARKET_CN.get(sub_market, sub_market)
