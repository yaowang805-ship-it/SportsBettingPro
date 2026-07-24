"""每日虚拟投注结算报告 — 钉钉推送（纯中文）

读取 virtual_portfolio.json，汇总 bb_vs_pinnacle 投注的结算结果，
包括当日新增结算、累计盈亏、待结算清单，推送到钉钉。

用法:
    python3 src/report/daily_settlement.py               # 生成报告并推送
    python3 src/report/daily_settlement.py --no-push      # 仅打印不推送
"""
import json, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR, send_dingtalk
from config.logging_config import get_logger

logger = get_logger(__name__)

PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"
LAST_REPORT_FILE = DATA_DIR / "daily_settlement_last.json"


def _load_portfolio() -> dict:
    """委托给虚拟投注引擎的规范加载器。"""
    from src.betting.bb_virtual_bet import _load_portfolio as _real_load
    return _real_load()


def _load_last_cutoff() -> str:
    """读取上次报告的截止时间戳。"""
    if LAST_REPORT_FILE.exists():
        try:
            return json.loads(LAST_REPORT_FILE.read_text()).get("cutoff", "")
        except Exception:
            pass
    return ""


def _save_last_cutoff(cutoff: str):
    LAST_REPORT_FILE.write_text(json.dumps({"cutoff": cutoff}))


def _bj_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


def _get_settled_at(entry):
    if isinstance(entry, dict):
        return entry.get("settled_at", entry.get("date", ""))
    return ""

def _get_result(entry):
    """统一提取 result，兼容旧数据用 status 字段。"""
    if isinstance(entry, dict):
        r = entry.get("result")
        if r:
            return r
        return entry.get("status", "")
    return str(entry) if entry else ""


def _format_bet_line(h: dict) -> str:
    """格式化单笔结算明细行。"""
    profit = h.get("profit", 0)
    stake = h.get("stake", 0)
    odds = h.get("odds", 0)
    icon = "✅" if _get_result(h) == "won" else "❌"
    profit_str = f"+¥{profit:.0f}" if profit > 0 else f"¥{profit:.0f}"

    # 新格式：有 home_cn 字段
    home = h.get("home_cn", "")
    away = h.get("away_cn", "")
    if home and away:
        market = h.get("market_type", "")
        league = h.get("league", "")
        label = f"[{league}] {home} vs {away}" if league else f"{home} vs {away}"
        if market:
            return f"{icon} {label} | {market} @ {odds:.2f} | ¥{stake:.0f} → {profit_str}"
        return f"{icon} {label} | @ {odds:.2f} | ¥{stake:.0f} → {profit_str}"

    # 旧格式：从 bet_id 解析
    bid = h.get("id", "")
    raw = bid.replace("bb_vs_pin_", "", 1)
    if "_" in raw:
        parts = raw.split("_")
        if len(parts) >= 4:
            market = parts[0]
            outcome = parts[-1]
            team_parts = parts[1:-1]
            if len(team_parts) >= 2:
                home = team_parts[-2]
                away = team_parts[-1]
                if outcome == "客胜":
                    return f"{icon} {home} vs {away} | {market} @ {odds:.2f} | ¥{stake:.0f} → {profit_str}"
                return f"{icon} {home} vs {away} | {outcome} @ {odds:.2f} | ¥{stake:.0f} → {profit_str}"

    # 最简回退
    return f"{icon} ¥{stake:.0f} @ {odds:.2f} → {profit_str}"


def build_report():
    """构建结算报告。返回 (body_text, stats_dict)。"""
    portfolio = _load_portfolio()
    history = portfolio.get("history", [])
    pending = portfolio.get("pending_bets", [])
    settled_dict = portfolio.get("settled", {})
    balance = portfolio.get("balance", 10000.0)
    initial = portfolio.get("initial_bankroll", 10000.0)
    daily_budget = portfolio.get("daily_budget", {})

    # 只统计 bb_vs_pinnacle 投注（兼容旧数据无 source 字段）
    bb_history = [h for h in history if h.get("source") == "bb_vs_pinnacle"]
    if not bb_history:
        bb_history = history  # 旧数据降级：所有记录都是 bb_vs_pinnacle

    bb_pending = [b for b in pending if b.get("source") == "bb_vs_pinnacle"]

    today = _bj_now().strftime("%m/%d")
    lines = []
    lines.append(f"📊 虚拟投注结算报告 {today}")
    lines.append("")

    # ── 今日新增结算 ──
    last_cutoff = _load_last_cutoff()
    if last_cutoff:
        new_history = [h for h in bb_history if
                       (h.get("settled_at") or h.get("date") or "") > last_cutoff]
    else:
        day_ago = (_bj_now() - timedelta(hours=24)).isoformat()
        new_history = [h for h in bb_history if
                       (h.get("settled_at") or h.get("date") or "") > day_ago]

    new_won = sum(1 for h in new_history if _get_result(h) == "won")
    new_lost = sum(1 for h in new_history if _get_result(h) == "lost")
    new_profit = sum(h.get("profit", 0) for h in new_history)

    lines.append(f"**【今日结算】** {len(new_history)} 笔")
    if new_history:
        lines.append(f"✅ 赢 {new_won} / ❌ 输 {new_lost} / 盈亏 {new_profit:+.0f}¥")
        lines.append("")
        for h in new_history[-8:]:
            icon = "✅" if _get_result(h) == "won" else "❌"
            market = h.get("market_type", "?")
            odds = h.get("odds", 0)
            profit = h.get("profit", 0)
            score = h.get("score", "")
            home = h.get("home_cn", "?")
            away = h.get("away_cn", "?")
            lines.append(f"{icon} {home} vs {away}")
            s = f"   {market} @ {odds} | 盈亏 {profit:+.0f}¥"
            if score:
                s += f" | 比分 {score}"
            lines.append(s)
        lines.append("")
    else:
        lines.append("暂无新增结算")
        lines.append("")

    # ── 昨日推送统计 ──
    yesterday = (_bj_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yester_hist = [h for h in bb_history if (h.get("settled_at") or h.get("date") or "").startswith(yesterday)]
    yester_pend = [b for b in bb_pending if (b.get("created_at") or "").startswith(yesterday)]
    yester_won = sum(1 for h in yester_hist if _get_result(h) == "won")
    yester_lost = sum(1 for h in yester_hist if _get_result(h) == "lost")
    yester_void = sum(1 for h in yester_hist if _get_result(h) in ("void", "push", "refund"))
    yester_profit = sum(h.get("profit", 0) for h in yester_hist)

    if yester_hist or yester_pend:
        lines.append(f"**【昨日推送统计】** {yesterday}")
        lines.append(f"已结算 {len(yester_hist)} 笔 / 待结算 {len(yester_pend)} 笔")
        if yester_hist:
            lines.append(f"✅ 赢 {yester_won} / ❌ 输 {yester_lost} / ⓪ 无效 {yester_void} / 盈亏 {yester_profit:+.0f}¥")
        lines.append("")

    # ── 截至现在的累计统计 ──
    total_bets = len(bb_history)
    won = sum(1 for h in bb_history if _get_result(h) == "won")
    lost = sum(1 for h in bb_history if _get_result(h) == "lost")
    void = sum(1 for h in bb_history if _get_result(h) in ("void", "push", "refund"))
    total_profit = sum(h.get("profit", 0) for h in bb_history)
    total_stake = sum(h.get("stake", 0) for h in bb_history)
    roi = round(total_profit / (total_stake or 1) * 100, 2)
    win_rate = round(won / ((won + lost) or 1) * 100, 1)

    lines.append(f"**【累计统计】** {total_bets} 笔")
    lines.append(f"✅ {won} / ❌ {lost} / ⓪ 无效 {void}")
    lines.append(f"胜率 {win_rate}% | ROI {roi:+.2f}%")
    lines.append(f"总盈亏 {total_profit:+.0f}¥ | 余额 {balance:.0f}¥")
    lines.append("")

    # ── 结算明细 ──
    won_bets = [h for h in bb_history if _get_result(h) == "won"]
    lost_bets = [h for h in bb_history if _get_result(h) == "lost"]
    lines.append(f"**【结算明细】** {total_bets} 笔")
    if won_bets:
        lines.append(f"✅ 赢 ({len(won_bets)})")
        for h in won_bets[-10:]:
            lines.append(f"  {_format_bet_line(h)}")
        lines.append("")
    if lost_bets:
        lines.append(f"❌ 输 ({len(lost_bets)})")
        for h in lost_bets[-10:]:
            lines.append(f"  {_format_bet_line(h)}")
        lines.append("")

    # ── 止损状态 ──
    # 按日汇总盈亏，计算连输天数
    daily_pnl = defaultdict(float)
    for h in bb_history:
        d = (h.get("settled_at") or h.get("date") or "")[:10]
        if d:
            daily_pnl[d] += h.get("profit", 0)
    today_str = _bj_now().strftime("%Y-%m-%d")
    sorted_dates = sorted([d for d in daily_pnl if d < today_str], reverse=True)
    consecutive_loss = 0
    for d in sorted_dates:
        if daily_pnl[d] < 0:
            consecutive_loss += 1
        else:
            break
    if consecutive_loss >= 5:
        stop_status = "🛑 连输{}天，已停投".format(consecutive_loss)
    elif consecutive_loss >= 3:
        stop_status = "⚠️ 连输{}天，预算减半".format(consecutive_loss)
    elif consecutive_loss > 0:
        stop_status = "📊 连输{}天（尚未触发止损）".format(consecutive_loss)
    else:
        stop_status = ""
    if stop_status:
        lines.append(f"**【止损状态】** {stop_status}")
        lines.append("")

    # ── 待结算 ──
    pending_count = len(bb_pending)
    if pending_count:
        exposure = sum(b.get("stake", 0) for b in bb_pending)
        lines.append(f"**【待结算】** {pending_count} 笔（敞口 {exposure:.0f}¥）")
        lines.append("")
        for b in bb_pending[:5]:
            home = b.get("home_cn", "?")
            away = b.get("away_cn", "?")
            market = b.get("market_type", "?")
            odds = b.get("odds", 0)
            stake = b.get("stake", 0)
            ev = b.get("ev_pct", 0)
            lines.append(f"⏳ {home} vs {away}")
            lines.append(f"   {market} @ {odds} | ¥{stake:.0f} | EV={ev:.1f}%")
        if pending_count > 5:
            lines.append(f"   ... 还有 {pending_count - 5} 笔")
        lines.append("")

    # ── 当日预算 ──
    budget_date = daily_budget.get("date", "")
    budget_used = daily_budget.get("used", 0)
    # 从 bb_virtual_bet 获取实际日预算，避免硬编码不一致
    try:
        from src.betting.bb_virtual_bet import DAILY_BANKROLL
    except ImportError:
        DAILY_BANKROLL = 50000.0
    if budget_date and budget_used:
        lines.append(f"**【当日投注】**")
        lines.append(f"日期 {budget_date} | 已用 {budget_used:.0f}¥ / {DAILY_BANKROLL:.0f}¥")
        lines.append("")

    # ── 待人工确认（三态结算 unresolved） ──
    try:
        from src.monitor.auto_settle import _load_unresolved
        unresolved = _load_unresolved()
    except Exception:
        unresolved = []
    if unresolved:
        lines.append(f"**【待人工确认】** {len(unresolved)} 笔无法自动结算")
        for u in unresolved[:5]:
            home = u.get("home", "?")
            away = u.get("away", "?")
            market = u.get("market", "?")
            reason = u.get("reason", "")
            source_q = u.get("source_quality", "?")
            lines.append(f"⏳ {home} vs {away} | {market} | 源:{source_q} | {reason}")
        if len(unresolved) > 5:
            lines.append(f"   ... 还有 {len(unresolved) - 5} 笔")
        lines.append("")

    body = "\n".join(lines)

    stats = {
        "total_bets": total_bets, "won": won, "lost": lost, "void": void,
        "total_profit": total_profit, "roi": roi, "win_rate": win_rate,
        "new_settled": len(new_history), "new_won": new_won, "new_lost": new_lost,
        "new_profit": new_profit, "pending": pending_count,
        "exposure": sum(b.get("stake", 0) for b in bb_pending),
        "balance": balance,
    }
    return body, stats


def push_report():
    """构建报告并推送到钉钉。"""
    body, stats = build_report()
    if not body:
        logger.info("空报告，跳过推送")
        return

    now_utc = datetime.now(timezone.utc).isoformat()
    _save_last_cutoff(now_utc)

    title = f"结算报告 {_bj_now().strftime('%m/%d')}"
    ok = send_dingtalk(title, body)
    if ok:
        logger.info("结算报告已推送: %s", title)
    else:
        logger.warning("结算报告推送失败")

    print(body)
    print(f"\n--- {title} ---")
    print(f"今日 {stats['new_settled']}笔 / {stats['new_profit']:+.0f}¥ | 累计 {stats['total_profit']:+.0f}¥")

    return stats


def main():
    if "--no-push" in sys.argv:
        body, stats = build_report()
        print(body)
        print(f"\n统计: {stats}")
    else:
        push_report()


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
