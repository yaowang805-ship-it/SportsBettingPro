"""投注权重矩阵 V2 — 极细粒度数据驱动版

设计原则:
  1. 赔率区间极度细化: 足球 0.5步长, 网球 0.2步长(低赔), 篮球 0.5步长
  2. 全量数据驱动: Pinnacle 61,404场足球 + 5,013场网球 + 57,504场NBA + 219笔BB结算
  3. 每项权重 = f(Pinnacle 真实 vig, 赔率区间 ROI, BB/FB 溢价, 市场质量)
  4. 核心公式: stake% = base_weight × league_vig_mult × odds_roi_factor × market_quality

数据源:
  S级: Pinnacle 收盘赔率 × 赛果 (足球 61,404场/17联赛, 网球 5,013场/5赛事级, NBA 57,504场/15季)
  A级: BB 实际赔率分布 (1,697场比赛), 219笔真实结算
  B级: FB 赔率对比, the-odds-api 辅助数据
  C级: 公开研究 (棒球 OU > ML, MMA 映射错误率高)

使用方法:
  from config.weight_matrix_v2 import get_stake_pct
  stake_pct = get_stake_pct(sport="football", league="英超", sub_market="1x2", odds=2.50)
  # → 0.04 (4% of bankroll)
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

# =====================================================================
# 赔率区间定义 — 极度细化
# =====================================================================

# 足球赔率区间: 0.5步长 (核心 1.5-5.0), 1.0步长 (扩展 5.0-15.0), 跳跃 (>15.0)
FOOTBALL_ODDS_BINS = [
    # (max_odds, label)
    (1.3,  "1.01-1.30"),
    (1.5,  "1.30-1.50"),
    (1.8,  "1.50-1.80"),
    (2.0,  "1.80-2.00"),
    (2.2,  "2.00-2.20"),
    (2.5,  "2.20-2.50"),
    (2.8,  "2.50-2.80"),
    (3.0,  "2.80-3.00"),
    (3.3,  "3.00-3.30"),
    (3.5,  "3.30-3.50"),
    (3.8,  "3.50-3.80"),
    (4.0,  "3.80-4.00"),
    (4.3,  "4.00-4.30"),
    (4.5,  "4.30-4.50"),
    (4.8,  "4.50-4.80"),
    (5.0,  "4.80-5.00"),
    (5.5,  "5.00-5.50"),
    (6.0,  "5.50-6.00"),
    (7.0,  "6.00-7.00"),
    (8.0,  "7.00-8.00"),
    (9.0,  "8.00-9.00"),
    (10.0, "9.00-10.00"),
    (12.0, "10.00-12.00"),
    (15.0, "12.00-15.00"),
    (20.0, "15.00-20.00"),
    (999.0,">20.00"),
]

# 网球赔率区间: 极度细化 (低赔 0.1-0.2 步长，中赔 0.5 步长)
TENNIS_ODDS_BINS = [
    (1.15, "<1.15"),
    (1.25, "1.15-1.25"),
    (1.35, "1.25-1.35"),
    (1.50, "1.35-1.50"),
    (1.70, "1.50-1.70"),
    (1.90, "1.70-1.90"),
    (2.10, "1.90-2.10"),
    (2.50, "2.10-2.50"),
    (3.00, "2.50-3.00"),
    (4.00, "3.00-4.00"),
    (5.00, "4.00-5.00"),
    (6.00, "5.00-6.00"),
    (8.00, "6.00-8.00"),
    (10.00,"8.00-10.00"),
    (15.00,"10.00-15.00"),
    (999.0,">15.00"),
]

# 篮球赔率区间: 集中在 1.3-3.0, 超高赔几乎不存在
BASKETBALL_ODDS_BINS = [
    (1.3,  "<1.30"),
    (1.5,  "1.30-1.50"),
    (1.7,  "1.50-1.70"),
    (1.9,  "1.70-1.90"),
    (2.1,  "1.90-2.10"),
    (2.4,  "2.10-2.40"),
    (2.7,  "2.40-2.70"),
    (3.0,  "2.70-3.00"),
    (3.5,  "3.00-3.50"),
    (4.0,  "3.50-4.00"),
    (5.0,  "4.00-5.00"),
    (7.0,  "5.00-7.00"),
    (10.0, "7.00-10.00"),
    (999.0,">10.00"),
]


def _get_bin_index(odds: float, bins: list) -> int:
    """返回赔率在给定 bin 列表中的索引。"""
    for i, (max_val, _) in enumerate(bins):
        if odds < max_val:
            return i
    return len(bins) - 1


# =====================================================================
# 联赛 Vig 可靠性系数 (Pinnacle 61,404场真实数据)
# =====================================================================
# vig 越低 → Pinnacle 定价越准 → 公平价越可信 → 比价 EV 越真实 → 权重越高
# 系数公式: league_vig_mult = 2.5 / vig (2.5% vig → 1.0x baseline)
# 封顶: 0.35 ~ 1.50

FOOTBALL_LEAGUE_VIG = {
    # 极高可靠性 (vig<2.5%): Pinnacle 定价极准
    "德甲": {"vig": 1.88, "mult": 1.33},
    "德乙": {"vig": 1.93, "mult": 1.30},
    # 高可靠性 (2.5-3.5%)
    "英冠": {"vig": 3.25, "mult": 0.92},
    "法乙": {"vig": 3.30, "mult": 0.91},
    "英乙": {"vig": 3.32, "mult": 0.90},
    "意乙": {"vig": 3.57, "mult": 0.84},
    "英超": {"vig": 3.72, "mult": 0.81},
    # 中等 (3.5-4.5%)
    "荷甲": {"vig": 3.89, "mult": 0.77},
    "法甲": {"vig": 4.06, "mult": 0.74},
    "西乙": {"vig": 4.13, "mult": 0.73},
    "西甲": {"vig": 4.49, "mult": 0.67},
    "比甲": {"vig": 4.33, "mult": 0.69},
    "苏超": {"vig": 4.38, "mult": 0.68},
    # 较低可靠性 (4.5%+)
    "英甲": {"vig": 4.60, "mult": 0.65},
    "意甲": {"vig": 4.97, "mult": 0.60},
    "土超": {"vig": 5.53, "mult": 0.54},
    # 低可靠性 (>6%)
    "葡超": {"vig": 7.20, "mult": 0.42},
    "希超": {"vig": 7.41, "mult": 0.40},
}

# =====================================================================
# FOOTBALL 1X2 — 按联赛 × 赔率区间的权重矩阵
# =====================================================================
# 数据源: Pinnacle 39,493场收盘赔率 × 10区间 ROI + BB ~7% 溢价
# 权重 = f(PinROI + 7% BB溢价 > 0 → bet; 溢价越高 → 权重越高)
# 区间: 26 bins (0.5步长) × 17 联赛

# 赔率区间 ROI 模式 (基于 Pinnacle 历史): 不同联赛在不同赔率区间表现差异大
# 核心规律:
#   低赔 (<2.0): 普遍正期望 (Pin vig 可克服)
#   中赔 (2.0-4.0): 联赛差异最大, 五大联赛 OK, 低级别差
#   高赔 (4.0-7.0): 仅德甲/英超/英冠/意乙/法甲有正 ROI
#   超高 (>7.0): 仅五大联赛部分区间 OK

# 每个联赛的赔率区间权重 (0-6 → 仓位%)
# 区间映射: <1.3, 1.3-1.5, 1.5-1.8, 1.8-2.0, 2.0-2.2, 2.2-2.5, 2.5-2.8, 2.8-3.0,
#            3.0-3.3, 3.3-3.5, 3.5-3.8, 3.8-4.0, 4.0-4.3, 4.3-4.5, 4.5-4.8, 4.8-5.0,
#            5.0-5.5, 5.5-6.0, 6.0-7.0, 7.0-8.0, 8.0-9.0, 9.0-10.0,
#            10.0-12.0, 12.0-15.0, 15.0-20.0, >20.0

FB_1X2_WEIGHTS = {
    # ── 德甲 (vig=1.88%): Pinnacle 最准 → 所有区间最宽 ──
    "德甲":   [6, 6, 6, 6, 6, 5, 4, 4, 4, 4, 3, 3, 3, 2, 2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0],
    # ── 德乙 (vig=1.93%) ──
    "德乙":   [6, 6, 6, 5, 5, 4, 3, 3, 2, 2, 2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 英超 (vig=3.72%): 5-7 +10.6%!! ──
    "英超":   [5, 5, 4, 4, 3, 3, 3, 2, 2, 2, 2, 1, 3, 3, 3, 3, 4, 3, 2, 1, 1, 1, 1, 0, 0, 0],
    # ── 英冠 (5-7 +7.6%, 7-10 +10%!) ──
    "英冠":   [5, 5, 4, 4, 3, 3, 2, 2, 2, 2, 2, 2, 3, 3, 3, 2, 3, 3, 2, 2, 1, 1, 0, 0, 0, 0],
    # ── 英甲: 低赔最优, 高赔快速衰减 ──
    "英甲":   [5, 5, 4, 4, 3, 3, 2, 2, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 英乙 ──
    "英乙":   [4, 4, 4, 3, 3, 3, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 西甲: <2 OK, 2-2.5 OK, >10 +7.9%, 中段差 ──
    "西甲":   [5, 5, 4, 4, 3, 3, 2, 2, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1],
    # ── 西乙 ──
    "西乙":   [4, 4, 3, 3, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 意甲: <1.5 OK, >10 +5.2%, 中段差 ──
    "意甲":   [5, 5, 3, 3, 2, 2, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0],
    # ── 意乙: 3-3.5 +1.1%, 5-7 +9.6%!! ──
    "意乙":   [5, 5, 4, 3, 3, 2, 2, 2, 2, 2, 1, 0, 2, 2, 3, 3, 3, 2, 2, 0, 0, 0, 0, 0, 0, 0],
    # ── 法甲: >10 +28.7%!! ──
    "法甲":   [5, 5, 4, 4, 3, 3, 2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 3, 3],
    # ── 法乙 ──
    "法乙":   [5, 5, 4, 4, 3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 荷甲 ──
    "荷甲":   [4, 4, 3, 3, 2, 2, 3, 3, 3, 3, 3, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 葡超: 2.5-3最优 ──
    "葡超":   [4, 3, 3, 4, 4, 4, 5, 4, 3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 土超: <2 +4-7%!! ──
    "土超":   [5, 5, 5, 4, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 比甲 ──
    "比甲":   [4, 4, 3, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 苏超 ──
    "苏超":   [5, 5, 4, 4, 3, 3, 2, 2, 2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 希超: 3-3.5 +6.1%!! ──
    "希超":   [4, 4, 3, 3, 2, 2, 3, 3, 3, 3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    # ── 默认: 非欧洲联赛 ──
    "_DEFAULT": [4, 4, 3, 3, 3, 2, 2, 2, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}

# =====================================================================
# FOOTBALL OU (大小球) — 按联赛 × 赔率区间
# =====================================================================
# OU 赔率集中在 1.5-2.5, 高赔率几乎不存在
# Pinnacle OU vig 普遍低于 1X2 (3.87% vs 4.29%) → 比价更可靠

FB_OU_WEIGHTS = {
    "德甲":   [5, 5, 4, 4, 3, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "德乙":   [4, 4, 3, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "英超":   [4, 4, 3, 3, 4, 4, 5, 5, 4, 3, 2, 2, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "英冠":   [4, 4, 3, 2, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "英甲":   [4, 4, 3, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "英乙":   [4, 4, 3, 2, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "西甲":   [4, 3, 3, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "西乙":   [4, 3, 2, 2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "意甲":   [5, 4, 3, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "意乙":   [4, 3, 3, 2, 2, 3, 4, 4, 3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "法甲":   [4, 4, 3, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "法乙":   [4, 4, 3, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "荷甲":   [3, 3, 2, 2, 3, 3, 4, 4, 3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "葡超":   [4, 4, 3, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "土超":   [3, 3, 2, 2, 2, 3, 3, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "希超":   [3, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "_DEFAULT": [3, 3, 3, 2, 2, 2, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}

# =====================================================================
# FOOTBALL HT (半场 1X2) — 基于全场 1X2 调整
# =====================================================================
# 半场 1X2: Pinnacle 有 HT 数据但样本较少
# 策略: 全场 1X2 权重 × 0.85 (半场不确定性更高, 但 BB 溢价仍是真实的)
# 41笔结算: ROI -4%, 需要更严格的门槛

# HT 在半场独有的特征:
# - 低赔率信号更可靠（强队半场领先概率高于全场）
# - 高赔率噪声更大（半场爆冷概率高于全场）
# 调整: 低赔 <2.0 保持, 中赔 2.0-4.0 降 1 档, 高赔 >4.0 降 2 档

# =====================================================================
# FOOTBALL 特殊市场
# =====================================================================
# DC (双重机会): 从 1X2 推导, O退水率更高 → 严格折扣
#   8笔结算 全输 0% 胜率 → 封杀
# DNB (平局退款): BB vs Pinnacle 定义差异 → 高度保守
# BTTS (双边进球): 135笔 结算 ROI +12% → 有信号, 小注
# OE (单双): 无 Pinnacle 数据 → 保守
# HTFT (半全场): 4笔 全输, BB/Pin 定义不同 → 封杀
# Corner (角球): 无 Pinnacle 数据 → 保守

SPECIAL_MARKET_WEIGHTS = {
    "dc": 0,      # 封杀: 0% 胜率
    "htft": 0,    # 封杀: 定义不一致
    "dnb": {
        "max_stake_pct": 0.02,  # 最大 2%
        "odds_cap": 5.0,        # 5.0 以上跳过
    },
    "btts": {
        "max_stake_pct": 0.03,  # 最大 3%
        "odds_cap": 3.0,         # 3.0 以上跳过 (集中在 1.5-2.5)
    },
    "oe": {
        "max_stake_pct": 0.01,
        "odds_cap": 2.5,
    },
    "corner": {
        "max_stake_pct": 0.01,
        "odds_cap": 3.0,
    },
}

# =====================================================================
# TENNIS — 极度细化的赛事级别 × 赔率区间矩阵
# =====================================================================
# 数据源: Pinnacle 5,013场 × 7区间 (网球市场效率分析)
# 16 bins × 5 赛事级别 + Challenger/ITF

TENNIS_WEIGHTS = {
    # Masters 1000 (vig=1.80%): Pinnacle 最锐利的网球市场
    # <1.15: -3.7%, 1.15-1.25: -3.7%, 1.25-1.35: -3.7%, 1.35-1.50: -5.2%,
    # 1.50-1.70: -1.8%, 1.70-1.90: -1.8%, 1.90-2.10: -1.8%, 2.10-2.50: -3.4%,
    # 2.50-3.00: -3.4%, 3.00-4.00: +9.4%!!, 4.00-5.00: +9.4%!!,
    # 5.00-6.00: -14.4%, 6.00-8.00: -14.4%, 8.00-10.00: -14.4%, 10.00-15.00: +7.7%, >15: +7.7%
    "Masters":    [3, 3, 3, 3, 4, 4, 4, 3, 3, 6, 6, 0, 0, 0, 4, 4],

    # Grand Slam (vig=5.69%): 公众投注量大, 线被推偏
    # 仅低赔 1.15-1.50 正 ROI, 高赔全部负
    "Grand Slam": [5, 5, 5, 6, 4, 4, 3, 3, 2, 0, 0, 0, 0, 0, 0, 0],

    # ATP 500 (vig=5.34%): 仅低赔 OK
    "ATP 500":    [4, 4, 4, 3, 3, 3, 3, 3, 2, 1, 0, 0, 0, 0, 0, 0],

    # ATP 250 (vig=3.78%): 1.3-1.5 正 ROI, 5-10 正 ROI 但样本小
    "ATP 250":    [3, 3, 3, 4, 3, 3, 3, 2, 1, 0, 0, 4, 4, 4, 0, 0],

    # WTA: 保守, 参照 ATP 250 但降档 (数据更少)
    "WTA":        [2, 2, 2, 3, 2, 2, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0],

    # Challenger: 匹配质量差, 仅低赔率极小注
    "Challenger": [1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],

    # ITF: 匹配最差, 几乎不投
    "ITF":        [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "W15":        [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "M15":        [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "W25":        [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "M25":        [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "_DEFAULT":   [1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}

# 网球让盘 (HC): 双重 FLB (低赔方同时覆盖盘口) → 更保守
TENNIS_HC_WEIGHTS = {
    "Masters":    [2, 2, 2, 2, 2, 2, 2, 1, 1, 2, 2, 0, 0, 0, 0, 0],
    "Grand Slam": [2, 2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "ATP 500":    [2, 2, 2, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "ATP 250":    [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "WTA":        [1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "_DEFAULT":   [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}

# 网球大小分 (OU): Pinnacle 数据最少, 最保守
TENNIS_OU_WEIGHTS = {
    "Masters":    [2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0],
    "Grand Slam": [2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "_DEFAULT":   [1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}


# =====================================================================
# BASKETBALL — NBA 15季模型回测 (57,504场)
# =====================================================================
# 数据源: 模型回测 (不是 Pinnacle 收盘, 是模型 edge)
# 赔率集中在 1.5-3.0, 高赔几乎不存在

NBA_WEIGHTS = {
    "hc":  [6, 6, 6, 6, 5, 5, 4, 4, 3, 2, 1, 0, 0, 0],   # 让分: edge+2.2%/yr 最可靠
    "1x2": [6, 6, 6, 5, 5, 4, 4, 3, 2, 1, 0, 0, 0, 0],   # 胜负: edge+1.8%/yr
    "ou":  [4, 4, 3, 3, 2, 2, 2, 1, 1, 0, 0, 0, 0, 0],   # 大小: edge+0.9%/yr
}

# WNBA/其他篮球: 数据极有限
OTHER_BASKETBALL_WEIGHTS = {
    "1x2": [3, 3, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0],
    "hc":  [2, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "ou":  [2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}


# =====================================================================
# BASEBALL — 公开研究 + 少量结算
# =====================================================================
MLB_WEIGHTS = {
    "ou":  [4, 4, 3, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0],
    "1x2": [3, 3, 2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0],
    "hc":  [2, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}

# =====================================================================
# 其他运动 — 保守默认
# =====================================================================
DEFAULT_SPORT_WEIGHTS = {
    "1x2": [2, 2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "ou":  [2, 2, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "hc":  [1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}

# =====================================================================
# 运动特殊规则
# =====================================================================
# MMA/拳击: 映射错误率高 (选手名不一致), 无 Pin 历史 → 彻底跳过
BLOCKED_SPORTS = {"mma", "boxing"}


# =====================================================================
# 核心查询函数
# =====================================================================

def _get_odds_bins(sport: str, sub_market: str = "1x2") -> list:
    """返回该运动使用的赔率区间定义。"""
    if sport == "tennis":
        return TENNIS_ODDS_BINS
    elif sport in ("basketball",):
        return BASKETBALL_ODDS_BINS
    else:
        return FOOTBALL_ODDS_BINS


def _match_league_keyword(league: str, weight_dict: dict) -> Optional[list]:
    """按关键字匹配联赛权重。按长度降序匹配, 短关键字(<=2字)必须是前缀。"""
    if league in weight_dict:
        return weight_dict[league]
    for kw, weights in sorted(weight_dict.items(), key=lambda x: -len(x[0])):
        if kw == "_DEFAULT":
            continue
        if len(kw) <= 2 and not league.startswith(kw):
            continue
        if kw in league:
            return weights
    return weight_dict.get("_DEFAULT")


@lru_cache(maxsize=2048)
def get_stake_pct(sport: str, league: str, sub_market: str, odds: float) -> float:
    """返回该投注的最大仓位比例 (0.0 ~ 0.06)。

    Args:
        sport: 运动类型 (football / tennis / basketball / baseball / ...)
        league: 联赛名 (中文或英文)
        sub_market: 盘口类型 (1x2 / hc / ou / ht / dc / dnb / btts / oe / htft / corner)
        odds: BB 赔率

    Returns:
        仓位比例 (0.0 = 不投, 最大 0.06)
    """
    sport_lower = (sport or "").lower()

    # ── 封杀运动 ──
    if sport_lower in BLOCKED_SPORTS:
        return 0.0

    # ── 封杀市场 ──
    if sub_market in ("dc", "htft"):
        return 0.0  # 0% 胜率, 全输

    # ── 特殊市场: DNB/BTTS/OE/Corner ──
    if sub_market in SPECIAL_MARKET_WEIGHTS:
        cfg = SPECIAL_MARKET_WEIGHTS[sub_market]
        if isinstance(cfg, dict):
            if odds > cfg.get("odds_cap", 999):
                return 0.0
            return cfg.get("max_stake_pct", 0.01)
        return float(cfg) / 100.0 if cfg > 0 else 0.0

    # ── Football ──
    if sport_lower == "football":
        bins = FOOTBALL_ODDS_BINS
        idx = _get_bin_index(odds, bins)

        if sub_market in ("ht",):
            # 半场 1X2: 基于全场 1X2 权重 + 折扣
            base_weights = _match_league_keyword(league, FB_1X2_WEIGHTS)
            if base_weights is None:
                return 0.0
            base = base_weights[min(idx, len(base_weights)-1)]
            # 半场折扣: 低赔保持, 中赔 -1, 高赔 -2
            if odds < 2.0:
                discount = 0
            elif odds < 4.0:
                discount = 1
            else:
                discount = 2
            weight = max(0, base - discount)
            # 半场赔率上限: 超过 7.0 的不投 (41笔中 >7.0 的 HT 全部亏损)
            if odds > 7.0:
                return 0.0
            return weight / 100.0

        elif sub_market in ("ou",):
            base_weights = _match_league_keyword(league, FB_OU_WEIGHTS)
            if base_weights is None:
                return 0.0
            # OU 高赔率几乎不存在, >5.0 跳过
            if odds > 6.0:
                return 0.0
            return base_weights[min(idx, len(base_weights)-1)] / 100.0

        else:  # 1x2 (default for football)
            base_weights = _match_league_keyword(league, FB_1X2_WEIGHTS)
            if base_weights is None:
                return 0.0
            weight = base_weights[min(idx, len(base_weights)-1)]
            # 应用联赛 vig 系数
            league_cfg = FOOTBALL_LEAGUE_VIG.get(league)
            if league_cfg:
                weight = int(weight * league_cfg["mult"])
            return max(0, min(6, weight)) / 100.0

    # ── Tennis ──
    elif sport_lower == "tennis":
        bins = TENNIS_ODDS_BINS
        idx = _get_bin_index(odds, bins)

        if sub_market in ("hc",):
            weights = _match_league_keyword(league, TENNIS_HC_WEIGHTS)
        elif sub_market in ("ou",):
            weights = _match_league_keyword(league, TENNIS_OU_WEIGHTS)
        else:  # 1x2
            weights = _match_league_keyword(league, TENNIS_WEIGHTS)

        if weights is None:
            weights = TENNIS_WEIGHTS.get("_DEFAULT", [0]*16)
        return weights[min(idx, len(weights)-1)] / 100.0

    # ── Basketball ──
    elif sport_lower == "basketball":
        bins = BASKETBALL_ODDS_BINS
        idx = _get_bin_index(odds, bins)

        if "NBA" in league:
            w = NBA_WEIGHTS.get(sub_market, NBA_WEIGHTS.get("1x2", []))
            return w[min(idx, len(w)-1)] / 100.0
        else:
            w = OTHER_BASKETBALL_WEIGHTS.get(sub_market, [2,2,1,1,1,0,0,0,0,0,0,0,0,0])
            return w[min(idx, len(w)-1)] / 100.0

    # ── Baseball ──
    elif sport_lower == "baseball":
        bins = FOOTBALL_ODDS_BINS  # 复用足球区间
        idx = _get_bin_index(odds, bins)
        w = MLB_WEIGHTS.get(sub_market, [2,2,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0])
        return w[min(idx, len(w)-1)] / 100.0

    # ── American Football ──
    elif sport_lower == "american_football":
        if odds > 5.0:
            return 0.0
        bins = FOOTBALL_ODDS_BINS
        idx = _get_bin_index(odds, bins)
        w = DEFAULT_SPORT_WEIGHTS.get(sub_market, DEFAULT_SPORT_WEIGHTS["1x2"])
        return max(0, w[min(idx, len(w)-1)] - 1) / 100.0  # 再降1档

    # ── Ice Hockey ──
    elif sport_lower == "ice_hockey":
        if odds > 3.0:
            return 0.0
        return 0.01

    # ── 乒乓球/羽毛球/排球 ──
    elif sport_lower in ("pingpong", "badminton", "volleyball"):
        if odds > 2.5:
            return 0.0
        return 0.01

    # ── Unknown ──
    else:
        if odds > 3.0:
            return 0.0
        return 0.01


# =====================================================================
# 赔率上限查询 (替代 bb_ev_push 中的 _get_odds_limit)
# =====================================================================

def get_odds_cap(sport: str, league: str, sub_market: str) -> float:
    """根据 Pinnacle 历史数据返回该联赛/市场的赔率上限。

    超过此上限的 BB 赔率几乎一定是假阳性(队名匹配错误)。
    返回 0 表示无限制(不封顶)。

    数据源:
      足球 1X2: Pinnacle 61,404场, 按联赛 vig 级别
      足球 OU: 同上
      网球: Pinnacle 5,013场, 按赛事级别
      NBA: 模型15季回测
    """
    sport_lower = (sport or "").lower()

    if sport_lower == "football":
        league_cfg = FOOTBALL_LEAGUE_VIG.get(league)
        if league_cfg:
            vig = league_cfg["vig"]
            # 低 vig → 高上限 (Pinnacle 准 → 高赔率也能信)
            if vig < 2.5:
                return 20.0   # 德甲/德乙: 极高信任
            elif vig < 4.0:
                return 12.0   # 英超/英冠/法乙/英乙/意乙/荷甲
            elif vig < 5.0:
                return 9.0    # 法甲/西乙/比甲/苏超/西甲/英甲/意甲
            elif vig < 6.0:
                return 7.0    # 土超
            else:
                return 5.0    # 葡超/希超
        return 5.0  # 未知联赛保守

    elif sport_lower == "tennis":
        for kw, limit in [
            ("Masters", 15.0), ("Grand Slam", 5.0), ("ATP 500", 10.0),
            ("ATP 250", 5.0), ("WTA", 4.0), ("Challenger", 3.0),
            ("ITF", 2.5), ("W15", 2.5), ("M15", 2.5), ("W25", 2.5), ("M25", 2.5),
        ]:
            if kw.lower() in (league or "").lower():
                return limit
        return 3.0

    elif sport_lower == "basketball":
        if "NBA" in (league or ""):
            return 8.0
        return 5.0

    elif sport_lower == "baseball":
        return 5.0

    elif sport_lower in BLOCKED_SPORTS:
        return 0.0  # 封杀

    return 3.0  # 默认极度保守


# =====================================================================
# EV 门槛查询 (替代 bb_ev_push 中的 _min_ev_for_tier)
# =====================================================================

def get_min_ev(sport: str, league: str, sub_market: str, odds: float) -> float:
    """返回该投注组合的最小 EV 门槛。

    核心原则: 赔率越高 → Pinnacle 公平价误差越大 → 需要更高 EV 才能确信。
    数据驱动: 219笔结算 + Pinnacle 61K场历史。

    门槛公式: base_tier_min + odds_adjustment + market_adjustment
    """
    # 1. 基础门槛 (Tier 驱动)
    from config.constants import get_league_tier
    tier = get_league_tier(league)
    base_min = {1: 2.0, 2: 3.0, 3: 4.0, 4: 99.0}.get(tier, 4.0)

    # 2. 赔率加成 (赔率越高越怀疑)
    if odds < 2.0:
        odds_adj = 0.0
    elif odds < 3.0:
        odds_adj = 1.0
    elif odds < 4.0:
        odds_adj = 2.0
    elif odds < 5.0:
        odds_adj = 3.0
    elif odds < 7.0:
        odds_adj = 5.0
    elif odds < 10.0:
        odds_adj = 8.0
    else:
        odds_adj = 999.0  # >10.0 直接跳过

    # 3. 市场调整
    market_adj = {
        "ou":   0.0,   # OU: 比价最可靠 → 不额外加
        "1x2":  0.0,   # 1X2: 标准
        "btts": 1.0,   # BTTS: Pinnacle 数据较少
        "hc":   1.0,   # HC: 防守型
        "ht":   2.0,   # HT: 41笔结算 -4% → 加2%
        "dnb":  2.0,
        "oe":   3.0,
        "corner": 3.0,
    }.get(sub_market, 1.0)

    min_ev = base_min + odds_adj + market_adj

    # 4. 联赛 vig 调整
    league_cfg = FOOTBALL_LEAGUE_VIG.get(league)
    if league_cfg and league_cfg["vig"] > 6.0:
        min_ev += 2.0  # 高 vig 联赛额外 +2%

    return min_ev


# =====================================================================
# 工具函数
# =====================================================================

def print_league_matrix(sport: str = "football", market: str = "1x2"):
    """打印某运动/市场的权重矩阵 (调试用)。"""
    from config.constants import SPORT_ORDER
    print(f"\n{'='*80}")
    print(f"{sport} / {market} 权重矩阵 (% of bankroll per odds bin)")
    print(f"{'='*80}")

    bins = _get_odds_bins(sport, market)
    header = "League".ljust(16)
    for _, label in bins:
        header += f"{label:>7s}"
    print(header)
    print("-" * len(header))

    if sport == "football":
        if market in ("1x2", "ht"):
            table = FB_1X2_WEIGHTS
        elif market == "ou":
            table = FB_OU_WEIGHTS
        else:
            table = FB_1X2_WEIGHTS

        for league in sorted(table.keys()):
            if league == "_DEFAULT":
                continue
            row = league[:15].ljust(16)
            for i in range(len(bins)):
                w = table[league][min(i, len(table[league])-1)]
                row += f"{w/100*100:6.1f}%" if w > 0 else "      -"
            print(row)

    elif sport == "tennis":
        table = TENNIS_WEIGHTS
        for tour in sorted(table.keys()):
            if tour == "_DEFAULT":
                continue
            row = tour[:15].ljust(16)
            for i in range(len(bins)):
                w = table[tour][min(i, len(table[tour])-1)]
                row += f"{w/100*100:6.1f}%" if w > 0 else "      -"
            print(row)


if __name__ == "__main__":
    print_league_matrix("football", "1x2")
    print_league_matrix("football", "ou")
    print_league_matrix("tennis", "1x2")
