#!/usr/bin/env python3
"""赛前赔率刷新 + 盘口变动重检。

功能：
  1. 读取当天推荐记录
  2. 赛前 1-2 小时重新拉取赔率
  3. 对比推荐赔率 vs 当前最佳赔率
  4. 赔率恶化 > 5% → 钉钉警报
  5. 赔率改善 > 3% → 标记「可加注」
  6. 检测 steam move 信号

用法：
  python src/monitor/prematch_check.py
  python src/monitor/prematch_check.py --sport nba
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

from fetchers.odds_api import fetch_basketball_odds, fetch_football_odds
from src.monitor.line_movement import BOOK_SHARPNESS

# 赔率变动阈值
WORSEN_THRESHOLD = 0.05    # 恶化 > 5% → 警报
IMPROVE_THRESHOLD = 0.03   # 改善 > 3% → 可加注
STEAM_THRESHOLD = 0.05     # Steam move 阈值

SOURCES = {
    "nba": ROOT / "data" / "storage" / "daily_bb_recommendations.json",
    "football": ROOT / "data" / "storage" / "daily_fb_recommendations.json",
}


def _load_recs(sport: str) -> list:
    """加载推荐记录。"""
    path = SOURCES.get(sport)
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("recommendations", [])
    except Exception as e:
        logger.warning("  ⚠️ 读取 %s 推荐失败: %s", sport, e)
        return []


def _best_h2h_odds(odds_data: list, home_team: str, away_team: str) -> tuple:
    """从 odds API 响应中找到指定比赛的最佳 H2H 赔率。"""
    h_lower, a_lower = home_team.strip().lower(), away_team.strip().lower()
    for match in odds_data:
        mh = match.get("home_team", "").strip().lower()
        ma = match.get("away_team", "").strip().lower()
        if mh != h_lower or ma != a_lower:
            continue

        best_odds = None
        best_bm = ""
        sharp_before = set()
        for bm in match.get("bookmakers", []):
            bm_name = bm.get("title", "unknown").lower().replace(" ", "")
            is_sharp = any(k in bm_name for k, v in BOOK_SHARPNESS.items() if v >= 0.7)
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for out in mkt.get("outcomes", []):
                    if out.get("name", "").strip().lower() == h_lower:
                        price = out.get("price")
                        if price and (best_odds is None or price > best_odds):
                            best_odds = price
                            best_bm = bm.get("title", "unknown")
                        # 记录 sharp books 的赔率
                        if is_sharp:
                            sharp_before.add(price)

        # 也检测 sharp 变化
        curr_per_book = {}
        for bm in match.get("bookmakers", []):
            bm_name = bm.get("title", "unknown").lower().replace(" ", "")
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for out in mkt.get("outcomes", []):
                    if out.get("name", "").strip().lower() == h_lower:
                        curr_per_book[bm_name] = out.get("price")

        return best_odds, best_bm, list(sharp_before), curr_per_book
    return None, "", [], {}


def _detect_steam(rec_odds: float, current_odds: float) -> bool:
    """检测是否存在 steam move 信号。"""
    if not rec_odds or not current_odds:
        return False
    return abs(current_odds - rec_odds) / rec_odds >= STEAM_THRESHOLD


def check_sport(sport: str, fetch_fn: callable) -> list:
    """对一个运动执行赛前赔率重检。

    Returns:
        [{"match": ..., "rec_odds": ..., "current_odds": ..., "change_pct": ..., ...}, ...]
    """
    recs = _load_recs(sport)
    if not recs:
        logger.info("  📭 %s: 无推荐记录", sport)
        return []

    now = datetime.now(timezone.utc)
    results = []

    # 筛选赛前 30min ~ 3h 内的推荐
    pending = []
    for r in recs:
        ct = r.get("commence_time", "")
        if not ct:
            continue
        try:
            match_time = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            continue
        mins_to = (match_time - now).total_seconds() / 60
        if 30 <= mins_to <= 180:  # 赛前 30min ~ 3h
            pending.append((r, match_time, mins_to))

    if not pending:
        logger.info("  📭 %s: 无符合赛前窗口(30min~3h)的推荐", sport)
        return []

    # 拉取当前赔率
    try:
        odds_data = fetch_fn(force=True)
    except Exception as e:
        logger.warning("  ⚠️ %s 赔率拉取失败: %s", sport, e)
        return []

    if not odds_data:
        logger.warning("  ⚠️ %s 赔率为空", sport)
        return []

    for r, match_time, mins_to in pending:
        home = r.get("home_team", "")
        away = r.get("away_team", "")
        rec_odds = r.get("odds", 0)

        cur_odds, bookmaker, sharp_odds, per_book = _best_h2h_odds(odds_data, home, away)
        if cur_odds is None:
            logger.info("  ⏭️ %s vs %s: 赔率已从API移除", home, away)
            continue

        change_pct = (cur_odds - rec_odds) / rec_odds if rec_odds > 0 else 0

        is_steam = _detect_steam(rec_odds, cur_odds)
        needs_alert = change_pct <= -WORSEN_THRESHOLD  # 赔率下降 = 恶化
        can_add = change_pct >= IMPROVE_THRESHOLD       # 赔率上升 = 改善

        entry = {
            "sport": sport,
            "home_team": home,
            "away_team": away,
            "rec_odds": rec_odds,
            "current_odds": cur_odds,
            "change_pct": round(change_pct, 4),
            "bookmaker": bookmaker,
            "mins_to_match": round(mins_to, 0),
            "is_steam": is_steam,
            "needs_alert": needs_alert,
            "can_add": can_add,
            "match_time": match_time.isoformat(),
        }
        results.append(entry)

        if needs_alert:
            logger.warning("  🚨 %s vs %s: 赔率恶化 %.1f%% (%.2f→%.2f, %s)",
                           home, away, change_pct * 100, rec_odds, cur_odds, bookmaker)
        elif can_add:
            logger.info("  ✅ %s vs %s: 赔率改善 %+.1f%% (%.2f→%.2f, %s)",
                        home, away, change_pct * 100, rec_odds, cur_odds, bookmaker)
        elif is_steam:
            logger.info("  🔥 %s vs %s: Steam Move 信号 (%.2f→%.2f)",
                        home, away, rec_odds, cur_odds)
        else:
            logger.info("  ℹ️ %s vs %s: 变动 %.1f%% (%.2f→%.2f)",
                        home, away, change_pct * 100, rec_odds, cur_odds)

    return results


def send_alerts(results: list):
    """对需要警报的结果发送钉钉通知。"""
    alerts = [r for r in results if r.get("needs_alert")]
    improvements = [r for r in results if r.get("can_add")]

    if not alerts and not improvements:
        return

    try:
        from src.notify.dingtalk import get_notifier
        notifier = get_notifier()
    except Exception:
        logger.warning("  ⚠️ 钉钉通知不可用")
        return

    lines = []
    if alerts:
        lines.append("### 🚨 赔率恶化警报")
        for r in alerts[:5]:
            lines.append(
                f"- {r['home_team']} vs {r['away_team']}: "
                f"{r['rec_odds']:.2f} → {r['current_odds']:.2f} "
                f"({r['change_pct']:+.1%})"
            )

    if improvements:
        lines.append("### ✅ 赔率改善可关注")
        for r in improvements[:5]:
            lines.append(
                f"- {r['home_team']} vs {r['away_team']}: "
                f"{r['rec_odds']:.2f} → {r['current_odds']:.2f} "
                f"({r['change_pct']:+.1%})"
            )

    msg = notifier.build_markdown_message(
        "【赛前赔率重检】", "\n\n".join(lines)
    )
    notifier.send(msg, "赛前赔率变动通知")


def run_prematch_check(sport: str = None) -> dict:
    """执行完整赛前赔率重检。

    Args:
        sport: "nba"/"football"/None=全部

    Returns:
        {"nba": [...], "football": [...]}
    """
    logger.info("\n" + "=" * 55)
    logger.info("  🔍 赛前赔率重检 - %s", datetime.now().strftime('%Y-%m-%d %H:%M'))
    logger.info("=" * 55)

    all_results = {}
    sports_to_check = [("nba", fetch_basketball_odds), ("football", fetch_football_odds)]

    for s, fn in sports_to_check:
        if sport and s != sport:
            continue
        logger.info("\n  📊 %s:", s.upper())
        results = check_sport(s, fn)
        all_results[s] = results
        if results:
            n_alerts = sum(1 for r in results if r.get("needs_alert"))
            n_improve = sum(1 for r in results if r.get("can_add"))
            logger.info("  ✅ %s 重检完成: %d 场, %d 警报, %d 改善",
                       s, len(results), n_alerts, n_improve)

    # 发送通知
    for s, results in all_results.items():
        send_alerts(results)

    return all_results


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["nba", "football", "all"], default="all")
    args = parser.parse_args()
    run_prematch_check(sport=args.sport if args.sport != "all" else None)


if __name__ == "__main__":
    main()
