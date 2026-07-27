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
    BANKROLL,
    MAX_BETS as MAX_OPPORTUNITIES,
    EV_CAP,
    KELLY_FRACTION,
    SPORT_ORDER,
    get_league_tier as _get_league_tier,
    league_multiplier,
)

COMPARISON_FILE = DATA_DIR / "bb_vs_pinnacle_comparison.json"
FB_COMPARISON_FILE = DATA_DIR / "bb_vs_pinnacle_comparison_FB.json"
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
CONSISTENCY_WARN_THRESHOLD = 0.20

# 不靠谱联赛 — 匹配质量差、假阳性多，直接屏蔽（从固定文件加载）
_BANNED_LEAGUES = _load_banned_leagues()

# CLV 暂停缓存 — 进程内只从 SQLite 加载一次
_CLV_SUSPENSIONS_CACHE = None


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
    "htft": 0.80,
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
    "英甲": 1.00, "意甲": 1.00,
    # 低准确度 (抽水>5%): Pinnacle较不准→比价可靠性低→-15%
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
    "dnb":  0.80, "dc": 0.80, "ht": 0.75, "corner": 0.70,
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
# 低抽水市场 → 公平价更可靠 → 略高 Kelly
# 无Pinnacle收盘数据的市场 → 保守 Kelly
KELLY_BY_MARKET = {
    "ou":   0.55,  # OU抽水最低(3.87%)→最可靠
    "btts": 0.52,  # 学术研究支持
    "hc":   0.50,  # 标准
    "dnb":  0.50,  # 中性
    "1x2":  0.45,  # 抽水稍高(4.29%)，且历史假阳性多→略降
    "ht":   0.45,  # 半场1X2→对齐全场1X2（Pinnacle有HT数据，无理由更低）
    "dc":   0.45,  # 从1X2推导
    "oe":   0.35,  # 无Pinnacle数据
    "corner": 0.35, # 无Pinnacle数据
    "htft": 0.35,  # 无Pinnacle数据
}
def _get_kelly_for_market(sub_market: str) -> float:
    """根据市场类型返回对应的 Kelly 分数。"""
    return KELLY_BY_MARKET.get(sub_market, KELLY_FRACTION)

# ── 日预算总额控制 (取代分组预算上限) ──
# 纯 Kelly 分配, 总额 ¥10,000/天, 按投注额比例压缩
TOTAL_DAILY_BUDGET = 10000  # 日预算总额


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

def _log_clv(opps: list):
    """将推送机会的 CLV 数据写入 SQLite（主）和 CSV（备份）。"""
    from config.database import insert_push_clv
    insert_push_clv(opps)

    # CSV 备份
    import csv
    exists = CLV_LOG_FILE.exists()
    with open(CLV_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow([
                "timestamp", "sport", "league", "home", "away",
                "designation", "sub_market", "bb_odds", "pin_odds",
                "fair_price", "ev_pct", "stake", "tier", "match_epoch",
            ])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for o in opps:
            writer.writerow([
                now,
                o.get("sport", ""),
                o.get("league", ""),
                o.get("home_cn", ""),
                o.get("away_cn", ""),
                o.get("designation", ""),
                o.get("_sub_market", o.get("_market", "")),
                o.get("bb_odds", 0),
                o.get("pin_odds", 0),
                o.get("fair_price", 0),
                o.get("ev_pct", 0),
                o.get("_stake", 0),
                o.get("_tier", 0),
                o.get("_pin_epoch", ""),
            ])


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
    "btts": "双边进球",
    "dc": "双重机会",
    "dnb": "平局退款",
    "oe": "单/双",
    "htft": "半全场",
    "corner": "角球",
}


def _min_ev_for_tier(tier: int) -> float:
    """每层最低 EV 门槛。T1 最可信门槛最低，T3 需显著更高 edge 才推。"""
    if tier == 1:
        return 3.0
    elif tier == 2:
        return 4.0
    elif tier == 3:
        return 5.0
    return 99.0  # Tier 4 不推送

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


def _calc_kelly_stakes(opps: list) -> list:
    """按 Kelly 比例计算投注额，加单注上限 + 单场上限 + 市场预算上限。"""
    from config.constants import MAX_STAKE_PCT as _MAX_STAKE_PCT, PER_MATCH_CAP_PCT as _PER_MATCH_CAP_PCT

    # 第一遍：计算毛 Kelly 投注额
    for o in opps:
        k = o.get("_kelly_pct", 0)
        stake = round(BANKROLL * k / 100)
        max_stake = BANKROLL * _MAX_STAKE_PCT
        stake = int(min(stake, max_stake))
        if stake < 5:
            stake = 0
        o["_stake"] = stake

    # 第二遍：同一比赛多盘口 → 按比例压缩到单场上限
    from collections import defaultdict
    match_groups = defaultdict(list)
    for o in opps:
        key = (o.get("home_cn", ""), o.get("away_cn", ""))
        match_groups[key].append(o)

    per_match_max = BANKROLL * _PER_MATCH_CAP_PCT
    for key, group in match_groups.items():
        total = sum(o["_stake"] for o in group)
        if total > per_match_max:
            ratio = per_match_max / total
            for o in group:
                o["_stake"] = max(0, round(o["_stake"] * ratio))

    # 第三遍：总额预算控制（替代分组预算上限）
    # 纯 Kelly 决定相对比例，总额超过日预算时等比压缩
    # 预算耗尽时仍保留 stake=0 供展示
    spent, today = _load_budget_tracker()
    daily_used = sum(spent.values()) if spent else 0
    remaining = TOTAL_DAILY_BUDGET - daily_used

    total_wanted = sum(o["_stake"] for o in opps if o["_stake"] > 0)
    if remaining <= 0:
        for o in opps:
            o["_stake"] = 0
        logger.info("日预算已用完 (¥%d/¥%d), 保留机会供展示", int(daily_used), TOTAL_DAILY_BUDGET)
    elif total_wanted > remaining:
        ratio = remaining / total_wanted
        for o in opps:
            if o["_stake"] > 0:
                o["_stake"] = max(0, round(o["_stake"] * ratio))
        logger.info("日预算压缩: 需¥%d 剩¥%d, 压缩至%.0f%%",
                    total_wanted, int(remaining), ratio * 100)

    # 保存预分配（总额）供下次推送参考
    new_total = daily_used + sum(o["_stake"] for o in opps if o["_stake"] > 0)
    _save_budget_tracker({"total": new_total}, today)

    return opps


def _correct_budget_tracker(opps: list):
    """推送成功后重算预算消耗，排除因指纹去重被过滤的机会。"""
    spent, today = _load_budget_tracker()
    total = sum(o["_stake"] for o in opps if o["_stake"] > 0)
    # 保留当天之前推送的累积，加上本次实际
    prev_total = sum(spent.values()) if spent else 0
    _save_budget_tracker({"total": prev_total + total}, today)


def _collect_opportunities(match, market_key):
    """从指定市场收集 +EV 机会。校准过滤：时间匹配必须高分才推送。

    为每条机会附加 bb_price_source 字段，标记该赔率来自哪个平台（BB/FB）。
    """
    # 96小时窗口过滤：超过未来96小时的比赛不推送（资金效率平衡）
    pin_epoch = match.get("start_time_pin_epoch")
    if pin_epoch:
        now_epoch = datetime.now(timezone.utc).timestamp()
        if pin_epoch > now_epoch + 96 * 3600:
            return []
        # 已开赛过滤：开赛时间已过的比赛不推送（给5分钟缓冲）
        if pin_epoch + 300 < now_epoch:
            return []

    match_type = match.get("match_type", "unknown")
    match_score = match.get("match_score", 0.7)
    # 时间匹配（非队名匹配）需要高置信度，防止推错比赛。
    # 门限必须与 bb_vs_pinnacle.py Phase 2 保持一致：
    #   网球 0.75，其他 0.70
    if match_type == "time":
        sport = match.get("sport", "")
        min_ok = 0.75 if sport == "tennis" else 0.70
        if match_score < min_ok:
            return []
    league = match.get("league", "")
    home_cn = match.get("home_bb", "")
    away_cn = match.get("away_bb", "")
    league_mult = league_multiplier(league)

    # 屏蔽不靠谱联赛
    for banned in _BANNED_LEAGUES:
        if banned in league:
            return []

    # CLV 趋势暂停检查：连续负 CLV → 暂时跳过该 (sport, league)
    if (match.get("sport", ""), league) in _get_clv_suspensions():
        return []

    # 联赛可信度分层过滤
    tier = _get_league_tier(league)
    if tier == 4:
        return []  # Tier 4 仅扫描不推送
    # Tier 2/3: 非队名匹配且匹配分<0.80 不推送（防假阳性）
    if tier >= 2 and match_type != "name" and match_score < 0.80:
        return []
    min_ev = _min_ev_for_tier(tier)

    # 确定该市场类型对应哪个平台提供了最高赔率
    _MK_TO_SOURCE_KEY = {
        "opportunities": "ml",
        "handicap": "handicap",
        "over_under": "ou",
        "double_chance": "dc",
        "draw_no_bet": "dnb",
    }
    platform_sources = match.get("platform_sources", {})
    source_key = _MK_TO_SOURCE_KEY.get(market_key, "ml")
    price_source = platform_sources.get(source_key, match.get("bb_price_source", "BB"))

    result = []
    for opp in match.get(market_key, []):
        ev = opp.get("ev_pct", 0)
        if ev < min_ev:  # 按 Tier 动态门槛过滤
            continue
        bb_odds = opp.get("bb_odds", 0)
        pin_odds = opp.get("pin_odds", 0)

        # EV 上限过滤：EV > EV_CAP 几乎全是假阳性（中文队名匹配到错误的英文队名）
        if ev > EV_CAP:
            continue

        # 超高赔率过滤：BB 赔率 > 15.0 且不是主流联赛 → 跳过
        # （小联赛弱队不可能有真实 15+ 赔率，通常是匹配错误）
        if bb_odds > 15.0 and league_mult < 1.0:
            continue

        # 市场子类型识别：区分同一 market_key 下的不同市场（如 1X2 / HT / BTTS / DC）
        sub_market = opp.get("_market", "")
        if not sub_market or sub_market == "main":
            # 根据 market_key 推导子类型
            _MK_TO_SUB = {
                "opportunities": "1x2",
                "handicap": "hc",
                "over_under": "ou",
                "double_chance": "dc",
                "draw_no_bet": "dnb",
            }
            sub_market = _MK_TO_SUB.get(market_key, "1x2")

        # 赔率上限过滤: Pinnacle 历史数据按运动/联赛/市场限制
        _odds_limit = _get_odds_limit(match.get("sport", ""), league, sub_market)
        if _odds_limit and bb_odds > _odds_limit:
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
            "home_cn": home_cn,
            "away_cn": away_cn,
            "home_team": match.get("home_pin", home_cn),
            "away_team": match.get("away_pin", away_cn),
            "designation": display_name,
            "bb_odds": bb_odds,
            "pin_odds": pin_odds,
            "fair_price": fair,
            "ev_pct": ev,
            "start_time_bb": match.get("start_time_bb", ""),
            "_market_type": market_key,  # "opportunities"|"handicap"|"over_under"|...
            "_sub_market": sub_market,  # "1x2"|"ht"|"btts"|"dc"|"oe"|"htft"|...

            "_match_score": match_score,
            "_score": score,
            "_weighted_ev": weighted_ev,
            "_market_weight": market_w,
            "_kelly_pct": kelly_pct,
            "_tier": tier,
            "_pin_epoch": match.get("start_time_pin_epoch"),  # 用于显示开赛时间
            "bb_price_source": price_source,  # 标记赔率来源平台
        })
    return result


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
    # 读取主对比文件
    main_opps = _read_comparison_file(COMPARISON_FILE)
    # 读取FB独立对比文件
    fb_opps = _read_comparison_file(FB_COMPARISON_FILE)
    if not fb_opps:
        return main_opps

    # 去重合并：FB机会中指纹不在主列表的才添加
    main_fps = {_make_fingerprint(o) for o in main_opps}
    merged = list(main_opps)
    added = 0
    for o in fb_opps:
        fp = _make_fingerprint(o)
        if fp not in main_fps:
            merged.append(o)
            main_fps.add(fp)
            added += 1
    if added:
        logger.info("FB独立对比: 添加 %d 个独有机会", added)

    # 二次去重：同场比赛同一盘口只保留最高赔率
    # （联赛名可能因API来源不同而不一致，导致指纹不匹配）
    # 注意到 designation 可能因空格或数字精度不同而不一致，特做 whitespace 归一化
    best_per_match = {}
    dup_removed = 0
    for o in merged:
        match_key = (
            o.get("sport", ""),
            o.get("home_cn", "").strip(),
            o.get("away_cn", "").strip(),
            o.get("designation", "").replace(" ", "").replace("（", "(").replace("）", ")"),
        )
        existing = best_per_match.get(match_key)
        if existing is None or o.get("bb_odds", 0) > existing.get("bb_odds", 0):
            best_per_match[match_key] = o

    if len(best_per_match) < len(merged):
        dup_removed = len(merged) - len(best_per_match)
        merged = list(best_per_match.values())
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
        # 低匹配度过滤：Phase 2 时间匹配产生错误赛果的风险高
        match_score = match.get("match_score", 1.0)
        if match_score < 0.85:
            continue
        for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
            qualified.extend(_collect_opportunities(match, mk))
    return qualified


def _diversify_and_rank(qualified: list) -> list:
    """多样性选择 + 按联赛 Tier 排序 + Kelly 分配。"""
    if not qualified:
        return []

    # 各运动至少保留 1 条（按 Tier 优先选）
    selected = []
    selected_ids = set()
    for sport in ("football", "basketball", "tennis", "baseball", "american_football",
                   "pingpong", "badminton", "volleyball", "boxing", "mma", "ice_hockey"):
        sport_opps = [o for o in qualified if o.get("sport") == sport]
        if sport_opps:
            best = max(sport_opps, key=lambda x: (4 - x.get("_tier", 3), x["_score"]))
            selected.append(best)
            selected_ids.add(id(best))

    remaining = [o for o in qualified if id(o) not in selected_ids]
    # 核心改动：按 Tier 排序（1 优先），同 Tier 内按 score 降序
    remaining.sort(key=lambda o: (o.get("_tier", 3), -o["_score"]))
    max_remaining = MAX_OPPORTUNITIES - len(selected)
    selected.extend(remaining[:max_remaining])
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

    # 从对比文件读匹配数据和机会数据（主对比 + FB 独立对比）
    for fpath in (COMPARISON_FILE, FB_COMPARISON_FILE):
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
    _TIER_LABEL = {1: "T1", 2: "T2", 3: "T3"}
    now_str = datetime.now(timezone.utc).astimezone().strftime("%m/%d %H:%M")
    total_allocated = sum(o["_stake"] for o in qualified)

    # 数据新鲜度：读取文件 mtime 显示提取时间
    bb_file = DATA_DIR / "bb_odds_extracted.json"
    pin_file = COMPARISON_FILE
    bb_time = ""
    pin_time = ""
    try:
        if bb_file.exists():
            bb_mtime = datetime.fromtimestamp(bb_file.stat().st_mtime, tz=timezone.utc).astimezone()
            bb_time = bb_mtime.strftime("%m/%d %H:%M")
    except (OSError, ValueError):
        pass
    try:
        if pin_file.exists():
            pin_mtime = datetime.fromtimestamp(pin_file.stat().st_mtime, tz=timezone.utc).astimezone()
            pin_time = pin_mtime.strftime("%m/%d %H:%M")
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

    # 按比赛分组：(sport, league, home_cn, away_cn)
    from collections import OrderedDict
    groups = OrderedDict()
    for o in qualified:
        gkey = (o.get("sport", ""), o.get("league", ""), o.get("home_cn", ""), o.get("away_cn", ""))
        if gkey not in groups:
            groups[gkey] = []
        groups[gkey].append(o)

    # 组间排序：按运动 → Tier → 联赛 → 最早开赛时间（同联赛紧挨着）
    def group_sort_key(item):
        (sport, league, home, away), opps = item
        tier = opps[0].get("_tier", 3)
        min_epoch = min((o.get("_pin_epoch") or 9999999999) for o in opps)
        return (SPORT_ORDER.get(sport, 99), tier, league or "", min_epoch)
    sorted_groups = sorted(groups.items(), key=group_sort_key)

    lines = list(warning_lines)
    prev_sport = None
    prev_league = None
    match_idx = 0

    for (sport, league, home, away), opps in sorted_groups:
        # 组内按加权 EV 降序（市场质量高的优先）
        opps.sort(key=lambda o: -o.get("_weighted_ev", o["ev_pct"]))

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
            lines.append(f"  [{tier_label}] {league}")
            prev_league = league

        match_idx += 1
        bj_time = opps[0].get("start_time_bb", "") or _format_bj_time(opps[0].get("_pin_epoch"))
        time_suffix = f"  ({bj_time})" if bj_time else ""
        lines.append(f"  ##### #{match_idx} {home} 对 {away}{time_suffix}")

        for o in opps:
            oc = o["designation"]
            pinny = round(o.get("pin_odds", 0), 2) if o.get("pin_odds", 0) > 0 else 0
            fair = o.get("fair_price") or round(o["pin_odds"], 2)
            bb_odds = o["bb_odds"]
            ev_pct = o["ev_pct"]
            stake = o["_stake"]
            confidence = "✓" if o.get("_match_score", 0) >= 0.95 else "◷"

            # 来源平台标签
            src = o.get("bb_price_source", "BB")
            if src == "ALL":
                src = "BB/FB"
            source_label = f"{src}价"

            lines.append(
                f"    [{oc}] {confidence} 公平价: {fair}"
                + (f" | Pinnacle: {pinny}" if o.get("pin_odds", 0) > 0 else " | 推导: 1X2")
                + f" | {source_label}: {bb_odds} | 溢价: +{ev_pct}% | 投注: ¥{stake:,}"
            )

    # 数据时间（用文件 mtime，即实际提取时间）
    data_time_parts = []
    if bb_time:
        data_time_parts.append(f"BB数据 {bb_time}")
    if pin_time:
        data_time_parts.append(f"Pinnacle {pin_time}")
    data_time_str = " | ".join(data_time_parts) if data_time_parts else f"数据 {now_str}"

    title = f"+EV 投注推荐: {match_idx} 场比赛"
    body = (
        f"**{title}**\n\n"
        f"{data_time_str} | 总额 ¥{total_allocated:,}\n"
        + (f"**{sport_summary_line}**\n\n" if sport_summary_line else "")
        + (f"来源: {platform_stats}\n\n" if platform_stats else "\n")
        + "\n".join(lines).strip()
    )
    body += ("\n\n---\n"
             "💡 T1=Pinnacle最可靠 T2=主流联赛 T3=低级别 | "
             "公平价 = Pinnacle去抽水赔率 | "
             "溢价 = (售价 - 公平价) / 公平价 | "
             "来源: BB=BB价 FB=FB价 BB/FB=两平台相同 | "
             "赔率实时变动，以 Pinnacle 网站当前价为准")
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


def _prepare_opportunities(force=False):
    """对比文件必须 < 30 分钟, 确保赔率是实时的, 拒绝缓存。"""
    if not COMPARISON_FILE.exists():
        return []
    mtime = COMPARISON_FILE.stat().st_mtime
    age_min = (time.time() - mtime) / 60
    if age_min > 30:
        print(f"❌ 对比文件过期 ({age_min:.0f}分钟前)，拒绝使用缓存")
        return []

    qualified = _collect_opportunities_from_file()
    if not qualified:
        return []

    return _diversify_and_rank(qualified)


def build_report(force: bool = False):
    """构建格式化的 BB vs Pinnacle +EV 报告。返回 (body_text, qualified_opportunities).

    Args:
        force: 跳过 2 小时新鲜度检查，即使对比文件较旧也继续推送。
    """
    qualified = _prepare_opportunities(force=force)
    if not qualified:
        if not COMPARISON_FILE.exists():
            return "no comparison data", []
        return "no +EV opportunities", []

    sport_summary = _compute_sport_summary()
    warnings = _check_sport_consistency(qualified)
    clv_warnings, _ = _check_clv_trend(qualified)
    if clv_warnings:
        warnings = (warnings or []) + clv_warnings
    body = _format_body(qualified, warnings, sport_summary)
    return body, qualified


# ── 推送去重 ──

def _make_fingerprint(o: dict) -> str:
    """为一条机会生成唯一指纹：sport|league|home|away|盘口（含线值）|子市场|比赛日期

    含子市场（_sub_market）防止 1X2 客胜 vs HT 客胜 等跨市场误杀。
    盘口线参与指纹：线变了（如 -9.5→-10.5）视为新机会可重新推送。
    仅归一化队名空格，防止 "(女)" 前不一致空格导致误判为不同机会。
    """
    match_date = ""
    ep = o.get("_pin_epoch")
    if ep:
        try:
            dt = datetime.fromtimestamp(ep, tz=timezone.utc)
            match_date = dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass
    # 归一化空格：前后 trim + 内部连续空格 → 单空格
    def _norm(s):
        import re as _re
        return _re.sub(r'\s+', ' ', s.strip()) if s else s
    sub = o.get("_sub_market", o.get("_market", ""))
    return f"{_norm(o.get('sport',''))}|{_norm(o.get('league',''))}|{_norm(o.get('home_cn',''))}|{_norm(o.get('away_cn',''))}|{_norm(o.get('designation',''))}|{sub}|{match_date}"


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


def _filter_pushed(qualified: list) -> list:
    """过滤已推送机会，同盘口EV上涨>1%允许重推。"""
    existing = _load_fingerprints()
    if not existing:
        return qualified
    new = []
    skipped = 0
    re_pushed = 0
    for o in qualified:
        fp = _make_fingerprint(o)
        ev = o.get("ev_pct", 0)
        if fp in existing:
            old_ev = existing[fp]
            if ev - old_ev > 1.0:
                re_pushed += 1
                new.append(o)
            else:
                skipped += 1
        else:
            new.append(o)
    if skipped:
        logger.info("去重过滤: 跳过 %d 条已推送机会", skipped)
    if re_pushed:
        logger.info("EV重推: %d 条机会EV上涨>1%重新推送", re_pushed)
    return new


def push_report(place_bets=False, incremental=False, qualified=None, force=False, label: str = ""):
    """推送报告到钉钉。

    Args:
        place_bets: 是否执行自动投注。
        incremental: 增量扫描标记。
        qualified: 可选，来自 build_report 的已处理机会列表。为 None 时独立预处理。
        force: 跳过指纹去重，强制推送。
        label: 增量扫描类型标签（如 "24h内临场" 或 "24-72h早盘"）
    """
    # 时间窗口过滤：增量扫描只推送对应时间段的比赛
    if incremental and qualified:
        now_ts = time.time()
        h24_sec = 24 * 3600
        h72_sec = 72 * 3600
        if "24h内" in label or "临场" in label:
            qualified = [o for o in qualified
                        if o.get("_pin_epoch") and (o["_pin_epoch"] - now_ts) <= h24_sec]
        elif "72h" in label or "早盘" in label:
            qualified = [o for o in qualified
                        if o.get("_pin_epoch") and h24_sec < (o["_pin_epoch"] - now_ts) <= h72_sec]
    if not DINGTALK_WEBHOOK:
        logger.info("no DINGTALK_WEBHOOK configured")
        return

    if qualified is None:
        qualified = _prepare_opportunities(force=True)

    if not qualified:
        logger.info("no +EV opportunities found")
        return

    # 一致性检查用去重前的数据，防止被指纹去重大幅减少 count 导致误报
    pre_dedup_counts = {}
    for o in qualified:
        s = o.get("sport", "unknown")
        pre_dedup_counts[s] = pre_dedup_counts.get(s, 0) + 1

    if not force:
        qualified = _filter_pushed(qualified)
    if not qualified:
        logger.info("所有机会均已推送过，跳过")
        return

    warnings = _check_sport_consistency(qualified, pre_dedup_counts)
    clv_warnings, _ = _check_clv_trend(qualified)
    if clv_warnings:
        warnings = (warnings or []) + clv_warnings
    body = _format_body(qualified, warnings)
    if not body:
        logger.info("empty body, skip")
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
        # 指纹保存 (含EV追踪, 同盘口EV上涨>1%可重推)
        from config.database import add_fingerprints
        new_fps = {_make_fingerprint(o): o.get("ev_pct", 0) for o in qualified}
        add_fingerprints(new_fps)
        # JSON 二次备份
        existing = _load_fingerprints()
        for fp, ev in new_fps.items():
            if fp not in existing or ev > existing[fp]:
                existing[fp] = ev
        _save_fingerprints(existing)

        # 预算修正：去除因指纹去重被过滤的机会，保留实际推送消耗
        _correct_budget_tracker(qualified)

        # CLV 追踪：记录每条推送的赔率数据用于收盘线价值分析
        _log_clv(qualified)

        logger.info("BB vs Pinnacle +EV report pushed (%d opportunities)", body.count('#####'))
    else:
        logger.warning("BB vs Pinnacle push failed")


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
    """CLI 入口 / pipeline_orchestrator 入口。"""
    force_fresh = "--force" in sys.argv
    body, qualified = build_report(force=force_fresh)
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
        push_report(place_bets=("--no-bet" not in sys.argv),
                    incremental="--incremental" in sys.argv,
                    qualified=qualified if qualified else None,
                    force=force_fresh,
                    label=label_flag)
    return body


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
