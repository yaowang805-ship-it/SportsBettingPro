"""多源比分获取器 — 聚合 ESPN + football-data.org + The Odds API + BSD + BALLDONTLIE + 直播吧。

优先级: ESPN → football-data.org → The Odds API → BSD (足球) → BALLDONTLIE (NBA) → 直播吧

各源覆盖:
  ESPN:             ~30 联赛 (主流足球/篮球/美足/棒球)
  football-data.org: 15+ 欧洲/巴西足球联赛
  The Odds API:     61 种运动 (夏季联赛/篮球/棒球/冰球/美足)
  BSD (Bzzoiro):    30+ 足球联赛 (含中超/K联赛/巴甲/澳超等)
  BALLDONTLIE:      NBA 比赛数据
  直播吧:           中国联赛 (中超/中甲/中乙/CBA)
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from config.logging_config import get_logger
from config.settings import (
    ODDS_API_KEYS as _ODDS_API_KEYS,
    BSD_API_KEY as _BSD_API_KEY,
    BALLDONTLIE_API_KEY as _BALLDONTLIE_API_KEY,
    FOOTBALL_DATA_API_KEY as _FOOTBALL_DATA_API_KEY,
)

logger = get_logger(__name__)

# ── ESPN ──
from fetchers.espn_scores import (
    fetch_espn_scores as _espn_fetch,
    LEAGUE_ESPN_PATH as _ESPN_LEAGUES,
)

# ── football-data.org ──
_FD_API_BASE = "https://api.football-data.org/v4"

_FD_COMPETITIONS = {
    "英格兰超级联赛": "PL", "西班牙甲级联赛": "PD", "德国甲级联赛": "BL1",
    "意大利甲级联赛": "SA", "法国甲级联赛": "FL1", "巴西甲级联赛": "BSA",
    "荷兰甲级联赛": "DED", "葡萄牙超级联赛": "PPL", "英格兰冠军联赛": "ELC",
    "欧洲冠军联赛": "CL", "欧洲冠军联赛-资格赛": "CL", "欧足联欧洲联赛": "EC",
    "欧足联欧洲联赛-资格赛": "EC", "欧冠": "CL", "欧联": "EC",
}

# ── The Odds API v4 ──
_OA_API_BASE = "https://api.the-odds-api.com/v4"

_ODDS_API_LEAGUES = {
    "WNBA": "basketball_wnba", "美国女子职业篮球联赛": "basketball_wnba",
    "NBA夏季联赛": "basketball_nba_summer_league", "NBA Summer League": "basketball_nba_summer_league",
    "MLB": "baseball_mlb", "美国职业棒球联盟": "baseball_mlb",
    "NPB": "baseball_npb", "日本职业棒球": "baseball_npb", "KBO": "baseball_kbo",
    "巴西甲级联赛": "soccer_brazil_campeonato", "巴甲": "soccer_brazil_campeonato",
    "巴西乙级联赛": "soccer_brazil_serie_b", "巴乙": "soccer_brazil_serie_b",
    "智利甲级联赛": "soccer_chile_campeonato",
    "中超": "soccer_china_superleague", "中国足球超级联赛": "soccer_china_superleague",
    "韩国K1联赛": "soccer_korea_kleague1",
    "瑞典超级联赛": "soccer_sweden_allsvenskan", "瑞典超": "soccer_sweden_allsvenskan",
    "瑞典甲级联赛": "soccer_sweden_superettan", "瑞典甲": "soccer_sweden_superettan",
    "挪威超级联赛": "soccer_norway_eliteserien", "挪威超": "soccer_norway_eliteserien",
    "芬兰超级联赛": "soccer_finland_veikkausliiga", "芬超": "soccer_finland_veikkausliiga",
    "奥地利甲级联赛": "soccer_austria_bundesliga", "丹麦超级联赛": "soccer_denmark_superliga",
    "瑞士超级联赛": "soccer_switzerland_superleague", "比利时甲级联赛": "soccer_belgium_first_div",
    "俄罗斯超级联赛": "soccer_russia_premier_league",
    "英格兰联赛杯": "soccer_england_efl_cup",
    "英格兰冠军联赛": "soccer_efl_champ", "英冠": "soccer_efl_champ",
    "英格兰甲级联赛": "soccer_england_league1", "英甲": "soccer_england_league1",
    "英格兰乙级联赛": "soccer_england_league2", "英乙": "soccer_england_league2",
    "德国乙级联赛": "soccer_germany_bundesliga2", "德乙": "soccer_germany_bundesliga2",
    "德国丙级联赛": "soccer_germany_liga3", "德丙": "soccer_germany_liga3",
    "德国杯": "soccer_germany_dfb_pokal", "德杯": "soccer_germany_dfb_pokal",
    "德国甲级联赛": "soccer_germany_bundesliga", "德甲": "soccer_germany_bundesliga",
    "英格兰超级联赛": "soccer_epl", "英超": "soccer_epl",
    "西班牙甲级联赛": "soccer_spain_la_liga", "西甲": "soccer_spain_la_liga",
    "意大利甲级联赛": "soccer_italy_serie_a", "意甲": "soccer_italy_serie_a",
    "法国甲级联赛": "soccer_france_ligue_one", "法甲": "soccer_france_ligue_one",
    "荷兰甲级联赛": "soccer_netherlands_eredivisie", "荷甲": "soccer_netherlands_eredivisie",
    "墨西哥超级联赛": "soccer_mexico_ligamx", "墨超": "soccer_mexico_ligamx",
    "阿根廷甲级联赛": "soccer_argentina_primera_division", "阿甲": "soccer_argentina_primera_division",
    "苏格兰超级联赛": "soccer_spl", "苏超": "soccer_spl",
    "美国职业大联盟": "soccer_usa_mls", "美职联": "soccer_usa_mls",
    "南美解放者杯": "soccer_conmebol_copa_libertadores", "解放者杯": "soccer_conmebol_copa_libertadores",
    "南美俱乐部杯": "soccer_conmebol_copa_sudamericana", "南美杯": "soccer_conmebol_copa_sudamericana",
    "世界杯": "soccer_fifa_world_cup",
    "NFL": "americanfootball_nfl", "NFL Preseason": "americanfootball_nfl_preseason",
    "NFL美国职业美式足球": "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf", "CFL": "americanfootball_cfl",
    "NHL": "icehockey_nhl",
    # 新增覆盖
    "秘鲁甲级联赛": "soccer_peru_primeira_division",
    "立陶宛甲级联赛": "soccer_lithuania_a_lyga",
    "冰岛超级联赛": "soccer_iceland_pepsi_deild",
    "MLB 美国职业棒球大联盟": "baseball_mlb",
    "WNBA 美国职业女子篮球联赛": "basketball_wnba",
    "俄罗斯甲级联赛": "soccer_russia_first_league",
    "俄罗斯乙级A组联赛": "soccer_russia_second_league_a",
    "苏格兰联赛杯": "soccer_scotland_league_cup",
    "瑞典超甲级联赛": "soccer_sweden_superettan",
}


def _load_odds_api_keys() -> List[str]:
    return [k for k in _ODDS_API_KEYS if k]


def _fetch_odds_api_scores(sport_key: str, days_back: int = 3) -> list:
    keys = _load_odds_api_keys()
    if not keys:
        return []
    for api_key in keys:
        url = f"{_OA_API_BASE}/sports/{sport_key}/scores"
        params = {"apiKey": api_key, "daysFrom": str(min(days_back, 3))}
        try:
            import requests
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 429:
                continue
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list):
                continue
            result = []
            for g in data:
                if not g.get("completed"):
                    continue
                scores = g.get("scores") or []
                home_score = away_score = None
                for s in scores:
                    if s.get("name") == g.get("home_team"):
                        home_score = s.get("score")
                    elif s.get("name") == g.get("away_team"):
                        away_score = s.get("score")
                if home_score is None or away_score is None:
                    continue
                result.append({
                    "home_team": g["home_team"], "away_team": g["away_team"],
                    "home_score": int(home_score), "away_score": int(away_score),
                    "completed": True,
                    "scores": [
                        {"name": g["home_team"], "score": str(home_score)},
                        {"name": g["away_team"], "score": str(away_score)},
                    ],
                    "source": "the-odds-api",
                })
            if result:
                return result
        except Exception as e:
            logger.debug("The Odds API 请求失败 (%s): %s", sport_key, e)
            continue
    return []


# ── BSD (Bzzoiro Sports Data) ──
_BSD_API_BASE = "https://sports.bzzoiro.com"
_BSD_TOKEN: Optional[str] = None


def _get_bsd_token() -> Optional[str]:
    global _BSD_TOKEN
    if _BSD_TOKEN:
        return _BSD_TOKEN
    _BSD_TOKEN = _BSD_API_KEY or None
    return _BSD_TOKEN


# BB 联赛名 → BSD league.name 的映射（用于过滤 BSD 结果）
# BSD 使用英文联赛名，BB 用中文
_BSD_LEAGUE_NAMES = {
    "中超": "Chinese Super League",
    "中国足球超级联赛": "Chinese Super League",
    "韩国K1联赛": "K League 1",
    "巴西甲级联赛": "Brasileirão Serie A",
    "巴甲": "Brasileirão Serie A",
    "巴西乙级联赛": "Brasileirão Serie B",
    "巴乙": "Brasileirão Serie B",
    "澳洲甲级联赛": "NPL Queensland",  # BSD 用 NPL 命名
    "澳大利亚新南威尔士州北部全国超级联赛": "NPL Queensland",
    "芬兰超级联赛": "Veikkausliiga",
    "芬超": "Veikkausliiga",
    "芬兰甲级联赛": "Ykkösliiga",
    "冰岛超级联赛": "Besta deild",
    "冰岛甲级联赛": "1. deild",
    "挪威超级联赛": "Eliteserien",
    "挪威超": "Eliteserien",
    "挪威甲级联赛": "OBOS-ligaen",
    "瑞典超级联赛": "Allsvenskan",
    "瑞典超": "Allsvenskan",
    "瑞典甲级联赛": "Superettan",
    "瑞典甲": "Superettan",
    "丹麦超级联赛": "Superliga",
    "波兰甲级联赛": "Ekstraklasa",
    "克罗地亚甲级联赛": "HNL",
    "爱尔兰超级联赛": "League of Ireland Premier",
    "爱超": "League of Ireland Premier",
    "英格兰联赛杯": "EFL Cup",
    "球会友谊赛": "Club Friendly",
    "世界杯": "World Cup",
    "南美解放者杯": "Copa Libertadores",
    "解放者杯": "Copa Libertadores",
    "南美俱乐部杯": "Copa Sudamericana",
    "南美杯": "Copa Sudamericana",
}


def _fetch_bsd_scores(league: str, days_back: int = 3) -> list:
    """从 BSD API 获取已完成比赛比分。

    BSD 覆盖 30+ 足球联赛（含亚洲/美洲/欧洲），
    通过 date_from/date_to 过滤出比赛并匹配联赛名。
    """
    token = _get_bsd_token()
    if not token:
        return []

    # 查找 BSD 联赛名映射
    bsd_league_name = _BSD_LEAGUE_NAMES.get(league)
    if not bsd_league_name:
        return []

    import requests
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    url = f"{_BSD_API_BASE}/api/matches/"
    params = {"date_from": date_from, "date_to": date_to, "limit": 100}
    headers = {"Authorization": f"Token {token}"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            logger.debug("BSD %s: HTTP %d", league, r.status_code)
            return []
        data = r.json()
        results = []
        for m in data.get("results", []):
            if m.get("status") != "finished":
                continue
            ml_name = (m.get("league") or {}).get("name", "")
            # 模糊匹配联赛名
            if bsd_league_name.lower() not in ml_name.lower():
                continue
            hs = m.get("home_score")
            vs = m.get("away_score")
            if hs is None or vs is None:
                continue
            results.append({
                "home_team": m["home_team"], "away_team": m["away_team"],
                "home_score": int(hs), "away_score": int(vs),
                "completed": True,
                "scores": [
                    {"name": m["home_team"], "score": str(hs)},
                    {"name": m["away_team"], "score": str(vs)},
                ],
                "source": "bsd",
            })
        return results
    except Exception as e:
        logger.debug("BSD 请求失败 (%s): %s", league, e)
        return []


# ── BALLDONTLIE (NBA 比赛数据) ──
_BALLDONTLIE_KEY: Optional[str] = None


def _get_balldontlie_key() -> Optional[str]:
    global _BALLDONTLIE_KEY
    if _BALLDONTLIE_KEY:
        return _BALLDONTLIE_KEY
    _BALLDONTLIE_KEY = _BALLDONTLIE_API_KEY or None
    return _BALLDONTLIE_KEY


def _fetch_balldontlie_scores(days_back: int = 3) -> list:
    """从 BALLDONTLIE 获取 NBA 已完成比赛（含比分）。

    ⚠️ 免费版 5 次/分钟，需节省使用。
    """
    key = _get_balldontlie_key()
    if not key:
        return []

    import requests
    now = datetime.now(timezone.utc)
    start_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    url = "https://api.balldontlie.io/v1/games"
    params = {"start_date": start_date, "end_date": end_date, "per_page": 100}
    headers = {"Authorization": key}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            logger.debug("BALLDONTLIE: HTTP %d", r.status_code)
            return []
        data = r.json()
        results = []
        for g in data.get("data", []):
            if g.get("status") != "Final":
                continue
            home_team = (g.get("home_team") or {}).get("full_name", "")
            away_team = (g.get("visitor_team") or {}).get("full_name", "")
            hs = g.get("home_team_score")
            vs = g.get("visitor_team_score")
            if not home_team or not away_team or hs is None or vs is None:
                continue
            results.append({
                "home_team": home_team, "away_team": away_team,
                "home_score": int(hs), "away_score": int(vs),
                "completed": True,
                "scores": [
                    {"name": home_team, "score": str(hs)},
                    {"name": away_team, "score": str(vs)},
                ],
                "source": "balldontlie",
            })
        return results
    except Exception as e:
        logger.debug("BALLDONTLIE 请求失败: %s", e)
        return []


# ── 直播吧 ──
from fetchers.zhibo8_scores import (
    fetch_league_results as _zhibo8_fetch,
    BB_TO_ZHIBO8_CODE as _ZHIBO8_MAP,
)
from fetchers.zhibo8_scores import CODE_LEAGUE_MAP as _ZHIBO8_CODES


def _get_football_data_key() -> Optional[str]:
    return _FOOTBALL_DATA_API_KEY or None


def _fetch_fd_matches(competition_code: str, days_back: int = 3) -> list:
    key = _get_football_data_key()
    if not key:
        return []
    import requests
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")
    url = f"{_FD_API_BASE}/competitions/{competition_code}/matches"
    params = {"dateFrom": date_from, "dateTo": date_to, "status": "FINISHED"}
    headers = {"X-Auth-Token": key}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        matches = data.get("matches", [])
        result = []
        for m in matches:
            try:
                home = m["homeTeam"]["name"]
                away = m["awayTeam"]["name"]
                hs = m["score"]["fullTime"]["home"]
                aas = m["score"]["fullTime"]["away"]
                if home and away and hs is not None and aas is not None:
                    result.append({
                        "home_team": home, "away_team": away,
                        "home_score": hs, "away_score": aas,
                        "completed": True,
                        "scores": [
                            {"name": home, "score": str(hs)},
                            {"name": away, "score": str(aas)},
                        ],
                        "source": "football-data.org",
                    })
            except (KeyError, TypeError):
                continue
        return result
    except Exception as e:
        logger.debug("football-data.org 请求失败: %s", e)
        return []


# Sport → league names that have score source coverage
_SPORT_LEAGUES = {
    "football": [
        "英超", "西甲", "德甲", "意甲", "法甲", "英冠", "德乙", "法乙",
        "巴甲", "荷甲", "葡超", "比甲", "土超", "苏超", "J联赛", "澳超",
        "美职联", "墨超", "阿甲", "瑞典超", "挪威超", "芬超", "爱超",
        "瑞典甲", "中超", "西乙", "巴乙", "英甲", "英乙", "意乙",
        "欧冠", "欧联", "解放者杯", "南美杯", "世界杯",
        "丹麦超级联赛", "瑞士超级联赛", "奥地利甲级联赛",
        "波兰甲级联赛", "捷克甲级联赛", "克罗地亚甲级联赛",
        "保加利亚甲级联赛", "塞尔维亚超级联赛", "罗马尼亚甲级联赛",
        "俄罗斯超级联赛", "俄罗斯甲级联赛", "厄瓜多尔甲级联赛",
        "秘鲁甲级联赛", "立陶宛甲级联赛", "冰岛超级联赛",
        "乌拉圭甲级联赛", "巴拉圭甲级联赛",
        "英格兰联赛杯", "南美俱乐部杯", "南美解放者杯",
        "欧足联欧洲协会联赛-资格赛", "欧足联欧洲协会联赛",
        "欧足联欧洲联赛-资格赛", "欧洲冠军联赛-资格赛",
    ],
    "basketball": [
        "NBA", "WNBA", "EuroLeague", "NBA夏季联赛",
    ],
    "baseball": [
        "MLB", "NPB", "KBO",
    ],
    "americanfootball": [
        "NFL", "NCAAF", "CFL",
    ],
    "tennis": [],  # No free score source
}


def get_completed_scores_by_sport(sport: str, days_back: int = 3) -> list:
    """按 sport 名称获取该运动所有联赛的已完成比赛结果。

    遍历该运动所有已知联赛，去重后返回合并结果。
    用于 auto_settle.py 的 sport 级联退避。
    """
    leagues = _SPORT_LEAGUES.get(sport)
    if not leagues:
        # 尝试模糊匹配：key 包含 sport 字符串
        for key, league_list in _SPORT_LEAGUES.items():
            if key in sport or sport in key:
                leagues = league_list
                break
    if not leagues:
        return []

    seen = set()
    results = []
    for league in leagues:
        batch = get_completed_scores(league, days_back)
        for g in batch:
            dedup_key = (g.get("home_team", ""), g.get("away_team", ""),
                         g.get("home_score"), g.get("away_score"))
            if dedup_key not in seen:
                seen.add(dedup_key)
                results.append(g)
    logger.info("sport级联退避 %s: 查询 %d 个联赛，获得 %d 场比赛", sport, len(leagues), len(results))
    return results


def get_completed_scores(league: str, days_back: int = 3) -> list:
    """多源获取指定联赛的已完成比赛结果。

    优先级: ESPN → football-data.org → The Odds API → BSD → BALLDONTLIE → 直播吧

    Args:
        league: BB 联赛名（中文，如"西班牙甲级联赛"）
        days_back: 往回查几天

    Returns:
        [{home_team, away_team, home_score, away_score, completed, scores}, ...]
    """

    # 1. ESPN (免费，无需密钥)
    if league in _ESPN_LEAGUES:
        espn_results = _espn_fetch(league, days_back)
        if espn_results:
            formatted = []
            for g in espn_results:
                formatted.append({
                    "home_team": g["home_team"], "away_team": g["away_team"],
                    "home_score": g["home_score"], "away_score": g["away_score"],
                    "completed": g.get("completed", True),
                    "game_date": g.get("game_date", ""),
                    "scores": [
                        {"name": g["home_team"], "score": str(g["home_score"])},
                        {"name": g["away_team"], "score": str(g["away_score"])},
                    ],
                    "source": "espn",
                })
            return formatted

    # 2. football-data.org (仅欧洲/巴西足球)
    fd_code = _FD_COMPETITIONS.get(league)
    if fd_code:
        fd_results = _fetch_fd_matches(fd_code, days_back)
        if fd_results:
            return fd_results

    # 3. The Odds API v4 (61 种运动，有 quota 限制)
    oa_key = _ODDS_API_LEAGUES.get(league)
    if oa_key:
        oa_results = _fetch_odds_api_scores(oa_key, days_back)
        if oa_results:
            return oa_results

    # 4. BSD (30+ 足球联赛，免费无限制)
    bsd_results = _fetch_bsd_scores(league, days_back)
    if bsd_results:
        return bsd_results

    # 5. BALLDONTLIE (NBA，5次/分钟)
    if league in ("NBA", "美国职业篮球联赛"):
        bd_results = _fetch_balldontlie_scores(days_back)
        if bd_results:
            return bd_results

    # 6. 直播吧 (中国联赛)
    if league in _ZHIBO8_MAP or league in _ZHIBO8_CODES:
        zb_results = _zhibo8_fetch(league, days_back)
        if zb_results:
            return zb_results

    return []


def get_all_source_coverage() -> Dict[str, List[str]]:
    """返回各数据源覆盖的联赛列表。"""
    return {
        "espn": sorted(_ESPN_LEAGUES.keys()),
        "football-data": sorted(_FD_COMPETITIONS.keys()),
        "the-odds-api": sorted(
            l for l, k in sorted(_ODDS_API_LEAGUES.items())
            if k not in ("soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
                         "soccer_italy_serie_a", "soccer_france_ligue_one")
        ),
        "bsd": sorted(_BSD_LEAGUE_NAMES.keys()),
        "balldontlie": ["NBA", "美国职业篮球联赛"],
        "zhibo8": sorted(_ZHIBO8_MAP.keys()) + sorted(
            [v[1] for v in _ZHIBO8_CODES.values() if v[1]]
        ),
    }


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()

    coverage = get_all_source_coverage()
    print("=== 数据源覆盖 ===")
    for source, leagues in coverage.items():
        print(f"\n{source}: {len(leagues)} 个联赛")
        for l in sorted(leagues)[:10]:
            print(f"  - {l}")
        if len(leagues) > 10:
            print(f"  ... 还有 {len(leagues)-10} 个")

    # 测试各源
    test_leagues = [
        "厄瓜多尔甲级联赛",  # ESPN
        "巴西甲级联赛",      # football-data.org / BSD / Odds API
        "WNBA",              # ESPN / Odds API
        "中超",              # BSD / 直播吧 / Odds API
        "NBA夏季联赛",       # Odds API
    ]
    for league in test_leagues:
        scores = get_completed_scores(league, days_back=3)
        print(f"\n{league}: {len(scores)} 场已完成")
        for s in scores[:3]:
            print(f"  {s['home_team']} {s['home_score']}-{s['away_score']} {s['away_team']} ({s['source']})")
