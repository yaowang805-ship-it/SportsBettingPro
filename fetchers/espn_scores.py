"""ESPN 免费比分 API — 无需 API Key。

用于比赛结果结算，无配额限制。支持 NBA + 五大联赛。
"""
from datetime import datetime, timezone, timedelta
import time
from typing import Dict, List, Optional, Tuple

import requests

from config.logging_config import get_logger

logger = get_logger(__name__)

# ESPN 联赛路径映射: league_name → (ESPN sport path, display_name)
LEAGUE_ESPN_PATH = {
    "NBA": ("basketball/nba", "NBA"),
    "英超": ("soccer/eng.1", "英超"),
    "英格兰超级联赛": ("soccer/eng.1", "英超"),
    "西甲": ("soccer/esp.1", "西甲"),
    "西班牙甲级联赛": ("soccer/esp.1", "西甲"),
    "德甲": ("soccer/ger.1", "德甲"),
    "德国甲级联赛": ("soccer/ger.1", "德甲"),
    "意甲": ("soccer/ita.1", "意甲"),
    "意大利甲级联赛": ("soccer/ita.1", "意甲"),
    "法甲": ("soccer/fra.1", "法甲"),
    "法国甲级联赛": ("soccer/fra.1", "法甲"),
    "巴甲": ("soccer/bra.1", "巴甲"),
    "巴西甲级联赛": ("soccer/bra.1", "巴甲"),
    "荷甲": ("soccer/ned.1", "荷甲"),
    "荷兰甲级联赛": ("soccer/ned.1", "荷甲"),
    "葡超": ("soccer/por.1", "葡超"),
    "葡萄牙超级联赛": ("soccer/por.1", "葡超"),
    "美职联": ("soccer/usa.1", "美职联"),
    "美国职业大联盟": ("soccer/usa.1", "美职联"),
    "墨超": ("soccer/mex.1", "墨超"),
    "墨西哥超级联赛": ("soccer/mex.1", "墨超"),
    "阿甲": ("soccer/arg.1", "阿甲"),
    "阿根廷甲级联赛": ("soccer/arg.1", "阿甲"),
    "土超": ("soccer/tur.1", "土超"),
    "土耳其超级联赛": ("soccer/tur.1", "土超"),
    "苏超": ("soccer/sco.1", "苏超"),
    "苏格兰超级联赛": ("soccer/sco.1", "苏超"),
    "J联赛": ("soccer/jpn.1", "J联赛"),
    "日本职业联赛": ("soccer/jpn.1", "J联赛"),
    "澳超": ("soccer/aus.1", "澳超"),
    "澳洲甲级联赛": ("soccer/aus.1", "澳超"),
    "比甲": ("soccer/bel.1", "比甲"),
    "比利时甲级联赛": ("soccer/bel.1", "比甲"),
    "德乙": ("soccer/ger.2", "德乙"),
    "德国乙级联赛": ("soccer/ger.2", "德乙"),
    "法乙": ("soccer/fra.2", "法乙"),
    "法国乙级联赛": ("soccer/fra.2", "法乙"),
    "英冠": ("soccer/eng.2", "英冠"),
    "英格兰冠军联赛": ("soccer/eng.2", "英冠"),
    "解放者杯": ("soccer/conmebol.libertadores", "解放者杯"),
    "欧冠": ("soccer/uefa.champions", "欧冠"),
    "欧洲冠军联赛": ("soccer/uefa.champions", "欧冠"),
    "欧洲冠军联赛-资格赛": ("soccer/uefa.champions", "欧冠"),
    "欧联": ("soccer/uefa.europa", "欧联"),
    "欧足联欧洲联赛": ("soccer/uefa.europa", "欧联"),
    "欧足联欧洲联赛-资格赛": ("soccer/uefa.europa", "欧联"),
    "欧足联欧洲协会联赛": ("soccer/uefa.europa.conf", "欧协联"),
    "欧足联欧洲协会联赛-资格赛": ("soccer/uefa.europa.conf", "欧协联"),
    "NFL": ("football/nfl", "NFL"),
    "世界杯": ("soccer/fifa.world", "世界杯"),
    "WNBA": ("basketball/wnba", "WNBA"),
    "西乙": ("soccer/esp.2", "西乙"),
    "西班牙乙级联赛": ("soccer/esp.2", "西乙"),
    "巴乙": ("soccer/bra.2", "巴乙"),
    "巴西乙级联赛": ("soccer/bra.2", "巴乙"),
    "英甲": ("soccer/eng.3", "英甲"),
    "英格兰甲级联赛": ("soccer/eng.3", "英甲"),
    "英乙": ("soccer/eng.4", "英乙"),
    "英格兰乙级联赛": ("soccer/eng.4", "英乙"),
    "意乙": ("soccer/ita.2", "意乙"),
    "意大利乙级联赛": ("soccer/ita.2", "意乙"),
    "中超": ("soccer/chn.1", "中超"),
    "瑞典超": ("soccer/swe.1", "瑞典超"),
    "瑞典超级联赛": ("soccer/swe.1", "瑞典超"),
    "挪威超": ("soccer/nor.1", "挪威超"),
    "超级挪威联赛": ("soccer/nor.1", "挪威超"),
    "芬超": ("soccer/fin.1", "芬超"),
    "芬兰超级联赛": ("soccer/fin.1", "芬超"),
    "芬兰甲级联赛": ("soccer/fin.2", "芬甲"),
    "爱超": ("soccer/irl.1", "爱超"),
    "爱尔兰超级联赛": ("soccer/irl.1", "爱超"),
    "瑞典甲": ("soccer/swe.2", "瑞典甲"),
    "瑞典甲级联赛": ("soccer/swe.2", "瑞典甲"),
    "南美杯": ("soccer/conmebol.sudamericana", "南美杯"),
    "乌拉圭甲级联赛": ("soccer/uru.1", "乌拉圭甲"),
    "阿根廷全国联赛": ("soccer/arg.2", "阿根廷全国联赛"),
    "英格兰联赛杯": ("soccer/eng.carabao", "英联杯"),
    "俄罗斯超级联赛": ("soccer/rus.1", "俄超"),
    "苏格兰联赛杯": ("soccer/sco.league_cup", "苏联赛杯"),
    "罗马尼亚甲级联赛": ("soccer/rou.1", "罗甲"),
    "厄瓜多尔甲级联赛": ("soccer/ecu.1", "厄甲"),
    "巴拉圭甲级联赛": ("soccer/par.1", "巴拉圭甲"),
    # MLB
    "MLB 美国职业棒球大联盟": ("baseball/mlb", "MLB"),
    "MLB联赛": ("baseball/mlb", "MLB"),
    "MLB": ("baseball/mlb", "MLB"),
}

# 反查: sport_key → league_name
SPORT_KEY_TO_LEAGUE = {
    "basketball_nba": "NBA",
    "soccer_epl": "英超",
    "soccer_spain_la_liga": "西甲",
    "soccer_germany_bundesliga": "德甲",
    "soccer_italy_serie_a": "意甲",
    "soccer_france_ligue_one": "法甲",
    "soccer_brazil_campeonato": "巴甲",
    "soccer_netherlands_eredivisie": "荷甲",
    "soccer_portugal_primeira_liga": "葡超",
    "soccer_usa_mls": "美职联",
    "soccer_mexico_liga_mx": "墨超",
    "soccer_argentina_primera_division": "阿甲",
    "soccer_turkey_super_league": "土超",
    "soccer_scotland_premiership": "苏超",
    "soccer_japan_j_league": "J联赛",
    "soccer_australia_aleague": "澳超",
    "soccer_belgium_first_div": "比甲",
    "soccer_germany_bundesliga2": "德乙",
    "soccer_france_ligue_two": "法乙",
    "soccer_england_championship": "英冠",
    "soccer_copa_libertadores": "解放者杯",
    "soccer_uefa_champions_league": "欧冠",
    "soccer_uefa_europa_league": "欧联",
    "americanfootball_nfl": "NFL",
    "soccer_fifa_world_cup": "世界杯",
    "basketball_wnba": "WNBA",
    "soccer_spain_segunda_division": "西乙",
    "soccer_brazil_serie_b": "巴乙",
    "soccer_china_superleague": "中超",
    "soccer_sweden_allsvenskan": "瑞典超",
    "soccer_norway_eliteserien": "挪威超",
    "soccer_finland_veikkausliiga": "芬超",
    "soccer_league_of_ireland": "爱超",
    "soccer_sweden_superettan": "瑞典甲",
    "soccer_germany_dfb_pokal": "德杯",
    "soccer_italy_serie_b": "意乙",
    "soccer_conmebol_copa_sudamericana": "南美杯",
    "baseball_mlb": "MLB",
    "baseball_npb": "日本职业棒球",
    "soccer_iceland_pepsi_deild": "冰岛超级联赛",
    "soccer_peru_primeira_division": "秘鲁甲级联赛",
    "soccer_lithuania_a_lyga": "立陶宛甲级联赛",
    "soccer_russia_premier_league": "俄罗斯超级联赛",
    "soccer_russia_first_league": "俄罗斯甲级联赛",
}


def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    """通用 JSON 抓取（HTTPS → HTTP 降级 → curl 降级）。

    ESPN API: HTTPS 被 Edgesuite CDN 封杀返回 403，HTTP 正常。
    """
    urls_to_try = [url]
    if url.startswith("https://"):
        urls_to_try.append(url.replace("https://", "http://", 1))

    REQ_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for try_url in urls_to_try:
        try:
            resp = requests.get(try_url, timeout=timeout, headers=REQ_HEADERS)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 403 and try_url.startswith("https://"):
                continue  # HTTPS blocked, try HTTP
            logger.warning("⚠️ ESPN API 返回 %s: %s", resp.status_code, try_url.split("?")[0])
        except Exception as e:
            logger.warning("⚠️ ESPN API 请求失败 %s: %s", try_url.split("?")[0], e)

    # curl 降级（绕过 LibreSSL / proxy 兼容性问题 + ESPN WAF）
    CURL_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    )
    for try_url in urls_to_try:
        try:
            import subprocess, json
            cmd = [
                'curl', '-s', '--compressed', '-L',
                '--max-time', str(timeout),
                '-H', f'User-Agent: {CURL_UA}',
                '-H', 'Accept: application/json, text/plain, */*',
                '-H', 'Accept-Language: en-US,en;q=0.9',
                try_url,
            ]
            import os
            for env_var in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
                val = os.environ.get(env_var)
                if val:
                    cmd.extend(['-x', val])
                    break
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                logger.debug("curl fallback success: %s", try_url.split("?")[0])
                return data
            else:
                logger.debug("curl fallback failed rc=%d len=%d: %s",
                           result.returncode, len(result.stdout or ""), try_url.split("?")[0])
        except Exception:
            pass
    return None


def _extract_stat(competitor: dict, stat_name: str) -> Optional[int]:
    """从 ESPN competitor statistics 中提取指定统计值。"""
    stats = competitor.get("statistics", [])
    for s in stats:
        if s.get("name") == stat_name:
            try:
                return int(s.get("displayValue", "0"))
            except (ValueError, TypeError):
                return None
    return None


def _parse_espn_game(event: dict) -> Optional[dict]:
    """解析 ESPN 单场比赛数据。

    Returns:
        {home_team, away_team, home_score, away_score,
         home_corners, away_corners, status, completed}
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
        corners = _extract_stat(c, "wonCorners")
        if c.get("homeAway") == "home":
            home_team = (name, score, corners)
        else:
            away_team = (name, score, corners)

    if not home_team or not away_team:
        # 如果没有 homeAway 字段，默认第一个是主队
        home_team = (competitors[0].get("team", {}).get("displayName", ""),
                     int(competitors[0].get("score", 0) or 0),
                     _extract_stat(competitors[0], "wonCorners"))
        away_team = (competitors[1].get("team", {}).get("displayName", ""),
                     int(competitors[1].get("score", 0) or 0),
                     _extract_stat(competitors[1], "wonCorners"))

    status_type = comp.get("status", {}).get("type", {}).get("name", "")
    is_completed = status_type in ("STATUS_FINAL", "STATUS_FULL_TIME", "STATUS_FULL_TIME_EXTRA")

    return {
        "home_team": home_team[0].strip(),
        "away_team": away_team[0].strip(),
        "home_score": home_team[1],
        "away_score": away_team[1],
        "home_corners": home_team[2],
        "away_corners": away_team[2],
        "status": status_type,
        "completed": is_completed,
        "game_date": game_date,
    }


# 失败冷却: 永久性 400/404 的联赛 slug 冷却 1 小时, 避免每次结算循环都重试同一个坏 slug
# (uefa.conference 曾 10 分钟 43 次 400 空转耗 CPU)
_FAILED_COOLDOWN = {}   # sport_path -> 到期时间戳
_FAILED_TTL = 3600      # 1 小时


def _cooled_down(sport_path: str) -> bool:
    """该联赛 slug 是否在失败冷却期内(此前全量拉取失败)。"""
    now = time.time()
    expired = [k for k, v in _FAILED_COOLDOWN.items() if v <= now]
    for k in expired:
        _FAILED_COOLDOWN.pop(k, None)
    return _FAILED_COOLDOWN.get(sport_path, 0) > now


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
    if _cooled_down(sport_path):
        return []
    now = datetime.now(timezone.utc)

    results = []
    got_any = False
    for day_offset in range(days_back - 1, -1, -1):
        date_str = (now - timedelta(days=day_offset)).strftime("%Y%m%d")
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard"
            f"?dates={date_str}"
        )
        data = _fetch_json(url)
        if not data:
            continue
        got_any = True

        events = data.get("events", [])
        for event in events:
            game = _parse_espn_game(event)
            if game and game["completed"]:
                results.append(game)

    if not got_any:
        _FAILED_COOLDOWN[sport_path] = time.time() + _FAILED_TTL
    return results


def _parse_tennis_match(comp: dict) -> Optional[dict]:
    """解析 ESPN 网球单场。网球用 winner 布尔标记，比分是盘数（结算只需胜负）。"""
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None
    names = []
    winner_flags = []
    for c in competitors:
        name = (c.get("athlete") or c.get("team") or {}).get("displayName", "")
        names.append(name.strip())
        winner_flags.append(bool(c.get("winner")))

    status = comp.get("status", {}).get("type", {}).get("name", "")
    if status != "STATUS_FINAL":
        return None

    # 胜负转成 1/0 比分，结算侧用 home_score > away_score 判主胜
    if winner_flags[0]:
        hs, gs = 1, 0
    elif winner_flags[1]:
        hs, gs = 0, 1
    else:
        hs = gs = 0
    return {
        "home_team": names[0],
        "away_team": names[1],
        "home_score": hs,
        "away_score": gs,
        "status": status,
        "completed": True,
        "game_date": comp.get("date", ""),
    }


def fetch_tennis_scores(days_back: int = 3) -> List[dict]:
    """从 ESPN 获取 ATP + WTA 已完成网球比赛结果。

    网球结构: events=赛事(tournament) → groupings=轮次 → competitions=单场。
    返回: [{home_team, away_team, home_score, away_score, completed}]。
    """
    results = []
    now = datetime.now(timezone.utc)
    for tour in ("atp", "wta"):
        for day_offset in range(days_back - 1, -1, -1):
            date_str = (now - timedelta(days=day_offset)).strftime("%Y%m%d")
            url = (
                f"https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard"
                f"?dates={date_str}"
            )
            data = _fetch_json(url)
            if not data:
                continue
            for event in data.get("events", []):
                for grouping in event.get("groupings", []):
                    for comp in grouping.get("competitions", []):
                        game = _parse_tennis_match(comp)
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
