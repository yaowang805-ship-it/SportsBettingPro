"""投注追踪器 — 每条推送 → 记录 → 结算 → 真实盈亏。

铁律:
1. 每次推送必须记录所有投注的完整信息 (含 Pinnacle 队名)
2. 比赛结束后自动拉取赛果并结算
3. 产出真实盈亏数据 (按运动/联赛/盘口/赔率拆分)
"""
import json, os, sys, time, csv, logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

TRACKED_BETS_FILE = DATA_DIR / "tracked_bets.json"
SETTLED_BETS_FILE = DATA_DIR / "settled_bets.json"


# ═══════════════════════════════════════════════════════════════════════════
# 投注记录
# ═══════════════════════════════════════════════════════════════════════════

def load_tracked_bets():
    """加载所有已追踪投注。"""
    if TRACKED_BETS_FILE.exists():
        try:
            data = json.loads(TRACKED_BETS_FILE.read_text())
            if not isinstance(data, dict):
                data = {}
            # 防御: 旧文件/重置可能缺 meta 或 bets 键, 补齐避免 record_bets KeyError('meta')
            data.setdefault("bets", [])
            data.setdefault("meta", {})
            return data
        except (json.JSONDecodeError, OSError):
            logger.warning("追踪文件损坏，重建")
    return {"bets": [], "meta": {"created": datetime.now(timezone.utc).isoformat()}}


def save_tracked_bets(data):
    """原子写入追踪文件。"""
    tmp = TRACKED_BETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(TRACKED_BETS_FILE)


def _compute_simple_kelly_stake(o: dict) -> int:
    """A/B测试: Simple Half Kelly from EV%.  cap 2% per bet (对齐职业标准)."""
    ev = o.get("ev_pct", 0)
    odds = o.get("bb_odds", 0)
    if ev > 0 and odds > 1:
        pct = min((ev / 100) / (odds - 1) * 0.5, 0.02)
        stake = int(20000 * pct)  # ¥20,000 bankroll
        return stake if stake >= 30 else 0
    return 0


def _compute_simple_kelly_pct(o: dict) -> float:
    """A/B测试: Simple Half Kelly 仓位百分比."""
    ev = o.get("ev_pct", 0)
    odds = o.get("bb_odds", 0)
    if ev > 0 and odds > 1:
        return round(min((ev / 100) / (odds - 1) * 0.5, 0.02) * 100, 2)
    return 0.0


def record_bets(opportunities: list, push_label: str = ""):
    """在推送时记录投注。每条机会 → 一条追踪记录。

    Args:
        opportunities: bb_ev_push 产生的 qualified list
        push_label: 推送标签 (如 "每日定时全量推送")
    """
    if not opportunities:
        return

    data = load_tracked_bets()
    existing_ids = {b["push_id"] for b in data["bets"]}
    new_count = 0

    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    for o in opportunities:
        # 生成唯一 ID: sport_league_home_away_market_designation_date
        push_id = _make_push_id(o)
        if push_id in existing_ids:
            continue  # 已记录，跳过

        bet = {
            "push_id": push_id,
            "push_time": now_iso,
            "push_label": push_label,
            "sport": o.get("sport", ""),
            "league": o.get("league", ""),
            "league_cn": o.get("league_cn", o.get("league", "")),  # 中文联赛名, 结算用中文名匹配结算源(2026-08-24)
            "bb_match_id": o.get("bb_match_id", ""),  # BB比赛ID, 结算按ID精确匹配
            "home": o.get("home_cn", ""),
            "away": o.get("away_cn", ""),
            "home_pin": o.get("home_team", o.get("home_pin", "")),
            "away_pin": o.get("away_team", o.get("away_pin", "")),
            "designation": o.get("designation", ""),
            "sub_market": o.get("_sub_market", o.get("_market", "")),
            "bb_odds": o.get("bb_odds", 0),
            "pin_odds": o.get("pin_odds", 0),
            "fair_price": o.get("fair_price", 0),
            "ev_pct": o.get("ev_pct", 0),
            "stake": o.get("_stake", 0),           # V5 Matrix 投注额
            "kelly_pct": o.get("_kelly_pct", 0),     # V5 Matrix Kelly%
            # A/B 测试: Simple Kelly 策略 (对比基准)
            "simple_stake": _compute_simple_kelly_stake(o),
            "simple_kelly_pct": _compute_simple_kelly_pct(o),
            # 结算字段
            "profit": None,            # V5 Matrix 盈亏
            "profit_simple": None,      # Simple Kelly 盈亏
            "tier": o.get("_tier", 0),
            "match_score": o.get("match_score", 0),
            "match_epoch": o.get("_pin_epoch", 0),
            "match_time_bb": o.get("start_time_bb", ""),
            "match_time_pin": o.get("start_time_pin", ""),
            "bb_price_source": o.get("bb_price_source", "BB"),
            # 结算字段
            "status": "pending",
            "result": None,
            "settled_at": None,
            "home_score": None,
            "away_score": None,
            "profit": None,
            "settle_source": None,
            "settle_attempts": 0,
            "last_settle_attempt": None,
        }
        data["bets"].append(bet)
        new_count += 1

    if new_count > 0:
        data["meta"]["last_push"] = now_iso
        data["meta"]["last_push_count"] = new_count
        save_tracked_bets(data)
        logger.info("📝 投注追踪: +%d 条新记录 (累计 %d)", new_count, len(data["bets"]))
    else:
        logger.info("📝 投注追踪: 0 条新记录 (全部重复)")


def _make_push_id(o: dict) -> str:
    """生成投注唯一 ID，同场比赛同一盘口不重复记录。"""
    home = o.get("home_cn", "")
    away = o.get("away_cn", "")
    league = o.get("league", "")
    designation = o.get("designation", "").replace(" ", "").replace("（", "(").replace("）", ")")
    sub = o.get("_sub_market", o.get("_market", ""))
    sport = o.get("sport", "")
    epoch = o.get("_pin_epoch", 0)
    # 用比赛日避免跨天同名比赛冲突
    match_date = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%d") if epoch else ""
    return f"{sport}|{league}|{home}|{away}|{designation}|{sub}|{match_date}"


# ═══════════════════════════════════════════════════════════════════════════
# 盈亏统计
# ═══════════════════════════════════════════════════════════════════════════

def get_pnl_summary() -> dict:
    """从追踪记录计算真实盈亏。"""
    data = load_tracked_bets()
    bets = data.get("bets", [])

    # V5.10: void 不能计入 ROI 分母。
    # 实测 133 笔 void 里 118 笔连比分都没有(settle_source=timeout_void), 本质是
    # "拿不到赛果"被一次性作废, 不是真正的退款。把它们算进 settled 会把分母从
    # ¥6,361 稀释到 ¥20,461 —— 真实亏损 -14.4% 被掩盖成 -4.5%, 差 3.2 倍。
    # 真实盈亏只看真正定了胜负的注; void 单列, 让"无法结算"这件事本身可见。
    # half_won/half_lost 是真实盈亏(半赢半输), 必须计入 decided, 不能算进 voided
    decided = [b for b in bets if b.get("result") in ("won", "lost", "half_won", "half_lost")]
    voided = [b for b in bets if b.get("status") == "settled"
              and b.get("result") not in ("won", "lost", "half_won", "half_lost")]
    settled = decided
    pending = [b for b in bets if b.get("status") == "pending"]

    total_stake = sum(b["stake"] for b in settled)
    total_profit = sum(b.get("profit", 0) or 0 for b in settled)

    by_sport = defaultdict(lambda: {"bets": 0, "stake": 0, "profit": 0, "won": 0, "lost": 0})
    for b in settled:
        s = b["sport"]
        by_sport[s]["bets"] += 1
        by_sport[s]["stake"] += b["stake"]
        by_sport[s]["profit"] += b.get("profit", 0) or 0
        if b.get("result") == "won":
            by_sport[s]["won"] += 1
        elif b.get("result") == "lost":
            by_sport[s]["lost"] += 1

    by_league = defaultdict(lambda: {"bets": 0, "stake": 0, "profit": 0})
    for b in settled:
        lg = b.get("league", "?")
        by_league[lg]["bets"] += 1
        by_league[lg]["stake"] += b["stake"]
        by_league[lg]["profit"] += b.get("profit", 0) or 0

    # A/B 对比: Simple Kelly vs V5 Matrix
    simple_stake = sum(b.get("simple_stake", 0) or 0 for b in settled)
    simple_profit = sum(b.get("profit_simple", 0) or 0 for b in settled)
    simple_roi = round(simple_profit / simple_stake * 100, 2) if simple_stake > 0 else 0

    return {
        "total_bets": len(bets),
        "settled": len(settled),          # 只含真正定了胜负的
        "pending": len(pending),
        # V5.10: 无法结算的单列出来 —— 这个数越大, 上面的 ROI 代表性越弱
        "unresolved": len(voided),
        "unresolved_stake": sum(b.get("stake", 0) or 0 for b in voided),
        "total_stake": total_stake,
        "total_profit": total_profit,
        "roi_pct": round(total_profit / total_stake * 100, 2) if total_stake > 0 else 0,
        # A/B test
        "simple_stake": simple_stake,
        "simple_profit": simple_profit,
        "simple_roi_pct": simple_roi,
        "v5_stake": total_stake,
        "v5_profit": total_profit,
        "v5_roi_pct": round(total_profit / total_stake * 100, 2) if total_stake > 0 else 0,
        "roi_pct": round(total_profit / total_stake * 100, 2) if total_stake > 0 else 0,
        "by_sport": dict(by_sport),
        "by_league": dict(by_league),
    }


def get_unsettled_bets(hours_after_match: float = 2.0) -> list:
    """获取所有需要结算的投注（比赛已结束超过 N 小时仍未结算的）。

    Args:
        hours_after_match: 比赛结束多少小时后才开始尝试结算
    """
    data = load_tracked_bets()
    now_epoch = time.time()
    unsettled = []

    for b in data["bets"]:
        if b.get("status") != "pending":
            continue
        match_epoch = b.get("match_epoch", 0)
        if not match_epoch:
            continue
        # 比赛预计结束时间 (开赛 + 2.5h 作为比赛时长估计)
        match_end_epoch = match_epoch + 2.5 * 3600
        if now_epoch > match_end_epoch + hours_after_match * 3600:
            unsettled.append(b)

    return unsettled


def settle_bet(push_id: str, result: str, home_score=None, away_score=None,
               profit=None, source: str = ""):
    """结算单笔投注。

    Args:
        push_id: 投注唯一 ID
        result: won / lost / void / half_won / half_lost
        home_score, away_score: 比分
        profit: 盈亏 (正=盈利, 负=亏损, 0=void)
        source: 赛果来源 (espn/football_data/zhiboba)
    """
    data = load_tracked_bets()
    for b in data["bets"]:
        if b["push_id"] == push_id:
            b["status"] = "settled"
            b["result"] = result
            b["home_score"] = home_score
            b["away_score"] = away_score
            b["settled_at"] = datetime.now(timezone.utc).isoformat()
            b["settle_source"] = source

            # 自动计算盈亏 (V5 Matrix)
            if profit is None:
                stake = b["stake"]
                if result == "won":
                    profit = stake * (b["bb_odds"] - 1)
                elif result == "lost":
                    profit = -stake
                elif result == "void":
                    profit = 0
                elif result == "half_won":
                    profit = stake * (b["bb_odds"] - 1) / 2
                elif result == "half_lost":
                    profit = -stake / 2
                else:
                    profit = 0
            b["profit"] = round(profit, 2)

            # A/B: Simple Kelly 盈亏
            simple_stake = b.get("simple_stake", 0) or 0
            if result == "won":
                b["profit_simple"] = round(simple_stake * (b["bb_odds"] - 1), 2)
            elif result == "lost":
                b["profit_simple"] = -simple_stake
            elif result == "void":
                b["profit_simple"] = 0
            elif result == "half_won":
                b["profit_simple"] = round(simple_stake * (b["bb_odds"] - 1) / 2, 2)
            elif result == "half_lost":
                b["profit_simple"] = round(-simple_stake / 2, 2)
            else:
                b["profit_simple"] = 0

            # V5.2: 同步到 RiskManager（冷却/回撤/连败状态）— 仅 won/lost（void/half 不喂）
            if result in ("won", "lost"):
                try:
                    from src.risk.manager import RiskManager
                    rm = RiskManager()
                    _odds = b.get("bb_odds", 2.0) or 2.0
                    _fair = b.get("fair_price", 0) or 0
                    _prob = 1.0 / _fair if _fair > 1.0 else (1.0 / _odds if _odds > 1.0 else 0.5)
                    _stake = b.get("stake", 0) or 0
                    rm.record_outcome(
                        _stake, result == "won", _odds, _prob,
                        sport=b.get("sport", ""),
                        home_team=b.get("home", ""),
                        away_team=b.get("away", ""),
                        bet_type=b.get("sub_market", b.get("designation", "h2h")))
                except Exception:
                    pass

            save_tracked_bets(data)
            logger.info("✅ 结算: %s → %s (¥%.0f) [%s]", push_id[:60], result, profit, source)
            return True

    logger.warning("结算失败: push_id 未找到 — %s", push_id[:60])
    return False


def mark_unsettleable(push_id: str, reason: str = ""):
    """把一笔无法核实赛果的投注标记为 unsettleable(不参与 ROI 分母)。

    与 void 严格区分: void = 比赛取消/退款(本金回来); unsettleable = 我们拿不到
    赛果(联赛无源覆盖/超回查窗口/BB 无 id), 不是退款, 更不该算成"本金全回"去
    稀释 ROI。多源赛果回查窗口约 3 天, 默认 7 天仍未结算即处置。
    """
    data = load_tracked_bets()
    for b in data["bets"]:
        if b.get("push_id") == push_id:
            b["status"] = "unsettleable"
            b["result"] = "unsettleable"
            b["profit"] = 0
            b["settled_at"] = datetime.now(timezone.utc).isoformat()
            b["settle_source"] = "timeout"
            if reason:
                b["unsettleable_reason"] = reason
            save_tracked_bets(data)
            logger.info("⏱️ 超时作废: %s → unsettleable (%s)", push_id[:60], reason or "7天无赛果")
            return True
    return False


def auto_mark_unsettleable(days: float = 7.0, dry_run: bool = False) -> int:
    """兜底: pending 超过 days 天仍未结算的投注 → unsettleable。

    不碰 void(退款语义), 不碰 settled。返回处理的笔数。
    """
    data = load_tracked_bets()
    now = time.time()
    threshold = days * 24 * 3600
    hit = [b for b in data["bets"]
           if b.get("status") == "pending"
           and (b.get("match_epoch") or 0) > 0
           and now - b["match_epoch"] > threshold]
    if dry_run:
        return len(hit)
    n = 0
    for b in hit:
        if mark_unsettleable(b["push_id"], f"pending {days}天无赛果"):
            n += 1
    return n
