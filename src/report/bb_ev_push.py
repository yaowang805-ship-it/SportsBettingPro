"""BB体育 vs Pinnacle +EV 钉钉推送 — 格式与 ev_push.py 一致，零售→BB价。"""
import json, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR, DINGTALK_WEBHOOK

logger = get_logger(__name__)

# 推荐记录追踪 — 记录每次推送的所有推荐
from src.report import recommendation_tracker

from config.constants import (
    BANKROLL as _BASE_BANKROLL,
    get_dynamic_bankroll,
    MAX_BETS as MAX_OPPORTUNITIES,
    EV_CAP,
    KELLY_FRACTION,
    SPORT_ORDER,
    get_league_tier as _get_league_tier,
    league_multiplier,
)

COMPARISON_FILE = DATA_DIR / "bb_vs_pinnacle_comparison.json"
COMPARISON_FILE_NEAR = DATA_DIR / "bb_vs_pinnacle_comparison_near.json"
COMPARISON_FILE_FAR = DATA_DIR / "bb_vs_pinnacle_comparison_far.json"
COMPARISON_FILE_URGENT = DATA_DIR / "bb_vs_pinnacle_comparison_urgent.json"
ODDSAPI_COMPARISON_FILE = DATA_DIR / "bb_vs_oddsapi_comparison.json"
FINGERPRINT_FILE = DATA_DIR / "pushed_fingerprints.json"
CLV_LOG_FILE = DATA_DIR / "clv_tracking.csv"
BUDGET_TRACKER_FILE = DATA_DIR / "budget_tracker.json"

# 联赛配置数据（从固定文件加载）
BANNED_LEAGUES_FILE = DATA_DIR / "banned_leagues.json"
LEAGUE_TIERS_FILE = DATA_DIR / "league_tiers.json"


def _load_banned_leagues():
    if BANNED_LEAGUES_FILE.exists():
        return json.loads(BANNED_LEAGUES_FILE.read_text())
    return []


_OUTCOME_CN = {"home": "主胜", "draw": "和局", "away": "客胜"}
_REVERSE_CN = {v: k for k, v in _OUTCOME_CN.items()}

# ── 一致性追踪 ──
# 保存每次推送的 per-sport 机会数，用于检测异常波动
PUSH_META_FILE = DATA_DIR / "push_consistency_meta.json"
# 一个 sport 的机会数比前次下降超过此比例时，在推送中发出警告
CONSISTENCY_WARN_THRESHOLD = 0.50  # 放宽阈值: 指纹去重会造成大幅波动, 50%以内正常

# 不靠谱联赛 — 匹配质量差、假阳性多，直接屏蔽（从固定文件加载）
_BANNED_LEAGUES = _load_banned_leagues()

# ── 中文队名 Unicode 归一化 ──
# BB 和 FB 平台对同一支队伍的队名可能返回不同汉字变体
# (如 兹/茲、维/維)，导致 BB/FB 去重失效，同一比赛同一盘口被推送两次。
# 映射方向: 繁体/异体 → 简体/标准，保证两个平台的名称可比。
_UNICODE_VARIANT_MAP = {
    # 常见繁→简
    0x8332: 0x5179,  # 茲 → 兹 (维德祖罗茲→维德祖罗兹)
    0x7F85: 0x7F57,  # 羅 → 罗
    0x723E: 0x5C14,  # 爾 → 尔
    0x4E9E: 0x4E9A,  # 亞 → 亚
    0x7DAD: 0x7EF4,  # 維 → 维
    0x7D93: 0x7ECF,  # 經 → 经
    0x8655: 0x5904,  # 處 → 处
    0x7D05: 0x7EA2,  # 紅 → 红
    0x7D1A: 0x7EA7,  # 級 → 级
    0x8056: 0x5723,  # 聖 → 圣
    # 全角括号 → 半角
    0xFF08: 0x0028,  # （ → (
    0xFF09: 0x0029,  # ） → )
    # 全角数字/字母 → 半角
    0xFF10: 0x0030,  # ０ → 0
    0xFF11: 0x0031,  # １ → 1
    0xFF12: 0x0032,  # ２ → 2
    0xFF13: 0x0033,  # ３ → 3
    0xFF14: 0x0034,  # ４ → 4
    0xFF15: 0x0035,  # ５ → 5
    0xFF16: 0x0036,  # ６ → 6
    0xFF17: 0x0037,  # ７ → 7
    0xFF18: 0x0038,  # ８ → 8
    0xFF19: 0x0039,  # ９ → 9
}


def _normalize_cn(s: str) -> str:
    """归一化中文字符: 繁体→简体, 全角→半角。去重和指纹生成前统一调用。"""
    if not s:
        return s
    result = []
    for ch in s:
        cp = ord(ch)
        result.append(chr(_UNICODE_VARIANT_MAP.get(cp, cp)))
    return "".join(result)


# CLV 暂停缓存 — 进程内只从 SQLite 加载一次
_CLV_SUSPENSIONS_CACHE = None


def _get_league_pnl_summary(qualified: list) -> str:
    """从结算日志中提取本次推送涉及联赛的累计P&L。"""
    import csv
    from collections import defaultdict
    from pathlib import Path
    sp = Path(__file__).resolve().parent.parent / "data" / "storage" / "settlement_log.csv"
    if not sp.exists():
        return ""
    by_lg = defaultdict(lambda: {"stake": 0.0, "profit": 0.0, "n": 0})
    with open(sp) as f:
        for r in csv.DictReader(f):
            by_lg[r.get("league", "")]["stake"] += float(r.get("stake", 0))
            by_lg[r.get("league", "")]["profit"] += float(r.get("profit", 0))
            by_lg[r.get("league", "")]["n"] += 1
    # 收集本次推送涉及的联赛
    push_leagues = set(o.get("league", "") for o in qualified)
    parts = []
    for lg in sorted(push_leagues):
        d = by_lg.get(lg)
        if d and d["n"] >= 5:
            roi = d["profit"] / d["stake"] * 100 if d["stake"] > 0 else 0
            parts.append(f"{lg}: {d['n']}笔 ROI {roi:+.0f}%")
    return " | ".join(parts[:5]) if parts else ""


def _get_clv_suspensions():
    global _CLV_SUSPENSIONS_CACHE
    if _CLV_SUSPENSIONS_CACHE is None:
        _CLV_SUSPENSIONS_CACHE = _load_clv_suspensions()
    return _CLV_SUSPENSIONS_CACHE

# ===================================================================
# 市场质量权重 — 按运动区分，基于真实数据驱动
# ===================================================================
# 数据源:
#   1. NBA 15赛季回测 (2012-2026, 57,504场): Spread ¥22.4/场 > ML ¥17.7/场 > OU ¥9.0/场
#   2. 足球135笔结算: OU +14% ROI > BTTS +12% > HC +5% > 1X2 -55%
#   3. 网球FLB研究: 低赔方比高赔方实际回报好39%
#   4. 棒球公开研究: 总得分 > 独赢

# ===================================================================
# 比价权重: Pinnacle越准确 → 公平价越可靠 → 比价信号越真实 → 权重越高
# ===================================================================
# 核心原理:
#   我们不预测，我们比价。BB/FB赔率 vs Pinnacle公平价。
#   Pinnacle定价越准(抽水越低) → 公平价越可信 → 比价找到的+EV越真实 → 高权重
#   Pinnacle定价越松(抽水越高) → 公平价有噪音 → 可能是假+EV → 低权重
#
# 数据源:
#   ⚽足球: Pinnacle 72,806场收盘 (18联赛×13季, 283,368笔)
#   🏀NBA: 15季模型回测 (57,504场)
#   --- 总计 130,310场 ---

# --- 足球: Pinnacle准确度驱动 ---
# OU抽水3.87% → 准确度96.1% → 比价最可靠
# 1X2抽水4.29% → 准确度95.8% → 略逊, 差异仅0.42%
# 德甲抽水1.88%(98.1%准确) > 希超7.41%(92.6%)
MARKET_QUALITY_FOOTBALL = {
    "ou":   1.11,  # 最准(3.87%抽水)→比价最可靠
    "btts": 1.05,  # 研究支持
    "hc":   1.00,  # 标准
    "dnb":  1.00,  # 中性
    "dc":   0.95,  # 从1X2推导
    "1x2":  0.90,  # 稍逊(4.29%抽水)
    "ht":   0.90,  # 半场1X2→对齐全场1X2（同类市场，相同Pinnacle数据覆盖）
    "corner": 0.80,
    "htft": 0.50,  # 半全场: BB/Pin 定义不同(含不含加时), 历史溢价33%, 严格压低
    "oe":   0.85,
}

# Pinnacle足球联赛准确度 (抽水越低→比价越可靠→联赛乘数加成)
# 数据: 72,806场 Pinnacle收盘赔率 × 18联赛
PINNACLE_LEAGUE_ACCURACY = {
    # 高准确度 (抽水<3%): Pinnacle最准→比价最可靠→+15%
    "德甲": 1.15, "德乙": 1.15,
    # 中高准确度 (抽水3-4%): +5%
    "英冠": 1.05, "法乙": 1.05, "英乙": 1.05, "意乙": 1.05,
    "英超": 1.05, "荷甲": 1.05, "法甲": 1.05,
    # 标准 (抽水4-5%): 1.0
    "西乙": 1.00, "比甲": 1.00, "苏超": 1.00, "西甲": 1.00,
    "英甲": 1.00,
    # 中低准确度 (抽水5-6%): Pinnacle 定价偏松 → -10%
    "意甲": 0.90,
    # 低准确度 (抽水>6%): Pinnacle较不准→比价可靠性低→-15%
    "土超": 0.85, "葡超": 0.85, "希超": 0.85,
}

# --- 篮球: 模型edge驱动 ---
# edge越高 → 说明Pinnacle在该市场定价空间越大 → 比价空间越大
MARKET_QUALITY_BASKETBALL = {
    "hc":   1.34,  # 让分: 比价空间最大(edge+2.2%)
    "1x2":  1.06,  # 胜负: 比价空间中等(edge+1.8%)
    "ou":   0.60,  # 大小分: 比价空间较小(edge+0.9%)
}

# --- 网球市场权重 (Pinnacle 5,013场, 3年ATP/WTA数据) ---
# Pinnacle抽水3.85%, 按赛事级别差异大:
#   Masters 1000: 1.80% (最准), Grand Slam: 5.69% (最差)
# FLB严重: 低赔ROI=-1.4%, 高赔ROI=-15.3%
# 核心: 按赛事级别 + 赔率范围双重过滤
MARKET_QUALITY_TENNIS = {
    "1x2":  1.05,  # Pinnacle抽水3.85% → 比足球1X2(4.29%)更准
    "hc":   0.80,  # 让盘: 谨慎
    "ou":   0.70,  # 大小分: 谨慎
}

# 网球赔率上限 — 按赛事级别, 基于Pinnacle 5,013场真实ROI
# 上限=该级别该赔率范围仍有正期望的最高赔率
TENNIS_ODDS_LIMITS = {
    # 赛事级别关键字 → 最大BB赔率
    "Grand Slam": 5.0,        # >5.0 ROI=-34.4%, >10.0 ROI惨不忍睹
    "Masters": 5.0,           # 3.0-5.0 ROI=+9.4% → 可以投到5.0
    "ATP 500": 4.0,           # 3.0-5.0 ROI=-5.5% → 略微放宽
    "ATP 250": 5.0,           # 3.0-5.0虽差但5.0-10.0 ROI=+17.1%(有value)
    "WTA": 5.0,               # WTA暂用ATP250同参数
    "Challenger": 3.0,        # 挑战赛: 匹配质量差, 保守
    "ITF": 2.5,               # ITF低级别: 匹配质量最差, 只投低赔
    "W15": 2.5, "M15": 2.5,  # ITF Futures: 极度保守
    "W25": 2.5, "M25": 2.5,
}

def _get_tennis_odds_limit(league: str) -> float:
    """根据联赛名返回网球赔率上限。"""
    for keyword, limit in TENNIS_ODDS_LIMITS.items():
        if keyword.lower() in league.lower():
            return limit
    return 3.0  # 默认保守

# --- 全运动赔率上限 (基于 Pinnacle 历史数据) ---
_ODDS_LIMITS_CACHE = None

def _load_odds_limits():
    global _ODDS_LIMITS_CACHE
    if _ODDS_LIMITS_CACHE is None:
        import json
        limits_file = DATA_DIR / ".." / ".." / "odds" / "odds_limits.json"
        # Try multiple paths
        from pathlib import Path
        for p in [DATA_DIR.parent / "odds" / "odds_limits.json",
                  Path("data/odds/odds_limits.json")]:
            if p.exists():
                try:
                    _ODDS_LIMITS_CACHE = json.loads(p.read_text())
                    break
                except (json.JSONDecodeError, OSError):
                    pass
        if _ODDS_LIMITS_CACHE is None:
            # 文件不存在时用内置默认值 (基于Pinnacle历史数据)
            _ODDS_LIMITS_CACHE = {
                "football": {
                    "德甲": {"1x2_limit": 20.0, "ou_limit": 20.0}, "德乙": {"1x2_limit": 20.0, "ou_limit": 20.0},
                    "英超": {"1x2_limit": 8.0, "ou_limit": 9.6}, "英冠": {"1x2_limit": 10.0, "ou_limit": 12.0},
                    "西甲": {"1x2_limit": 8.0, "ou_limit": 9.6}, "意甲": {"1x2_limit": 6.0, "ou_limit": 7.2},
                    "法甲": {"1x2_limit": 8.0, "ou_limit": 9.6}, "葡超": {"1x2_limit": 5.0, "ou_limit": 6.0},
                    "荷甲": {"1x2_limit": 8.0, "ou_limit": 9.6}, "土超": {"1x2_limit": 5.0, "ou_limit": 6.0},
                    "希超": {"1x2_limit": 5.0, "ou_limit": 6.0},
                },
                "tennis": {
                    "Masters": 10.0, "Grand Slam": 5.0, "ATP 500": 10.0, "ATP 250": 5.0,
                    "WTA": 5.0, "Challenger": 3.0, "ITF": 2.5, "W15": 2.5, "M15": 2.5,
                },
                "basketball": {"NBA": {"hc": 10.0, "1x2": 8.0, "ou": 8.0}},
                "baseball": {"MLB": {"1x2": 5.0, "ou": 5.0, "hc": 5.0}},
                "default": {"1x2": 5.0, "ou": 5.0, "hc": 5.0},
            }
    return _ODDS_LIMITS_CACHE

def _get_odds_limit(sport: str, league: str, market: str) -> float:
    """根据 Pinnacle 历史数据返回该运动/联赛/市场的赔率上限。

    Returns:
        最大允许的 BB 赔率, 超过此值跳过。
        返回 0 表示无限制。
    """
    limits = _load_odds_limits()
    if not limits:
        return 0  # No limits loaded → no restriction

    # 1. Sport-specific lookup
    sport_data = limits.get(sport, {})
    if not sport_data:
        return limits.get("default", {}).get(market, 5.0)

    # 2. League-specific lookup
    if isinstance(sport_data, dict):
        # Check for by-league data (football, tennis)
        for league_key, league_limits in sport_data.items():
            if league_key.lower() in (league or "").lower():
                if isinstance(league_limits, dict):
                    return league_limits.get(f"{market}_limit", league_limits.get(market, 0))
        # Check for by-tournament data (tennis)
        if "by_tournament" in sport_data:
            for key, limit in sport_data.get("by_tournament", {}).items():
                if key.lower() in (league or "").lower():
                    return limit

    # 3. Default for sport
    default_limit = sport_data.get(market, sport_data.get(f"{market}_limit", 0))
    if default_limit:
        return default_limit

    return limits.get("default", {}).get(market, 5.0)

# 网球赛事级别准确度 (Pinnacle 5,013场数据)
# vig越低→Pinnacle越准→比价越可靠
PINNACLE_TENNIS_ACCURACY = {
    "Masters": 1.50,    # vig=1.80%, Pinnacle最准→比价最可靠
    "Grand Slam": 0.70, # vig=5.69%, 公众投注量大→线被推偏
    "ATP 500": 0.75,    # vig=5.34%
    "ATP 250": 1.06,    # vig=3.78%
    "WTA": 1.00,        # 默认
}

def _get_tennis_accuracy(league: str) -> float:
    """根据联赛名返回网球准确度加成。"""
    for keyword, bonus in PINNACLE_TENNIS_ACCURACY.items():
        if keyword.lower() in league.lower():
            return bonus
    return 1.0

# --- 棒球市场权重 (公开研究) ---
MARKET_QUALITY_BASEBALL = {
    "ou":   1.10,  # 总得分 > 独赢 (公开研究确认超额收益)
    "1x2":  0.90,  # 独赢: 效率高
    "hc":   0.90,  # 让分: 中性
}

# --- 默认权重 (未知运动) ---
MARKET_QUALITY_DEFAULT = {
    "1x2":  1.00, "hc": 1.00, "ou": 1.00, "btts": 1.00,
    "dnb":  0.80, "dc": 0.80, "ht": 0.75, "ht_hc": 0.75, "ht_ou": 0.75, "corner": 0.70,
    "oe":   0.80, "htft": 0.70,
}

def _get_market_quality(sport: str = "") -> dict:
    """根据运动返回对应的市场质量权重表。"""
    sport_lower = (sport or "").lower()
    if sport_lower == "football":
        return MARKET_QUALITY_FOOTBALL
    if sport_lower in ("basketball",):
        return MARKET_QUALITY_BASKETBALL
    if sport_lower in ("tennis",):
        return MARKET_QUALITY_TENNIS
    if sport_lower in ("baseball",):
        return MARKET_QUALITY_BASEBALL
    return MARKET_QUALITY_DEFAULT

def _get_market_weight(sub_market: str, sport: str = "") -> float:
    """返回指定运动+市场的质量权重。"""
    return _get_market_quality(sport).get(sub_market, 1.0)

# --- Kelly 分市场差异化 (基于Pinnacle真实效率, 跨运动通用) ---
# Pinnacle 近乎完美校准，所有市场都能信任。差异仅来自抽水。
# V5.2 数据质量导向: 有 Pinnacle 直接收盘数据的盘口 > 推导盘口
# 数据量: 1X2 111K > HC 49K > OU 46K > 角球 5.7万; DC/HT/BTTS/DNB/OE 是推导(借数据)
KELLY_BY_MARKET = {
    "ou":   0.52,  # 抽水最低但数据只有46K, 与1X2对齐
    "hc":   0.50,  # 49K AH数据, 标准
    "1x2":  0.50,  # V5.2: 111K最多数据, devig已修(旧0.45是假阳性包袱)
    "corner": 0.45, # V5.1: 5.7万 AH数据, 第4重要市场
    "btts": 0.45,  # V5.2: 推导自OU, 无独立Pinnacle数据 → 下调
    "dnb":  0.45,  # V5.2: 推导 → 下调
    "dc":   0.40,  # V5.2: 推导 → 下调
    "ht":   0.40,  # V5.2: 半场1X2, 有HT结果分布(平局42%)驱动
    "ht_hc": 0.40, # V5.2: 半场让球, 有HT净胜球分布
    "ht_ou": 0.40, # V5.2: 半场大小球, 有HT进球分布
    "oe":   0.30,  # V5.2: 无数据 → 下调
    "htft": 0.25,  # 低流动性+高抽水(5-7%), 严格控制
}
def _get_kelly_for_market(sub_market: str) -> float:
    """根据市场类型返回对应的 Kelly 分数。"""
    return KELLY_BY_MARKET.get(sub_market, KELLY_FRACTION)

# ── 日预算总额控制 (取代分组预算上限) ──
# 纯 Kelly 分配, 总额 ¥20,000/天, 按投注额比例压缩
TOTAL_DAILY_BUDGET = 20000  # 日预算总额


def _load_budget_tracker():
    """加载当日预算跟踪器。日期不匹配时自动重置。"""
    from config.database import get_budget, save_budget
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    spent = get_budget(today)
    return spent, today


def _save_budget_tracker(spent: dict, date_str: str):
    """保存预算跟踪器（写入 SQLite）。"""
    from config.database import save_budget
    save_budget({k: v for k, v in spent.items() if v > 0}, date_str)


# ── CLV 追踪 ──

def _get_streak_multiplier() -> float:
    """近3天连亏→降仓50%，直到出现盈利日。"""
    import json as _json
    pf_file = DATA_DIR / "storage" / "virtual_portfolio.json"
    if not pf_file.exists():
        return 1.0

    try:
        pf = _json.loads(pf_file.read_text())
        history = pf.get("history", [])
        from collections import defaultdict
        by_day = defaultdict(lambda: {"profit": 0})
        for h in history:
            date = str(h.get("date", ""))[:10]
            by_day[date]["profit"] += h.get("profit", 0)

        recent = sorted(by_day.items())[-3:]  # 最近3天
        losing_days = sum(1 for _, d in recent if d["profit"] < 0)
        if losing_days >= 3:
            return 0.5  # 连亏3天，仓位减半
        elif losing_days >= 2:
            return 0.7  # 连亏2天，降30%
        return 1.0
    except Exception:
        return 1.0


def _auto_sync():
    """推送成功后自动同步: 清除 pyc 缓存 + Git 提交 (非阻塞)。"""
    try:
        import subprocess, os
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "scripts", "auto_sync.sh")
        if os.path.exists(script):
            subprocess.Popen(["bash", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass  # 静默失败, 不影响推送


def _maybe_send_health_report(qualified: list, place_bets: bool):
    """每周一/周四推送组合健康报告（蒙特卡洛风险 + 近7天绩效）。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    if now.weekday() not in (0, 3):  # 周一(0) 和 周四(3)
        return
    if now.hour < 8 or now.hour > 20:  # 只在 8:00-20:00 发
        return

    # 加载组合数据
    import json as _json
    pf_file = DATA_DIR / "storage" / "virtual_portfolio.json"
    if not pf_file.exists():
        return
    pf = _json.loads(pf_file.read_text())
    history = pf.get("history", [])
    if len(history) < 20:
        return  # 样本不足

    # 统计
    won = [h for h in history if h.get("status") == "won"]
    lost = [h for h in history if h.get("status") == "lost"]
    settled = won + lost
    if not settled:
        return
    wr = len(won) / len(settled)
    avg_odds = sum(h.get("odds", 0) for h in settled) / len(settled)
    total_profit = sum(h.get("profit", 0) for h in history)
    total_stake = sum(h.get("stake", 0) for h in settled)

    # 近7天
    cutoff = (now - timedelta(days=7)).isoformat()
    recent = [h for h in history if h.get("date", "") >= cutoff]
    recent_profit = sum(h.get("profit", 0) for h in recent)

    # 蒙特卡洛
    mc = _run_monte_carlo(wr, avg_odds, n_sims=5000)

    # 构建报告
    lines = [
        "## 📊 组合健康报告",
        "",
        f"**已结算**: {len(history)} 笔 | 胜率: {wr*100:.0f}% | 均赔: {avg_odds:.2f}",
        f"**累计盈亏**: ¥{total_profit:+,.0f} | 近7天: ¥{recent_profit:+,.0f}",
        "",
        "### 🎲 蒙特卡洛风险模拟 (5,000次)",
        f"| 破产概率 | 平均最大回撤 | 中位回报 | 最差回报 | 生存率 |",
        f"|---|---|---|---|---|",
        f"| {mc['ruin_prob']:.1f}% | {mc['avg_max_dd']:.1f}% | {mc['median_return']:+.0f}% | {mc['worst_return']:+.0f}% | {mc['survival_rate']:.1f}% |",
        "",
        f"💡 破产概率 = 连续亏损导致本金损失70%以上的概率",
        f"💡 中位回报 = 500笔投注后期望的中位数回报率",
    ]

    from config.settings import send_dingtalk
    send_dingtalk("📊 组合健康报告", "\n".join(lines))


def _log_clv(opps: list):
    """将推送机会的 CLV 数据写入 SQLite（主）和 CSV（备份）。"""
    from config.database import insert_push_clv
    insert_push_clv(opps)

    # CSV 备份
    # V5.11: 改 DictWriter 按表头写。之前用 csv.writer 定长 20 列, 而 clv_tracking.csv
    # 表头是 22 列(validate 侧加过 pin_max_stake/line) —— 真实投注行的 line 恒空,
    # 采集器对 hc/ou 拿不到线值就直接跳过, 真实投注的让球/大小球永远采不到收盘价。
    # 同时 designation 写**原始**标签(不带"(线值)"后缀), 与 validate 口径一致。
    import csv
    fieldnames = ["timestamp", "sport", "league", "home", "away", "home_pin", "away_pin",
                  "designation", "sub_market", "bb_odds", "pin_odds", "fair_price", "ev_pct",
                  "stake", "tier", "match_epoch", "bb_price_source", "pin_league_id",
                  "pin_match_id", "source", "pin_max_stake", "line"]
    exists = CLV_LOG_FILE.exists()
    if exists:
        # 表头以磁盘为准: validate 侧(clv_collector)加列时会重排表头, 这边若按自己的
        # 顺序写就又会错位 —— 正是这次丢掉 3 天真实投注数据的原因。缺列先就地迁移
        # (复用采集器的幂等迁移), 再按磁盘表头顺序写。
        try:
            from src.monitor.clv_collector import _migrate_csv_header
            _migrate_csv_header(CLV_LOG_FILE, fieldnames)
        except Exception:
            pass
        try:
            with open(CLV_LOG_FILE, encoding="utf-8-sig", newline="") as f:
                disk_header = next(csv.reader(f), None)
            if disk_header and set(disk_header) >= set(fieldnames):
                fieldnames = disk_header
        except (OSError, StopIteration):
            pass
    with open(CLV_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for o in opps:
            line = str(o.get("line", "") or "")
            desig = o.get("designation", "")
            # 推送用的 designation 是展示名 "让球主胜(+0/0.5)", 剥回原始标签
            if line and desig.endswith(f"({line})"):
                desig = desig[: -(len(line) + 2)]
            writer.writerow({
                "timestamp": now,
                "sport": o.get("sport", ""),
                "league": o.get("league", ""),
                "home": o.get("home_cn", ""),
                "away": o.get("away_cn", ""),
                "home_pin": o.get("home_team", o.get("home_pin", "")),   # Pinnacle 英文名
                "away_pin": o.get("away_team", o.get("away_pin", "")),   # Pinnacle 英文名
                "designation": desig,
                "sub_market": o.get("_sub_market", o.get("_market", "")),
                "bb_odds": o.get("bb_odds", 0),
                "pin_odds": o.get("pin_odds", 0),
                "fair_price": o.get("fair_price", 0),
                "ev_pct": o.get("ev_pct", 0),
                "stake": o.get("_stake", 0),
                "tier": o.get("_tier", 0),
                "match_epoch": o.get("_pin_epoch", 0) or 0,  # V5 fix: empty → 0, not ''
                "bb_price_source": o.get("bb_price_source", ""),  # BB / FB / BB/FB
                "pin_league_id": o.get("_pin_league_id", ""),
                "pin_match_id": o.get("_pin_match_id", ""),
                "source": "push",
                "pin_max_stake": o.get("pin_max_stake", "") or o.get("_pin_max_stake", ""),
                "line": line,
            })


# ── CLV 趋势检查 ──

_CLV_WARN_THRESHOLD = 50    # 连续 N 条中位数 CLV < 0 → 黄色警告
_CLV_STOP_THRESHOLD = 100   # 连续 N+ 条中位数 CLV < 0 → 红色暂停
_CLV_MEDIAN_CUTOFF = -1.0   # 中位数 CLV 低于此值触发


def _check_clv_trend(opps: list) -> tuple:
    """检查每个 (sport, league) 的 CLV 趋势。

    Returns:
        (warnings, suspensions)
        warnings: list[str] — 推送中展示的警告文本
        suspensions: set of (sport, league) tuples — 应暂停的组合
    """
    from config.database import get_clv_trend

    # 获取已暂停的列表
    suspended = _load_clv_suspensions()

    combos_seen = set()
    for o in opps:
        sport = o.get("sport", "")
        league = o.get("league", "")
        if not sport or not league:
            continue
        key = (sport, league)
        if key in combos_seen:
            continue
        combos_seen.add(key)

    warnings = []
    new_suspensions = set()

    for sport, league in combos_seen:
        records = get_clv_trend(sport, league, limit=_CLV_STOP_THRESHOLD)
        if not records or len(records) < 20:
            continue

        # 计算中位数 CLV
        clv_values = [r["clv"] for r in records]
        median_clv = sorted(clv_values)[len(clv_values) // 2]

        # 检查连续负 CLV 的最长长度
        max_consecutive_neg = 0
        current_run = 0
        for r in records:
            if r.get("clv", 0) < 0:
                current_run += 1
                max_consecutive_neg = max(max_consecutive_neg, current_run)
            else:
                current_run = 0

        is_suspended = (sport, league) in suspended

        if is_suspended:
            warnings.append(
                f"⏸️ {sport}/{league} CLV 持续为负({median_clv:+.1f}%中位, "
                f"最长{max_consecutive_neg}条), 已暂停推送"
            )
            new_suspensions.add((sport, league))
        elif max_consecutive_neg >= _CLV_STOP_THRESHOLD or median_clv < _CLV_MEDIAN_CUTOFF - 2:
            warnings.append(
                f"🛑 {sport}/{league} CLV 严重为负(中位{median_clv:+.1f}%, "
                f"连续{max_consecutive_neg}条负值), 暂停推送"
            )
            new_suspensions.add((sport, league))
        elif max_consecutive_neg >= _CLV_WARN_THRESHOLD or median_clv < _CLV_MEDIAN_CUTOFF:
            warnings.append(
                f"⚠️ {sport}/{league} CLV 趋势不佳(中位{median_clv:+.1f}%, "
                f"连续{max_consecutive_neg}条负值), 关注"
            )

    # 持久化暂停列表
    if new_suspensions != suspended:
        _save_clv_suspensions(new_suspensions)

    return warnings, new_suspensions


def _load_clv_suspensions() -> set:
    """从 SQLite 加载已暂停的 (sport, league) 组合。"""
    from config.database import get_push_meta
    raw = get_push_meta("clv_suspensions")
    if raw:
        try:
            return {tuple(item) for item in json.loads(raw)}
        except (json.JSONDecodeError, TypeError):
            pass
    return set()


def _save_clv_suspensions(suspensions: set):
    """持久化暂停列表到 SQLite。"""
    from config.database import set_push_meta
    set_push_meta("clv_suspensions", json.dumps(sorted(suspensions), ensure_ascii=False))


# 中文市场名（用于推送显示）
_MARKET_CN = {
    "1x2": "独赢",
    "hc": "让球",
    "ou": "大小球",
    "ht": "上半场",
    "ht_hc": "上半场让球",
    "ht_ou": "上半场大小球",
    "btts": "双边进球",
    "dc": "双重机会",
    "dnb": "平局退款",
    "oe": "单/双",
    "htft": "半全场",
    "corner": "角球",
}


def _min_ev_for_tier(tier: int, sport: str = "", league: str = "") -> float:
    """V5.1: 全运动分层EV门槛 — T1=2%, T2=2.5-3%, T3=4-5%, T4=6%+"""
    from config.constants import get_tier_strategy
    strategy = get_tier_strategy(sport, league, tier)
    return strategy["ev_floor"]


# ── V5.10 数据驱动 EV 门槛矩阵 ──
# 由 scripts/compute_ev_thresholds.py 每晚从 clv_results.csv 重算。
# 原理: 2% 统一门槛实测开在噪声里(2-3% 档 CLV -2.16%/正率35%), 必须按盘口/运动/时间分层。
_threshold_matrix = None
_threshold_matrix_ts = 0.0


def _load_threshold_matrix():
    global _threshold_matrix, _threshold_matrix_ts
    mf = DATA_DIR / "ev_threshold_matrix.json"
    if not mf.exists():
        return None
    try:
        m = mf.stat().st_mtime
        if _threshold_matrix is None or m != _threshold_matrix_ts:
            _threshold_matrix = json.loads(mf.read_text())
            _threshold_matrix_ts = m
        return _threshold_matrix
    except Exception:
        return None


def _matrix_min_ev(sub_market: str, sport: str, league: str = "", lead_minutes=None):
    """返回该盘口的矩阵门槛; 矩阵不存在返回 None(回退 tier 门槛)。

    门槛 = 盘口层(时间窗×盘口独立门槛优先, 否则 base+time pp) + 运动微调 + 联赛微调。
    三级回退: 联赛层(n≥30 才设) → 运动层 → 盘口层。
    """
    mtx = _load_threshold_matrix()
    if not mtx:
        return None
    markets = mtx.get("markets", {})
    is_main = sub_market in mtx.get("main_markets", ("1x2", "hc", "ou"))
    # 运动×盘口独立门槛(2026-08-25): 非足球运动 n≥30 用各自数据; 足球走时间×盘口维度。
    if sport != "football":
        sm_thr = mtx.get("sport_market", {}).get(sport, {}).get(sub_market)
        if sm_thr is not None:
            thr = sm_thr
        else:
            # 非足球但样本不足: base + sport_adjust(不加足球主导的时间维度)
            thr = markets.get(sub_market, mtx.get("default_threshold", 8.0))
            thr += mtx.get("sport_adjust", {}).get(sport, 0.0)
    else:
        # 足球: base + 时间窗×盘口独立门槛(该格有数据则用它, 否则 time_adjust pp)
        thr = markets.get(sub_market, mtx.get("default_threshold", 8.0))
        market_time = mtx.get("market_time", {})
        if lead_minutes is not None and market_time:
            for _b in mtx.get("time_adjust", []):
                if _b.get("min_minutes", 0) <= lead_minutes < _b.get("max_minutes", 10**9):
                    _t = market_time.get(sub_market, {}).get(_b.get("label"))
                    if _t is not None:
                        thr = _t
                    elif is_main:
                        thr += _b.get("adjust", 0.0)
                    break
    # 联赛层调整(数据驱动, 样本不足的联赛矩阵里没有, 自动回退运动层)
    if league:
        _la = mtx.get("league_adjust", {}).get(f"{sport}|{league}")
        if _la is not None:
            thr += _la
    return max(1.0, thr)

# EV 上限 — EV > 此值几乎全是假阳性（队名匹配到错误比赛）
# 使用 constants.EV_CAP (12.0)


def _check_sport_consistency(opportunities: list, pre_dedup_counts: Optional[dict] = None) -> list:
    """对比上次推送的 per-sport 机会数，有显著下降时返回警告列表。

    pre_dedup_counts: 去重前的 per-sport 计数，用于检测（防止被指纹去重误导）。
                      传 None 则直接用 opportunities 的计数。
    """
    counts = {}
    for o in opportunities:
        s = o.get("sport", "unknown")
        counts[s] = counts.get(s, 0) + 1

    compare_counts = pre_dedup_counts or counts

    prev = None
    if PUSH_META_FILE.exists():
        try:
            prev = json.loads(PUSH_META_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    warnings = []
    if prev:
        prev_counts = prev.get("per_sport_counts", {})
        for sport, count in compare_counts.items():
            prev_count = prev_counts.get(sport, 0)
            if prev_count > 0:
                change = (count - prev_count) / prev_count
                if change < -CONSISTENCY_WARN_THRESHOLD:
                    warnings.append(
                        f"⚠️ {sport} 推送数锐减 {abs(change)*100:.0f}% "
                        f"({prev_count}→{count})，请确认匹配是否正常"
                    )
                elif change > CONSISTENCY_WARN_THRESHOLD * 2:
                    warnings.append(
                        f"📈 {sport} 推送数激增 {change*100:.0f}% "
                        f"({prev_count}→{count})，请确认是否混入异常机会"
                    )

    # 保存当前实际推送的数据供下次对比
    PUSH_META_FILE.write_text(json.dumps({
        "per_sport_counts": counts,
        "total": len(opportunities),
        "pre_dedup_total": sum(compare_counts.values()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))

    return warnings


def _run_monte_carlo(win_rate: float, avg_odds: float, n_sims: int = 10000, n_bets: int = 500):
    """蒙特卡洛破产风险模拟。

    Args:
        win_rate: 历史胜率 (0.0-1.0)
        avg_odds: 平均赔率
        n_sims: 模拟次数
        n_bets: 每次模拟的投注次数
    Returns:
        dict with ruin_prob, max_drawdown, median_return, worst_return, best_return
    """
    import random
    random.seed(42)

    stake_pct = 0.02  # 每注 2% (保守假设)
    ruin_threshold = 0.3  # 回撤 70% 视为破产

    results = {"ruin": 0, "max_dd": [], "final": []}

    for _ in range(n_sims):
        bankroll = 1.0
        peak = 1.0
        max_dd = 0.0

        for _ in range(n_bets):
            if bankroll < 0.01:  # 破产
                results["ruin"] += 1
                bankroll = 0.0
                break

            bet = bankroll * stake_pct
            if random.random() < win_rate:
                bankroll += bet * (avg_odds - 1)
            else:
                bankroll -= bet

            if bankroll > peak:
                peak = bankroll
            dd = (peak - bankroll) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        results["max_dd"].append(max_dd)
        results["final"].append(bankroll)

    valid = [f for f in results["final"] if f > 0]
    return {
        "ruin_prob": results["ruin"] / n_sims * 100,
        "avg_max_dd": sum(results["max_dd"]) / n_sims * 100,
        "median_return": (sorted(valid)[len(valid)//2] - 1) * 100 if valid else 0,
        "worst_return": (min(valid) - 1) * 100 if valid else 0,
        "best_return": (max(valid) - 1) * 100 if valid else 0,
        "survival_rate": len(valid) / n_sims * 100,
    }


def _apply_risk_manager_safety(opps: list) -> list:
    """V5.2: RiskManager 安全层 — 冷却停注 + 回撤/连败折扣。

    只取 RiskManager 的冷却/回撤/连败保护（权重矩阵已算 Kelly，这里不重复）。
    Fail-open: RiskManager 初始化异常时不阻断投注。
    """
    try:
        from src.risk.manager import RiskManager, RISK_STATE_FILE
        rm = RiskManager()
    except Exception:
        return opps

    # 状态新鲜度检查：risk_state 超过 24h 未更新则 fail-open（不应用回撤/连败，避免陈旧状态误停投）
    try:
        if RISK_STATE_FILE.exists():
            import time as _t
            if _t.time() - RISK_STATE_FILE.stat().st_mtime > 24 * 3600:
                return opps
    except Exception:
        pass

    # V5.10(2026-08-22 用户要求): 取消熔断机制和风控停止下注。
    # 比价套利的核心是抓住转瞬即逝的 +EV 机会, 止损会错过机会。用户明确要求
    # 不因"单日亏损/回撤/连败"停注 —— 之前的 daily_loss 熔断(单日亏¥2325>¥2000)
    # 把全部 _stake 清零, 导致整天无推送。只保留软上限(降低 stake), 不停止下注。

    # 公开 API：只取 get_max_stake 软上限（内部含回撤/连亏/ML动态仓位/联合凯利/分散度）
    cumulative_exposure = 0.0
    for o in opps:
        stake = o.get("_stake", 0)
        if stake <= 0:
            continue
        # 软上限：风险感知最大 stake（内部含回撤/连亏/ML动态仓位/联合凯利/分散度）
        try:
            fair = o.get("fair_price", 0)
            prob = 1.0 / fair if fair and fair > 0 else 0
            max_stake = rm.get_max_stake(
                edge_or_prob=prob, odds=o.get("bb_odds", 0),
                input_is_prob=True,
                sport=o.get("sport", ""), home_team=o.get("home_cn", ""),
                away_team=o.get("away_cn", ""), market=o.get("designation", ""),
                league=o.get("league", ""),
                market_type=o.get("_sub_market", "") or o.get("_market", ""),
            )
            if max_stake > 0 and max_stake < stake:
                o["_stake"] = int(max_stake)
        except Exception:
            pass  # 字段缺失或计算失败 → 保留原 stake
        cumulative_exposure += o.get("_stake", 0)  # 用最终 stake 累计敞口
    return opps


def _calc_kelly_stakes(opps: list) -> list:
    """V4.4 简化纯 Kelly 仓位分配。

    公式: stake = BANKROLL × V4_Kelly% × AH_discount × cross_discount

    V4_Kelly% 已内化:
      - Pinnacle 历史 WR (627K 场, 30 bins)
      - BB 溢价 (实际对比数据中位数统计)
      - 运动级数据置信度 (NFL×0.75, NHL×0.65)
      - 样本量连续置信度 (n=10→100平滑)
      - 联赛级独立标定 (25 联赛)
      - 市场差异 (OU vs 1X2 vs HT)

    移除的冗余乘数:
      - HFA: Pinnacle WR 已含主场效应
      - CLV: WR 数据已反映
      - steam: 噪音信号, 赔率变动原因复杂
      - kelly_mult: 重复 Kelly 计算
      - streak: 赌徒谬误 (过去≠未来)
    """
    from config.constants import MAX_STAKE_PCT as _MAX_STAKE_PCT, PER_MATCH_CAP_PCT as _PER_MATCH_CAP_PCT, get_dynamic_bankroll as _get_bankroll
    from config.constants import PER_LEAGUE_CAP_PCT as _PER_LEAGUE_CAP_PCT, PER_SPORT_CAP_PCT as _PER_SPORT_CAP_PCT
    from config.weight_matrix_v5 import get_kelly_stake_pct

    bankroll = _get_bankroll()

    for o in opps:
        odds = o.get("bb_odds", 0)
        ev = o.get("ev_pct", 0)
        sport = o.get("sport", "")
        league = o.get("league_cn", o.get("league", ""))  # V5.7: 用中文联赛名匹配 V5
        sub = o.get("_sub_market", o.get("_market", ""))
        match_type = o.get("_match_type", "")
        match_score = o.get("_match_score", 0)

        stake_pct = get_kelly_stake_pct(sport, league, sub, odds, match_type, match_score)
        if stake_pct <= 0:
            # V5.9: 权重矩阵该赔率区间历史ROI<=0(或联赛无历史数据)时, 不再硬否决当前+EV机会。
            # 比价套利的核心信号是实时 devig EV — _kelly_pct 已按盘口Kelly+联赛权重算好(半Kelly)。
            # 历史 bin 只该做置信度折扣, 不应把当前明确的 +EV 机会归零。
            kelly_pct = o.get("_kelly_pct", 0)
            if kelly_pct > 0 and ev > 1.0:
                stake_pct = min(kelly_pct / 100, 0.02)
            else:
                o["_stake"] = 0; o["_raw_stake"] = 0
                continue
        # V5: 推导盘口(btts/oe/dnb)无历史数据 → Kelly可能极低 → 用简单公式兜底
        if sub in ("btts", "oe", "dnb"):
            kelly_pct = o.get("_kelly_pct", 0)
            simple_pct = min(kelly_pct / 100, 0.015) if kelly_pct > 0 else 0
            stake_pct = max(stake_pct, simple_pct)  # 取较高者

        # V4.5: HC 已有独立 Pinnacle AH 收盘数据标定, 无需额外折扣

        # V4.4: 匹配置信度折扣 (时间匹配不如队名匹配可靠)
        if match_type == "time" and match_score < 0.90:
            stake_pct *= 0.80  # 低置信时间匹配 → 减 20%

        # V5.1: 全运动分层仓位上限 (Tier差异化)
        _SPORT_CAPS = {
            "football": 0.04, "basketball": 0.03, "baseball": 0.03,
            "american_football": 0.02, "ice_hockey": 0.02, "tennis": 0.02,
        }
        sport_cap = _SPORT_CAPS.get(sport, 0.02)
        # V5.1: 分层策略覆盖 (T3/T4更保守)
        from config.constants import get_tier_strategy, get_league_tier
        tier = get_league_tier(league)
        tier_cfg = get_tier_strategy(sport, league, tier)
        tier_cap = tier_cfg.get("max_stake_pct", sport_cap)
        stake_pct = min(stake_pct, min(sport_cap, tier_cap))
        # V5.8: 低级别联赛(T3/T4)单注降权 — 薄盘幻影EV高, 实测全亏(23笔-100%), 额外打折
        if tier >= 3:
            stake_pct *= 0.6 if tier == 3 else 0.4
        # V5.1: per-sport赔率策略 (10万+Pinnacle+723笔实盘)
        max_odds = tier_cfg.get("max_odds", 20.0)
        odds_kelly_mult = 1.0  # 默认不改Kelly
        # V5.9: 赔率分层策略只对标定过的直接盘口(1X2/HC/OU)。特殊盘口(正确比分/半场/角球/DC/BTTS/OE等)
        # 有独立的 SPECIAL_MARKET_CAPS 上限和 EV 门槛, 再套 1X2 赔率分层的 min_ev(高赔5-10%)
        # 会把合法的特殊盘口高赔机会误杀归零。
        if sub in ("1x2", "hc", "ou"):
            try:
                from src.evolve.odds_strategy_optimizer import get_odds_strategy
                odds_cfg = get_odds_strategy(odds, sport=sport)
                max_odds = min(max_odds, odds_cfg.get("max_odds", 20.0))
                odds_kelly_mult = odds_cfg.get("kelly_mult", 1.0)
                # V5.9: 不再二次 EV 门槛否决 — 收集阶段 get_min_ev + tier ev_floor 已过滤,
                # 权重矩阵(历史ROI) + EV-Kelly兜底已按 edge 定仓。这里只保留 kelly_mult(降仓)+max_odds。
            except ImportError: pass
        if odds > max_odds:
            o["_stake"] = 0; o["_raw_stake"] = 0
            continue

        # V5.1: per-sport赔率策略Kelly乘数
        stake_pct *= odds_kelly_mult
        # V5.9: 高赔率特殊盘方差折扣 — 正确比分/净胜球等高方差无历史盘, 赔率>8 再降仓 (防 ¥100 封顶顶格)
        if sub in ("correct_score", "winning_margin", "total_goals_range", "first_to_score") and odds > 8.0:
            stake_pct *= 0.3
        stake = int(bankroll * stake_pct)
        o["_raw_stake"] = stake
        # 用户(2026-08-22)澄清: 取消熔断/风控停注, 但保留最低投注额 ¥30 下限 ——
        # 低于 ¥30 的不推送(太碎没意义), 高于 ¥30 的只要过盘口门槛就推。
        min_stake = tier_cfg.get("min_stake", 30)
        o["_stake"] = stake if stake >= min_stake else 0

    # 第二遍：总额超预算时, 按 stake 降序取 top 保留, 超出清零 (不摊薄, 集中在最优机会)
    daily_budget = bankroll  # V4.5: 动态日预算
    _active = sorted([o for o in opps if o["_stake"] > 0], key=lambda o: -o["_stake"])
    _cum = 0
    for o in _active:
        if _cum + o["_stake"] > daily_budget:
            o["_stake"] = 0
        else:
            _cum += o["_stake"]

    # 第三遍：跨盘口相关性折扣 + 单场上限
    from collections import defaultdict
    match_groups = defaultdict(list)
    for o in opps:
        if o["_stake"] <= 0:
            continue
        key = (o.get("sport", ""), o.get("home_cn", "").strip(), o.get("away_cn", "").strip())
        match_groups[key].append(o)

    per_match_max = bankroll * _PER_MATCH_CAP_PCT
    for key, group in match_groups.items():
        # V4.4: 跨盘口相关性折扣 — 同场多盘口联合 Kelly 调整
        if len(group) >= 2:
            discount = _cross_market_correlation_discount(group)
            if discount < 1.0:
                for o in group:
                    o["_stake"] = max(0, round(o["_stake"] * discount))
                    o["_corr_discount"] = round(discount, 3)

        _active = sorted([o for o in group if o["_stake"] > 0], key=lambda o: -o["_stake"])
        _cum = 0
        for o in _active:
            if _cum + o["_stake"] > per_match_max:
                o["_stake"] = 0
            else:
                _cum += o["_stake"]

    # 第四遍：单联赛/单运动总敞口上限（防集中度风险）
    league_groups = defaultdict(list)
    sport_groups = defaultdict(list)
    for o in opps:
        if o["_stake"] <= 0:
            continue
        league_groups[(o.get("sport", ""), o.get("league", ""))].append(o)
        sport_groups[o.get("sport", "")].append(o)

    per_league_max = bankroll * _PER_LEAGUE_CAP_PCT
    for key, group in league_groups.items():
        _active = sorted([o for o in group if o["_stake"] > 0], key=lambda o: -o["_stake"])
        _cum = 0
        for o in _active:
            if _cum + o["_stake"] > per_league_max:
                o["_stake"] = 0
            else:
                _cum += o["_stake"]

    # 单运动总敞口上限 (用户指定: 足球主力40%, 篮球/网球各20%, 其他保守)
    _SPORT_TOTAL_CAPS = {
        "football": 0.40, "basketball": 0.20, "tennis": 0.20,
        "baseball": 0.10, "american_football": 0.10, "ice_hockey": 0.10,
        "mma": 0.05, "boxing": 0.05,
    }
    for key, group in sport_groups.items():
        sport_cap = _SPORT_TOTAL_CAPS.get(key, _PER_SPORT_CAP_PCT)
        per_sport_max = bankroll * sport_cap
        _active = sorted([o for o in group if o["_stake"] > 0], key=lambda o: -o["_stake"])
        _cum = 0
        for o in _active:
            if _cum + o["_stake"] > per_sport_max:
                o["_stake"] = 0
            else:
                _cum += o["_stake"]

    # 第五遍：RiskManager 安全层（冷却停注 + 回撤/连败折扣）
    opps = _apply_risk_manager_safety(opps)

    return opps


# ── 跨盘口相关性矩阵 ──
# 基于足球/篮球市场间结构关系标定
# corr=1.0 完全重叠, corr=0.0 完全独立
_MARKET_CORRELATION = {
    # 1x2 vs others
    ("1x2", "1x2"): 1.0,
    ("1x2", "ou"): 0.35,     # 主胜→进球多→大球, 中等相关
    ("1x2", "hc"): 0.85,     # 让球和独赢高度重叠
    ("1x2", "btts"): 0.30,   # 弱相关
    ("1x2", "ht"): 0.45,     # 半场结果与全场中等相关
    ("1x2", "dc"): 0.90,     # 双重机会与独赢几乎重叠
    ("1x2", "dnb"): 0.95,    # 平局退款几乎等于独赢
    # OU vs others
    ("ou", "ou"): 1.0,
    ("ou", "hc"): 0.30,      # 让球和大小球弱相关
    ("ou", "btts"): 0.55,    # BTTS 与大球中等相关
    ("ou", "ht"): 0.40,      # 半场大小球相关
    ("ou", "dc"): 0.25,
    ("ou", "dnb"): 0.30,
    # HC vs others
    ("hc", "hc"): 1.0,
    ("hc", "btts"): 0.25,
    ("hc", "ht"): 0.50,      # 半场让球相关
    ("hc", "dc"): 0.80,
    ("hc", "dnb"): 0.85,
    # BTTS vs others
    ("btts", "btts"): 1.0,
    ("btts", "ht"): 0.30,
    ("btts", "dc"): 0.20,
    ("btts", "dnb"): 0.25,
    # HT vs others
    ("ht", "ht"): 1.0,
    ("ht", "dc"): 0.40,
    ("ht", "dnb"): 0.45,
    # DC/DNB
    ("dc", "dc"): 1.0,
    ("dc", "dnb"): 0.70,
    ("dnb", "dnb"): 1.0,
}


def _cross_market_correlation_discount(group: list) -> float:
    """计算同场多盘口的联合 Kelly 折扣因子。

    当同一场比赛投注多个盘口时，由于盘口间的结构性相关，
    独立 Kelly 会高估联合优势。此函数计算折扣以修正。

    discount = 1 / sqrt(1 + avg_corr × (n - 1))
    - n=1: discount=1.0 (无折扣)
    - n=2, corr=0.85 (1x2+hc): discount≈0.74 (减 26%)
    - n=2, corr=0.35 (1x2+ou): discount≈0.86 (减 14%)
    - n=3, mixed: discount≈0.65-0.75
    """
    n = len(group)
    if n <= 1:
        return 1.0

    # 提取每个机会的市场类型
    markets = []
    for o in group:
        sub = o.get("_sub_market", o.get("_market", ""))
        # 归一化市场名
        if sub in ("handicap", "hc"):
            markets.append("hc")
        elif sub in ("over_under", "ou"):
            markets.append("ou")
        elif sub in ("btts",):
            markets.append("btts")
        elif sub in ("ht", "half_time"):
            markets.append("ht")
        elif sub in ("double_chance", "dc"):
            markets.append("dc")
        elif sub in ("draw_no_bet", "dnb"):
            markets.append("dnb")
        else:
            markets.append("1x2")

    # 计算平均成对相关性
    total_corr = 0.0
    pair_count = 0
    for i in range(n):
        for j in range(i + 1, n):
            m1, m2 = markets[i], markets[j]
            # 对称查找
            corr = _MARKET_CORRELATION.get((m1, m2), _MARKET_CORRELATION.get((m2, m1), 0.3))
            total_corr += corr
            pair_count += 1

    if pair_count == 0:
        return 1.0

    avg_corr = total_corr / pair_count

    # 折扣公式: 基于联合 Kelly 近似
    # discount = 1 / (1 + avg_corr × (n-1) × 0.5)
    # 确保折扣不低于 0.4 (最多减 60%)
    import math
    discount = 1.0 / (1.0 + avg_corr * (n - 1) * 0.5)
    return max(0.4, discount)


def _correct_budget_tracker(opps: list):
    """推送成功后重算预算消耗，排除因指纹去重被过滤的机会。"""
    spent, today = _load_budget_tracker()
    total = sum(o["_stake"] for o in opps if o["_stake"] > 0)
    # 保留当天之前推送的累积，加上本次实际
    prev_total = sum(spent.values()) if spent else 0
    _save_budget_tracker({"total": prev_total + total}, today)


# 上一次 BB 赔率快照缓存 (用于蒸汽移动检测)
_SNAPSHOT_CACHE = None


def _load_snapshot_cache():
    """加载上一次的 BB 赔率快照，用于检测赔率蒸汽移动。"""
    global _SNAPSHOT_CACHE
    if _SNAPSHOT_CACHE is not None:
        return _SNAPSHOT_CACHE
    snap_file = DATA_DIR / "bb_odds_snapshot.json"
    _SNAPSHOT_CACHE = {}
    if snap_file.exists():
        try:
            data = json.loads(snap_file.read_text())
            matches = data.get("matches", {})
            # matches 可能是 dict (keyed by event_id) 或 list
            if isinstance(matches, dict):
                match_list = matches.values()
            else:
                match_list = matches
            for m in match_list:
                if not isinstance(m, dict):
                    continue
                home = (m.get("home_cn") or m.get("home") or "").strip()
                away = (m.get("away_cn") or m.get("away") or "").strip()
                if not home or not away:
                    continue
                for mk in ("moneyline", "handicap", "over_under", "ml", "hc", "ou"):
                    market_data = m.get(mk)
                    if not market_data:
                        continue
                    # market_data 可能是 list 或 dict
                    items = market_data if isinstance(market_data, list) else [market_data]
                    for opp in items:
                        if not isinstance(opp, dict):
                            continue
                        desig = opp.get("designation", "")
                        key = f"{home}|{away}|{desig}"
                        _SNAPSHOT_CACHE[key] = opp.get("odds", opp.get("bb_odds", 0))
        except (json.JSONDecodeError, OSError):
            pass
    return _SNAPSHOT_CACHE


def _lookup_snapshot_odds(home: str, away: str, designation: str) -> float:
    """查找上一次快照中对应比赛的 BB 赔率。0 = 无快照数据。"""
    cache = _load_snapshot_cache()
    key = f"{home}|{away}|{designation}"
    return cache.get(key, 0)


def _lookup_bb_price_for_fb_match(match, market_key):
    """FB-only 匹配回查 BB 数据，找到同场比赛的 BB 赔率。

    Returns: dict {designation: bb_odds} 或 None。
    如果 BB 价格更好，调用者用它替换 FB 价格。
    """
    try:
        from src.scrapers.bb_data import load_bb_odds
        bb_matches = load_bb_odds()
    except Exception:
        return None

    home = match.get("home_bb", "").strip()
    away = match.get("away_bb", "").strip()
    if not home or not away:
        return None

    # 队名匹配: 优先完全匹配，其次模糊匹配
    for m in bb_matches:
        m_home = m.get("home", "").strip()
        m_away = m.get("away", "").strip()
        # 简单匹配
        if home in m_home and away in m_away:
            odds_ft = m.get("odds_ft", {})
            result = {}
            if market_key == "opportunities":
                ml = odds_ft.get("ml", [])
                labels = ["主胜", "和局", "客胜"]
                for i, lbl in enumerate(labels):
                    if i < len(ml) and ml[i]:
                        result[lbl] = ml[i]
            elif market_key == "over_under":
                ou = odds_ft.get("ou", [])
                if len(ou) >= 2:
                    result["大球"] = ou[0]
                    result["小球"] = ou[1]
            return result if result else None
    return None


def _verify_bb_price_exists(home: str, away: str, designation: str,
                            expected_price: float, market_key: str,
                            line=None) -> bool:
    """验证比较引擎给出的 BB 价格是否真实存在于 BB 原始数据中。

    防止 home/away 反转、线错配等 bug 产生 phantom price。
    """
    if expected_price <= 0:
        return False
    # line 安全转换 (JSON 可能返回 string)
    if line is not None:
        try: line = float(line)
        except (ValueError, TypeError): line = None
    try:
        from src.scrapers.bb_data import load_bb_odds
        bb_matches = load_bb_odds()
    except Exception:
        return True

    if not home or not away:
        return True

    for m in bb_matches:
        m_home = m.get("home", "").strip()
        m_away = m.get("away", "").strip()
        # 宽松匹配: 比较引擎可能对队名做了归一化 (去国家/去空格),
        # 逐个检查 home/away 是否出现在 BB 数据中, 允许反转
        home_in_home = home in m_home or m_home in home
        away_in_away = away in m_away or m_away in away
        home_in_away = home in m_away or m_away in home
        away_in_home = away in m_home or m_home in away
        if not ((home_in_home and away_in_away) or (home_in_away and away_in_home)):
            continue

        odds_ft = m.get("odds_ft", {})
        candidates = []

        # V5.2: DC/DNB/BTTS 是推导盘口, BB 原始数据无对应字段, phantom 价格检查不适用
        if market_key in ("double_chance", "draw_no_bet", "btts"):
            return True

        if market_key == "opportunities":
            ml = odds_ft.get("ml", [])
            candidates = [o for o in ml if o]
            ht = odds_ft.get("ht", {})
            if isinstance(ht, dict):
                ht_ml = ht.get("ml", [])
                if ht_ml:
                    candidates.extend([o for o in ht_ml if o])

        elif market_key == "handicap":
            hc = odds_ft.get("handicap", {})
            if isinstance(hc, dict):
                bb_home_line = hc.get("home_line")
                bb_away_line = hc.get("away_line")
                # 检查线是否匹配
                if line is not None:
                    line_matches = False
                    for bl in [bb_home_line, bb_away_line]:
                        if bl is not None and abs(abs(bl) - abs(line)) <= 0.6:
                            line_matches = True
                            break
                    if not line_matches:
                        # 检查 alternate lines
                        for alt in odds_ft.get("alternate_handicaps", []):
                            al = alt.get("home_line") or alt.get("away_line")
                            if al is not None and abs(abs(al) - abs(line)) <= 0.6:
                                candidates.extend([alt.get("home_odds", 0), alt.get("away_odds", 0)])
                                line_matches = True
                                break
                    if not line_matches:
                        return False  # 线不存在于 BB
                if not candidates:
                    candidates = [hc.get("home_odds", 0), hc.get("away_odds", 0)]

        elif market_key == "over_under":
            ou = odds_ft.get("total", {})
            if isinstance(ou, dict):
                candidates = [ou.get("over_odds", 0), ou.get("under_odds", 0)]

        # 验证: 预期价格是否在 BB 数据中 (3% 容差)
        for c in candidates:
            if c and abs(c - expected_price) / c < 0.03:
                return True
        return False  # phantom price

    return True  # 比赛不在 BB 数据中，放行


def _ml_dir_source(platform_sources, sub_market, designation):
    """返回该方向(主/和/客)真正的赔率来源平台(BB/FB)。

    之前 platform_sources["ml"] 是整场单值, FB 主/和高就整场标 FB, 导致
    "客胜其实 BB 更高、却标 FB价"的错标(2026-08-23 用户反馈)。现按 ml_dir/ht_ml_dir
    逐方向取来源, 取不到回退 None(调用方用整场 price_source)。
    """
    des = (designation or "").lower()
    key = "ht_ml_dir" if str(sub_market).startswith("ht") else "ml_dir"
    dirs = platform_sources.get(key)
    if not isinstance(dirs, list):
        return None
    if "和" in des or "draw" in des:
        idx = 1
    elif "客" in des or "away" in des:
        idx = 2 if len(dirs) >= 3 else 1
    else:  # 主
        idx = 0
    if idx < len(dirs):
        return dirs[idx]
    return None


def _collect_opportunities(match, market_key):
    """从指定市场收集 +EV 机会。校准过滤：时间匹配必须高分才推送。

    为每条机会附加 bb_price_source 字段，标记该赔率来自哪个平台（BB/FB）。
    """
    sport = match.get("sport", "")
    # V4.5: 运动差异化窗口 — 非足球放宽到5天
    pin_epoch = match.get("start_time_pin_epoch")
    bb_epoch = _parse_bb_time(match.get("start_time_bb", ""))
    now_epoch = datetime.now(timezone.utc).timestamp()
    # 取 BB 和 Pin 中较早的时间作为开赛时间
    effective_epoch = min(
        pin_epoch if pin_epoch else float('inf'),
        bb_epoch if bb_epoch else float('inf'),
    )
    if effective_epoch != float('inf'):
        # V4.5: 运动差异化窗口 — 非足球比赛少, 放宽到5天
        if sport == "football":
            max_hours = 72
        else:
            max_hours = 120  # 5天: 其他运动比赛少, 不能太挑
        if effective_epoch > now_epoch + max_hours * 3600:
            return []
        if effective_epoch + 300 < now_epoch:
            return []

    match_type = match.get("match_type", "unknown")
    match_score = match.get("match_score", 0.7)
    # sport 已在上方提取
    # 时间匹配（非队名匹配）需要高置信度，防止推错比赛。
    # 门限必须与 bb_vs_pinnacle.py Phase 2 保持一致：
    #   网球 0.75，其他 0.70
    # 注: MMA/拳击的时间匹配已在 _read_comparison_file 整场跳过，此处是兜底
    if match_type == "time":
        min_ok = 0.45 if sport == "tennis" else 0.70  # V4.5: 网球日期无关匹配放宽
        if match_score < min_ok:
            return []
    league = match.get("league", "")
    # V5.7: 用 BB 中文联赛名匹配 V5(键是中文全称/简称, 精确匹配, 不用子串模糊)
    league_cn = match.get("league_cn") or league
    home_cn = match.get("home_bb_cn") or match.get("home_bb", "")
    away_cn = match.get("away_bb_cn") or match.get("away_bb", "")
    league_mult = league_multiplier(league_cn)

    # 屏蔽不靠谱联赛 (中英文都要检查 — BB EN切换后英文名不含中文关键词, 需同时查中文名)
    for banned in _BANNED_LEAGUES:
        if banned in league or banned in league_cn:
            return []

    # CLV 趋势暂停检查：连续负 CLV → 暂时跳过该 (sport, league)
    if (match.get("sport", ""), league) in _get_clv_suspensions():
        return []

    # 2-way运动独赢价格校验: BB/Pin差>2x→市场错配(如让分混入独赢)
    flags_check = match.get("flags", [])
    if any("独赢价格异常" in f for f in flags_check) and market_key == "opportunities":
        return []  # 跳过该场比赛的所有独赢机会

    # 联赛可信度分层过滤
    tier = _get_league_tier(league_cn)
    if tier == 4:
        pass  # V4.3: Tier4不再封杀, 允许推送(低权重)
    # Tier 2/3: 非队名匹配且匹配分<0.80 不推送（防假阳性）
    if tier >= 2 and match_type != "name" and match_score < 0.80:
        return []
    min_ev = _min_ev_for_tier(tier, sport=match.get("sport", ""), league=league_cn)

    # 确定该市场类型对应哪个平台提供了最高赔率
    _MK_TO_SOURCE_KEY = {
        "opportunities": "ml",
        "handicap": "handicap",
        "over_under": "ou",
        "double_chance": "dc",
        "draw_no_bet": "dnb",
        "btts": "btts",
    }
    platform_sources = match.get("platform_sources", {})
    source_key = _MK_TO_SOURCE_KEY.get(market_key, "ml")
    price_source = platform_sources.get(source_key, match.get("bb_price_source", "BB"))

    # V4.2: FB-only 匹配回查 BB 数据 — 用更好的价格
    #   场景: BB通道队名映射失败, FB通道成功但只用了FB价格
    #   如果 BB 同场比赛有更好赔率 → 用 BB 价格
    if price_source == "FB" and not platform_sources:
        _bb_odds = _lookup_bb_price_for_fb_match(match, market_key)
        if _bb_odds:
            price_source = "BB/FB"  # 标记两个平台都有
        # V5: 美式足球FB独赢不可靠 — FB盘口结构与Pin不对齐, 赔率错配高频
        elif sport == "american_football":
            return []  # 直接拒绝FB-only的AF机会

    result = []
    # FB-only 匹配回查 BB 价 (在循环外查一次, 在循环内按 designation 替换)
    bb_better_prices = None
    if price_source == "BB/FB" and "_bb_odds" in dir():
        bb_better_prices = _bb_odds

    for opp in match.get(market_key, []):
        ev = opp.get("ev_pct", 0)
        bb_odds = opp.get("bb_odds", 0)
        pin_odds = opp.get("pin_odds", 0)

        # 🔴 终极保护: 验证 BB 价格是否真实存在于 BB 数据中
        # 防止 home/away 反转、线错配等比较引擎 bug 产生 phantom price
        # V4.5: name匹配+高分(>=0.95) → 队名已确认, 跳过价格验证
        if match_type == "name" and match_score >= 0.95:
            _price_ok = True
        else:
            # 转换 line 为 float (JSON 可能返回 string)
            _raw_line = opp.get("line")
            try: _line = float(_raw_line) if _raw_line else None
            except (ValueError, TypeError): _line = None

            _price_ok = _verify_bb_price_exists(
                match.get("home_bb", ""), match.get("away_bb", ""),
                opp.get("designation", ""), bb_odds, market_key,
                _line
            )
        if not _price_ok:
            continue  # 价格不真实，跳过此机会

        # FB-only 匹配: 如果 BB 有同样 designation 的更好价格 → 替换
        designation_orig = opp.get("designation", "")
        if bb_better_prices and designation_orig in bb_better_prices:
            bb_better = bb_better_prices[designation_orig]
            if bb_better > bb_odds:
                bb_odds = bb_better
                # 重新计算 EV (用原 fair_price)
                fair = opp.get("fair_price", 0)
                if fair > 0:
                    ev = round((bb_better - fair) / fair * 100, 2)

        # 市场子类型识别：区分同一 market_key 下的不同市场（如 1X2 / HT / BTTS / DC）
        # ⚠️ 必须在使用 sub_market 之前定义（V4 get_min_ev/get_odds_cap 需要）
        sub_market = opp.get("_market", "")
        if not sub_market or sub_market == "main":
            # 根据 market_key 推导子类型
            _MK_TO_SUB = {
                "opportunities": "1x2",
                "handicap": "hc",
                "over_under": "ou",
                "double_chance": "dc",
                "draw_no_bet": "dnb",
                "btts": "btts",
            }
            sub_market = _MK_TO_SUB.get(market_key, "1x2")

        # ── V2 动态 EV 门槛: 赔率越高 → 门槛越高 ──
        # V4 的 get_min_ev 基于 Pinnacle 107K场数据
        from config.weight_matrix_v5 import get_min_ev
        v3_min_ev = get_min_ev(match.get("sport", ""), league_cn, sub_market, bb_odds)
        # V5: FB独有机会降门槛 — FB赔率比BB更接近Pin, edge天然低, 含金量更高
        if price_source == "FB" and not platform_sources:
            v3_min_ev = max(1.0, v3_min_ev * 0.6)  # 降到60%
        # V5.5: 热门 devig 交叉验证 — Shin 与比例法分歧>3% 且是热门(低赔)时,
        #        Shin 可能高估热门 EV, 把门槛 +2% 补偿 devig 方法不确定性。
        if (any("热门devig分歧" in f for f in match.get("flags", []))
                and bb_odds < 2.5):
            v3_min_ev += 2.0
        # V5.1: WNBA 的 OU UNDER(小球)是-EV(-20.8%), 提高门槛而非过滤
        # 108场实测: 买OVER +10.9%, 买UNDER -20.8% → 市场系统性低估WNBA高进球
        # (必须在 ev<v3_min_ev 检查之前生效, 否则是死代码不生效)
        if (match.get("sport", "") == "basketball" and "WNBA" in (league or "")
                and sub_market == "ou"):
            _des = opp.get("designation", "") or ""
            if "小" in _des or "under" in _des.lower():
                v3_min_ev = max(v3_min_ev, 3.0)  # 小球提高门槛到3% (补偿-20.8%负期望)

        if ev < v3_min_ev:
            continue

        # 同时也要过旧的 Tier 底线（兜底）
        if ev < min_ev:
            continue

        # V5.10 数据驱动门槛矩阵(盘口层+运动层+临场规则) — 每晚由 compute_ev_thresholds 重算
        _lead_min = (pin_epoch - time.time()) / 60 if pin_epoch else None
        _mtx_ev = _matrix_min_ev(sub_market, match.get("sport", ""),
                                  league=match.get("league", ""), lead_minutes=_lead_min)
        if _mtx_ev is not None and ev < _mtx_ev:
            continue

        # EV 上限过滤：高赔率天然高 EV，用动态上限防假阳性
        # 特殊盘口(先进球/正确比分/半场/角球等)Pin定价粗, 高EV可能是真机会不是错配
        # (用户 2026-08-23: 先进球不要因高EV拦截, 先判错配, 没错配就收集数据驱动)。
        # 只对主体盘口(1x2/hc/ou)用12%上限; 特殊盘口放宽到50%, 靠队名匹配score+CLV数据拦截。
        if sub_market in ("1x2", "hc", "ou"):
            _dynamic_cap = max(EV_CAP, (bb_odds - 1) * 20)
        else:
            _dynamic_cap = max(50.0, (bb_odds - 1) * 40)
        if ev > _dynamic_cap:
            continue

        # ── V4 赔率上限: 基于 Pinnacle 全量数据 ──
        from config.weight_matrix_v5 import get_odds_cap
        _odds_cap = get_odds_cap(match.get("sport", ""), league_cn, sub_market)
        if _odds_cap > 0 and bb_odds > _odds_cap:
            continue

        # HTFT/半全场 EV 上限：此类市场 Pinnacle 盘口常与 BB 不是同一市场
        # (如 Pinnacle "半全场" 含加时 vs BB 不含)，导致假 EV 极高
        # 收紧上限 50%→30%，降低推送虚高机会的风险
        if sub_market == "htft" and ev > 30:
            continue

        # V4.2: 溢价异常高检查改为 per-opportunity (不再用 match-level flag 误伤无辜)
        flags = match.get("flags", [])

        # 球员冲突 = 同一队出现在 Pin 多场比赛(赛前+滚球两个 matchup), 匹配到的 Pin 价
        # 可能来自错误的场次 → 假 EV(实测 MLB 赛前[1.56,2.59] 被滚球[...,1.91] 错配, 假EV 23.67%)。
        # 直接过滤, 不推送(用户 2026-08-23 反馈)。
        if any("球员冲突" in f for f in flags):
            continue

        # 仅保留 HTFT ev>30 的直接封杀（BB/Pin 半全场定义不一致，产品不同）
        if sub_market == "htft" and ev > 30:
            continue

        # V5: MMA/Boxing 全封 — 中文译名到英文的映射不存在, 所有匹配都是假阳性
        if sport in ("mma", "boxing"):
            continue
        # V5: ITF/Challenger网球 + 溢价异常高 → 球员名错配高频, 直接拒绝
        # V5.1: ITF/Challenger/W系列 全部直接拒绝 (Pinnacle无覆盖/流动性差/诚信风险)
        if sport == "tennis":
            is_low_tier = any(kw in (league or "").lower() for kw in ("itf", "challenger", "w15", "m15", "w25", "m25", "w35", "w50", "w75", "挑战赛"))
            if is_low_tier:
                continue
        # MMA/拳击: BB 与 Pinnacle 赔率偏差 >25% → 映射错误
        if sport in ("mma", "boxing") and pin_odds > 0:
            dev = abs(bb_odds - pin_odds) / pin_odds
            if dev > 0.25:
                continue

        market_w = _get_market_weight(sub_market, match.get("sport", ""))

        # 加权 EV：原始 EV × 市场质量权重
        # 用于排序（市场权重低的如 1x2 需要更高 EV 才能排在前面）
        weighted_ev = round(ev * market_w, 2)

        # 原始 EV 已通过 min_ev 检查，加权 EV 不用于过滤（仅排序）
        # Kelly 用原始 EV，不用加权（投注额基于实际 edge）
        fair = opp.get("fair_price") or round(pin_odds, 2)
        kelly_pct = 0
        if bb_odds > 1:
            # 按市场类型使用不同 Kelly（基于回测 ROI）
            market_kelly = _get_kelly_for_market(sub_market)
            kelly = (ev / 100) / (bb_odds - 1) * market_kelly
            kelly_pct = round(kelly * 100 * league_mult, 2)

        # 综合评分：加权溢价 × 匹配度 × 联赛权重
        score = round(weighted_ev * match_score * league_mult, 2)

        # 带盘口信息的显示名
        desig = opp.get("designation", "")
        line = opp.get("line", "")
        display_name = f"{desig}({line})" if line else desig

        result.append({
            "sport": match.get("sport", ""),
            "league": league,
            "league_cn": match.get("league_cn") or league,
            "home_cn": home_cn,
            "away_cn": away_cn,
            "home_team": match.get("home_pin", home_cn),
            "away_team": match.get("away_pin", away_cn),
            "designation": display_name,
            "bb_odds": bb_odds,
            "pin_odds": pin_odds,
            "fair_price": fair,
            "_snapshot_bb_odds": _lookup_snapshot_odds(home_cn, away_cn, display_name),
            "ev_pct": ev,
            "_warn": "⚠️ 溢价异常高，请核对" if ev > 20 else "",
            "start_time_bb": match.get("start_time_bb", ""),
            "bb_match_id": match.get("bb_match_id", ""),  # BB比赛ID, 结算按ID匹配
            "_pin_league_id": match.get("pin_league_id", ""),  # V5.9: 供CLV采集按ID直拉
            "_pin_match_id": match.get("pin_match_id", ""),
            "_market_type": market_key,  # "opportunities"|"handicap"|"over_under"|...
            "_sub_market": sub_market,  # "1x2"|"ht"|"btts"|"dc"|"oe"|"htft"|...
            "line": line,               # 盘口线 (让球/大小), 供二次验价+虚拟投注
            "pin_max_stake": match.get("pin_max_stake", ""),  # Pin 注额上限=定价信心, 进CLV库供后续分析
            "_match_type": match_type,  # "name"|"time", 供低置信时间匹配降仓
            "_ml_swapped": any("主客反转" in f for f in match.get("flags", [])),  # 二次验价方向映射用

            "_match_score": match_score,
            "_score": score,
            "_weighted_ev": weighted_ev,
            "_market_weight": market_w,
            "_kelly_pct": kelly_pct,
            "_tier": tier,
            "_pin_epoch": match.get("start_time_pin_epoch"),  # 用于显示开赛时间
            "bb_price_source": _ml_dir_source(platform_sources, sub_market,
                                              opp.get("designation", "")) or price_source,
        })
    return result


def _parse_bb_time(start_time_bb: str):
    """将 BB 时间字符串 'MM/DD HH:MM'（北京时间）转 UTC epoch。失败返回 None。"""
    if not start_time_bb or not start_time_bb.strip():
        return None
    try:
        # 补齐年份：用当前年份，但如果 MM/DD 跨年则用上一年
        now = datetime.now(timezone(timedelta(hours=8)))
        year = now.year
        dt_bj = datetime.strptime(f"{year}/{start_time_bb}", "%Y/%m/%d %H:%M")
        dt_bj = dt_bj.replace(tzinfo=timezone(timedelta(hours=8)))
        # 如果解析出的日期比现在晚超过6个月，可能是跨年
        if (dt_bj - now).days > 180:
            dt_bj = datetime.strptime(f"{year-1}/{start_time_bb}", "%Y/%m/%d %H:%M")
            dt_bj = dt_bj.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt_bj.timestamp()
    except (ValueError, OSError):
        return None


def _format_bj_time(pin_epoch):
    """Convert Pinnacle UTC epoch to Beijing time string 'MM/DD HH:MM'."""
    if not pin_epoch:
        return ""
    try:
        dt = datetime.fromtimestamp(pin_epoch, tz=timezone.utc)
        bj = dt.astimezone(timezone(timedelta(hours=8)))
        return bj.strftime("%m/%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


def _collect_opportunities_from_file():
    """从对比文件收集所有 +EV 机会，返回 raw qualified list（未排序/未 Kelly）。

    同时读取主对比(BB+FB合并)和FB独立对比文件，去重合并。
    同场比赛同一盘口取最高赔率（跨文件联赛名可能不同导致指纹不匹配）。
    """
    # 全量扫描(PUSH_LABEL 含"全量扫描")读 MAIN(24-72h远盘); 增量扫描只读 near/urgent(<24h),
    # 不读 MAIN —— 否则远盘机会被增量提前推掉, 全量扫描反而没新机会(2026-08-26 用户反馈)。
    import os as _os
    _is_full_scan = "全量扫描" in _os.environ.get("PUSH_LABEL", "")
    # 读取主对比文件(仅全量扫描)
    main_opps = _read_comparison_file(COMPARISON_FILE) if _is_full_scan else []
    # 读取 near/far/urgent 独立对比文件
    near_opps = _read_comparison_file(COMPARISON_FILE_NEAR)
    far_opps = _read_comparison_file(COMPARISON_FILE_FAR)
    urgent_opps = _read_comparison_file(COMPARISON_FILE_URGENT)
    if near_opps: main_opps.extend(near_opps)
    if far_opps: main_opps.extend(far_opps)
    if urgent_opps: main_opps.extend(urgent_opps)
    # 读取辅助对比文件 (the-odds-api: WNBA/NCAAF/Boxing等)
    aux_opps = _read_comparison_file(ODDSAPI_COMPARISON_FILE)
    # 合并辅助对比机会 (WNBA/NCAAF/Boxing等)
    if aux_opps:
        main_opps.extend(aux_opps)
        logger.info("辅助对比(the-odds-api): 添加 %d 个机会", len(aux_opps))

    # FB 已通过 --with-fb 合并进主对比(取最高赔率), 不再单独读 FB 对比文件(2026-08-23)
    merged = list(main_opps)

    # 二次去重：同场比赛同一盘口只保留最高赔率。
    # 队名可能因 BB/FB 平台翻译不同而完全不同 (如 "弗雷斯尼洛虾" vs "甘布西诺弗雷斯尼洛")，
    # 不能仅靠队名字符匹配。改用 比赛时间 + 参赛方 交叉比对。
    best_per_match = {}
    dup_removed = 0
    for o in merged:
        designation = o.get("designation", "").replace(" ", "").replace("（", "(").replace("）", ")")
        sport = o.get("sport", "")
        league = _normalize_cn(o.get("league", "") or "")
        home = _normalize_cn(o.get("home_cn", "").strip())
        away = _normalize_cn(o.get("away_cn", "").strip())
        start = o.get("start_time_bb", "") or str(o.get("_pin_epoch", ""))

        # V4.4: 四层 key — BB/FB 队名翻译可能完全不同, 加纯时间key
        # 联赛名可能因 BB/FB 翻译不同而不同 (如 "欧足联欧洲会议联赛" vs "欧足联欧洲协会联赛")
        key_exact = (sport, league, home, away, designation)
        key_home_time = (sport, home, start, designation)
        key_away_time = (sport, away, start, designation)
        # 兜底key含联赛, 避免同运动同时刻同盘口的不同比赛被误合并(周六22:00十场足球全推"主胜")
        key_time_only = (sport, league, start, designation)      # BB/FB队名完全不同时兜底

        existing = (best_per_match.get(key_exact) or
                    best_per_match.get(key_home_time) or
                    best_per_match.get(key_away_time) or
                    best_per_match.get(key_time_only))

        if existing is None or o.get("bb_odds", 0) > existing.get("bb_odds", 0):
            # 替换旧条目时，清理指向旧对象的所有 key
            if existing is not None:
                stale = [k for k, v in best_per_match.items() if v is existing]
                for k in stale:
                    del best_per_match[k]
            for k in (key_exact, key_home_time, key_away_time, key_time_only):
                best_per_match[k] = o

    unique_count = len({id(v) for v in best_per_match.values()})
    if unique_count < len(merged):
        dup_removed = len(merged) - unique_count
        merged = list({id(v): v for v in best_per_match.values()}.values())
        logger.info("同场去重: 移除 %d 个较低赔率机会，保留 %d 个", dup_removed, len(merged))

    return merged


def _read_comparison_file(path):
    """读取单个对比文件，返回机会列表。"""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    details = data.get("details", [])
    qualified = []
    for match in details:
        # 低匹配度过滤：非足球运动提高门槛（拳击/MMA 映射错误率高）
        match_score = match.get("match_score", 1.0)
        match_type = match.get("match_type", "")
        sport = match.get("sport", "")
        if sport == "football":
            min_score = 0.85  # 比赛多，高标准
        elif sport in ("boxing", "mma"):
            # V4.5: 中英文名依赖Phase2 odds+time, 放宽门槛(比赛少)
            min_score = 0.70
        elif sport == "tennis":
            min_score = 0.45  # 日期无关匹配分低, 但有赔率+拼音双重验证
        else:
            min_score = 0.70  # 非主力运动比赛少，放宽
        if match_score < min_score:
            continue
        # 球员冲突 + 时间匹配 → 过期数据，直接跳过整场比赛
        flags = match.get("flags", [])
        # V5: 网球ITF低级别 + 球员冲突 → 肯定是错配, 直接跳过
        if sport == "tennis" and any("球员冲突" in f for f in flags) and any("溢价异常高" in f for f in flags):
            continue
        if sport != "tennis" and match_type == "time" and any("球员冲突" in f for f in flags):
            continue
        for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
            qualified.extend(_collect_opportunities(match, mk))
    return qualified


def _diversify_and_rank(qualified: list) -> list:
    """多样性选择 + 按联赛 Tier 排序 + Kelly 分配。

    保证: 每种运动至少 1 条 + 每种盘口类型至少 N 条，降低集中风险。
    """
    if not qualified:
        return []

    selected = []
    selected_ids = set()

    # --- 第一轮：每种运动至少 1 条 ---
    # V5.10: 乒乓/羽毛球 已封杀(零Pinnacle数据)。拳击/排球 Pinnacle 有数据, 8-17 已解封
    # (weight_matrix BLOCKED_SPORTS 只含 pingpong/badminton), 这里补回 boxing。
    for sport in ("football", "basketball", "tennis", "baseball", "american_football",
                   "mma", "boxing", "ice_hockey"):
        sport_opps = [o for o in qualified if o.get("sport") == sport]
        if sport_opps:
            best = max(sport_opps, key=lambda x: (4 - x.get("_tier", 3), x["_score"]))
            selected.append(best)
            selected_ids.add(id(best))

    # --- 第二轮：每种盘口类型至少 N 条（分散风险） ---
    MARKET_MIN = {
        "1x2": 3,   # 独赢 主/和/客
        "hc": 2,     # 让球
        "ou": 3,     # 大小球
        "dc": 2,     # 双重机会
        "ht": 1,     # 上半场
        "btts": 1,   # 双边进球
        "dnb": 1,    # 平局退款
        "htft": 1,   # 半全场
        "oe": 1,     # 单/双
    }
    remaining_for_market = [o for o in qualified if id(o) not in selected_ids]
    for sub_market, min_n in MARKET_MIN.items():
        candidates = [o for o in remaining_for_market
                      if o.get("_sub_market") == sub_market and id(o) not in selected_ids]
        candidates.sort(key=lambda o: (o.get("_tier", 3), -o["_score"]))
        for o in candidates[:min_n]:
            selected.append(o)
            selected_ids.add(id(o))

    # --- 第三轮：V5 Bet Everything — 有edge就上, 不限数量 ---
    remaining = [o for o in qualified if id(o) not in selected_ids]
    remaining.sort(key=lambda o: (o.get("_tier", 3), -o["_score"]))
    selected.extend(remaining)  # V5: no cap
    qualified = selected

    # 最终展示排序：按运动 → Tier → 联赛名 → 开赛时间（同联赛紧挨着）
    qualified.sort(key=lambda o: (
        SPORT_ORDER.get(o.get("sport", ""), 99),
        o.get("_tier", 3),
        o.get("league", "") or "",
        o.get("_pin_epoch") if o.get("_pin_epoch") else 9999999999,
    ))

    # Kelly 分配（预算耗尽时保留机会，stake=0 仅展示不投注）
    qualified = _calc_kelly_stakes(qualified)
    # V5.10(2026-08-22 用户要求): 不再因 stake 小就屏蔽 —— _calc_kelly_stakes 已保证
    # 每个过了盘口门槛的机会 _stake>=10, 这里无需再过滤。

    qualified.sort(key=lambda o: o.get("_stake", 0), reverse=True)

    return qualified


def _compute_sport_summary():
    """从对比文件和BB数据计算各运动的比赛数、匹配数和机会数。

    Returns:
        dict: {sport: {"total": N, "matched": N, "opps_ge_1": N, "opps_ge_2": N}}
    """
    SPORTS = {"football": "足球", "basketball": "篮球", "tennis": "网球",
              "baseball": "棒球", "american_football": "美式足球",
              "pingpong": "乒乓球", "badminton": "羽毛球", "volleyball": "排球",
              "boxing": "拳击", "mma": "MMA", "ice_hockey": "冰球"}
    summary = {s: {"total": 0, "matched": 0, "opps_ge_1": 0, "opps_ge_2": 0}
               for s in SPORTS}

    # 从 BB 数据读各运动总比赛数
    bb_file = DATA_DIR / "bb_odds_extracted.json"
    if bb_file.exists():
        try:
            bb_data = json.loads(bb_file.read_text())
            from src.scrapers.bb_vs_pinnacle import detect_sport
            for m in bb_data.get("matches", []):
                s = detect_sport(m)
                if s in summary:
                    summary[s]["total"] += 1
        except Exception:
            pass

    # 从对比文件读匹配数据和机会数据（FB 已通过 --with-fb 合并进主对比, 不再单独读 FB 文件）
    for fpath in (COMPARISON_FILE,):
        if fpath.exists():
            try:
                data = json.loads(fpath.read_text())
                ps = data.get("per_sport_matched", {})
                for s, c in ps.items():
                    if s in summary:
                        summary[s]["matched"] += c
                for entry in data.get("details", []):
                    s = entry.get("sport", "")
                    if s not in summary:
                        continue
                    for market_key in ["opportunities", "handicap", "over_under",
                                       "double_chance", "draw_no_bet"]:
                        for opp in entry.get(market_key, []):
                            ev = opp.get("ev_pct", 0)
                            if ev >= 1:
                                summary[s]["opps_ge_1"] += 1
                            if ev >= 2:
                                summary[s]["opps_ge_2"] += 1
            except Exception:
                pass

    return summary


def _format_body(qualified: list, warnings: Optional[list] = None,
                 sport_summary: Optional[dict] = None) -> str:
    """将 qualified 机会列表格式化为钉钉推送文本。按比赛分组，同场多盘口合并显示。"""
    if not qualified:
        return ""

    SPORT_CN = {"football": "⚽ 足球", "basketball": "🏀 篮球", "tennis": "🎾 网球",
                "baseball": "⚾ 棒球", "american_football": "🏈 美式足球",
                "pingpong": "🏓 乒乓球", "badminton": "🏸 羽毛球",
                "volleyball": "🏐 排球", "boxing": "👊 拳击",
                "mma": "🥊 MMA", "ice_hockey": "🏒 冰球"}
    SPORT_EMOJI = {"football": "⚽", "basketball": "🏀", "tennis": "🎾",
                   "baseball": "⚾", "american_football": "🏈",
                   "pingpong": "🏓", "badminton": "🏸",
                   "volleyball": "🏐", "boxing": "👊",
                   "mma": "🥊", "ice_hockey": "🏒"}
    _TIER_LABEL = {1: "T1", 2: "T2", 3: "T3", 4: "T4"}
    now_str = datetime.now(timezone.utc).astimezone().strftime("%m/%d %H:%M")
    total_allocated = sum(o["_stake"] for o in qualified)

    # 数据新鲜度：读取文件 mtime 显示提取时间
    bb_file = DATA_DIR / "bb_odds_extracted.json"
    # V5.1: Pin时间戳取所有对比文件(MAIN+NEAR+FAR)里最新的, 不是固定主文件
    pin_file = COMPARISON_FILE
    bb_time = ""
    pin_time = ""
    pin_stale_warning = ""
    try:
        if bb_file.exists():
            bb_mtime = datetime.fromtimestamp(bb_file.stat().st_mtime, tz=timezone.utc).astimezone()
            bb_time = bb_mtime.strftime("%m/%d %H:%M")
    except (OSError, ValueError):
        pass
    try:
        _all_pin_files = [f for f in (COMPARISON_FILE, COMPARISON_FILE_NEAR, COMPARISON_FILE_FAR) if f.exists()]
        if _all_pin_files:
            pin_file = max(_all_pin_files, key=lambda f: f.stat().st_mtime)
            pin_mtime = datetime.fromtimestamp(pin_file.stat().st_mtime, tz=timezone.utc).astimezone()
            pin_time = pin_mtime.strftime("%m/%d %H:%M")
            # 检测 Pinnacle 数据过期
            pin_age_min = (datetime.now(timezone.utc).astimezone() - pin_mtime).total_seconds() / 60
            if pin_age_min > 120:
                pin_stale_warning = f" ⚠️{int(pin_age_min / 60)}h过期"
            elif pin_age_min > 60:
                pin_stale_warning = " ⚠️过期"
    except (OSError, ValueError):
        pass

    # 来源平台统计
    source_counts = {}
    for o in qualified:
        src = o.get("bb_price_source", "BB")
        label = {"BB": "BB", "FB": "FB", "BOTH": "BB/FB"}.get(src, src)
        source_counts[label] = source_counts.get(label, 0) + 1
    platform_stats = " | ".join(
        f"{s}价{x}条" for s, x in sorted(source_counts.items())
    )

    # 一致性警告
    warning_lines = []
    if warnings:
        for w in warnings:
            warning_lines.append(f"{w}")
        warning_lines.append("")

    # ── 各运动全景统计（无机会的运动也显示）──
    sport_summary_line = ""
    if sport_summary:
        parts = []
        for s, cn in [("football","足球"),("basketball","篮球"),("tennis","网球"),
                       ("baseball","棒球"),("american_football","美式足球"),
                       ("pingpong","乒乓球"),("badminton","羽毛球"),
                       ("volleyball","排球"),("boxing","拳击"),
                       ("mma","MMA"),("ice_hockey","冰球")]:
            info = sport_summary.get(s, {})
            t = info.get("total", 0)
            o1 = info.get("opps_ge_1", 0)
            o2 = info.get("opps_ge_2", 0)
            emoji = SPORT_EMOJI.get(s, "")
            if t > 0:
                if o2 > 0:
                    parts.append(f"{emoji}{cn}{t}场{o2}个≥2%")
                elif o1 > 0:
                    parts.append(f"{emoji}{cn}{t}场{o1}个≥1%")
                else:
                    parts.append(f"{emoji}{cn}{t}场无+EV")
            else:
                parts.append(f"{emoji}{cn}无数据")
        sport_summary_line = " | ".join(parts)

    # 按比赛分组：先按平台(BB/FB)再按 (sport, league, home_cn, away_cn)
    # V5.7: BB/FB 分开展示, 用户投注时不用反复切换平台
    from collections import OrderedDict
    groups = OrderedDict()
    for o in qualified:
        src = o.get("bb_price_source", "BB")
        platform = "FB" if src == "FB" else "BB"  # BB/FB 两平台都有的算 BB
        gkey = (platform, o.get("sport", ""), o.get("league", ""), o.get("home_cn", ""), o.get("away_cn", ""))
        if gkey not in groups:
            groups[gkey] = []
        groups[gkey].append(o)

    # 组间排序：平台(BB先) → 运动 → Tier → 联赛 → 最早开赛时间（同联赛紧挨着）
    def group_sort_key(item):
        (platform, sport, league, home, away), opps = item
        tier = opps[0].get("_tier", 3)
        min_epoch = min((o.get("_pin_epoch") or 9999999999) for o in opps)
        return (0 if platform == "BB" else 1, SPORT_ORDER.get(sport, 99), tier, league or "", min_epoch)
    sorted_groups = sorted(groups.items(), key=group_sort_key)

    lines = list(warning_lines)
    prev_platform = None
    prev_sport = None
    prev_league = None
    match_idx = 0

    for (platform, sport, league, home, away), opps in sorted_groups:
        # 组内按加权 EV 降序（市场质量高的优先）
        opps.sort(key=lambda o: -o.get("_weighted_ev", o["ev_pct"]))
        # 该组所有机会都因投注额归零被过滤(低于¥30/赔率超上限) → 整组跳过,
        # 否则会推送"只有队名没有赔率/投注额"的空壳标题(2026-08-23 用户反馈)。
        if not any(o.get("_stake", 0) > 0 for o in opps):
            continue

        # 平台分隔：BB/FB 分开, 投注不用切换平台
        if platform != prev_platform:
            if prev_platform is not None:
                lines.append("")
            lines.append("## " + ("🟦 BB 平台" if platform == "BB" else "🟨 FB 平台"))
            prev_platform = platform
            prev_sport = None
            prev_league = None

        sport_label = SPORT_CN.get(sport, "")
        if sport != prev_sport:
            if prev_sport is not None:
                lines.append("")
            lines.append(sport_label)
            prev_sport = sport
            prev_league = None

        tier = opps[0].get("_tier", 3)
        tier_label = _TIER_LABEL.get(tier, "")
        if league != prev_league:
            # V5.1: 风控敏感联赛标注 — 软书限额 sharp 玩家的利基市场
            try:
                from config.league_profile import is_sensitive_league
                _sensitive = is_sensitive_league(league, sport)
            except ImportError:
                _sensitive = False
            _sensitive_tag = " ⚠️风控敏感" if _sensitive else ""
            _league_display = opps[0].get("league_cn") or league
            lines.append(f"  [{tier_label}] {_league_display}{_sensitive_tag}")
            prev_league = league

        match_idx += 1
        bj_time = opps[0].get("start_time_bb", "") or _format_bj_time(opps[0].get("_pin_epoch"))
        time_suffix = f"  ({bj_time})" if bj_time else ""
        lines.append(f"  ##### #{match_idx} {home} 对 {away}{time_suffix}")

        # V4.4: 跨盘口相关性折扣提示
        if len(opps) >= 2:
            disc = opps[0].get("_corr_discount", 1.0)
            if disc < 1.0:
                disc_pct = round((1 - disc) * 100)
                lines.append(f"    🔗 跨盘口折扣 -{disc_pct}% ({len(opps)}盘口联合Kelly)")

        # 去重: 同场比赛同一盘口+赔率只显示一条
        seen_lines = set()
        for o in opps:
            oc = o["designation"]
            line_key = (oc, o.get("bb_odds", 0), o.get("pin_odds", 0), o.get("ev_pct", 0))
            if line_key in seen_lines:
                continue
            seen_lines.add(line_key)
            pinny = round(o.get("pin_odds", 0), 2) if o.get("pin_odds", 0) > 0 else 0
            fair = o.get("fair_price") or round(o["pin_odds"], 2)
            bb_odds = o["bb_odds"]
            ev_pct = o["ev_pct"]
            stake = o["_stake"]
            # V5.1: 分层策略 — T3/T4不显示"建议", 直接过滤
            sport = o.get("sport", "")
            league = o.get("league", "")
            tier = o.get("_tier", 3)
            from config.constants import get_tier_strategy
            tier_cfg = get_tier_strategy(sport, league, tier)

            # V5.1: 分层控制"建议"显示
            if stake < 30:
                if tier_cfg.get("allow_suggest", True):
                    stake = o.get("_raw_stake", stake)
                    stake_note = " (建议)"
                else:
                    # T3/T4 低级别联赛不显示碎单建议, 直接跳过该机会
                    stake = 0
                    stake_note = ""
            else:
                stake_note = ""

            # V5.1: 分层最低投注额
            min_stake = tier_cfg.get("min_stake", 0)
            if min_stake > 0 and stake < min_stake:
                stake = 0
                stake_note = ""

            # 铁律(2026-08-22 no-stop-betting-rule): 投注额归零(赔率超上限/低于最低额)
            # 的机会不得推送 —— 之前注释写"跳过"但缺 continue, 导致"投注:¥0"仍被推。
            if stake <= 0:
                continue
            confidence = "✓" if o.get("_match_score", 0) >= 0.95 else "◷"

            # 来源平台标签
            src = o.get("bb_price_source", "BB")
            if src == "ALL":
                src = "BB/FB"
            source_label = f"{src}价"

            warn = o.get("_warn", "")
            # 让球盘线易移动, 加提示 (避免用户看到线已不存在的推送)
            if o.get("_sub_market", "") in ("hc", "ht_hc") and not warn:
                warn = "⚠️让球线易移动"
            repush = o.get("_repush_reason", "")
            # 同场同盘口的第 2 条线: 标注上一条推的是哪条, 便于对照(用户规则)
            prev_line = o.get("_prev_line")
            lines.append(
                f"    [{oc}] {confidence} 公平价: {fair}"
                + (f" | Pinnacle: {pinny}" if o.get("pin_odds", 0) > 0 else " | 推导: 1X2")
                + f" | {source_label}: {bb_odds} | 溢价: +{ev_pct}% | 投注: ¥{stake:,}{stake_note}"
                + (f" 🔄重推({repush})" if repush else "")
                + (f" 📌本场该盘口上次推的线: {prev_line}" if prev_line else "")
                + (f" {warn}" if warn else "")
            )

    # 数据时间（用文件 mtime，即实际提取时间）
    data_time_parts = []
    if bb_time:
        data_time_parts.append(f"BB数据 {bb_time}")
    if pin_time:
        pin_label = f"Pin数据 {pin_time}{pin_stale_warning}"
        data_time_parts.append(pin_label)
    data_time_str = " | ".join(data_time_parts) if data_time_parts else f"数据 {now_str}"

    title = f"+EV 投注推荐: {match_idx} 场比赛"
    body = (
        f"**{title}**\n\n"
        f"{data_time_str} | 总额 ¥{total_allocated:,}\n"
        + (f"**{sport_summary_line}**\n\n" if sport_summary_line else "")
        + (f"来源: {platform_stats}\n\n" if platform_stats else "\n")
        + "\n".join(lines).strip()
    )
    # V4.5: 联赛级累计 P&L
    pnl_line = _get_league_pnl_summary(qualified)
    body += ("\n\n---\n"
             "💡 T1=Pinnacle最可靠 T2=主流联赛 T3=低级别 | "
             "公平价 = Pinnacle去抽水赔率 | "
             "溢价 = (售价 - 公平价) / 公平价 | "
             "来源: BB=BB价 FB=FB价 BB/FB=两平台相同 | "
             "⚠️ 赔率与盘口线实时变动，下注前请以 BB/Pinnacle 当前盘口为准（让球线尤其易移动）")
    if pnl_line:
        body += f"\n📊 {pnl_line}"
    # 策略参数快照 (Pinnacle 61,404场 + NBA 57,504场真实数据)
    body += (f"\n📐 权重(数据驱动): ⚽OU={MARKET_QUALITY_FOOTBALL.get('ou',1):.2f} "
             f"1X2={MARKET_QUALITY_FOOTBALL.get('1x2',1):.2f} | "
             f"🏀HC={MARKET_QUALITY_BASKETBALL.get('hc',1):.2f} "
             f"OU={MARKET_QUALITY_BASKETBALL.get('ou',1):.2f} | "
             f"Kelly OU={KELLY_BY_MARKET.get('ou',.5):.2f} "
             f"1X2={KELLY_BY_MARKET.get('1x2',.5):.2f} | "
             f"结算: {len(_get_settleable_summary())}联赛已验证")
    return body

def _get_settleable_summary() -> set:
    """获取已验证可结算的联赛集合（供 footer 显示）。"""
    try:
        from src.core.settleability import get_settleable_stats
        stats = get_settleable_stats()
        return {name for name, e in stats.get("leagues", {}).items() if e.get("successes", 0) > 0}
    except ImportError:
        return set()


def _prepare_opportunities(skip_freshness=False):
    """对比文件必须 < 30 分钟, 确保赔率是实时的, 拒绝缓存。"""
    # 检查最新的对比文件(near/far/main)
    newest_age = 999
    for f in (COMPARISON_FILE_NEAR, COMPARISON_FILE_FAR, COMPARISON_FILE):
        if f.exists():
            age = (time.time() - f.stat().st_mtime) / 60
            newest_age = min(newest_age, age)
    if newest_age > 30 and not skip_freshness:
        print(f"❌ 对比文件过期 ({newest_age:.0f}分钟前)，拒绝使用缓存")
        return []

    qualified = _collect_opportunities_from_file()
    if not qualified:
        return []

    return _diversify_and_rank(qualified)


def build_report(skip_freshness: bool = False, incremental: bool = False):
    """构建格式化的 BB vs Pinnacle +EV 报告。返回 (body_text, qualified_opportunities).

    🔴 铁律：实时拉取失败 → 不发缓存数据，发钉钉告警。

    Args:
        skip_freshness: 跳过 30 分钟新鲜度检查，即使对比文件较旧也继续推送。
        incremental: 增量模式 — 跳过实时拉取，直接使用增量扫描的 near/far 文件。
    Returns:
        (body, qualified): body 为 None 表示不应推送。
    """
    errors = []
    if incremental:
        # 增量模式: 检查所有对比文件(MAIN+NEAR+FAR)的新鲜度
        # 取最新文件时间 — 增量扫描写NEAR/FAR, MAIN可能旧
        pin_age_min = 999.0
        need_refresh = False
        try:
            _all_files = [COMPARISON_FILE, COMPARISON_FILE_NEAR, COMPARISON_FILE_FAR]
            _existing = [f for f in _all_files if f.exists()]
            if _existing:
                _newest = max(_existing, key=lambda f: f.stat().st_mtime)
                pin_mtime = datetime.fromtimestamp(_newest.stat().st_mtime,
                                                    tz=timezone.utc).astimezone()
                pin_age_min = (datetime.now(timezone.utc).astimezone() - pin_mtime).total_seconds() / 60
        except Exception:
            pass
        if pin_age_min > 120:
            logger.warning("⚡ 增量模式 — Pin数据 %.0fh 过期，强制实时拉取", pin_age_min / 60)
            need_refresh = True
        if need_refresh:
            live_ok, errors = _refresh_live_odds()
            if not live_ok:
                _send_failure_alert(errors)
                return None, None
        else:
            logger.info("⚡ 增量模式 — 使用已有对比文件 (%.0fmin前), 跳过实时拉取", pin_age_min)
    else:
        # V5.5: 对比文件 < 5 分钟(刚跑完对比)则跳过重复实时拉取, 速度优先
        _fresh = False
        _cmp_age_min = 999.0
        if COMPARISON_FILE.exists():
            _cmp_age_min = (time.time() - COMPARISON_FILE.stat().st_mtime) / 60
            _fresh = _cmp_age_min < 5
        if _fresh:
            logger.info("⚡ 对比文件 %.1fmin 前刚生成, 跳过重复实时拉取", _cmp_age_min)
        else:
            # 🔴 铁律：全量推送前必须实时拉取赔率，不使用缓存
            live_ok, errors = _refresh_live_odds()
            if not live_ok:
                _send_failure_alert(errors)
                return None, None  # 不推送

    qualified = _prepare_opportunities(skip_freshness=skip_freshness)
    if not qualified:
        if not COMPARISON_FILE.exists():
            return "no comparison data", []
        return "no +EV opportunities", []

    sport_summary = _compute_sport_summary()
    warnings = _check_sport_consistency(qualified)
    clv_warnings, _ = _check_clv_trend(qualified)
    if clv_warnings:
        warnings = (warnings or []) + clv_warnings
    # 如果 FB 独立对比未完成，加一条提示（不影响主推送）
    if errors:
        warnings = (warnings or []) + [f"⚠️ {e}" for e in errors]
    body = _format_body(qualified, warnings, sport_summary)
    return body, qualified


# ── 推送去重 ──

def _make_fingerprint(o: dict) -> str:
    """为一条机会生成唯一指纹：sport|league|home|away|盘口（含线值）|子市场|比赛日期

    含子市场（_sub_market）防止 1X2 客胜 vs HT 客胜 等跨市场误杀。
    盘口线参与指纹：线变了（如 -9.5→-10.5）视为新机会可重新推送。
    归一化：空格 + 中文简繁体统一，防止 BB/FB 队名微小差异导致重复推送。
    """
    # V4.5: 使用北京时间 (与推送显示一致)
    match_date = "9999-12-31"
    ep = o.get("_pin_epoch")
    if ep:
        try:
            dt = datetime.fromtimestamp(ep, tz=timezone.utc)
            bj = dt.astimezone(timezone(timedelta(hours=8)))
            match_date = bj.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass
    # 归一化：空格 + 中文简繁体统一
    def _norm(s):
        import re as _re
        return _normalize_cn(_re.sub(r'\s+', ' ', s.strip())) if s else s
    # 队名归一化: 剥离俱乐部前缀(CD/CA/CF/AC/SC/AD/UD/RC), 防止"CD拉斯阿尼玛斯"vs"拉斯阿尼玛斯"误判为不同比赛
    def _norm_team(s):
        import re as _re
        s = (s or "").strip()
        s = _re.sub(r'^(CD|CA|CF|AC|SC|AD|UD|RC)\s+', '', s, flags=_re.I)
        return _norm(s)
    sub = o.get("_sub_market", o.get("_market", ""))
    # V4.5: 归一化 sub_market (opportunities/"" → "1x2", 与扫描器一致)
    if sub in ("", "opportunities"): sub = "1x2"
    elif sub in ("hc", "handicap"): sub = "hc"
    elif sub in ("ou", "over_under"): sub = "ou"
    # 盘口线参与指纹: 让球/大小球线变了 → 新机会, 不拦截
    line = o.get("line", "") or o.get("_line", "")
    line_str = f"|{line}" if line and sub in ("hc", "handicap", "ou", "over_under") else ""
    return f"{_norm(o.get('sport',''))}|{_norm(o.get('league',''))}|{_norm_team(o.get('home_cn',''))}|{_norm_team(o.get('away_cn',''))}|{_norm(o.get('designation',''))}|{sub}{line_str}|{match_date}"


def _load_fingerprints() -> dict:
    from config.database import load_fingerprints as _db_load
    return _db_load()


def _save_fingerprints(fps: dict):
    """原子写入指纹文件 + SQLite。fps = {fingerprint: ev_pct}"""
    from config.database import save_fingerprints
    save_fingerprints(fps)
    # 同时写入 JSON 作为二次备份
    tmp = FINGERPRINT_FILE.with_suffix(".fptmp")
    tmp.write_text(json.dumps(fps, ensure_ascii=False, indent=2))
    tmp.replace(FINGERPRINT_FILE)


def _parse_line(s):
    """解析让球线: '+0/0.5'→0.25, '-0.5'→-0.5, '1'→1.0; 失败返回 None"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    sign = -1 if s.startswith('-') else 1
    s = s.lstrip('+-')
    try:
        if '/' in s:
            parts = [float(p) for p in s.split('/')]
            return sign * sum(parts) / len(parts)
        return sign * float(s)
    except (ValueError, TypeError):
        return None


def _lookup_bb_odds(match: dict, o: dict):
    """从 BB 提取数据(bb_odds_extracted.json 的单场)取对应方向/盘口的当前赔率。

    用于推送前 BB 漂移检测(零延迟: 只读本地已抓文件, 不额外调 BB API)。
    取不到 → 返回 None 保守放行(不因无法验证而误杀)。
    """
    mkt = o.get("_sub_market", o.get("_market", ""))
    des = (o.get("designation", "") or "").lower()
    ft = match.get("odds_ft") or {}
    ht = match.get("odds_ht") or {}

    def _ml(field, idx):
        ml = field.get("ml")
        if isinstance(ml, (list, tuple)) and len(ml) > idx:
            return ml[idx]
        return None

    def _side(target):
        if "客" in des or "away" in des:
            return "away"
        if "主" in des or "home" in des:
            return "home"
        if "和" in des or "平" in des or "draw" in des:
            return "draw"
        return target

    def _dc(field):
        # dc 数组顺序: [主/和(1X), 和/客(X2), 主/客(12)]
        dc = field.get("dc")
        if not isinstance(dc, (list, tuple)) or len(dc) < 3:
            return None
        if "主" in des and "和" in des:
            return dc[0]
        if "和" in des and "客" in des:
            return dc[1]
        if "主" in des and "客" in des:
            return dc[2]
        return None

    def _ml_idx(field):
        # 2-way 运动(篮球/网球/棒球/美足等) ml=[home, away]; 3-way(足球) ml=[home, draw, away]
        _ml_arr = field.get("ml")
        if isinstance(_ml_arr, (list, tuple)) and len(_ml_arr) >= 3:
            return {"home": 0, "draw": 1, "away": 2}.get(_side("away"), 2)
        return {"home": 0, "away": 1}.get(_side("away"), 1)

    if mkt == "1x2":
        return _ml(ft, _ml_idx(ft))
    if mkt == "ht":
        return _ml(ht, _ml_idx(ht))
    if mkt == "hc":
        hc = ft.get("handicap") or {}
        return hc.get("away_odds") if _side("away") == "away" else hc.get("home_odds")
    if mkt == "ht_hc":
        hc = ht.get("handicap") or {}
        return hc.get("away_odds") if _side("away") == "away" else hc.get("home_odds")
    if mkt == "ou":
        total = ft.get("total") or {}
        return total.get("over_odds") if ("大" in des or "over" in des) else total.get("under_odds")
    if mkt == "ht_ou":
        total = ht.get("total") or {}
        return total.get("over_odds") if ("大" in des or "over" in des) else total.get("under_odds")
    if mkt == "dc":
        return _dc(ft)
    if mkt == "ht_dc":
        return _dc(ht)
    return None


def _lookup_fresh_pin_odds(fresh: dict, o: dict):
    """从实时 Pinnacle matchup 数据里取对应方向/盘口的原始赔率。

    V5.9 全覆盖: 独赢/让球/大小/半场(独赢/让球/大小)/DC/BTTS/单双 (所有运动通用)。
    中文 designation → Pinnacle 英文 home/away/draw/over/under/yes/no/odd/even。
    让球/大小盘必须盘口线精确匹配(±0.1 带符号)，否则取不到 → 返回 None 保守放行。
    """
    mkt = o.get("_sub_market", o.get("_market", ""))
    designation = o.get("designation", "")
    des = (designation or "").lower()

    def _find_2way(field, target_desig, line=None):
        """2-way 盘(spread/total): 按方向和线匹配, 返回 price_decimal 或 0."""
        for entry in fresh.get(field, []) or []:
            for p in entry.get("prices", []) or []:
                if (p.get("designation", "") or "").lower() != target_desig:
                    continue
                if line is not None:
                    pts = p.get("points")
                    if pts is not None and abs(pts - line) > 0.1:
                        continue
                return p.get("price_decimal", 0)
        return None

    def _find_ml(field, target):
        """moneyline(3-way): 按 home/away/draw 匹配."""
        for ml in fresh.get(field, []) or []:
            for p in ml.get("prices", []) or []:
                if (p.get("designation", "") or "").lower() == target:
                    return p.get("price_decimal", 0)
        return None

    if mkt in ("1x2", "ht"):
        field = "moneyline" if mkt == "1x2" else "ht_moneyline"
        target = "home"
        if "客" in des or "away" in des:
            target = "away"
        elif "和" in des or "平" in des or "draw" in des:
            target = "draw"
        if o.get("_ml_swapped") and target in ("home", "away"):
            target = "away" if target == "home" else "home"
        return _find_ml(field, target)

    elif mkt in ("hc", "ht_hc"):
        field = "spread" if mkt == "hc" else "ht_spread"
        line = _parse_line(o.get("line"))
        td = "away" if ("客" in des or "away" in des) else "home"
        if o.get("_ml_swapped"):
            td = "home" if td == "away" else "away"
        return _find_2way(field, td, line)

    elif mkt in ("ou", "ht_ou"):
        field = "total" if mkt == "ou" else "ht_total"
        line = _parse_line(o.get("line"))
        td = "over" if ("大" in des or "over" in des) else "under"
        return _find_2way(field, td, line)

    elif mkt == "dc":
        target = ""
        if "主" in des and "客" in des:
            target = "12"
        elif "主" in des:
            target = "1x"
        elif "客" in des:
            target = "2x"
        for dc in fresh.get("double_chance", []) or []:
            for p in dc.get("prices", []) or []:
                if (p.get("designation", "") or "").lower() == target:
                    return p.get("price_decimal", 0)

    elif mkt == "btts":
        td = "yes" if ("是" in des or "yes" in des) else "no"
        for b in fresh.get("btts", []) or []:
            for p in b.get("prices", []) or []:
                if (p.get("designation", "") or "").lower() == td:
                    return p.get("price_decimal", 0)

    elif mkt == "oe":
        td = "odd" if ("单" in des or "odd" in des) else "even"
        for oe in fresh.get("oe", []) or []:
            for p in oe.get("prices", []) or []:
                if (p.get("designation", "") or "").lower() == td:
                    return p.get("price_decimal", 0)

    elif mkt == "corner":
        # 角球子比赛字段同主盘口(moneyline/spread/total), 从 designation 判断子盘口
        if "让球" in des or "让分" in des:
            line = _parse_line(o.get("line"))
            td = "away" if ("客" in des or "away" in des) else "home"
            return _find_2way("spread", td, line)
        elif "大" in des or "小" in des or "over" in des or "under" in des:
            line = _parse_line(o.get("line"))
            td = "over" if ("大" in des or "over" in des) else "under"
            return _find_2way("total", td, line)
        else:
            # 角球独赢
            target = "home"
            if "客" in des or "away" in des:
                target = "away"
            elif "和" in des or "draw" in des:
                target = "draw"
            return _find_ml("moneyline", target)

    return None


def _verify_odds_freshness(qualified: list, max_pin_drift: float = 0.08) -> list:
    """推送前二次验价：重新拉取 Pinnacle 实时赔率，与对比快照原始价偏差 > 8% 则过滤。

    根因修复 (2026-08-16 卢旺达女篮"客胜+6.57%"幻影EV):
      薄盘联赛 Pinnacle 几分钟内剧烈波动(该场客胜 2.24→2.83, 漂移26%), 对比快照用旧价
      产生幻影+EV。原实现三处缺陷:
        (1) 只验前10条 → T3/T4 低级别机会(如¥59客胜)漏验;
        (2) 1x2 用英文 designation 直接比中文"主胜/客胜" → 永不匹配, 实际没验;
        (3) EV 重算拿 raw 价比 devig 公平价, 方向错误。
      改为"实时原始价 vs 快照原始价"直接判相对漂移, 覆盖全部机会, 中文方向转 home/away 再匹配。
      实时数据找不到该场/该盘口时保守放行(不因无法验证而误杀)。
    """
    if not qualified:
        return qualified
    # 快速失败: Pinnacle 被 Cloudflare 封禁(SCAN_PAUSE 冷却)时直接放行,
    # 不逐个联赛触发重试/等待 — 否则 30+ 联赛 × 每个失败等待会把推送卡到超时。
    try:
        from src.scrapers.pinnacle_api import _SCAN_PAUSE_UNTIL
        import time as _time
        if _SCAN_PAUSE_UNTIL > _time.time():
            logger.info("Pinnacle Cloudflare 封禁冷却中, 跳过二次验价(直接放行 %d 条)", len(qualified))
            return qualified
    except Exception:
        pass
    try:
        from src.scrapers.pinnacle_markets import get_league_matchups_and_markets
        from src.scrapers.pinnacle_league_map import find_pinnacle_league_ids
        import json

        ps = json.loads((DATA_DIR / "pinnacle_league_structure.json").read_text())

        # 收集全部机会涉及的联赛(不再只验前10条)
        leagues_to_check = set()
        corner_leagues = set()
        for o in qualified:
            lid = find_pinnacle_league_ids(o.get("league", ""), ps)
            if lid:
                leagues_to_check.add(lid[0])
                if o.get("_sub_market", o.get("_market", "")) == "corner":
                    corner_leagues.add(lid[0])

        if not leagues_to_check:
            return qualified

        # 每联赛拉一次实时数据, 建 (home, away) → matchup 索引
        fresh_index = {}
        for lid in leagues_to_check:
            try:
                for r in get_league_matchups_and_markets(lid):
                    fresh_index[(r.get("home", ""), r.get("away", ""))] = r
            except Exception:
                pass

        # V5.9: 角球子比赛在 get_league_corner_markets (不在主盘口), 单独建索引
        corner_index = {}
        if corner_leagues:
            from src.scrapers.pinnacle_markets import get_league_corner_markets
            for lid in corner_leagues:
                try:
                    for r in get_league_corner_markets(lid):
                        corner_index[(r.get("home", ""), r.get("away", ""))] = r
                except Exception:
                    pass

        if not fresh_index and not corner_index:
            return qualified

        kept = []
        skipped = 0
        for o in qualified:
            pin_home = o.get("home_team", o.get("home_pin", ""))
            pin_away = o.get("away_team", o.get("away_pin", ""))
            is_corner = (o.get("_sub_market", o.get("_market", "")) == "corner")
            fresh = (corner_index.get((pin_home, pin_away)) if is_corner
                     else fresh_index.get((pin_home, pin_away)))
            if not fresh:
                kept.append(o)   # 实时数据无此场 → 无法验证, 保守放行
                continue

            fresh_odds = _lookup_fresh_pin_odds(fresh, o)
            if not fresh_odds or fresh_odds <= 0:
                kept.append(o)   # 实时数据无此盘口/方向 → 保守放行
                continue

            snap_pin = o.get("pin_odds", 0)
            if snap_pin > 0:
                drift = abs(fresh_odds - snap_pin) / snap_pin
                # 低级别联赛(T3/T4)薄盘, Pinnacle 线波动大, 用更严的 5% 阈值压幻影 EV
                _tier = o.get("_tier", 3)
                drift_limit = 0.05 if _tier >= 3 else max_pin_drift
                if drift > drift_limit:
                    logger.info("验价过滤(漂移%.0f%%>%.0f%%): %s Pin %.2f→%.2f",
                                drift * 100, drift_limit * 100, o.get("designation", ""),
                                snap_pin, fresh_odds)
                    skipped += 1
                    continue
            kept.append(o)

        if skipped:
            logger.info("二次验价: 过滤 %d 条漂移机会", skipped)
        return _verify_bb_drift(kept)
    except Exception as e:
        logger.warning("二次验价失败(跳过): %s", e)
        return qualified


def _append_bb_drift_log(rows: list):
    """把每次推送前 BB 漂移的逐笔数据写到 bb_drift_log.csv(攒精确数据, 供与 CLV 关联)。

    之前 _verify_bb_drift 只 log 被拦的, 无法量化"3-8% 漂移区间"的误杀代价。
    这里记录全部有 bb_match_id 且取得到最新价的机会: drift_pct>0=下跌, <0=上涨。
    列与 clv_results.csv 对齐(sport/league/home/away/home_pin/away_pin/sub_market/designation),
    方便事后按 (sport|league|home|away|designation|sub_market|日期) join 出"漂移 vs CLV"。
    """
    import csv
    f = DATA_DIR / "bb_drift_log.csv"
    cols = ["collect_time", "sport", "league", "home", "away", "home_pin", "away_pin",
            "sub_market", "designation", "tier", "bb_match_id",
            "snap_bb", "fresh_bb", "drift_pct", "action"]
    try:
        exists = f.exists() and f.stat().st_size > 0
        with open(f, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            if not exists:
                w.writeheader()
            for r in rows:
                w.writerow(r)
    except Exception as e:
        logger.warning("BB漂移日志写失败: %s", e)


def _verify_bb_drift(qualified: list, max_bb_drop: float = 0.10) -> list:
    """推送前 BB 漂移检测(零延迟): 只读本地已抓的 bb_odds_extracted.json, 不额外调 BB API。

    根因(2026-08-21 格鲁吉亚甲级"客胜 1.72→1.48"假机会): BB 是软博彩平台, 低级别联赛
    定价粗、几分钟内大幅修正(该场 14%), 对比快照抓到的是"临时高价", 投注后成交价已崩
    → 幻影 +EV。这里用本地最新 BB 数据对比快照 bb_odds, 若 BB 已大幅**下跌**(成交价会
    变差) 则过滤。上涨不拦(成交价更好是真机会)。取不到该场/该盘口时保守放行。
    零 API 调用: bb_odds_extracted.json 本就由扫描器定期抓取, 这里只读文件。
    """
    if not qualified:
        return qualified
    try:
        bb_file = DATA_DIR / "bb_odds_extracted.json"
        if not bb_file.exists():
            return qualified
        bb_data = json.loads(bb_file.read_text())
        matches = bb_data.get("matches", []) if isinstance(bb_data, dict) else bb_data
        if not matches:
            return qualified
        bb_index = {}
        for m in matches:
            mid = m.get("id")
            if mid is not None:
                bb_index[str(mid)] = m
        if not bb_index:
            return qualified

        kept = []
        skipped = 0
        drift_rows = []   # 逐笔漂移数据(全部可验证机会, 不只被拦的)
        now_iso = datetime.now(timezone.utc).isoformat()
        for o in qualified:
            snap_bb = o.get("bb_odds", 0)
            mid = o.get("bb_match_id")
            if not snap_bb or snap_bb <= 0 or mid is None:
                kept.append(o)
                continue
            bm = bb_index.get(str(mid))
            if not bm:
                kept.append(o)          # 本地 BB 数据无此场 → 保守放行
                continue
            fresh_bb = _lookup_bb_odds(bm, o)
            if not fresh_bb or fresh_bb <= 0:
                kept.append(o)          # 取不到该盘口 → 保守放行
                continue
            # drift_pct: 正值=下跌(成交价变差), 负值=上涨(成交价更好)
            drift_pct = (snap_bb - fresh_bb) / snap_bb * 100
            _tier = o.get("_tier", 3)
            bb_limit = 0.08 if _tier >= 3 else max_bb_drop
            _row = {
                "collect_time": now_iso,
                "sport": o.get("sport", ""),
                "league": o.get("league", ""),
                "home": o.get("home_cn", o.get("home", "")),
                "away": o.get("away_cn", o.get("away", "")),
                "home_pin": o.get("home_team", ""),
                "away_pin": o.get("away_team", ""),
                "sub_market": o.get("_sub_market", ""),
                "designation": o.get("designation", ""),
                "tier": _tier,
                "bb_match_id": mid,
                "snap_bb": snap_bb,
                "fresh_bb": fresh_bb,
                "drift_pct": round(drift_pct, 2),
            }
            # 只拦截"下跌"超过阈值(快照价虚高 → 临时高价假机会); 上涨不拦
            if drift_pct > bb_limit * 100:
                logger.info("BB漂移过滤(跌%.0f%%>%.0f%%): %s BB %.2f→%.2f (临时高价假机会)",
                            drift_pct, bb_limit * 100, o.get("designation", ""),
                            snap_bb, fresh_bb)
                skipped += 1
                _row["action"] = "filtered"
                drift_rows.append(_row)
                continue
            _row["action"] = "kept"
            drift_rows.append(_row)
            kept.append(o)
        if skipped:
            logger.info("BB漂移检测: 过滤 %d 条临时高价假机会", skipped)
        if drift_rows:
            _append_bb_drift_log(drift_rows)
        return kept
    except Exception as e:
        logger.warning("BB漂移检测失败(跳过): %s", e)
        return qualified


# ── 同场同盘口「盘口线」推送配额 (用户规则 2026-08-19) ──
# 同一场比赛的同一盘口(让球/大小球/...), 不同盘口线最多只推 2 次;
# 第二次推送时标注上一次推的是哪条线。
# 粒度按**盘口类型**算, 不分主客/大小 —— 即"让球主胜(-0.5)"和"让球客胜(+1.5)"
# 共用同一份配额(用户 2026-08-19 明确选择的严格口径)。
LINE_QUOTA_PER_MARKET = 2
LINE_QUOTA_FILE = DATA_DIR / "pushed_line_quota.json"

# 用户规则 (2026-08-19): 同一盘口线, BB/FB 赔率与溢价**同时**较上次推送值相对上涨
# 超过该比例 → 不限次数重推(并标注)。相对基准每次推送后抬高, 因此天然收敛。
REPUSH_REL_THRESHOLD = 0.05


def _market_group_key(o: dict) -> str:
    """同场同盘口的配额分组键: 指纹去掉 designation 和 line 两段。

    直接复用 _make_fingerprint 保证队名归一化/比赛日期口径完全一致 ——
    自己另写一套归一化必然和去重逻辑漂移(繁简体、俱乐部前缀那些坑)。
    """
    fp = _make_fingerprint(o)
    parts = fp.split("|")
    # 指纹格式: sport|league|home|away|designation(含线)|sub_market|line|date
    # 去掉 designation 与 line, 只留 场次 + 盘口类型 + 日期
    if len(parts) >= 8:
        return "|".join(parts[:4] + [parts[5], parts[-1]])
    return fp


def _load_line_quota() -> dict:
    """{group_key: {"lines": [线值字符串, ...], "ts": epoch}}"""
    try:
        import json as _json
        data = _json.loads(LINE_QUOTA_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_line_quota(quota: dict):
    """写回配额, 顺带清理 3 天前的记录防无限膨胀。"""
    try:
        import json as _json
        cutoff = time.time() - 3 * 86400
        pruned = {k: v for k, v in quota.items()
                  if isinstance(v, dict) and v.get("ts", 0) > cutoff}
        tmp = LINE_QUOTA_FILE.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(pruned, ensure_ascii=False))
        tmp.replace(LINE_QUOTA_FILE)
    except Exception as e:
        logger.warning("盘口线配额写入失败: %s", e)


def _commit_line_quota(qualified: list):
    """推送成功后才登记配额。

    与"指纹只在推送成功后写入"同一条纪律 —— 若在过滤阶段就写, 推送失败会白白
    吃掉配额, 真机会再也推不出去。
    """
    if not qualified:
        return
    quota = _load_line_quota()
    for o in qualified:
        line = str(o.get("line", "") or "")
        gk = _market_group_key(o)
        rec = quota.setdefault(gk, {"lines": [], "ts": time.time()})
        if line not in rec["lines"]:
            rec["lines"].append(line)
        rec["ts"] = time.time()
    _save_line_quota(quota)


def _save_qualified_fingerprints(qualified: list):
    """保存指纹 (每场最多2条, 与同场冷却一致)。"""
    from collections import defaultdict
    from config.database import add_fingerprints
    new_fps = {}
    match_groups = defaultdict(list)
    for o in qualified:
        key = (o.get("sport",""), o.get("home_cn","").strip(), o.get("away_cn","").strip())
        match_groups[key].append(o)
    for key, group in match_groups.items():
        group.sort(key=lambda o: o.get("_score", 0), reverse=True)
        for o in group:  # V4.4: 不限盘口数
            fp = _make_fingerprint(o)
            ev = o.get("ev_pct", 0)
            bb = o.get("bb_odds", 0)
            new_fps[fp] = {"ev": ev, "bb": bb, "ts": time.time()}
    add_fingerprints(new_fps)
    logger.info("指纹: %d条 (%d场)", len(new_fps), len(match_groups))


# V5: 不可结算联赛黑名单 — 仅限Pinnacle/BB无数据或盘口结构错配的联赛
# 不因ESPN不覆盖而拉黑 — 结算不了就标记pending, 不比价才是真损失
_UNSETTLEABLE_LEAGUES = {
    # 盘口结构错配 (非结算问题)
    "NFL 季前赛", "NFL Pre Season",
}


def _filter_unsettleable_leagues(qualified: list) -> list:
    """过滤已知不可结算的联赛。"""
    blocked = []
    kept = []
    for o in qualified:
        league = o.get("league", "")
        # 精确匹配 + 子串匹配 (如 "委内瑞拉超级联赛" in "委内瑞拉超级联赛 2026")
        blocked_flag = False
        for banned in _UNSETTLEABLE_LEAGUES:
            if banned in league:
                blocked_flag = True
                break
        if blocked_flag:
            blocked.append(f"{league} ({o.get('sport', '?')})")
        else:
            kept.append(o)

    if blocked:
        unique = sorted(set(blocked))
        logger.info("结算门禁: 拦截 %d 个机会 (联赛=%s)", len(blocked), ", ".join(unique))
    return kept


def _apply_match_exposure_cap(qualified: list) -> list:
    """同一场比赛累计投注不超过日预算 6%，超出的机会降权或跳过。

    比硬冷却更灵活: EV大涨时仍可追加，但总量受控。
    追踪文件: push_cooldown.json → 增加 match_total_stake 字段。
    """
    cooldown_file = DATA_DIR / "push_cooldown.json"
    now = time.time()
    from config.constants import get_dynamic_bankroll
    match_cap = get_dynamic_bankroll() * 0.06  # 单场累计≤6%日预算 (原 *1.0 是 bug, 100% 敞口)

    # 文件锁: 防止并发推送进程 read-modify-write 丢更新 (单场6%上限被绕过)
    import fcntl as _fcntl
    _lock_file = DATA_DIR / ".push_cooldown.lock"
    _lock_fd = open(_lock_file, "w")
    _fcntl.flock(_lock_fd.fileno(), _fcntl.LOCK_EX)  # 阻塞锁, 短操作

    # 加载记录 {match_id: {timestamp, total_stake}}
    records = {}
    if cooldown_file.exists():
        try:
            records = json.loads(cooldown_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # 清理24h过期记录 (兼容旧格式 float timestamp)
    cleaned = {}
    for k, v in records.items():
        if isinstance(v, dict):
            if now - v.get("timestamp", 0) < 86400:
                cleaned[k] = v
        elif isinstance(v, (int, float)):
            if now - v < 86400:
                cleaned[k] = {"timestamp": v, "total_stake": 0}
    records = cleaned

    kept = []
    skipped = 0
    capped = 0
    for o in qualified:
        match_id = "|".join([o.get("sport", ""), o.get("home_cn", "").strip(), o.get("away_cn", "").strip()])
        new_stake = o.get("_stake", 0)
        existing = records.get(match_id, {})
        if isinstance(existing, (int, float)):
            prev_stake = 0  # old format: just had timestamp
        else:
            prev_stake = existing.get("total_stake", 0)

        if prev_stake + new_stake > match_cap:
            # 超过单场上限: 压缩到剩余额度
            remaining = max(0, match_cap - prev_stake)
            if remaining >= 5:
                o["_stake"] = int(remaining)
                o["_raw_stake"] = int(remaining)
                capped += 1
            else:
                o["_stake"] = 0
                skipped += 1
                continue

        kept.append(o)
        records[match_id] = {
            "timestamp": now,
            "total_stake": prev_stake + o["_stake"],
        }

    cooldown_file.write_text(json.dumps(records, ensure_ascii=False))
    _fcntl.flock(_lock_fd.fileno(), _fcntl.LOCK_UN)
    _lock_fd.close()
    if skipped:
        logger.info("单场超限跳过: %d 条 (累计已超 ¥%.0f)", skipped, match_cap)
    if capped:
        logger.info("单场压缩: %d 条 (压缩至剩余额度)", capped)
    return kept


# ── 去重审计日志 ──
_DEDUP_LOG = None

def _get_dedup_log():
    global _DEDUP_LOG
    if _DEDUP_LOG is None:
        log_dir = DATA_DIR.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _DEDUP_LOG = log_dir / "push_dedup.log"
    return _DEDUP_LOG

def _audit_log(action, key, opp, reason=""):
    """记录每条推送机会的去重决定到 push_dedup.log。"""
    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
        home = opp.get("home_cn", "?") if isinstance(opp, dict) else "?"
        away = opp.get("away_cn", "?") if isinstance(opp, dict) else "?"
        bb = opp.get("bb_odds", 0) if isinstance(opp, dict) else 0
        ev = opp.get("ev_pct", 0) if isinstance(opp, dict) else 0
        line = f"[{now}] {action:7s} | {home} vs {away} | BB={bb:.2f} EV={ev:.1f}% | {reason} | {key[:80]}"
        log_file = _get_dedup_log()
        with open(log_file, 'a') as f:
            f.write(line + "\n")
    except Exception:
        pass  # 审计日志失败不影响推送


def _filter_pushed(qualified: list, time_window: str = "") -> list:
    """去重：一条机会推过一次就不再推送，除非 BB/FB 赔率上升 (或 EV 大涨兜底)。

    V5.3 (2026-08-15): 重推条件从"EV增≥阈值(临场1%/其他2%)"改为
    "BB赔率上升(>0.005ε)"——赔率上升=单笔EV严格增加=收益增加, 更直接灵敏。
    保留 EV 大涨(≥阈值)作为兜底(线朝有利方向移动但赔率未动的场景)。
    """
    _threshold = 1.0 if time_window == "urgent" else 2.0
    import json as _json, fcntl as _fcntl, os as _os
    from collections import defaultdict

    pushed_file = DATA_DIR / "pushed_opportunities.json"
    fp_file = FINGERPRINT_FILE
    lock_file = DATA_DIR / ".push_dedup.lock"

    # 文件锁
    _lock_fd = open(lock_file, "w")
    try: _fcntl.flock(_lock_fd.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError: _lock_fd.close(); return qualified

    # 读现有指纹 (V5: 合并两个去重文件)
    last_pushed = {}
    for src_file in (pushed_file, fp_file):
        if src_file.exists():
            try:
                data = _json.loads(src_file.read_text())
                for k, v in data.items():
                    if not k.startswith("_"):
                        last_pushed[k] = v
            except: pass
    last_pushed.pop("_version", None)

    now_epoch = time.time()
    result, skipped, re_pushed = [], 0, 0

    # 同场同盘口的盘口线配额: 已推过的线 + 本轮已放行的线
    _quota = _load_line_quota()
    _batch_lines = defaultdict(list)
    _quota_blocked = 0

    for o in qualified:
        # V4.5: 统一使用 _make_fingerprint 的 key 格式
        # sport|league|home|away|designation|sub_market|line|match_date(Beijing)
        # 含 Unicode 归一化 + 盘口线 + 北京时间日期
        key = _make_fingerprint(o)
        bb_now = o.get("bb_odds", 0)
        ev_now = o.get("ev_pct", 0)
        fair_now = o.get("fair_price", 0)
        kickoff = o.get("_pin_epoch", now_epoch + 86400)

        # ── 同场同盘口「盘口线」配额 (最多 2 条线) ──
        _line = str(o.get("line", "") or "")
        _gk = _market_group_key(o)
        _prev_lines = list((_quota.get(_gk) or {}).get("lines", [])) + _batch_lines[_gk]
        if _line not in _prev_lines:
            if len(_prev_lines) >= LINE_QUOTA_PER_MARKET:
                _quota_blocked += 1
                _audit_log("LINE_QUOTA", key, o,
                           f"同场同盘口已推 {len(_prev_lines)} 条线({','.join(_prev_lines)}), 拦截线 {_line}")
                continue
            if _prev_lines:
                # 第 2 条线 → 标注上一条线是多少
                o["_prev_line"] = _prev_lines[-1]
            _batch_lines[_gk].append(_line)

        # 更新指纹(会在下面save)
        old = last_pushed.get(key)
        last_pushed[key] = {"bb": bb_now, "ev": ev_now, "fair": fair_now,
                             "ts": time.time(), "kickoff": kickoff}

        # 新key → 推送
        if old is None:
            result.append(o)
            _audit_log("PUSHED", key, o, "new")
            continue

        # 旧key → 重复推送规则
        # 用户要求 (2026-08-18): 必须同时满足 EV 提升 >2% 且 BB/FB 赔率提升 >0.1 才重推。
        # (旧"赔率上升>0.005 即重推"太灵敏, 导致同一盘口 5 分钟内刷屏 3 次)
        old_ev = old.get("ev", 0)
        old_bb = old.get("bb", 0)
        # 兼容旧指纹: bb 曾存为独立 key {fp}_bb (值被 add_fingerprints 包成 {"ev": bb})
        if not old_bb:
            _legacy = last_pushed.get(key + "_bb", 0)
            if isinstance(_legacy, dict):
                old_bb = _legacy.get("ev", 0) or _legacy.get("bb", 0)
            elif isinstance(_legacy, (int, float)):
                old_bb = _legacy
        premium_delta = ev_now - old_ev
        # 触发条件: 同时满足 EV 提升>2% 且 赔率提升>0.1 (AND 关系, 缺一不重推)
        bb_rose = old_bb > 0 and bb_now > old_bb + 0.1
        ev_rose = premium_delta > 2.0

        # 用户规则 (2026-08-19): 同一盘口线, 只要 BB/FB 赔率**和**溢价同时较上次
        # 推送值上涨超过 5%(相对), 就可以不限次数重推, 并标注涨幅。
        # 注意是"相对上次推送值"而非固定基准 —— 每次重推都会把基准抬高, 所以要
        # 连续再涨 5% 才会再推一次, 天然收敛, 不会无限刷屏。
        bb_rose_pct = old_bb > 0 and bb_now > old_bb * (1 + REPUSH_REL_THRESHOLD)
        ev_rose_pct = old_ev > 0 and ev_now > old_ev * (1 + REPUSH_REL_THRESHOLD)

        if bb_rose and ev_rose:
            re_pushed += 1
            reason = f"赔率↑{bb_now - old_bb:+.2f}&EV↑{premium_delta:+.1f}%"
            o["_repush"] = True
            o["_repush_reason"] = reason
            result.append(o)
            _audit_log("REPUSH", key, o,
                       f"bb+{bb_now - old_bb:.2f}/EV+{premium_delta:.1f}%")
        elif bb_rose_pct and ev_rose_pct:
            re_pushed += 1
            _bb_pct = (bb_now / old_bb - 1) * 100
            _ev_pct = (ev_now / old_ev - 1) * 100
            o["_repush"] = True
            o["_repush_reason"] = (f"双涨≥5%: 赔率{old_bb:.2f}→{bb_now:.2f}(+{_bb_pct:.1f}%)"
                                   f" EV{old_ev:.1f}%→{ev_now:.1f}%(+{_ev_pct:.1f}%)")
            result.append(o)
            _audit_log("REPUSH_REL", key, o,
                       f"bb+{_bb_pct:.1f}%/EV+{_ev_pct:.1f}% (相对5%规则)")
        else:
            skipped += 1
            _audit_log("SKIPPED", key, o,
                       f"bb={bb_now}(旧{old_bb:.2f}) Δev={premium_delta:.1f}%")

    # V5.1 铁律: 指纹只在推送成功后写入, _filter_pushed 不写磁盘
    # cleaned + save 由 _save_qualified_fingerprints 在推送成功后执行

    if skipped: logger.info("去重: 跳过%d条", skipped)
    if re_pushed: logger.info("重推: %d条", re_pushed)
    if _quota_blocked:
        logger.info("盘口线配额: 拦截%d条 (同场同盘口最多%d条线)",
                    _quota_blocked, LINE_QUOTA_PER_MARKET)
    _fcntl.flock(_lock_fd.fileno(), _fcntl.LOCK_UN)
    _lock_fd.close()
    return result


def _opposite_direction(designation: str, sub_market: str = "") -> str:
    """从 designation 提取方向(主/客/和/大/小), 忽略盘口线。

    "让分主胜(-5.5)"→home, "让分客胜(+4.5)"→away, "大(141.5)"→over, "小"→under。
    DC(双重机会)的 1X/12/X2 两两都有交集, 无"对倒"概念 → 返回完整 designation 避免误拦。
    """
    d = (designation or "").lower()
    if sub_market == "dc":
        return d
    if "客" in d or "away" in d:
        return "away"
    if "主" in d or "home" in d:
        return "home"
    if "和" in d or "平" in d or "draw" in d:
        return "draw"
    if "大" in d or "over" in d:
        return "over"
    if "小" in d or "under" in d:
        return "under"
    return d


def _filter_opposite_side(qualified: list) -> list:
    """同一场比赛同一盘口已推过相反方向 → 跳过（防同场对倒）。

    根因 (2026-08-16 卢旺达女篮 + 08-17 墨西哥篮球): 同场让分先推客胜(-2.5)又推主胜(+4.5)。
    让分线带符号(-2.5/+4.5), 旧实现把盘口线塞进 match_key → 同盘口被误判为不同盘口漏拦截。
    改为: match_key 去掉盘口线(同场同市场只按 主/客/大/小 方向判互斥), 方向用 _opposite_direction。
    同方向重推(赔率上升)不受影响, 仍由 _filter_pushed 决定。
    """
    if not qualified:
        return qualified
    from collections import defaultdict

    pushed_dirs = defaultdict(set)  # match_key(sport|league|home|away|sub|date) → 已推方向集合
    for src_file in (FINGERPRINT_FILE, DATA_DIR / "pushed_opportunities.json"):
        if not src_file.exists():
            continue
        try:
            data = json.loads(src_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for k in data:
            if k.startswith("_"):
                continue
            parts = k.split("|")
            if len(parts) < 6:
                continue
            # key: sport|league|home|away|designation|sub_market[|line]|match_date
            # match_key 不含 designation 也不含盘口线 → 让分±符号不拆散同盘口
            match_key = "|".join([parts[0], parts[1], parts[2], parts[3], parts[5], parts[-1]])
            pushed_dirs[match_key].add(_opposite_direction(parts[4], parts[5]))

    kept = []
    skipped = 0
    for o in qualified:
        parts = _make_fingerprint(o).split("|")
        if len(parts) < 6:
            kept.append(o)
            continue
        match_key = "|".join([parts[0], parts[1], parts[2], parts[3], parts[5], parts[-1]])
        cur_dir = _opposite_direction(parts[4], parts[5])
        others = pushed_dirs.get(match_key, set()) - {cur_dir}
        if others:
            _audit_log("OPPOSITE", match_key, o, f"同场同盘口已推 {sorted(others)}")
            skipped += 1
            continue
        kept.append(o)

    if skipped:
        logger.info("双边拦截: 跳过 %d 条", skipped)
    return kept


def _refresh_live_odds():
    """推送前强制实时拉取 BB/FB + Pinnacle 赔率并重新比价。

    🔴 铁律：钉钉推送的任何比赛必须使用实时赔率，绝不使用缓存过时数据。
    实时拉取失败 → 不推送比赛，改为发送失败告警到钉钉。

    Returns:
        (live_ok, errors): live_ok=True 表示主对比(BB)实时比价全链路成功。
        errors 是警告列表，记录非致命失败（如 FB 失败但 BB 成功）。
    """
    from src.scrapers.bb_api_fetcher import fetch_all_sports, save_results
    from src.scrapers.bb_vs_pinnacle import compare_bb_vs_pinnacle
    from src.scrapers.bb_data import load_bb_odds

    logger.info("🔄 推送前实时拉取赔率...")
    errors = []
    t0 = time.time()

    # 1. 实时拉取 BB 和 FB 赔率
    bb_ok = False
    fb_ok = False
    try:
        # 🔴 铁律：fetch_all_sports 返回合并后的最新赔率, 必须落盘到 bb_odds_extracted.json,
        #    否则下面 load_bb_odds() 读到的是上次扫描的陈旧文件(导致盘口线错配/过期赔率)。
        fresh_matches = fetch_all_sports(with_fb=True)
        save_results(fresh_matches)
        bb_ok = True
        fb_ok = True
        logger.info("  ✅ BB/FB 赔率实时拉取完成 (%.0fs)", time.time() - t0)
    except Exception as e:
        # fetch_all_sports 失败时，BB 数据可能部分成功
        bb_extracted = DATA_DIR / "bb_odds_extracted.json"
        if bb_extracted.exists():
            age = (time.time() - bb_extracted.stat().st_mtime) / 60
            if age < 5:  # 5 分钟内可接受
                bb_ok = True
                logger.warning("  ⚠️ BB/FB 拉取异常但 BB 数据较新(%.0f分钟前)，继续", age)
            else:
                errors.append(f"BB/FB 赔率拉取失败: {str(e)[:100]}")
        else:
            errors.append(f"BB/FB 赔率拉取失败且无数据: {str(e)[:100]}")

    if not bb_ok:
        logger.error("  ❌ BB 赔率不可用，无法推送")
        return False, errors

    # 2. 加载 Pinnacle 联赛结构
    from src.scrapers.pinnacle_league_map import _load_league_structure
    try:
        all_pin_leagues = _load_league_structure()
    except Exception as e:
        errors.append(f"Pinnacle 联赛结构加载失败: {str(e)[:100]}")
        return False, errors

    if not all_pin_leagues:
        errors.append("Pinnacle 联赛结构为空")
        return False, errors

    # 3. BB 主数据 → 实时比价
    t1 = time.time()
    try:
        bb_matches = load_bb_odds()
    except Exception as e:
        errors.append(f"BB 赔率数据读取失败: {str(e)[:100]}")
        return False, errors

    if not bb_matches:
        errors.append("BB 赔率数据为空")
        return False, errors

    pin_ok = False
    # 🔴 铁律(Pin先BB后): 推送前不能先拉BB后拉Pin —— 公平价(Pin)必须先于零售价(BB),
    #    否则 BB 比 Pin 旧, 比价会用陈旧零售价对新鲜公平价(假edge)。
    #    全量扫描 Step2 已预取 Pin 缓存, 这里优先用 6h 内的缓存(Pin 早于本次 BB);
    #    缓存过期才回退到实时拉 Pin(顺序不完美但功能兜底)。
    pin_cache_path = DATA_DIR / "pin_matches_cache.json"
    _use_pin_cache = (pin_cache_path.exists()
                      and (time.time() - pin_cache_path.stat().st_mtime) < 6 * 3600)
    try:
        result = compare_bb_vs_pinnacle(
            bb_matches, all_pin_leagues,
            save_path=COMPARISON_FILE,
            use_pin_cache=_use_pin_cache,
        )
        if result is not None:
            logger.info("  ✅ BB主对比完成 (%.0fs), %d 场匹配, %d 个+EV",
                        time.time() - t1,
                        result.get("matched_matches", 0),
                        result.get("opportunities_total", 0))
            pin_ok = True
        else:
            errors.append("BB vs Pinnacle 比价返回空结果")
    except Exception as e:
        errors.append(f"Pinnacle 实时比价失败: {str(e)[:100]}")

    if not pin_ok:
        logger.error("  ❌ Pinnacle 实时比价失败，无法推送")
        return False, errors

    # 4. FB 独立对比 — V5.5 已移除(冗余): BB/FB 已合并取高值, 再单独比 FB 只是重拉 Pin(+40s)
    #    FB 独家机会(仅FB有BB无)边际价值低, 去掉以提速。若需恢复, 取消下面注释。
    # fb_extracted = DATA_DIR / "bb_odds_extracted_FB.json"
    # if fb_extracted.exists():
    #     ... (FB 独立对比代码已移除)

    logger.info("🎯 实时赔率比价全部完成 (%.0fs)", time.time() - t0)
    return True, errors


def _send_failure_alert(errors: list):
    """实时赔率拉取失败时，发送钉钉告警，告知用户具体失败原因。"""
    from config.settings import send_dingtalk
    now = datetime.now(timezone.utc).strftime("%m/%d %H:%M")
    lines = [
        f"## ⚠️ 实时赔率推送失败 ({now})",
        "",
        "以下环节失败，本次未推送任何比赛：",
        "",
    ]
    for i, err in enumerate(errors, 1):
        lines.append(f"{i}. {err}")
    lines.append("")
    lines.append("---")
    lines.append("请检查网络/VPN 后重新触发推送。")

    body = "\n".join(lines)
    title = "⚠️ 赔率拉取失败 — 未推送"
    try:
        # 赔率拉取失败属故障类 → urgent, 不能被例行日报挤掉
        if send_dingtalk(title, body, urgent=True):
            logger.info("钉钉失败告警已发送")
        else:
            logger.error("钉钉失败告警未送达(钉钉返回失败)")
    except Exception as e:
        logger.error("钉钉告警发送失败: %s", e)


def push_report(place_bets=False, incremental=False, qualified=None, skip_dedup: bool = False,
                skip_freshness: bool = False, label: str = "", skip_dingtalk: bool = False):
    """推送报告到钉钉。

    Args:
        place_bets: 是否执行自动投注。
        incremental: 增量扫描标记。
        qualified: 可选，来自 build_report 的已处理机会列表。为 None 时独立预处理。
        skip_dedup: 跳过指纹去重 (仅紧急手动重推, 需显式 --skip-dedup)。
        skip_freshness: 跳过对比文件新鲜度检查。
        label: 增量扫描类型标签（如 "24h内临场" 或 "24-72h早盘"）
        skip_dingtalk: 不推送钉钉, 但照常投注+记录(夜里 22:00-6:40 用户休息时用,
                       加快真实投注样本积累, 见 clv-threshold-matrix-todo 记忆)。
    """
    # 推送前二次验价（仅增量模式且 qualified 已由调用者提供时）
    if qualified and incremental:
        qualified = _verify_odds_freshness(qualified)

    # 时间窗口过滤：增量扫描只推送对应时间段的比赛
    if incremental and qualified:
        now_ts = time.time()
        h6_sec = 6 * 3600
        h24_sec = 24 * 3600
        h72_sec = 72 * 3600
        if "urgent" in label.lower() or "<6h" in label:
            qualified = [o for o in qualified
                        if o.get("_pin_epoch") and (o["_pin_epoch"] - now_ts) <= h6_sec]
        elif "24h内" in label or "临场" in label or "near" in label.lower():
            qualified = [o for o in qualified
                        if o.get("_pin_epoch") and h6_sec < (o["_pin_epoch"] - now_ts) <= h24_sec]
        elif "72h" in label or "早盘" in label or "far" in label.lower():
            qualified = [o for o in qualified
                        if o.get("_pin_epoch") and h24_sec < (o["_pin_epoch"] - now_ts) <= h72_sec]
    if not DINGTALK_WEBHOOK:
        logger.info("no DINGTALK_WEBHOOK configured")
        return

    if qualified is None:
        qualified = _prepare_opportunities(skip_freshness=skip_freshness)

    if not qualified:
        logger.info("no +EV opportunities found")
        return

    # 一致性检查用去重前的数据，防止被指纹去重大幅减少 count 导致误报
    pre_dedup_counts = {}
    for o in qualified:
        s = o.get("sport", "unknown")
        pre_dedup_counts[s] = pre_dedup_counts.get(s, 0) + 1

    # V5: 临场<6h去重阈值1%, 中程/远端2%
    _is_urgent = incremental and label and ("<6h" in label or "临场" in label or "urgent" in label.lower())
    if not skip_dedup:
        qualified = _filter_pushed(qualified, time_window="urgent" if _is_urgent else "")
    if not qualified:
        logger.info("所有机会均已推送过，跳过")
        return

    # 双边拦截：同一场同一盘口已推过相反方向 → 跳过（防同场对倒）
    qualified = _filter_opposite_side(qualified)
    if not qualified:
        logger.info("双边拦截后无机会，跳过")
        return

    # 单场推送冷却：同一比赛 4 小时内最多推送 1 次（防止 EV 微小波动导致反复推送）
    qualified = _apply_match_exposure_cap(qualified)

    # 结算可行性门禁: 已知0%成功率的联赛直接拦截 (避免推送无法验证的投注)
    qualified = _filter_unsettleable_leagues(qualified)

    warnings = _check_sport_consistency(qualified, pre_dedup_counts)
    clv_warnings, _ = _check_clv_trend(qualified)
    if clv_warnings:
        warnings = (warnings or []) + clv_warnings
    body = _format_body(qualified, warnings)
    if not body or body.count('#####') == 0:
        # V5.1 铁律: 指纹只在推送成功后写入 — 空推不存指纹
        # 0 条机会(全被门槛/注额过滤)不推, 只留日志(2026-08-23 用户反馈"0条"空推刷屏)
        logger.info("empty body or 0 机会 (指纹未保存 — 下次扫描重新评估)")
        return

    # 扫描标记
    if incremental:
        prefix = label if label else "⚡ 增量扫描"
        title = f"{prefix} +EV 机会: {body.count('#####')} 条"
        body = body.replace("**+EV 投注推荐", f"**{prefix} +EV 机会", 1)
    elif label:
        # 全量/定时扫描带标签（如「每日定时全量推送」）
        title = f"{label} +EV 机会: {body.count('#####')} 条"
        body = body.replace("**+EV 投注推荐", f"**{label} +EV 机会", 1)
    else:
        title = f"+EV 投注推荐: {body.count('#####')} 条"

    from config.settings import send_dingtalk
    # V5.10: skip_dingtalk=True(夜里) 时跳过钉钉, 但照常投注+记录 —— 加快真实投注样本积累
    ok = True
    if not skip_dingtalk:
        ok = send_dingtalk(title, body)
        if not ok:
            # 重试一次（偶发网络抖动）
            time.sleep(2)
            ok = send_dingtalk(title, body)
    if ok:
        # 记录本次推送的所有推荐比赛
        scan_type = "incremental" if incremental else "full"
        recommendation_tracker.log_recommendations(qualified, scan_type=scan_type)

        # 投注前过滤：结算门禁（能投/不能投，不是权重）
        # 权重来自 Pinnacle 130,310场历史数据，不来自结算验证
        bettable = qualified
        from src.core.settleability import is_league_settleable, is_league_probationary
        blocked_leagues = set()
        for o in qualified:
            league = o.get("league", "")
            sport = o.get("sport", "")
            if not is_league_settleable(league, sport) and not is_league_probationary(league, sport):
                blocked_leagues.add(league)

        if blocked_leagues:
            bettable = [o for o in bettable
                        if is_league_settleable(o.get("league", ""), o.get("sport", ""))
                        or is_league_probationary(o.get("league", ""), o.get("sport", ""))]
            logger.info("结算门禁: 跳过 %d 个不可结算联赛", len(blocked_leagues))
            for l in sorted(blocked_leagues):
                logger.info("  🚫 %s", l)

        # 投注后保存指纹
        if place_bets and bettable:
            from src.betting.bb_virtual_bet import PUSH_STAGING_FILE, place_bets_from_push
            PUSH_STAGING_FILE.write_text(json.dumps(bettable, ensure_ascii=False, indent=2))
            logger.info("推送机会已暂存到 %s，开始投注...", PUSH_STAGING_FILE)
            place_bets_from_push(bettable)
        elif place_bets:
            logger.info("无可投注机会（全部被结算可行性过滤）")
        # 指纹永存 — 无论模式, 推送成功即记录
        _save_qualified_fingerprints(qualified)
        # 同场同盘口的盘口线配额, 同样只在推送成功后登记
        _commit_line_quota(qualified)
        # JSON 二次备份 (必须与 _save_qualified_fingerprints 同字段, 含 bb!)
        # V5.5 bug修复: 原代码只写 {"ev","ts"} 丢了 bb, 导致 _filter_pushed 读 JSON 时
        #   old_bb 恒为 0 → "BB赔率上升即重推"永远不触发 → 增量扫描每次只剩 1 场新比赛。
        existing = _load_fingerprints()
        for o in qualified:
            fp = _make_fingerprint(o)
            ev = o.get("ev_pct", 0)
            bb = o.get("bb_odds", 0)
            old_ev = existing[fp].get("ev", 0) if isinstance(existing.get(fp), dict) else (existing.get(fp) or 0)
            if fp not in existing or ev > old_ev:
                existing[fp] = {"ev": ev, "bb": bb, "ts": time.time()}
        _save_fingerprints(existing)

        # 预算修正：去除因指纹去重被过滤的机会，保留实际推送消耗
        _correct_budget_tracker(qualified)

        # CLV 追踪：记录每条推送的赔率数据用于收盘线价值分析
        _log_clv(qualified)

        # 投注追踪：每条推送记录完整信息，赛后结算用
        try:
            from src.monitor.bet_tracker import record_bets
            push_label = os.environ.get("PUSH_LABEL", "手动推送")
            record_bets(qualified, push_label=push_label)
        except Exception as e:
            logger.warning("投注追踪记录失败: %s", e)

        # 风险报告：蒙特卡洛模拟 + 组合健康检查
        _maybe_send_health_report(qualified, place_bets)

        logger.info("BB vs Pinnacle +EV report pushed (%d opportunities)", body.count('#####'))

        # 自动同步: 清除 pyc + Git 提交
        _auto_sync()
    else:
        logger.warning("BB vs Pinnacle push failed (重试后仍失败)")
        # 记录失败标志，供健康检查/日报告警（钉钉宕机时本地留痕）
        try:
            (DATA_DIR / "push_failure_flag.json").write_text(
                json.dumps({"ts": time.time(), "title": title[:120]}, ensure_ascii=False))
        except Exception:
            pass


# ── 格式验证（供 pre-commit 回归测试使用） ──

_FORMAT_MARKERS = {
    "header": "**+EV 投注推荐:",
    "match_prefix": "##### ",
    "fair_price": "公平价:",
    "pinnacle": "Pinnacle:",
    "retail": "价:",  # BB价 / FB价 / BB/FB价
    "edge": "溢价:",
    "stake": "投注:",
    "footer": "来源:",
}


def _validate_format(body: str) -> bool:
    """验证推送body包含所有关键标记。"""
    body_stripped = body.strip()
    if not body_stripped or len(body_stripped) < 50:
        return False
    for key, marker in _FORMAT_MARKERS.items():
        if marker not in body_stripped:
            return False
    return True


def main():
    """CLI 入口 / pipeline_orchestrator 入口。

    --force: 跳过对比文件新鲜度检查 (允许用旧文件推送)
    --skip-dedup: 跳过指纹去重 (仅紧急手动重推, 需显式传)
    --incremental: 增量模式
    --no-bet: 不自动投注
    """
    skip_freshness = "--force" in sys.argv
    skip_dedup = "--skip-dedup" in sys.argv
    is_incremental = "--incremental" in sys.argv
    try:
        body, qualified = build_report(skip_freshness=skip_freshness, incremental=is_incremental)
    except Exception as e:
        logger.error("build_report 崩溃: %s", e, exc_info=True)
        _send_failure_alert([f"build_report 异常: {str(e)[:200]}"])
        return None

    if body is None:
        # 实时赔率拉取失败，已发送钉钉告警，不推送
        logger.error("实时赔率拉取失败，跳过推送")
        return None

    print(body)

    # 保存推送机会到暂存文件
    if qualified and ("--place-bets" in sys.argv or "--stage" in sys.argv):
        from src.betting.bb_virtual_bet import PUSH_STAGING_FILE, place_bets_from_push
        PUSH_STAGING_FILE.write_text(json.dumps(qualified, ensure_ascii=False, indent=2))
        logger.info("推送机会已暂存到 %s: %d 场", PUSH_STAGING_FILE, len(qualified))
        place_bets_from_push(qualified)

    # 读取增量扫描标签（环境变量传递，进程隔离）
    label_flag = os.environ.get("PUSH_LABEL", "")

    if "--no-push" not in sys.argv:
        # V5.10: --night = 夜里模式(不推钉钉, 但照常投注+记录到真实投注库)
        _night = "--night" in sys.argv
        push_report(place_bets=("--no-bet" not in sys.argv) or _night,
                    incremental=is_incremental,
                    qualified=qualified if qualified else None,
                    skip_dedup=skip_dedup,
                    skip_freshness=skip_freshness,
                    label=label_flag,
                    skip_dingtalk=_night)
    return body


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
