"""联赛维度周报/月报 — 钉钉推送

从 virtual_portfolio.json 读取 BB vs Pinnacle 投注历史，
按联赛分析 ROI、胜率、盈亏，生成报告推送到钉钉。

用法:
    python3 src/report/periodic_report.py --weekly       # 近7天
    python3 src/report/periodic_report.py --monthly      # 近30天
    python3 src/report/periodic_report.py --days 14      # 自定义天数
    python3 src/report/periodic_report.py --no-push      # 仅打印不推送
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

SPORT_CN = {
    "soccer": "⚽ 足球", "basketball": "🏀 篮球", "baseball": "⚾ 棒球",
    "tennis": "🎾 网球", "american_football": "🏈 美式足球",
}


def _bj_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


def _load_history() -> list:
    if not PORTFOLIO_FILE.exists():
        return []
    try:
        pf = json.loads(PORTFOLIO_FILE.read_text())
        return [h for h in pf.get("history", []) if h.get("source") == "bb_vs_pinnacle"]
    except Exception:
        return []


def _build_league_stats(history: list) -> dict:
    """按联赛统计：投注数、胜/负/无效、盈亏、ROI、胜率、平均EV。"""
    # league_key = "sport | league_name"
    by_league = defaultdict(lambda: {
        "sport": "", "league": "", "bets": 0, "won": 0, "lost": 0,
        "void": 0, "profit": 0.0, "stake": 0.0, "ev_sum": 0.0,
    })

    for h in history:
        sport = h.get("sport", "")
        league = h.get("league", "未知")
        key = f"{sport}|{league}"
        rec = by_league[key]
        rec["sport"] = sport
        rec["league"] = league
        rec["bets"] += 1
        rec["stake"] += h.get("stake", 0)
        rec["ev_sum"] += h.get("ev_pct", 0)

        result = h.get("result", "")
        profit = h.get("profit", 0)
        rec["profit"] += profit
        if result == "won":
            rec["won"] += 1
        elif result == "lost":
            rec["lost"] += 1
        elif result in ("void", "push", "refund"):
            rec["void"] += 1
        else:
            rec["lost"] += 1  # unclassified → loss

    # 计算衍生指标
    stats = []
    for key, rec in by_league.items():
        total = rec["won"] + rec["lost"]
        rec["win_rate"] = round(rec["won"] / (total or 1) * 100, 1)
        rec["roi"] = round(rec["profit"] / (rec["stake"] or 1) * 100, 2)
        rec["avg_ev"] = round(rec["ev_sum"] / (rec["bets"] or 1), 1)
        rec["label"] = f"{SPORT_CN.get(rec['sport'], rec['sport'])} | {rec['league']}"
        stats.append(rec)

    stats.sort(key=lambda r: (-r["roi"], -r["bets"]))
    return stats


def _build_overall_stats(history: list) -> dict:
    """全局统计。"""
    won = sum(1 for h in history if h.get("result") == "won")
    lost = sum(1 for h in history if h.get("result") == "lost")
    void = sum(1 for h in history if h.get("result") in ("void", "push", "refund"))
    total_stake = sum(h.get("stake", 0) for h in history)
    total_profit = sum(h.get("profit", 0) for h in history)
    return {
        "bets": len(history),
        "won": won, "lost": lost, "void": void,
        "stake": total_stake, "profit": total_profit,
        "roi": round(total_profit / (total_stake or 1) * 100, 2),
        "win_rate": round(won / ((won + lost) or 1) * 100, 1),
    }


def build_report(days: int) -> str:
    """构建指定天数的分析报告。"""
    cutoff = (_bj_now() - timedelta(days=days)).isoformat()
    history = _load_history()
    recent = [h for h in history if (h.get("settled_at") or h.get("date") or "") > cutoff]

    if not recent:
        return f"近 {days} 天无已结算投注"

    overall = _build_overall_stats(recent)
    by_league = _build_league_stats(recent)

    period = "周报" if days <= 7 else "月报"
    now_str = _bj_now().strftime("%m/%d")

    lines = []
    lines.append(f"📊 {period} {now_str}")
    lines.append("")

    # 全局概况
    lines.append(f"**【全局概况】** {overall['bets']} 笔")
    lines.append(f"✅ {overall['won']} / ❌ {overall['lost']} / ⓪ 无效 {overall['void']}")
    lines.append(f"胜率 {overall['win_rate']}% | ROI {overall['roi']:+.2f}%")
    lines.append(f"总投入 ¥{overall['stake']:.0f} | 总盈亏 {overall['profit']:+.0f}¥")
    lines.append("")

    # 按联赛
    lines.append(f"**【联赛排行】** （按 ROI 降序）")
    lines.append("")
    for r in by_league:
        icon = "🟢" if r["roi"] > 5 else ("🔴" if r["roi"] < -5 else "⚪")
        lines.append(
            f"{icon} {r['label']}"
        )
        lines.append(
            f"   {r['bets']}笔 | 胜率{r['win_rate']}% | ROI{r['roi']:+.2f}% | "
            f"盈亏{r['profit']:+.0f}¥ | 平均EV={r['avg_ev']}%"
        )
    lines.append("")

    # 最佳/最差
    if len(by_league) >= 2:
        best = by_league[0]
        worst = by_league[-1]
        lines.append(f"🏆 最佳: {best['label']}  ROI {best['roi']:+.2f}%  盈亏 {best['profit']:+.0f}¥")
        if worst["roi"] < 0:
            lines.append(f"😢 最差: {worst['label']}  ROI {worst['roi']:+.2f}%  盈亏 {worst['profit']:+.0f}¥")

    body = "\n".join(lines)
    return body


def push_report(days: int):
    body = build_report(days)
    if body.startswith("近"):
        logger.info("跳过推送：%s", body)
        print(body)
        return

    period = "周报" if days <= 7 else "月报"
    title = f"{period} {_bj_now().strftime('%m/%d')}"
    ok = send_dingtalk(title, body)
    if ok:
        logger.info("%s已推送: %s", period, title)
    else:
        logger.warning("%s推送失败", period)

    print(body)
    print(f"\n--- {title} ---")


def main():
    if "--no-push" in sys.argv:
        no_push = True
    else:
        no_push = False

    if "--weekly" in sys.argv:
        days = 7
    elif "--monthly" in sys.argv:
        days = 30
    else:
        for a in sys.argv:
            if a.startswith("--days="):
                days = int(a.split("=")[1])
                break
        else:
            print("用法: python3 src/report/periodic_report.py --weekly|--monthly|--days=N [--no-push]")
            return

    if no_push:
        print(build_report(days))
    else:
        push_report(days)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
