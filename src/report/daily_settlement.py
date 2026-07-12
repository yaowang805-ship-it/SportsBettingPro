"""每日虚拟投注结算报告 — 钉钉推送（纯中文）

读取 virtual_portfolio.json，汇总 bb_vs_pinnacle 投注的结算结果，
包括当日新增结算、累计盈亏、待结算清单，推送到钉钉。

用法:
    python3 src/report/daily_settlement.py               # 生成报告并推送
    python3 src/report/daily_settlement.py --no-push      # 仅打印不推送
"""
import json, sys
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
    if PORTFOLIO_FILE.exists():
        try:
            return json.loads(PORTFOLIO_FILE.read_text())
        except Exception:
            pass
    return {"pending_bets": [], "settled": {}, "history": [], "balance": 10000.0,
            "initial_bankroll": 10000.0, "daily_budget": {}}


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


def _get_result_val(entry):
    """统一提取 result 字段（兼容 string 或 dict 格式）。"""
    if isinstance(entry, dict):
        return entry.get("result", "")
    return str(entry) if isinstance(entry, str) else ""


def _get_settled_at(entry):
    if isinstance(entry, dict):
        return entry.get("settled_at", entry.get("date", ""))
    return ""


def build_report():
    """构建结算报告。返回 (body_text, stats_dict)。"""
    portfolio = _load_portfolio()
    history = portfolio.get("history", [])
    pending = portfolio.get("pending_bets", [])
    settled_dict = portfolio.get("settled", {})
    balance = portfolio.get("balance", 10000.0)
    initial = portfolio.get("initial_bankroll", 10000.0)
    daily_budget = portfolio.get("daily_budget", {})

    # 只统计 bb_vs_pinnacle 投注
    bb_history = [h for h in history if h.get("source") == "bb_vs_pinnacle"]
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

    new_won = sum(1 for h in new_history if h.get("result") == "won")
    new_lost = sum(1 for h in new_history if h.get("result") == "lost")
    new_profit = sum(h.get("profit", 0) for h in new_history)

    lines.append(f"**【今日结算】** {len(new_history)} 笔")
    if new_history:
        lines.append(f"✅ 赢 {new_won} / ❌ 输 {new_lost} / 盈亏 {new_profit:+.0f}¥")
        lines.append("")
        for h in new_history[-8:]:
            icon = "✅" if h.get("result") == "won" else "❌"
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

    # ── 截至现在的累计统计 ──
    total_bets = len(bb_history)
    won = sum(1 for h in bb_history if h.get("result") == "won")
    lost = sum(1 for h in bb_history if h.get("result") == "lost")
    void = sum(1 for h in bb_history if h.get("result") in ("void", "push", "refund"))
    total_profit = sum(h.get("profit", 0) for h in bb_history)
    total_stake = sum(h.get("stake", 0) for h in bb_history)
    roi = round(total_profit / (total_stake or 1) * 100, 2)
    win_rate = round(won / ((won + lost) or 1) * 100, 1)

    lines.append(f"**【累计统计】** {total_bets} 笔")
    lines.append(f"✅ {won} / ❌ {lost} / ⓪ 无效 {void}")
    lines.append(f"胜率 {win_rate}% | ROI {roi:+.2f}%")
    lines.append(f"总盈亏 {total_profit:+.0f}¥ | 余额 {balance:.0f}¥")
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
    if budget_date and budget_used:
        lines.append(f"**【当日投注】**")
        lines.append(f"日期 {budget_date} | 已用 {budget_used:.0f}¥ / 10000¥")
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

    # 保证全中文
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
