"""ESPN 免费比分 API — 无需 API Key。

用于比赛结果结算，无配额限制。支持 NBA + 五大联赛。
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests

from config.logging_config import get_logger

logger = get_logger(__name__)

# ESPN 联赛路径映射: league_name → (ESPN sport path, display_name)
LEAGUE_ESPN_PATH = {
    "NBA": ("basketball/nba", "NBA"),
    "英超": ("soccer/eng.1", "英超"),
    "西甲": ("soccer/esp.1", "西甲"),
    "德甲": ("soccer/ger.1", "德甲"),
    "意甲": ("soccer/ita.1", "意甲"),
    "法甲": ("soccer/fra.1", "法甲"),
}

# 反查: sport_key → league_name
SPORT_KEY_TO_LEAGUE = {
    "basketball_nba": "NBA",
    "soccer_epl": "英超",
    "soccer_spain_la_liga": "西甲",
    "soccer_germany_bundesliga": "德甲",
    "soccer_italy_serie_a": "意甲",
    "soccer_france_ligue_one": "法甲",
}


def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    """通用 JSON 抓取。"""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            return resp.json()
        logger.warning("⚠️ ESPN API 返回 %s: %s", resp.status_code, url.split("?")[0])
        return None
    except Exception as e:
        logger.warning("⚠️ ESPN API 请求失败 %s: %s", url.split("?")[0], e)
        return None


def _parse_espn_game(event: dict) -> Optional[dict]:
    """解析 ESPN 单场比赛数据。

    Returns:
        {home_team, away_team, home_score, away_score, status, completed}
    """
    comps = event.get("competitions", [])
    if not comps:
        return None
    comp = comps[0]
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None

    # 比赛日期
    game_date = comp.get("date") or event.get("date", "")

    # 确定主客场 — ESPN 通常第一个是主队
    home_team, away_team = None, None
    for c in competitors:
        name = c.get("team", {}).get("displayName", "")
        score = c.get("score", "0")
        try:
            score = int(score)
        except (ValueError, TypeError):
            score = 0
        if c.get("homeAway") == "home":
            home_team = (name, score)
        else:
            away_team = (name, score)

    if not home_team or not away_team:
        # 如果没有 homeAway 字段，默认第一个是主队
        home_team = (competitors[0].get("team", {}).get("displayName", ""),
                     int(competitors[0].get("score", 0) or 0))
        away_team = (competitors[1].get("team", {}).get("displayName", ""),
                     int(competitors[1].get("score", 0) or 0))

    status_type = comp.get("status", {}).get("type", {}).get("name", "")
    is_completed = status_type == "STATUS_FINAL"

    return {
        "home_team": home_team[0].strip(),
        "away_team": away_team[0].strip(),
        "home_score": home_team[1],
        "away_score": away_team[1],
        "status": status_type,
        "completed": is_completed,
        "game_date": game_date,
    }


def fetch_espn_scores(league: str, days_back: int = 3) -> List[dict]:
    """从 ESPN 获取指定联赛的已完成比赛结果。

    Args:
        league: "NBA" / "英超" / "西甲" / "德甲" / "意甲" / "法甲"
        days_back: 往回看的天数

    Returns:
        [{home_team, away_team, home_score, away_score, completed}, ...]
    """
    path_info = LEAGUE_ESPN_PATH.get(league)
    if not path_info:
        logger.warning("ESPN 不支持的联赛: %s", league)
        return []

    sport_path, _ = path_info
    now = datetime.now(timezone.utc)

    results = []
    for day_offset in range(days_back - 1, -1, -1):
        date_str = (now - timedelta(days=day_offset)).strftime("%Y%m%d")
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard"
            f"?dates={date_str}"
        )
        data = _fetch_json(url)
        if not data:
            continue

        events = data.get("events", [])
        for event in events:
            game = _parse_espn_game(event)
            if game and game["completed"]:
                results.append(game)

    return results


def fetch_espn_scores_by_sport_key(sport_key: str, days_back: int = 3) -> List[dict]:
    """通过 Odds API sport_key 获取 ESPN 比分。

    Args:
        sport_key: "basketball_nba" / "soccer_epl" / etc.

    Returns:
        [{home_team, away_team, home_score, away_score, completed}, ...]
    """
    league = SPORT_KEY_TO_LEAGUE.get(sport_key)
    if not league:
        logger.debug("ESPN 不支持该 sport key: %s", sport_key)
        return []
    return fetch_espn_scores(league, days_back)


def build_espn_result_map(sport_key: str, days_back: int = 3) -> Dict[Tuple[str, str], Tuple[str, int, int]]:
    """构建 ESPN 比分查找映射。

    Returns:
        {(home_team_lower, away_team_lower): (winner_name, home_score, away_score)}
    """
    games = fetch_espn_scores_by_sport_key(sport_key, days_back)
    results = {}
    for g in games:
        if not g["completed"]:
            continue
        home = g["home_team"].strip().lower()
        away = g["away_team"].strip().lower()
        winner = g["home_team"] if g["home_score"] > g["away_score"] else (
            g["away_team"] if g["away_score"] > g["home_score"] else "DRAW")
        results[(home, away)] = (winner, g["home_score"], g["away_score"])
    return results
