"""多体育通用 +EV 扫描器 — Pinnacle vs 零售对比。

支持体育: 篮球/棒球/网球
市场: h2h（独赢）/ spreads（让分盘）/ totals（大小分）
       alternate_spreads（备选让分）/ alternate_totals（备选大小分）

备选盘口通过 event-level endpoint 获取，每场比赛额外消耗 4 积分。
使用: scan_sport() 返回 +EV 机会列表，各体育的薄包装器调用它。
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set

from config.logging_config import get_logger
from config.settings import DATA_DIR, ODDS_API_KEY

logger = get_logger(__name__)

SCAN_MARKETS = "h2h,spreads,totals"
MAX_EDGE_PCT = 30.0  # 超过此值的 edge 视为数据错误
KELLY_FRACTION = 0.10  # 1/10 Kelly 保守策略

# ===== the-odds-api 每日限额 =====
DAILY_LIMIT = 600
_COUNTER_FILE = DATA_DIR / "locks" / "odds_api_counter.json"


def _api_call(label: str) -> bool:
    """API 调用前检查+计数。返回 True=可继续，False=已达上限。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        data = json.loads(_COUNTER_FILE.read_text())
        if data.get("date") != today:
            data = {"date": today, "count": 0}
    except Exception:
        data = {"date": today, "count": 0}

    if data["count"] >= DAILY_LIMIT:
        logger.warning("API 配额已耗尽 (%d/%d)，跳过 %s", data["count"], DAILY_LIMIT, label)
        return False

    _COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"date": today, "count": data["count"] + 1}
    _COUNTER_FILE.write_text(json.dumps(data, ensure_ascii=False))
    logger.info("API 配额: %d/%d (%s)", data["count"], DAILY_LIMIT, label)
    return True


def _fetch_odds(sport_key: str, regions: str) -> list:
    """从 the-odds-api 抓取赔率。"""
    if not ODDS_API_KEY:
        logger.error("ODDS_API_KEY 未配置")
        return []
    if not _api_call(f"odds {sport_key} {regions}"):
        return []
    import requests
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    try:
        resp = requests.get(url, params={
            "apiKey": ODDS_API_KEY, "regions": regions,
            "markets": SCAN_MARKETS, "oddsFormat": "decimal",
        }, timeout=15)
        if resp.status_code == 200:
            remaining = resp.headers.get("x-requests-remaining", "?")
            logger.info("  API 剩余: %s 次 (region=%s)", remaining, regions)
            data = resp.json()
            return data if isinstance(data, list) else []
        logger.warning("  API 错误 %s: %s", resp.status_code, resp.text[:100])
        return []
    except Exception as e:
        logger.warning("  API 请求异常: %s", e)
        return []


def _remove_vig(price1: float, price2: float) -> tuple:
    """去除 Pinnacle 抽水，返回 (公平价_1, 公平价_2)。"""
    if price1 <= 0 or price2 <= 0:
        return price1, price2
    vig = (1.0 / price1) + (1.0 / price2)
    if vig <= 0:
        return price1, price2
    fair1 = round(1.0 / ((1.0 / price1) / vig), 4)
    fair2 = round(1.0 / ((1.0 / price2) / vig), 4)
    return fair1, fair2


def _get_pinnacle_prices(game: dict, market_key: str) -> dict:
    """从 Pinnacle 提取指定市场的赔率。"""
    for bm in game.get("bookmakers", []):
        if "pinnacle" in bm.get("title", "").lower():
            for mkt in bm.get("markets", []):
                if mkt.get("key") == market_key:
                    return {o.get("name", ""): {"price": o.get("price", 0), "point": o.get("point")}
                            for o in mkt.get("outcomes", [])}
    return {}


def _get_retail_best(game: dict, market_key: str) -> dict:
    """从零售博彩公司提取指定市场的最佳赔率。"""
    best = {}
    for bm in game.get("bookmakers", []):
        if "pinnacle" in bm.get("title", "").lower():
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") == market_key:
                for o in mkt.get("outcomes", []):
                    key = o.get("name", "")
                    pt = o.get("point")
                    if pt is not None:
                        key = f"{key}|{pt}"
                    price = o.get("price", 0)
                    if key not in best or price > best[key]["price"]:
                        best[key] = {"name": o.get("name", ""), "price": price, "point": pt}
    return best


def _eval_h2h(home_team: str, away_team: str, commence_time: str,
              eu_game: dict, us_game: dict, sport: str, league: str) -> List[dict]:
    """评估独赢市场。"""
    pinny = _get_pinnacle_prices(eu_game, "h2h")
    if not pinny:
        return []
    ph = pinny.get(home_team, {}).get("price")
    pa = pinny.get(away_team, {}).get("price")
    if not ph or not pa:
        return []
    fh, fa = _remove_vig(ph, pa)
    retail = _get_retail_best(us_game, "h2h")
    opps = []
    for team, fair, outcome_cn in [(home_team, fh, "主胜"), (away_team, fa, "客胜")]:
        r = retail.get(team)
        if not r:
            continue
        rp = r["price"]
        if fair <= 0 or rp <= 1:
            continue
        ev = round((rp - fair) / fair * 100, 2)
        if ev < 3 or ev > MAX_EDGE_PCT:
            continue
        kelly = (ev / 100) / (rp - 1) * KELLY_FRACTION
        opps.append({
            "sport": sport, "league": league, "market": "h2h", "market_label": "独赢",
            "home_team": home_team, "away_team": away_team,
            "outcome": outcome_cn, "outcome_label": outcome_cn,
            "point": None, "point_str": "", "commence_time": commence_time,
            "fair_price": fair, "retail_odds": rp, "edge_pct": ev,
            "odds": rp, "kelly_pct": round(kelly * 100, 2),
        })
    return opps


def _eval_spreads(home_team: str, away_team: str, commence_time: str,
                  eu_game: dict, us_game: dict, sport: str, league: str) -> List[dict]:
    """评估让分盘市场。"""
    pinny = _get_pinnacle_prices(eu_game, "spreads")
    if not pinny:
        return []
    ph = pinny.get(home_team)
    pa = pinny.get(away_team)
    if not ph or not pa:
        return []
    fh, fa = _remove_vig(ph["price"], pa["price"])
    retail = _get_retail_best(us_game, "spreads")
    opps = []
    for team, fair, pe, side in [(home_team, fh, ph, "主"), (away_team, fa, pa, "客")]:
        pt = pe.get("point")
        r = retail.get(f"{team}|{pt}") or retail.get(team)
        if not r:
            continue
        rp = r["price"]
        if fair <= 0 or rp <= 1:
            continue
        ev = round((rp - fair) / fair * 100, 2)
        if ev < 3 or ev > MAX_EDGE_PCT:
            continue
        pt_str = f"{pt:+g}" if pt is not None else ""
        kelly = (ev / 100) / (rp - 1) * KELLY_FRACTION
        opps.append({
            "sport": sport, "league": league, "market": "spreads", "market_label": "让分盘",
            "home_team": home_team, "away_team": away_team,
            "outcome": f"{side}胜_{team}", "outcome_label": f"{side}胜",
            "point": pt, "point_str": pt_str, "commence_time": commence_time,
            "fair_price": fair, "retail_odds": rp, "edge_pct": ev,
            "odds": rp, "kelly_pct": round(kelly * 100, 2),
        })
    return opps


def _eval_totals(home_team: str, away_team: str, commence_time: str,
                 eu_game: dict, us_game: dict, sport: str, league: str) -> List[dict]:
    """评估大小分盘市场。"""
    pinny = _get_pinnacle_prices(eu_game, "totals")
    if not pinny:
        return []
    over = pinny.get("Over")
    under = pinny.get("Under")
    if not over or not under:
        return []
    fo, fu = _remove_vig(over["price"], under["price"])
    pt = over.get("point")
    retail = _get_retail_best(us_game, "totals")
    opps = []
    for suffix, fair, pe in [("大", fo, over), ("小", fu, under)]:
        on = "Over" if suffix == "大" else "Under"
        r = retail.get(on) or retail.get(f"{on}|{pt}")
        if not r:
            continue
        rp = r["price"]
        if fair <= 0 or rp <= 1:
            continue
        ev = round((rp - fair) / fair * 100, 2)
        if ev < 3 or ev > MAX_EDGE_PCT:
            continue
        kelly = (ev / 100) / (rp - 1) * KELLY_FRACTION
        opps.append({
            "sport": sport, "league": league, "market": "totals", "market_label": "大小分",
            "home_team": home_team, "away_team": away_team,
            "outcome": f"{suffix}{pt}", "outcome_label": f"{suffix}{pt}",
            "point": pt, "point_str": "", "commence_time": commence_time,
            "fair_price": fair, "retail_odds": rp, "edge_pct": ev,
            "odds": rp, "kelly_pct": round(kelly * 100, 2),
        })
    return opps


def _get_alternate_pinnacle(game: dict, market_key: str) -> dict:
    """提取 Pinnacle 备选市场的所有赔率，按 point 分组。

    Returns: {point: {team/Over: price, team/Under: price}}
    """
    for bm in game.get("bookmakers", []):
        if "pinnacle" in bm.get("title", "").lower():
            for mkt in bm.get("markets", []):
                if mkt.get("key") == market_key:
                    result = {}
                    for o in mkt.get("outcomes", []):
                        name = o.get("name", "")
                        pt = o.get("point")
                        price = o.get("price", 0)
                        if pt is not None:
                            result.setdefault(pt, {})[name] = {"price": price, "point": pt}
                    return result
    return {}


def _get_alternate_retail_best(game: dict, market_key: str) -> dict:
    """提取零售博彩公司备选市场的最佳赔率。

    Returns: {point: {team/Over: best_price, team/Under: best_price}}
    """
    result = {}
    for bm in game.get("bookmakers", []):
        if "pinnacle" in bm.get("title", "").lower():
            continue
        for mkt in bm.get("markets", []):
            if mkt.get("key") == market_key:
                for o in mkt.get("outcomes", []):
                    name = o.get("name", "")
                    pt = o.get("point")
                    price = o.get("price", 0)
                    if pt is not None and price > 0:
                        entry = result.setdefault(pt, {})
                        if name not in entry or price > entry[name]:
                            entry[name] = price
    return result


def _eval_alternate_spreads(home_team: str, away_team: str, commence_time: str,
                            eu_game: dict, us_game: dict, sport: str, league: str) -> List[dict]:
    """评估备选让分盘市场（alternate_spreads）。

    Pinnacle 备选让分数据中，每支队伍在不同点位上各自独立：
      强势方（-1.5） vs 弱势方（+1.5）
    因此按 pt↔-pt 对称配对来去抽水，而非在同一点上找两队。
    """
    pinny = _get_alternate_pinnacle(eu_game, "alternate_spreads")
    if not pinny:
        return []
    retail = _get_alternate_retail_best(us_game, "alternate_spreads")
    if not retail:
        return []

    # 按队伍分类所有点位
    home_at = {}  # {pt: outcome}
    away_at = {}  # {pt: outcome}
    for pt, outcomes in pinny.items():
        if home_team in outcomes:
            home_at[pt] = outcomes[home_team]
        if away_team in outcomes:
            away_at[pt] = outcomes[away_team]

    opps = []
    for h_pt, h_oc in home_at.items():
        a_pt = -h_pt
        a_oc = away_at.get(a_pt)
        if not a_oc:
            continue
        if abs(h_pt) < 0.1:
            continue

        fh, fa = _remove_vig(h_oc["price"], a_oc["price"])
        r_home = retail.get(h_pt, {}).get(home_team)
        r_away = retail.get(a_pt, {}).get(away_team)

        for team, team_pt, fair, r_price, side in [
            (home_team, h_pt, fh, r_home, "主"),
            (away_team, a_pt, fa, r_away, "客"),
        ]:
            if not r_price or r_price <= 1 or fair <= 0:
                continue
            ev = round((r_price - fair) / fair * 100, 2)
            if ev < 3 or ev > MAX_EDGE_PCT:
                continue
            pt_str = f"{team_pt:+g}"
            kelly = (ev / 100) / (r_price - 1) * KELLY_FRACTION
            opps.append({
                "sport": sport, "league": league, "market": "alternate_spreads", "market_label": "让分盘",
                "home_team": home_team, "away_team": away_team,
                "outcome": f"{side}胜_{team}", "outcome_label": f"{side}胜",
                "point": team_pt, "point_str": pt_str, "commence_time": commence_time,
                "fair_price": fair, "retail_odds": r_price, "edge_pct": ev,
                "odds": r_price, "kelly_pct": round(kelly * 100, 2),
            })
    return opps


def _eval_alternate_totals(home_team: str, away_team: str, commence_time: str,
                           eu_game: dict, us_game: dict, sport: str, league: str) -> List[dict]:
    """评估备选大小分盘市场（alternate_totals）。"""
    pinny = _get_alternate_pinnacle(eu_game, "alternate_totals")
    if not pinny:
        return []
    retail = _get_alternate_retail_best(us_game, "alternate_totals")
    if not retail:
        return []

    opps = []
    for pt, outcomes in pinny.items():
        over = outcomes.get("Over", {}).get("price")
        under = outcomes.get("Under", {}).get("price")
        if not over or not under:
            continue
        fo, fu = _remove_vig(over, under)
        r_over = retail.get(pt, {}).get("Over")
        r_under = retail.get(pt, {}).get("Under")
        for suffix, fair, r_price in [("大", fo, r_over), ("小", fu, r_under)]:
            if not r_price or r_price <= 1 or fair <= 0:
                continue
            ev = round((r_price - fair) / fair * 100, 2)
            if ev < 3 or ev > MAX_EDGE_PCT:
                continue
            kelly = (ev / 100) / (r_price - 1) * KELLY_FRACTION
            opps.append({
                "sport": sport, "league": league, "market": "alternate_totals", "market_label": "大小分",
                "home_team": home_team, "away_team": away_team,
                "outcome": f"{suffix}{pt}", "outcome_label": f"{suffix}{pt}",
                "point": pt, "point_str": "", "commence_time": commence_time,
                "fair_price": fair, "retail_odds": r_price, "edge_pct": ev,
                "odds": r_price, "kelly_pct": round(kelly * 100, 2),
            })
    return opps


def get_active_leagues(sport_key_prefix: str) -> Set[str]:
    """从 API 获取当前活跃的联赛（按前缀筛选）。"""
    if not ODDS_API_KEY:
        return set()
    if not _api_call(f"sports_list {sport_key_prefix}"):
        return set()
    import requests
    try:
        url = f"https://api.the-odds-api.com/v4/sports/?apiKey={ODDS_API_KEY}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return {s["key"] for s in resp.json()
                    if isinstance(s, dict) and s.get("active")
                    and s.get("key", "").startswith(sport_key_prefix)}
    except Exception:
        pass
    return set()


def _fetch_event_odds(sport_key: str, event_id: str) -> dict:
    """抓取单场比赛的备选盘口赔率（合并 EU+US 区域）。"""
    if not ODDS_API_KEY:
        return {}
    if not _api_call(f"event_odds {sport_key} {event_id[:8]}"):
        return {}
    import requests
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds"
    try:
        resp = requests.get(url, params={
            "apiKey": ODDS_API_KEY, "regions": "eu,us",
            "markets": "alternate_spreads,alternate_totals",
            "oddsFormat": "decimal",
        }, timeout=15)
        if resp.status_code == 200:
            remaining = resp.headers.get("x-requests-remaining", "?")
            logger.debug("  event odds 剩余: %s (event=%s)", remaining, event_id[:8])
            data = resp.json()
            if isinstance(data, dict) and data.get("bookmakers"):
                return data
    except Exception:
        pass
    return {}


def scan_sport(sport_key_prefix: str, trusted_leagues: Optional[Set[str]],
               results_file: Path, sport_label: str) -> List[dict]:
    """扫描指定体育的所有 +EV 机会。

    Args:
        sport_key_prefix: API sport key 前缀，如 "baseball_", "tennis_"
        trusted_leagues: 可信联赛白名单，None=全部接受
        results_file: 结果保存路径
        sport_label: 日志标签
    """
    active = get_active_leagues(sport_key_prefix)
    if trusted_leagues is not None:
        to_scan = trusted_leagues & active
        inactive = trusted_leagues - active
        if inactive:
            logger.info(" 跳过不活跃联赛: %s", ", ".join(sorted(inactive)))
    else:
        to_scan = active

    if not to_scan:
        logger.info(" 无可扫描的活跃%s联赛", sport_label)
        return []

    all_opps = []
    for sport_key in sorted(to_scan):
        league_label = sport_key
        logger.info("扫描 %s ...", sport_key)
        eu_data = _fetch_odds(sport_key, "eu")
        if not eu_data:
            continue
        us_data = _fetch_odds(sport_key, "us,us2")
        if not us_data:
            continue

        eu_by_id = {g.get("id", ""): g for g in eu_data}
        us_by_id = {g.get("id", ""): g for g in us_data}
        common = set(eu_by_id.keys()) & set(us_by_id.keys())
        logger.info("  共同比赛: %d 场", len(common))

        for gid in common:
            eg = eu_by_id[gid]
            ug = us_by_id[gid]
            home, away = eg.get("home_team", ""), eg.get("away_team", "")
            ct = eg.get("commence_time", "")

            opps = _eval_h2h(home, away, ct, eg, ug, sport_label, league_label)
            opps += _eval_spreads(home, away, ct, eg, ug, sport_label, league_label)
            opps += _eval_totals(home, away, ct, eg, ug, sport_label, league_label)
            # 备选盘口（event-level endpoint，额外消耗 API 额度）
            event_data = _fetch_event_odds(sport_key, gid)
            if event_data:
                opps += _eval_alternate_spreads(home, away, ct, event_data, event_data, sport_label, league_label)
                opps += _eval_alternate_totals(home, away, ct, event_data, event_data, sport_label, league_label)

            if opps:
                logger.info("    场次 %s vs %s: %d 条 +EV", home, away, len(opps))
            all_opps.extend(opps)

    all_opps.sort(key=lambda x: x["edge_pct"], reverse=True)
    return all_opps


def scan_and_save(sport_key_prefix: str, trusted_leagues: Optional[Set[str]],
                  results_file: Path, sport_label: str) -> int:
    """扫描 +EV 机会并保存到文件。返回机会数。"""
    opps = scan_sport(sport_key_prefix, trusted_leagues, results_file, sport_label)
    results_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "total": len(opps),
        "opportunities": opps,
    }
    results_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    logger.info("  %s: 保存 %d 条 +EV 机会", sport_label, len(opps))
    return len(opps)
