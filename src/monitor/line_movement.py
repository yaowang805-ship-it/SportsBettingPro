"""盘口变动监测系统 — Steam Move Detection + 市场状态分类。

本模块:
  - 定时抓取所有联赛赔率快照（每博彩公司独立记录）
  - 检测 Steam Move（急剧盘口变动）
  - 分类市场状态（sharp/soft/neutral）
  - 识别 CLV 机会（模型 vs 市场差异）
  - 通过钉钉推送警报
  - 记录历史盘口数据用于分析
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from fetchers.odds_api import fetch_basketball_odds, fetch_football_odds
from config.settings import DATA_DIR
from config.logging_config import get_logger
from src.core.team_names import cn_team

logger = get_logger(__name__)

# 存储路径
SNAPSHOT_DIR = DATA_DIR / "odds_snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
LAST_SNAPSHOT_FILE = SNAPSHOT_DIR / "last_snapshot.json"
MOVEMENT_LOG = SNAPSHOT_DIR / "movements.json"
MARKET_STATE_FILE = SNAPSHOT_DIR / "market_state.json"
STEAM_LOG = SNAPSHOT_DIR / "steam_moves.json"

# 检测阈值
MIN_ODDS_CHANGE_PCT = 0.03     # 赔率变动 3% 以上视为显著
MIN_SPREAD_CHANGE = 0.5        # 让分盘变动 0.5 分以上视为显著
MIN_TOTAL_CHANGE = 0.5         # 大小球总分变动 0.5 分以上视为显著
STEAM_ODDS_CHANGE_PCT = 0.05   # 5%+ 变动力 Steam Move
STEAM_SPREAD_CHANGE = 1.0      # 1分+ 变动力 Steam Move
STEAM_WINDOW_HOURS = 2         # 蒸汽变动检测时间窗口

# 书商锋利度评级（0~1, 越高越 sharp）
BOOK_SHARPNESS = {
    "pinnacle": 1.0,
    "bet365": 0.6,
    "william hill": 0.5,
    "ladbrokes": 0.3,
    "betfair": 0.9,
    "betmgm": 0.3,
    "fanduel": 0.2,
    "draftkings": 0.2,
    "unibet": 0.4,
    "888sport": 0.3,
    "sportsbet": 0.3,
    "neds": 0.3,
    "pointsbet": 0.3,
    "betway": 0.4,
    "betstars": 0.2,
    "bovada": 0.4,
    "mybookie": 0.1,
    "betonline": 0.4,
    "bwin": 0.5,
    "smarkets": 0.8,
    "matchbook": 0.7,
}

# 需要监测的联赛
SPORTS_TO_MONITOR = [
    ("basketball_nba", "NBA"),
    ("soccer_epl", "英超"),
    ("soccer_spain_la_liga", "西甲"),
    ("soccer_germany_bundesliga", "德甲"),
    ("soccer_italy_serie_a", "意甲"),
    ("soccer_france_ligue_one", "法甲"),
]


def _extract_markets(data: List[Dict]) -> Dict[str, Any]:
    """从 odds API 响应中提取关键盘口信息（含每博彩公司数据）。

    Returns:
        {match_key: {home_team, away_team, commence_time,
                      h2h_home, h2h_away, spread_point, spread_odds, total_point, over_odds,
                      n_bookmakers, sharp_consensus, per_book: {book_name: {h2h_home, ...}}}}
    """
    result = {}
    for item in data:
        home = item.get("home_team", "")
        away = item.get("away_team", "")
        commence = item.get("commence_time", "")
        match_key = f"{home} @ {away}"
        bookmakers = item.get("bookmakers", [])

        best_h2h = None
        best_spread_point = None
        best_spread_odds = None
        best_total_point = None
        best_over_odds = None

        per_book = {}
        sharp_h2h_odds = []  # sharp books 的主胜赔率，用于共识计算
        counted_sharp = set()  # 去重：同一 sharp bookmaker 只计一次

        for bm in bookmakers:
            bm_name = bm.get("title", "unknown").lower().replace(" ", "")
            bm_info = {}
            # 判断此 bookmaker 是否为 sharp（逐 bookmaker 判断，避免 market 级别重复）
            is_sharp = any(k in bm_name for k, v in BOOK_SHARPNESS.items() if v >= 0.7)

            for market in bm.get("markets", []):
                key = market.get("key", "")
                outcomes = market.get("outcomes", [])
                if key == "h2h":
                    for out in outcomes:
                        oname = out.get("name", "").strip().lower()
                        price = out.get("price")
                        if oname == home.strip().lower() and price:
                            bm_info["h2h_home"] = price
                            if best_h2h is None or price > best_h2h:
                                best_h2h = price
                        elif oname == away.strip().lower() and price:
                            bm_info["h2h_away"] = price

                    # sharp consensus：每一家 sharp bookmaker 只计一次 h2h 主胜赔率
                    if is_sharp and bm_name not in counted_sharp:
                        home_price = bm_info.get("h2h_home")
                        if home_price:
                            sharp_h2h_odds.append(home_price)
                            counted_sharp.add(bm_name)

                elif key == "spreads":
                    for out in outcomes:
                        if out.get("name", "").strip().lower() == home.strip().lower():
                            bm_info["spread_point"] = out.get("point")
                            bm_info["spread_odds"] = out.get("price")
                            pt = out.get("point")
                            od = out.get("price")
                            if pt is not None and od is not None:
                                if best_spread_odds is None or od + abs(pt) * 0.3 > (best_spread_odds or 0) + abs(best_spread_point or 0) * 0.3:
                                    best_spread_point = pt
                                    best_spread_odds = od

                elif key == "totals":
                    pt = market.get("point") or (outcomes[0].get("point") if outcomes else None)
                    for out in outcomes:
                        if out.get("name") == "Over":
                            od = out.get("price")
                            if pt is not None and od is not None:
                                bm_info["total_point"] = pt
                                bm_info["over_odds"] = od
                                if best_over_odds is None or od > best_over_odds:
                                    best_over_odds = od
                                    best_total_point = pt

            per_book[bm_name] = bm_info

        # 计算 sharp 共识赔率（sharp books 的平均值）
        sharp_consensus = float(np.mean(sharp_h2h_odds)) if len(sharp_h2h_odds) >= 2 else None

        # 市场效率评分：每家 sharp 书商只计 1 次（避免名称匹配多个关键词重复计数）
        n_sharp = sum(1 for bk in per_book
                      if any(k in bk for k, v in BOOK_SHARPNESS.items() if v >= 0.7))

        result[match_key] = {
            "home_team": home,
            "away_team": away,
            "commence_time": commence,
            "h2h_home": best_h2h,
            "n_bookmakers": len(bookmakers),
            "n_sharp": n_sharp,
            "sharp_consensus": sharp_consensus,
            "spread_point": best_spread_point,
            "spread_odds": best_spread_odds,
            "total_point": best_total_point,
            "over_odds": best_over_odds,
            "per_book": per_book,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    return result


def detect_steam_moves(current: Dict[str, Any], previous: Dict[str, Any],
                        threshold_pct: float = STEAM_ODDS_CHANGE_PCT) -> List[Dict]:
    """检测 Steam Move（急剧盘口变动 + sharp money 信号）。

    Steam Move 的特征:
      1. 赔率变动超过阈值（5%+）
      2. Sharp books 率先变动
      3. 变动方向一致（非震荡）
    """
    steam_moves = []
    for match_key, curr in current.items():
        prev = previous.get(match_key)
        if not prev:
            continue

        curr_h2h = curr.get("h2h_home")
        prev_h2h = prev.get("h2h_home")
        if not curr_h2h or not prev_h2h or prev_h2h <= 0:
            continue

        change_pct = abs(curr_h2h - prev_h2h) / prev_h2h
        if change_pct < threshold_pct:
            continue

        # 检查 sharp books 是否有变动
        sharp_moved = False
        curr_per_book = curr.get("per_book", {})
        prev_per_book = prev.get("per_book", {})
        for bk_name, curr_info in curr_per_book.items():
            sharpness = 0.0
            for k, v in BOOK_SHARPNESS.items():
                if k in bk_name:
                    sharpness = v
                    break
            if sharpness < 0.7:
                continue
            prev_info = prev_per_book.get(bk_name, {})
            if curr_info.get("h2h_home") and prev_info.get("h2h_home"):
                bk_change = abs(curr_info["h2h_home"] - prev_info["h2h_home"]) / prev_info["h2h_home"]
                if bk_change >= 0.02:
                    sharp_moved = True
                    break

        steam_moves.append({
            "type": "steam",
            "match": match_key,
            "home_team": curr["home_team"],
            "away_team": curr["away_team"],
            "previous": prev_h2h,
            "current": curr_h2h,
            "change_pct": round(change_pct, 4),
            "direction": "up" if curr_h2h > prev_h2h else "down",
            "sharp_confirmed": sharp_moved,
            "n_bookmakers": curr.get("n_bookmakers", 0),
            "sharp_consensus": curr.get("sharp_consensus"),
            "timestamp": curr["timestamp"],
        })
    return steam_moves


def _log_steam_moves(moves: List[Dict]):
    """记录 steam moves 到单独文件。"""
    existing = []
    if STEAM_LOG.exists():
        try:
            existing = json.loads(STEAM_LOG.read_text())
        except Exception:
            pass
    existing.extend(moves)
    if len(existing) > 500:
        existing = existing[-500:]
    STEAM_LOG.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


def _compute_market_efficiency_score(market_data: Dict[str, Any]) -> float:
    """计算市场效率评分（0~1, 越高越 sharp）。

    考虑因素:
      - 书商数量: 越多越好
      - 有 sharp books: 加分
      - 赔率共识程度: 越高越高效
    """
    scores = []
    for match_key, info in market_data.items():
        n_books = info.get("n_bookmakers", 0)
        n_sharp = info.get("n_sharp", 0)
        # 基础分: 每 5 个书商得 0.2
        book_score = min(1.0, n_books / 25)
        sharp_bonus = min(0.3, n_sharp * 0.1)
        scores.append(min(1.0, book_score + sharp_bonus))
    return float(np.mean(scores)) if scores else 0.0


def classify_market_state(all_markets: Dict[str, Any]) -> str:
    """分类当前市场状态。

    Returns:
        "sharp" — 高效市场，适合做 CLV
        "neutral" — 普通市场
        "soft" — 低效市场，存在更多套利机会
        "unknown" — 数据不足
    """
    if not all_markets:
        return "unknown"

    efficiency = _compute_market_efficiency_score(all_markets)
    avg_books = np.mean([m.get("n_bookmakers", 0) for m in all_markets.values()]) if all_markets else 0

    if efficiency >= 0.6 and avg_books >= 15:
        return "sharp"
    elif efficiency >= 0.35:
        return "neutral"
    elif avg_books < 5:
        return "unknown"
    else:
        return "soft"


def _find_clv_opportunities(all_markets: Dict[str, Any]) -> List[Dict]:
    """识别 CLV 机会：sharp consensus vs 市场赔率差异。"""
    opportunities = []
    for match_key, info in all_markets.items():
        h2h = info.get("h2h_home")
        sharp_consensus = info.get("sharp_consensus")
        if not h2h or h2h <= 0:
            continue
        if not sharp_consensus or sharp_consensus <= 0:
            continue

        market_prob = 1.0 / h2h
        consensus_prob = 1.0 / sharp_consensus
        prob_diff = abs(market_prob - consensus_prob)
        if prob_diff > 0.05:
            opportunities.append({
                "type": "clv_opportunity",
                "match": match_key,
                "home_team": info["home_team"],
                "away_team": info["away_team"],
                "market_prob": round(market_prob, 3),
                "consensus_prob": round(consensus_prob, 3),
                "diff": round(prob_diff, 3),
                "direction": "sharp_seeing_value" if consensus_prob > market_prob else "sharp_fading",
                "n_sharp": info.get("n_sharp", 0),
                "timestamp": info["timestamp"],
            })
    return opportunities


def _format_steam_alert(movement: Dict) -> str:
    """格式化 steam move 警报消息。"""
    mtype = movement["type"]
    home = movement["home_team"]
    away = movement["away_team"]
    # 中文名（盘口变动属 NBA 或足球）
    sport = "nba" if movement.get("sport_key", "").startswith("basketball") else "football"
    home_cn = cn_team(home, sport=sport)
    away_cn = cn_team(away, sport=sport)

    if mtype == "steam":
        direction_emoji = "🔥" if movement.get("sharp_confirmed") else "⚠️"
        return (
            f"{direction_emoji} **Steam Move** {home_cn} vs {away_cn}\n"
            f"  赔率: {movement['previous']:.2f} → {movement['current']:.2f} "
            f"({movement['change_pct']:+.1%})\n"
            f"  Sharp确认: {'✅' if movement.get('sharp_confirmed') else '❌'} | "
            f"书商: {movement.get('n_bookmakers', '?')}"
        )
    elif mtype == "clv_opportunity":
        return (
            f"💎 **CLV 机会** {home_cn} vs {away_cn}\n"
            f"  市场概率: {movement.get('market_prob', 0):.1%} | "
            f"Sharp共识: {movement.get('consensus_prob', 0):.1%}\n"
            f"  差距: {movement.get('diff', 0):.1%} "
            f"| 信号: {movement.get('direction', '?')}"
        )
    return ""


def _load_last_snapshot() -> Dict[str, Any]:
    if LAST_SNAPSHOT_FILE.exists():
        try:
            return json.loads(LAST_SNAPSHOT_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_snapshot(data: Dict[str, Any]):
    LAST_SNAPSHOT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def detect_movements(current: Dict[str, Any], previous: Dict[str, Any]) -> List[Dict]:
    """对比两次快照，检测盘口变动。"""
    movements = []

    for match_key, curr in current.items():
        prev = previous.get(match_key)
        if not prev:
            continue

        # 赔率变动
        curr_h2h = curr.get("h2h_home")
        prev_h2h = prev.get("h2h_home")
        if curr_h2h and prev_h2h and prev_h2h > 0:
            change = abs(curr_h2h - prev_h2h) / prev_h2h
            if change >= MIN_ODDS_CHANGE_PCT:
                movements.append({
                    "type": "h2h",
                    "match": match_key,
                    "home_team": curr["home_team"],
                    "away_team": curr["away_team"],
                    "previous": prev_h2h,
                    "current": curr_h2h,
                    "change_pct": round(change, 4),
                    "direction": "up" if curr_h2h > prev_h2h else "down",
                    "timestamp": curr["timestamp"],
                })

        # 让分盘变动
        curr_sp = curr.get("spread_point")
        prev_sp = prev.get("spread_point")
        if curr_sp is not None and prev_sp is not None:
            change = abs(curr_sp - prev_sp)
            if change >= MIN_SPREAD_CHANGE:
                movements.append({
                    "type": "spread",
                    "match": match_key,
                    "home_team": curr["home_team"],
                    "away_team": curr["away_team"],
                    "previous": prev_sp,
                    "current": curr_sp,
                    "change": round(change, 2),
                    "direction": "more_favored" if curr_sp > prev_sp else "less_favored",
                    "timestamp": curr["timestamp"],
                })

        # 大小球变动
        curr_tp = curr.get("total_point")
        prev_tp = prev.get("total_point")
        if curr_tp is not None and prev_tp is not None:
            change = abs(curr_tp - prev_tp)
            if change >= MIN_TOTAL_CHANGE:
                movements.append({
                    "type": "total",
                    "match": match_key,
                    "home_team": curr["home_team"],
                    "away_team": curr["away_team"],
                    "previous": prev_tp,
                    "current": curr_tp,
                    "change": round(change, 2),
                    "direction": "up" if curr_tp > prev_tp else "down",
                    "timestamp": curr["timestamp"],
                })

    return movements


def _log_movements(movements: List[Dict]):
    """记录盘口变动历史。"""
    existing = []
    if MOVEMENT_LOG.exists():
        try:
            existing = json.loads(MOVEMENT_LOG.read_text())
        except Exception:
            pass
    existing.extend(movements)
    # 只保留最近 1000 条
    if len(existing) > 1000:
        existing = existing[-1000:]
    MOVEMENT_LOG.write_text(json.dumps(existing, ensure_ascii=False, indent=2))




def take_snapshot(force: bool = False) -> int:
    """抓取当前所有赔率快照，检测 Steam Move + 市场状态。

    Args:
        force: 是否强制重新拉取（默认 False，复用缓存）

    Returns:
        监测中的比赛数量
    """
    previous = _load_last_snapshot()
    all_markets = {}

    total_matches = 0

    # NBA：走 odds-api.io
    try:
        nba_data = fetch_basketball_odds(force=force)
        markets = _extract_markets(nba_data)
        all_markets.update(markets)
        total_matches += len(markets)
    except Exception as e:
        logger.error("  ⚠️ NBA 快照失败: %s", e)

    # 足球：走 BSD API（一次获取所有联赛，再按 sport_key 拆分）
    try:
        fb_data = fetch_football_odds(force=force)
        fb_by_league = defaultdict(list)
        for item in fb_data:
            fb_by_league[item.get("sport_key", "soccer_epl")].append(item)

        for sport_key, league_name in SPORTS_TO_MONITOR:
            if sport_key == "basketball_nba":
                continue
            league_data = fb_by_league.get(sport_key, [])
            if not league_data:
                logger.info("  ⏭️ %s 当前无赛事", league_name)
                continue
            markets = _extract_markets(league_data)
            all_markets.update(markets)
            total_matches += len(markets)
    except Exception as e:
        logger.error("  ⚠️ 足球快照失败: %s", e)

    if not all_markets:
        logger.warning("  ⚠️ 未获取到任何盘口数据")
        return 0

    # 检测变动 + Steam Move + CLV 机会
    if previous:
        # 1. 常规盘口变动
        movements = detect_movements(all_markets, previous)
        if movements:
            logger.info("  🔥 检测到 %d 个盘口变动", len(movements))
            _log_movements(movements)
            for m in movements[:3]:
                logger.info("    %s", _format_steam_alert(m))

        # 2. Steam Move 检测
        steam_moves = detect_steam_moves(all_markets, previous)
        if steam_moves:
            logger.info("  🚨 检测到 %d 个 Steam Move!", len(steam_moves))
            _log_steam_moves(steam_moves)

        # 3. CLV 机会检测
        clv_opps = _find_clv_opportunities(all_markets)
        if clv_opps:
            logger.info("  💎 检测到 %d 个 CLV 机会", len(clv_opps))

        # 4. 汇总发送通知
        all_alerts = movements[:3] + steam_moves[:3] + clv_opps[:3]
        if all_alerts:
            try:
                from src.notify.dingtalk import get_notifier
                notifier = get_notifier()
                for m in all_alerts[:5]:
                    alert = _format_steam_alert(m)
                    if alert:
                        msg = notifier.build_markdown_message("【盘口监测】", alert)
                        notifier.send(msg, "盘口变动")
            except Exception as e:
                logger.error("  ⚠️ 钉钉推送失败: %s", e)
    else:
        logger.info("  📸 首次快照已保存（%d 场比赛）", total_matches)

    # 市场状态分类
    state = classify_market_state(all_markets)
    logger.info("  📊 市场状态: %s", state)
    # 保存市场状态
    MARKET_STATE_FILE.write_text(json.dumps({
        "state": state,
        "efficiency": round(_compute_market_efficiency_score(all_markets), 3),
        "total_matches": total_matches,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    # 保存快照
    _save_snapshot(all_markets)
    return total_matches


def get_recent_movements(hours: int = 24) -> List[Dict]:
    """获取最近 N 小时的盘口变动记录。"""
    if not MOVEMENT_LOG.exists():
        return []
    try:
        data = json.loads(MOVEMENT_LOG.read_text())
    except Exception:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    for m in data:
        try:
            ts = datetime.fromisoformat(m.get("timestamp", ""))
            if ts >= cutoff:
                recent.append(m)
        except Exception:
            continue
    return recent


def print_steam_report(hours: int = 24):
    """打印盘口变动报告。"""
    movements = get_recent_movements(hours)
    logger.info("\n" + "=" * 60)
    logger.info("  🔥 盘口变动报告（近 %d 小时）", hours)
    logger.info("=" * 60)

    if not movements:
        logger.info("  无显著盘口变动记录")
        return

    logger.info("  共 %d 次变动", len(movements))

    h2h = [m for m in movements if m["type"] == "h2h"]
    spread = [m for m in movements if m["type"] == "spread"]
    total = [m for m in movements if m["type"] == "total"]
    logger.info("  赔率变动: %d | 让分变动: %d | 大小球变动: %d", len(h2h), len(spread), len(total))

    for m in movements[-10:]:
        logger.info("  %s", _format_steam_alert(m))
    logger.info("=" * 60)


def main():
    """盘口监测入口，每小时运行一次。"""
    logger.info("\n🔍 盘口变动监测 - %s", datetime.now().strftime('%Y-%m-%d %H:%M'))
    matches = take_snapshot(force=True)
    logger.info("  📊 监测 %d 场比赛", matches)
    print_steam_report(24)


if __name__ == "__main__":
    main()
