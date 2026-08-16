"""BB体育 vs Pinnacle +EV 虚拟投注引擎

从 bb_vs_pinnacle_comparison.json 读取 +EV 机会，
自动执行虚拟投注（投注到 virtual_portfolio.json）。

用法:
    python3 src/betting/bb_virtual_bet.py                 # 执行虚拟投注
    python3 src/betting/bb_virtual_bet.py --dry           # 预览不下单
    python3 src/betting/bb_virtual_bet.py --from-push     # 从推送暂存文件投注
"""
import json, sys, time, math, fcntl
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

from config.constants import (
    BANKROLL as DAILY_BANKROLL,
    INITIAL_BALANCE,
    EV_CAP as MAX_EV_PCT,
    MAX_BETS,
    KELLY_FRACTION as KELLY_FRAC,
    MAX_STAKE_PCT,
    PER_MATCH_CAP_PCT,
    MIN_EV_PCT,
    get_league_tier as _get_league_tier,
    league_multiplier as _league_multiplier,
)

PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"

# 推送暂存文件 — bb_ev_push.py 导出已筛选的机会列表
PUSH_STAGING_FILE = DATA_DIR / "push_staging.json"

# 跨进程锁 — 防止并发读写 virtual_portfolio.json
_LOCK_FD = None


@contextmanager
def _portfolio_lock():
    """独占锁：防止并发 pipeline 进程覆盖投注数据。"""
    global _LOCK_FD
    lock_path = PORTFOLIO_FILE.with_suffix(PORTFOLIO_FILE.suffix + ".lck")
    fd = open(lock_path, "w")
    _LOCK_FD = fd
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
        _LOCK_FD = None


# 止损参数
CONSECUTIVE_LOSS_STOP = 5         # 连输5天 → 停投
STOP_LOSS_MULTIPLIERS = {
    0: 1.0, 1: 1.0, 2: 1.0,       # 0-2天正常
    3: 0.5, 4: 0.5,                # 3-4天减半
    5: 0.0,                        # 5天停投
}

API_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
import requests
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})


def _today_str() -> str:
    return date.today().isoformat()


def _load_portfolio() -> dict:
    """加载 current portfolio state."""
    if PORTFOLIO_FILE.exists():
        try:
            pf = json.loads(PORTFOLIO_FILE.read_text())
            # 确保 daily_budget 字段存在
            pf.setdefault("daily_budget", {"date": "", "used": 0.0, "bets": 0})
            return pf
        except Exception:
            pass
    return {
        "balance": INITIAL_BALANCE,
        "initial_bankroll": INITIAL_BALANCE,
        "pending_bets": [],
        "settled": {},
        "history": [],
        "daily_budget": {"date": "", "used": 0.0, "bets": 0},
    }


def _save_portfolio(state: dict):
    """写入前备份，防止文件损坏导致数据丢失。"""
    if PORTFOLIO_FILE.exists():
        bak = PORTFOLIO_FILE.with_suffix(PORTFOLIO_FILE.suffix + ".bak")
        import shutil
        shutil.copy2(str(PORTFOLIO_FILE), str(bak))
    PORTFOLIO_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _check_daily_budget(portfolio: dict, daily_bankroll: float) -> tuple:
    """检查并重置每日预算。返回 (剩余预算, 是否新的一天)."""
    db = portfolio.setdefault("daily_budget", {"date": "", "used": 0.0, "bets": 0})
    today = _today_str()
    if db["date"] != today:
        # 新的一天，重置
        db["date"] = today
        db["used"] = 0.0
        db["bets"] = 0
        return daily_bankroll, True
    remaining = max(0.0, daily_bankroll - db["used"])
    return remaining, False


def _make_bet_id(home, away, outcome, market="1x2"):
    """生成唯一投注 ID。"""
    key = f"{market}_{home}_{away}_{outcome}"
    return f"bb_vs_pin_{key}".replace(" ", "_").replace(".", "")[:60]


def _calc_kelly_stake(bb_odds: float, fair_price: float, balance: float, league: str = "") -> float:
    """Kelly stake sizing — 基于公平价作为真实概率，应用联赛 Tier 加权。"""
    if fair_price <= 1 or bb_odds <= 1:
        return 0.0
    true_prob = 1.0 / fair_price
    b = bb_odds - 1.0
    # Full Kelly: f* = (bp - q) / b
    kelly = (b * true_prob - (1 - true_prob)) / b
    if kelly <= 0:
        return 0.0
    tier_mult = _league_multiplier(league)
    stake = balance * KELLY_FRAC * tier_mult * kelly
    max_stake = balance * MAX_STAKE_PCT
    return min(stake, max_stake)


def _calc_stop_loss_multiplier(portfolio: dict) -> float:
    """计算止损系数：连输 N 天 → 降仓/停投。

    从已结算历史按日汇总盈亏，从昨天往前数连续亏损天数。
    """
    history = [h for h in portfolio.get("history", [])
               if h.get("source") == "bb_vs_pinnacle" and h.get("result") in ("won", "lost")]

    # 按日汇总盈亏
    daily_pnl = defaultdict(float)
    for h in history:
        d = (h.get("settled_at") or h.get("date") or "")[:10]
        if d:
            daily_pnl[d] += h.get("profit", 0)

    today = date.today().isoformat()
    sorted_dates = sorted([d for d in daily_pnl if d < today], reverse=True)

    consecutive_loss = 0
    for d in sorted_dates:
        if daily_pnl[d] < 0:
            consecutive_loss += 1
        else:
            break

    mult = STOP_LOSS_MULTIPLIERS.get(min(consecutive_loss, CONSECUTIVE_LOSS_STOP), 1.0)
    return mult, consecutive_loss


def _format_stop_loss_msg(mult: float, loss_days: int) -> str:
    """格式化止损状态消息。"""
    if mult == 0.0:
        return f"🛑 止损触发：连输 {loss_days} 天，今日停投"
    if mult < 1.0:
        return f"⚠️ 连输 {loss_days} 天，预算减半 ¥{DAILY_BANKROLL * mult:.0f}/日"
    if loss_days > 0:
        return f"📊 连输 {loss_days} 天后回血，恢复正常预算"
    return ""


def _market_from_designation(desig: str) -> str:
    """从 designation 推断市场类型。"""
    d = desig.lower()
    if d in ("主胜", "客胜", "和局", "平"):
        return "1x2"
    if "让" in d or d.startswith("+") or d.startswith("-"):
        return "handicap"
    if d in ("大分", "小分", "大球", "小球", "大", "小") or "大" in d or "小" in d:
        return "over_under"
    return "1x2"


def place_bets(dry_run=False):
    """Main betting logic — 从比较文件读取 +EV 机会。"""
    comp_path = DATA_DIR / "bb_vs_pinnacle_comparison.json"
    if not comp_path.exists():
        logger.error("找不到比较结果文件，先运行 bb_vs_pinnacle.py")
        return

    comp = json.loads(comp_path.read_text())
    opportunities = comp.get("details", [])
    if not opportunities:
        logger.info("没有 +EV 机会")
        return

    with _portfolio_lock():
        portfolio = _load_portfolio()
        stop_mult, loss_days = _calc_stop_loss_multiplier(portfolio)
        daily_bankroll = DAILY_BANKROLL * stop_mult
        daily_remaining, is_new_day = _check_daily_budget(portfolio, daily_bankroll)
        # 日预算不超实际余额
        daily_remaining = min(daily_remaining, max(0, portfolio.get("balance", 0)))
        pending_ids = {b.get("id", "") for b in portfolio.get("pending_bets", [])}
        settled_ids = set(portfolio.get("settled", {}).keys())
        history_ids = {h.get("id", "") for h in portfolio.get("history", [])}
        existing_ids = pending_ids | settled_ids | history_ids

        bets_placed = 0
        total_stake = 0

        # 止损提示
        stop_msg = _format_stop_loss_msg(stop_mult, loss_days)
        if stop_msg:
            print(f"\n{stop_msg}\n")

        if stop_mult == 0.0:
            print("=" * 60)
            print("🛑 止损停投日，跳过")
            print("=" * 60)
            return 0

        # 全局余额检查
        if portfolio.get("balance", 0) <= 0:
            print(f"\n{'='*60}")
            print(f"🛑 余额为 ¥{portfolio['balance']:.0f}，跳过投注")
            print(f"{'='*60}")
            return 0

        print("=" * 60)
        print("BB体育 vs Pinnacle 虚拟投注")
        print(f"每日预算: ¥{daily_bankroll:.2f} (基准¥{DAILY_BANKROLL:.2f}) | 今日剩余: ¥{daily_remaining:.2f} | Kelly系数: {KELLY_FRAC}")
        print("=" * 60)

        for opp in opportunities:
            league = opp["league"]
            home = opp["home_bb"]
            away = opp["away_bb"]
            flags = opp.get("flags", [])
            sport = opp.get("sport", "football")

            if flags:
                print(f"\n⏭️ [{league}] {home} vs {away} — 跳过（异常标记: {flags}）")
                continue

            if bets_placed >= MAX_BETS:
                break

            match_key = (home, away)
            match_total_stake = 0.0
            PER_MATCH_CAP = daily_bankroll * PER_MATCH_CAP_PCT  # 单场总投注 ≤ 6%

            for mk in ("opportunities", "handicap", "over_under"):
                if bets_placed >= MAX_BETS:
                    break
                for o in opp.get(mk, []):
                    if bets_placed >= MAX_BETS:
                        break

                    ev = o["ev_pct"]
                    bb_odds = o["bb_odds"]
                    fair_price = o.get("fair_price", o["pin_odds"])
                    pin_odds = o["pin_odds"]
                    outcome = o["designation"]
                    hc_line = o.get("line", "")

                    if ev < MIN_EV_PCT:
                        continue
                    if ev > MAX_EV_PCT:
                        print(f"\n⏭️ [{league}] {home} vs {away} {outcome} — EV={ev}% 过高跳过")
                        continue

                    # Determine market type from the list key
                    if mk == "opportunities":
                        market_type = "1x2"
                    elif mk == "handicap":
                        market_type = "handicap"
                    else:
                        market_type = "over_under"

                    bet_id = _make_bet_id(home, away, outcome, market_type)
                    if bet_id in existing_ids:
                        print(f"  ⏭️ 已存在: {bet_id}")
                        continue

                    # Kelly stake using fair_price + tier multiplier
                    stake = _calc_kelly_stake(bb_odds, fair_price, daily_remaining, league=league)

                    if stake < 1.0:
                        print(f"  ⏭️ {outcome} @ {bb_odds} — 投注额={stake:.2f}")
                        continue

                    stake = max(1.0, min(stake, daily_remaining * MAX_STAKE_PCT))
                    if stake > daily_remaining:
                        stake = daily_remaining

                    # 余额检查：不超可用资金（扣减已投注额）
                    running_balance = portfolio.get("balance", 0) - total_stake
                    if running_balance < stake:
                        print(f"  ⏭️ {outcome} — 余额不足 (¥{running_balance:.0f} < ¥{stake:.0f})")
                        continue

                    # 单场总投注上限
                    if match_total_stake + stake > PER_MATCH_CAP:
                        allowed = PER_MATCH_CAP - match_total_stake
                        if allowed < 5:
                            print(f"  ⏭️ {outcome} — 单场上限已达 (¥{match_total_stake:.0f})")
                            continue
                        stake = allowed
                    match_total_stake += stake

                    bet = {
                        "id": bet_id,
                        "sport": sport,
                        "league": league,
                        "home_team": opp.get("home_pin", home),
                        "away_team": opp.get("away_pin", away),
                        "home_cn": home,
                        "away_cn": away,
                        "market": market_type,
                        "market_type": outcome,
                        "sub_market": opp.get("_sub_market", opp.get("_market", "")),
                        "line": hc_line,
                        "odds": bb_odds,
                        "stake": round(stake, 2),
                        "model_prob": round(1.0 / fair_price, 4),
                        "ev_pct": ev,
                        "pin_odds": pin_odds,
                        "fair_price": fair_price,
                        "source": "bb_vs_pinnacle",
                        "commence_time": opp.get("start_time_pin_epoch", ""),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }

                    if dry_run:
                        print(f"\n  📋 [{league}] {home} vs {away}")
                        print(f"    投注: {outcome} @ {bb_odds} | 公平价={fair_price}")
                        print(f"    金额: ¥{stake:.2f} (EV={ev:.1f}%, Kelly={KELLY_FRAC})")
                        bets_placed += 1
                        total_stake += stake
                    else:
                        portfolio["pending_bets"].append(bet)
                        daily_remaining -= stake
                        portfolio["daily_budget"]["used"] += stake
                        portfolio["daily_budget"]["bets"] += 1
                        existing_ids.add(bet_id)
                        bets_placed += 1
                        total_stake += stake
                        print(f"\n  ✅ [{league}] {home} vs {away}")
                        print(f"    投注: {outcome} @ {bb_odds} | ¥{stake:.2f} | EV={ev:.1f}%")

        if not dry_run and bets_placed > 0:
            daily_used = portfolio["daily_budget"]["used"]
            portfolio["balance"] = round(portfolio["balance"] - total_stake, 2)
            _save_portfolio(portfolio)
            print(f"\n{'='*60}")
            print(f"已投注 {bets_placed} 笔，总金额 ¥{total_stake:.2f}")
            print(f"今日已用: ¥{daily_used:.2f} / ¥{DAILY_BANKROLL:.2f}")
            print(f"组合余额: ¥{portfolio['balance']:.2f}")
            print(f"保存到 {PORTFOLIO_FILE}")
        elif dry_run:
            print(f"\n{'='*60}")
            print(f"预览: {bets_placed} 笔可投注，总金额 ¥{total_stake:.2f}")

        return bets_placed


def place_bets_from_push(opportunities, bankroll=DAILY_BANKROLL):
    """从推送的已筛选机会列表执行投注（stake 已预计算）。

    bankroll 默认用 DAILY_BANKROLL(¥20000), 之前硬编码 50000 导致日预算/单注上限 2.5 倍偏高。
    """
    if not opportunities:
        logger.info("空机会列表，跳过投注")
        return 0

    with _portfolio_lock():
        portfolio = _load_portfolio()
        stop_mult, loss_days = _calc_stop_loss_multiplier(portfolio)
        daily_bankroll = bankroll * stop_mult
        daily_remaining, is_new_day = _check_daily_budget(portfolio, daily_bankroll)
        # 日预算不超实际余额
        daily_remaining = min(daily_remaining, max(0, portfolio.get("balance", 0)))
        pending_ids = {b.get("id", "") for b in portfolio.get("pending_bets", [])}
        settled_ids = set(portfolio.get("settled", {}).keys())
        history_ids = {h.get("id", "") for h in portfolio.get("history", [])}
        existing_ids = pending_ids | settled_ids | history_ids

        bets_placed = 0
        bets_today = portfolio["daily_budget"].get("bets", 0)  # 跨调用累计
        total_stake = 0

        stop_msg = _format_stop_loss_msg(stop_mult, loss_days)
        if stop_msg:
            print(f"\n{stop_msg}\n")

        if stop_mult == 0.0:
            print(f"\n{'='*60}")
            print("🛑 止损停投日，跳过")
            print(f"{'='*60}")
            return 0

        # 全局余额检查
        balance = portfolio.get("balance", 0)
        if balance <= 0:
            print(f"\n{'='*60}")
            print(f"🛑 余额为 ¥{balance:.0f}，跳过投注")
            print(f"{'='*60}")
            return 0

        print(f"\n{'='*60}")
        print(f"推送投注 — 今日剩余 ¥{daily_remaining:.2f} / ¥{daily_bankroll:.2f} (基准¥{bankroll:.2f})")
        print(f"{'='*60}")

        for o in opportunities:
            if bets_today + bets_placed >= MAX_BETS:
                print(f"  ⏭️ 今日已达 {MAX_BETS} 笔上限")
                break

            stake = o.get("_stake", 0)
            raw_stake = o.get("_raw_stake", stake)
            if stake <= 0 and raw_stake <= 0:
                continue
            # 预算耗尽时仍记录投注(用于结算统计), 不扣余额
            track_only = (stake <= 0)
            if track_only:
                stake = raw_stake
            else:
                if daily_remaining <= 0:
                    track_only = True
                    stake = raw_stake

            if stake < 1:
                continue

            if not track_only:
                # 单注上限 + 预算检查 (仅实际投注)
                stake = min(stake, bankroll * MAX_STAKE_PCT)
                stake = min(stake, daily_remaining)
                if stake < 1:
                    continue
                running_balance = portfolio.get("balance", 0) - total_stake
                if running_balance < stake:
                    track_only = True  # 余额不足→仅记录
                    stake = raw_stake

            bb_odds = o["bb_odds"]
            outcome = o["designation"]
            home = o.get("home_cn", "")
            away = o.get("away_cn", "")
            league = o.get("league_cn") or o.get("league", "")

            # 从 upstream 传递的 _market_type 推断市场类型，兼容旧数据无此字段
            raw_mk = o.get("_market_type", "")
            if raw_mk in ("opportunities", "double_chance", "draw_no_bet"):
                market_type = "1x2"
            elif raw_mk in ("handicap", "over_under"):
                market_type = raw_mk
            else:
                # 旧数据降级：从 designation 和 line 推断
                desig = o.get("designation", "")
                hc_line = o.get("line", "")
                if "让" in desig or hc_line:
                    market_type = "handicap"
                elif desig.startswith(("大分", "小分", "大球", "小球", "大", "小")):
                    market_type = "over_under"
                else:
                    market_type = "1x2"

            bet_id = _make_bet_id(home, away, outcome, market_type)
            if bet_id in existing_ids:
                print(f"  ⏭️ 已存在: {bet_id}")
                continue

            fair_price = o.get("fair_price", o.get("pin_odds", 0))
            pin_odds = o.get("pin_odds", 0)
            ev = o.get("ev_pct", 0)

            bet = {
                "id": bet_id,
                "sport": o.get("sport", ""),
                "league": league,
                "home_team": o.get("home_pin", home),
                "away_team": o.get("away_pin", away),
                "home_cn": home,
                "away_cn": away,
                "market": market_type,
                "market_type": outcome,
                "sub_market": o.get("_sub_market", o.get("_market", "")),
                "line": o.get("line", ""),
                "odds": bb_odds,
                "stake": round(stake, 2),
                "model_prob": round(1.0 / fair_price, 4) if fair_price > 1 else 0,
                "ev_pct": ev,
                "pin_odds": pin_odds,
                "fair_price": fair_price,
                "source": "bb_vs_pinnacle",
                "commence_time": o.get("_pin_epoch", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "track_only": track_only,  # 仅记录不扣款
            }

            portfolio["pending_bets"].append(bet)
            if not track_only:
                daily_remaining -= stake
                portfolio["daily_budget"]["used"] = portfolio["daily_budget"].get("used", 0) + stake
                total_stake += stake
            portfolio["daily_budget"]["bets"] = portfolio["daily_budget"].get("bets", 0) + 1
            existing_ids.add(bet_id)
            bets_placed += 1
            tag = " 📝仅记录" if track_only else ""
            print(f"  ✅ [{league}] {home} vs {away}{tag}")
            print(f"    投注: {outcome} @ {bb_odds} | ¥{stake:.2f} | EV={ev:.1f}%")

        if bets_placed > 0:
            daily_used = portfolio["daily_budget"].get("used", 0)
            if total_stake > 0:
                portfolio["balance"] = round(portfolio["balance"] - total_stake, 2)
            _save_portfolio(portfolio)
            print(f"\n已记录 {bets_placed} 笔" + (f"，投注 ¥{total_stake:.2f}" if total_stake > 0 else " (仅跟踪,不扣款)"))
            print(f"今日已用: ¥{daily_used:.2f} / ¥{daily_bankroll:.2f}")
            print(f"组合余额: ¥{portfolio['balance']:.2f}")
        else:
            print("  无新增投注")

        return bets_placed


def main():
    dry_run = "--dry" in sys.argv
    from_push = "--from-push" in sys.argv

    if from_push:
        if not PUSH_STAGING_FILE.exists():
            print("推送暂存文件不存在，请先运行 bb_ev_push.py --stage-bets")
            return
        opps = json.loads(PUSH_STAGING_FILE.read_text())
        place_bets_from_push(opps)
    else:
        place_bets(dry_run=dry_run)


if __name__ == "__main__":
    main()
