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
    """构建结算报告。返回 (body_text, stats_dict)。数据源 = tracked_bets.json(真实投注)。

    2026-08-22 精简重设计: 用户反馈"太复杂太乱"。改为紧凑头部(3行核心指标)
    + 最近5笔结算, 去掉冗长的分区明细。
    """
    bets = _load_tracked_bets()
    settled = [b for b in bets if b.get("status") == "settled"]
    pending = [b for b in bets if b.get("status") == "pending"]
    unsettleable = [b for b in bets if b.get("status") == "unsettleable"]

    today = _bj_now().strftime("%m/%d")

    # 核心统计
    won = sum(1 for b in settled if _is_win(b.get("result", "")))
    lost = sum(1 for b in settled if _is_loss(b.get("result", "")))
    void = sum(1 for b in settled if b.get("result") in ("void", "push", "refund"))
    total_profit = sum(b.get("profit", 0) for b in settled)
    total_stake = sum(b.get("stake", 0) for b in settled)
    roi = round(total_profit / (total_stake or 1) * 100, 2)
    win_rate = round(won / ((won + lost) or 1) * 100, 1)

    # 今日新增结算(按 settled_at 增量)
    last_cutoff = _load_last_cutoff()
    if last_cutoff:
        new_settled = [b for b in settled if (b.get("settled_at") or "") > last_cutoff]
    else:
        day_ago = (_bj_now() - timedelta(hours=24)).isoformat()
        new_settled = [b for b in settled if (b.get("settled_at") or "") > day_ago]
    new_profit = sum(b.get("profit", 0) for b in new_settled)

    # 待结算敞口
    exposure = sum(b.get("stake", 0) for b in pending)

    lines = []
    lines.append(f"📊 投注结算 {today}")
    lines.append("")
    lines.append(f"💰 累计 {total_profit:+.0f}¥ · ROI {roi:+.2f}% · 胜率 {win_rate}%")
    lines.append(f"✅ {won}赢 · ❌ {lost}输 · ⓪ {void}无效  (共{len(settled)}笔)")
    lines.append("")
    lines.append(f"📈 今日 {new_profit:+.0f}¥  (新增结算 {len(new_settled)}笔)")
    lines.append(f"⏳ 待结算 {len(pending)}笔 · 敞口 ¥{exposure:.0f}")
    if unsettleable:
        lines.append(f"⚠️ 无法核实 {len(unsettleable)}笔(已剔除)")
    lines.append("")

    # 最近结算(按 settled_at 降序, 最多5笔, 赢输混合)
    sorted_settled = sorted(settled, key=lambda b: b.get("settled_at") or "", reverse=True)
    recent = [b for b in sorted_settled if _is_win(b.get("result", "")) or _is_loss(b.get("result", ""))][:5]
    if recent:
        lines.append("—— 最近结算 ——")
        for b in recent:
            lines.append(_format_bet_line(b))

    body = "\n".join(lines)

    stats = {
        "total_bets": len(settled), "won": won, "lost": lost, "void": void,
        "total_profit": total_profit, "roi": roi, "win_rate": win_rate,
        "new_settled": len(new_settled), "new_profit": new_profit,
        "pending": len(pending), "exposure": exposure,
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
