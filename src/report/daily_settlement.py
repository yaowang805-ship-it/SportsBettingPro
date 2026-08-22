"""每日真实投注结算报告 — 钉钉推送（纯中文）

读取 tracked_bets.json（真实投注库, 即推送的投注方案），汇总结算结果，
包括当日新增结算、累计盈亏、待结算清单，推送到钉钉。

2026-08-22 改: 原读 virtual_portfolio.json(虚拟投注组合), 与用户收到的推送投注方案
(tracked_bets)不一致。现改为直接读 tracked_bets.json, 报告与推送一一对应。

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

TRACKED_BETS_FILE = DATA_DIR / "tracked_bets.json"
LAST_REPORT_FILE = DATA_DIR / "daily_settlement_last.json"


def _load_tracked_bets() -> list:
    """加载真实投注库(tracked_bets.json)。返回 bet 列表。"""
    if not TRACKED_BETS_FILE.exists():
        return []
    data = json.loads(TRACKED_BETS_FILE.read_text())
    return data.get("bets", []) if isinstance(data, dict) else data


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
    """统一提取 result。tracked_bets 的 result 字段直接是 won/lost/void/half_won/half_lost。"""
    if isinstance(entry, dict):
        return entry.get("result", "")
    return str(entry) if entry else ""


def _is_win(r):
    return r in ("won", "half_won")


def _is_loss(r):
    return r in ("lost", "half_lost")


def _format_bet_line(b: dict) -> str:
    """格式化单笔结算明细行(tracked_bets 字段)。"""
    profit = b.get("profit", 0)
    stake = b.get("stake", 0)
    odds = b.get("bb_odds", 0)
    r = b.get("result", "")
    icon = "✅" if _is_win(r) else "❌"
    profit_str = f"+¥{profit:.0f}" if profit > 0 else f"¥{profit:.0f}"
    home = b.get("home", "")
    away = b.get("away", "")
    market = b.get("designation", "")
    league = b.get("league", "")
    label = f"[{league}] {home} vs {away}" if league else f"{home} vs {away}"
    if market:
        return f"{icon} {label} | {market} @ {odds:.2f} | ¥{stake:.0f} → {profit_str}"
    return f"{icon} {label} | @ {odds:.2f} | ¥{stake:.0f} → {profit_str}"


def build_report():
    """构建结算报告。返回 (body_text, stats_dict)。数据源 = tracked_bets.json(真实投注)。"""
    bets = _load_tracked_bets()
    settled = [b for b in bets if b.get("status") == "settled"]
    pending = [b for b in bets if b.get("status") == "pending"]
    unsettleable = [b for b in bets if b.get("status") == "unsettleable"]

    today = _bj_now().strftime("%m/%d")
    lines = []
    lines.append(f"📊 真实投注结算报告 {today}")
    lines.append("")

    # ── 今日新增结算 ──
    last_cutoff = _load_last_cutoff()
    if last_cutoff:
        new_settled = [b for b in settled if (b.get("settled_at") or "") > last_cutoff]
    else:
        day_ago = (_bj_now() - timedelta(hours=24)).isoformat()
        new_settled = [b for b in settled if (b.get("settled_at") or "") > day_ago]

    new_won = sum(1 for b in new_settled if _is_win(b.get("result", "")))
    new_lost = sum(1 for b in new_settled if _is_loss(b.get("result", "")))
    new_profit = sum(b.get("profit", 0) for b in new_settled)

    lines.append(f"**【今日结算】** {len(new_settled)} 笔")
    if new_settled:
        lines.append(f"✅ 赢 {new_won} / ❌ 输 {new_lost} / 盈亏 {new_profit:+.0f}¥")
        lines.append("")
        for b in new_settled[-8:]:
            icon = "✅" if _is_win(b.get("result", "")) else "❌"
            home = b.get("home", "?")
            away = b.get("away", "?")
            market = b.get("designation", "?")
            odds = b.get("bb_odds", 0)
            profit = b.get("profit", 0)
            hs, as_ = b.get("home_score"), b.get("away_score")
            lines.append(f"{icon} {home} vs {away}")
            s = f"   {market} @ {odds} | 盈亏 {profit:+.0f}¥"
            if hs is not None and as_ is not None:
                s += f" | 比分 {hs}-{as_}"
            lines.append(s)
        lines.append("")
    else:
        lines.append("暂无新增结算")
        lines.append("")

    # ── 昨日推送统计 ──
    yesterday = (_bj_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yester_settled = [b for b in settled if (b.get("settled_at") or "").startswith(yesterday)]
    yester_pending = [b for b in pending if (b.get("push_time") or "").startswith(yesterday)]
    yester_won = sum(1 for b in yester_settled if _is_win(b.get("result", "")))
    yester_lost = sum(1 for b in yester_settled if _is_loss(b.get("result", "")))
    yester_void = sum(1 for b in yester_settled if b.get("result") in ("void", "push", "refund"))
    yester_profit = sum(b.get("profit", 0) for b in yester_settled)

    if yester_settled or yester_pending:
        lines.append(f"**【昨日推送统计】** {yesterday}")
        lines.append(f"已结算 {len(yester_settled)} 笔 / 待结算 {len(yester_pending)} 笔")
        if yester_settled:
            lines.append(f"✅ 赢 {yester_won} / ❌ 输 {yester_lost} / ⓪ 无效 {yester_void} / 盈亏 {yester_profit:+.0f}¥")
        lines.append("")

    # ── 累计统计 ──
    total_bets = len(settled)
    won = sum(1 for b in settled if _is_win(b.get("result", "")))
    lost = sum(1 for b in settled if _is_loss(b.get("result", "")))
    void = sum(1 for b in settled if b.get("result") in ("void", "push", "refund"))
    total_profit = sum(b.get("profit", 0) for b in settled)
    total_stake = sum(b.get("stake", 0) for b in settled)
    roi = round(total_profit / (total_stake or 1) * 100, 2)
    win_rate = round(won / ((won + lost) or 1) * 100, 1)

    lines.append(f"**【累计统计】** {total_bets} 笔(已结算)")
    lines.append(f"✅ 赢 {won} / ❌ 输 {lost} / ⓪ 无效 {void}")
    lines.append(f"胜率 {win_rate}% | ROI {roi:+.2f}%")
    lines.append(f"总盈亏 {total_profit:+.0f}¥")
    if unsettleable:
        lines.append(f"⚠️ 无法核实 {len(unsettleable)} 笔(拿不到赛果, 已剔除)")
    lines.append("")

    # ── 结算明细 ──
    won_bets = [b for b in settled if _is_win(b.get("result", ""))]
    lost_bets = [b for b in settled if _is_loss(b.get("result", ""))]
    lines.append(f"**【结算明细】** {len(won_bets) + len(lost_bets)} 笔(赢输)")
    if won_bets:
        lines.append(f"✅ 赢 ({len(won_bets)})")
        for b in won_bets[-10:]:
            lines.append(f"  {_format_bet_line(b)}")
        lines.append("")
    if lost_bets:
        lines.append(f"❌ 输 ({len(lost_bets)})")
        for b in lost_bets[-10:]:
            lines.append(f"  {_format_bet_line(b)}")
        lines.append("")

    # ── 止损状态 ──
    daily_pnl = defaultdict(float)
    for b in settled:
        d = (b.get("settled_at") or "")[:10]
        if d:
            daily_pnl[d] += b.get("profit", 0)
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
    pending_count = len(pending)
    if pending_count:
        exposure = sum(b.get("stake", 0) for b in pending)
        lines.append(f"**【待结算】** {pending_count} 笔（敞口 {exposure:.0f}¥）")
        lines.append("")
        for b in pending[:5]:
            home = b.get("home", "?")
            away = b.get("away", "?")
            market = b.get("designation", "?")
            odds = b.get("bb_odds", 0)
            stake = b.get("stake", 0)
            ev = b.get("ev_pct", 0)
            lines.append(f"⏳ {home} vs {away}")
            lines.append(f"   {market} @ {odds} | ¥{stake:.0f} | EV={ev:.1f}%")
        if pending_count > 5:
            lines.append(f"   ... 还有 {pending_count - 5} 笔")
        lines.append("")

    # ── 当日投注 ──
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_bets = [b for b in bets if (b.get("push_time") or "").startswith(today_utc)]
    budget_used = sum(b.get("stake", 0) for b in today_bets)
    try:
        from config.constants import BANKROLL
    except ImportError:
        BANKROLL = 20000.0
    if today_bets:
        lines.append("**【当日投注】**")
        lines.append(f"日期 {today} | 已用 {budget_used:.0f}¥ / {BANKROLL:.0f}¥")
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
        "new_settled": len(new_settled), "new_won": new_won, "new_lost": new_lost,
        "new_profit": new_profit, "pending": pending_count,
        "exposure": sum(b.get("stake", 0) for b in pending),
        "unsettleable": len(unsettleable),
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
    # urgent=True: 结算报告是核心业务报告, 不应被非投注每日配额(6条/天)挤掉。
    # 否则早上被时间校准/健康报告用满配额后, 结算报告就发不出去(2026-08-22 实测)。
    ok = send_dingtalk(title, body, urgent=True)
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
