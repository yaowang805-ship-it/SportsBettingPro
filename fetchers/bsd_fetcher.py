"""BSD (Bzzoiro Sports Data) 免费足球赔率抓取器。

免费、无限量、14家博彩公司（含 Pinnacle、Bet365）。
覆盖 53 个足球联赛。

无需注册付费，已配置的 BSD_API_KEY 在 .env 中。
"""
import json
import sys
import time
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import BSD_API_KEY, DATA_DIR, SPORTS_API_TIMEOUT

BASE_URL = "https://sports.bzzoiro.com"
CACHE_DIR = DATA_DIR / "odds"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# BSD 联赛名 → 系统 sport_key 映射
BSD_LEAGUE_MAP = {
    "Premier League": "soccer_epl",
    "Championship": "soccer_epl",
    "La Liga": "soccer_spain_la_liga",
    "Liga F": "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "2. Bundesliga": "soccer_germany_bundesliga",
    "Serie A": "soccer_italy_serie_a",
    "Serie B": "soccer_italy_serie_a",
    "Ligue 1": "soccer_france_ligue_one",
    "Ligue 2": "soccer_france_ligue_one",
    "UEFA Champions League": "soccer_epl",
    "UEFA Europa League": "soccer_epl",
    "Eredivisie": "soccer_epl",
    "Liga Portugal": "soccer_epl",
    "Scottish Premiership": "soccer_epl",
    "MLS": "soccer_epl",
    "Super Lig": "soccer_epl",
    "Belgian Pro League": "soccer_epl",
    "Swiss Super League": "soccer_epl",
    "Saudi Pro League": "soccer_epl",
    "World Cup 2026": "soccer_epl",
    "Brazilian Serie A": "soccer_brazil_campeonato",
}

# 反向映射：sport_key → [BSD league names]
SPORT_KEY_TO_BSD = {}
for bsd_name, sk in BSD_LEAGUE_MAP.items():
    SPORT_KEY_TO_BSD.setdefault(sk, []).append(bsd_name)


def _headers() -> dict:
    return {"Authorization": f"Token {BSD_API_KEY}"}


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"bsd_{name}.json"


def _load_cache(name: str, max_age_minutes: int = 30):
    path = _cache_path(name)
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    if datetime.now() - mtime > timedelta(minutes=max_age_minutes):
        return None
    with open(path) as f:
        return json.load(f)


def _save_cache(name: str, data):
    path = _cache_path(name)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def fetch_upcoming_events(hours_ahead: int = 48) -> list:
    """从 BSD 获取即将开始的足球赛事列表。

    使用 predictions 端点（返回 135+ 比赛预测，含事件 ID）。
    """
    url = f"{BASE_URL}/api/v2/predictions/"
    try:
        resp = requests.get(url, headers=_headers(), timeout=SPORTS_API_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("BSD predictions 请求失败: %d", resp.status_code)
            return []
        data = resp.json()
        results = data.get("results", data.get("predictions", []))
    except Exception as e:
        logger.warning("BSD predictions 解析失败: %s", e)
        return []

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    events = []
    for p in results:
        event = p.get("event", {})
        date_str = event.get("event_date", "")
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt < now or dt > cutoff:
            continue

        league_name = event.get("league_name", "")
        sport_key = BSD_LEAGUE_MAP.get(league_name, "soccer_epl")

        events.append({
            "id": event["id"],
            "sport_key": sport_key,
            "commence_time": date_str,
            "home_team": event.get("home_team", ""),
            "away_team": event.get("away_team", ""),
            "league_name": league_name,
        })
    return events


def fetch_event_odds(event_id: int) -> Optional[dict]:
    """从 BSD 获取单场比赛的赔率比较数据。"""
    url = f"{BASE_URL}/api/v2/events/{event_id}/odds/comparison/"
    try:
        resp = requests.get(url, headers=_headers(), timeout=SPORTS_API_TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        logger.debug("BSD odds comparison 失败 event=%s: %s", event_id, e)
        return None


def _bsd_market_outcomes(market_data: dict, side_key: str, label: str):
    """从 BSD 市场数据提取某个 side（HOME/AWAY/DRAW/over/under）的赔率信息。"""
    side = market_data.get(side_key, {})
    return {
        "best_odds": side.get("best_odds"),
        "bookmakers": side.get("bookmakers", {}),
    }


def convert_bsd_odds_to_odds_api(bsd_data: dict, event_info: dict) -> Optional[dict]:
    """将 BSD odds/comparison 格式转换为 the-odds-api.com 兼容格式，包含 h2h 和 totals 市场。"""
    markets = bsd_data.get("markets", {})
    if not markets:
        return None

    home_team = event_info["home_team"]
    away_team = event_info["away_team"]

    # --- 收集所有博彩公司 ---
    all_bookmaker_slugs = set()
    if "1x2" in markets:
        for side in ["HOME", "AWAY", "DRAW"]:
            all_bookmaker_slugs.update(
                markets["1x2"].get(side, {}).get("bookmakers", {}).keys()
            )

    # --- 构建 best_odds 级别的市场 ---
    def _best_outcomes(market_key: str) -> list:
        """从 BSD 最佳赔率构建 outcomes 列表。"""
        m = markets.get(market_key)
        if not m:
            return []
        if market_key == "1x2":
            ho = m.get("HOME", {}).get("best_odds")
            ao = m.get("AWAY", {}).get("best_odds")
            do = m.get("DRAW", {}).get("best_odds")
            if ho and ao and do:
                return [
                    {"name": home_team, "price": float(ho)},
                    {"name": away_team, "price": float(ao)},
                    {"name": "Draw", "price": float(do)},
                ]
        elif market_key == "over_under_25":
            ov = m.get("over", {}).get("best_odds")
            un = m.get("under", {}).get("best_odds")
            if ov and un:
                return [
                    {"name": "Over", "price": float(ov), "point": 2.5},
                    {"name": "Under", "price": float(un), "point": 2.5},
                ]
        return []

    h2h_outcomes = _best_outcomes("1x2")
    totals_outcomes = _best_outcomes("over_under_25")

    # --- 无博彩公司单记录模式 ---
    if not all_bookmaker_slugs and h2h_outcomes:
        bm_markets = [{"key": "h2h", "outcomes": h2h_outcomes}]
        if totals_outcomes:
            bm_markets.append({"key": "totals", "outcomes": totals_outcomes})
        return {
            "id": str(bsd_data["event_id"]),
            "sport_key": event_info["sport_key"],
            "sport_title": "Football",
            "commence_time": event_info["commence_time"],
            "home_team": home_team,
            "away_team": away_team,
            "bookmakers": [{
                "key": "bsd_consensus",
                "title": "BSD Consensus",
                "markets": bm_markets,
            }],
        }

    # --- 为每家博彩公司构建市场 ---
    def _slug_odds(market_key: str, slug: str, sides: list) -> list:
        """读取某博彩公司在特定市场的 decimal_odds。"""
        m = markets.get(market_key)
        if not m:
            return []
        results = []
        for side_key, name in sides:
            odds = m.get(side_key, {}).get("bookmakers", {}).get(slug, {}).get("decimal_odds")
            if odds:
                results.append({"name": name, "price": float(odds)})
        return results

    bookmakers_list = []
    for slug in sorted(all_bookmaker_slugs)[:5]:
        h2h = _slug_odds("1x2", slug, [
            ("HOME", home_team), ("AWAY", away_team), ("DRAW", "Draw"),
        ])
        if len(h2h) < 3:
            continue

        bm_markets = [{"key": "h2h", "outcomes": h2h}]

        # 尝试提取该博彩公司的大小分赔率
        totals = _slug_odds("over_under_25", slug, [
            ("over", "Over"), ("under", "Under"),
        ])
        if len(totals) == 2:
            totals[0]["point"] = 2.5
            totals[1]["point"] = 2.5
            bm_markets.append({"key": "totals", "outcomes": totals})

        bm_name = slug.replace("-", " ").title()
        bookmakers_list.append({
            "key": slug,
            "title": bm_name,
            "markets": bm_markets,
        })

    if not bookmakers_list:
        return None

    return {
        "id": str(bsd_data["event_id"]),
        "sport_key": event_info["sport_key"],
        "sport_title": "Football",
        "commence_time": event_info["commence_time"],
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": bookmakers_list,
    }


def fetch_football_odds(force: bool = False, hours_ahead: int = 48) -> list:
    """主入口：获取足球赔率（BSD），转换为标准格式。"""
    cache_name = "football_all"
    if not force:
        cached = _load_cache(cache_name, max_age_minutes=15)
        if cached:
            return cached

    # 1. 获取即将开始的赛事
    events = fetch_upcoming_events(hours_ahead=hours_ahead)
    if not events:
        logger.info("  ⏭️ BSD: 无近期足球赛事")
        return []

    logger.info("  📋 BSD: %d 场即将开始的赛事", len(events))

    # 2. 并发获取每场比赛的赔率
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_event = {
            executor.submit(fetch_event_odds, ev["id"]): ev
            for ev in events
        }
        for future in concurrent.futures.as_completed(future_to_event):
            ev = future_to_event[future]
            try:
                odds_data = future.result()
                if odds_data:
                    converted = convert_bsd_odds_to_odds_api(odds_data, ev)
                    if converted:
                        results.append(converted)
            except Exception as e:
                logger.debug("BSD 处理失败 %s: %s", ev["id"], e)

    logger.info("  ✅ BSD: %d/%d 场有赔率数据", len(results), len(events))

    if results:
        _save_cache(cache_name, results)
    return results
