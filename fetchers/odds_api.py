import json, requests, time
from pathlib import Path
from datetime import datetime, timedelta
from config.settings import (
    BASKETBALL_API_KEY,
    FOOTBALL_API_KEY,
    FOOTBALL_ODDS_API_KEY,
    ODDS_API_KEY,
    ODDS_API_KEY_2,
    ODDS_API_KEY_3,
    ODDS_API_IO_KEY,
    DATA_DIR,
    SPORTS_API_TIMEOUT,
)

from config.logging_config import get_logger
logger = get_logger(__name__)

CACHE_DIR = DATA_DIR / 'odds'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SPORTS_LEAGUES = [
    'soccer_epl',
    'soccer_spain_la_liga',
    'soccer_germany_bundesliga',
    'soccer_italy_serie_a',
    'soccer_france_ligue_one',
    'soccer_brazil_campeonato',
    'soccer_brazil_serie_b',
    'soccer_netherlands_eredivisie',
    'soccer_portugal_primeira_liga',
    'soccer_spain_segunda_division',
    'soccer_china_superleague',
    'soccer_sweden_allsvenskan',
    'soccer_norway_eliteserien',
    'soccer_chile_campeonato',
    'soccer_finland_veikkausliiga',
    'soccer_league_of_ireland',
    'soccer_sweden_superettan',
    'soccer_germany_dfb_pokal',
    'soccer_england_championship',
    'soccer_italy_serie_b',
    'soccer_conmebol_copa_sudamericana',
    'basketball_wnba',
    'basketball_euroleague',
]


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f'live_{name}_odds.json'


def _load_cache(name: str, max_age_hours: int = 4):
    path = _cache_path(name)
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    if datetime.now() - mtime > timedelta(hours=max_age_hours):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # 防御：缓存应为列表（match 列表），拒绝错误 dict
    if not isinstance(data, list):
        logger.warning("⚠️ 缓存 %s 格式错误（dict 而非 list），忽略", name)
        path.unlink(missing_ok=True)
        return None
    return data


def _save_cache(name: str, data):
    path = _cache_path(name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)


# ── odds-api.io 逐事件赔率缓存（30秒 TTL，仅用于同进程内去重） ────
_IO_EVENT_CACHE_DIR = CACHE_DIR / 'io_event_cache'
_IO_EVENT_CACHE_TTL_SEC = 1800  # 30分钟文件缓存
_SESSION_EVENT_CACHE = {}       # 同进程内存缓存 {cache_key: (timestamp, data)}


def _io_event_cache_key(event_id: str, sport_key: str) -> str:
    return f"{sport_key}|{event_id}"


def _load_io_event_cache(event_id: str, sport_key: str):
    """三级缓存：内存 → 文件 → 无。"""
    ck = _io_event_cache_key(event_id, sport_key)
    # 内存缓存（同进程）
    now = time.time()
    if ck in _SESSION_EVENT_CACHE:
        ts, data = _SESSION_EVENT_CACHE[ck]
        if now - ts < _IO_EVENT_CACHE_TTL_SEC:
            return data
        del _SESSION_EVENT_CACHE[ck]
    # 文件缓存（跨进程）
    path = _IO_EVENT_CACHE_DIR / f"{sport_key}_{event_id}.json"
    if path.exists():
        mtime = path.stat().st_mtime
        if now - mtime < _IO_EVENT_CACHE_TTL_SEC:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            _SESSION_EVENT_CACHE[ck] = (now, data)
            return data
    return None


def _save_io_event_cache(event_id: str, sport_key: str, data):
    """写入文件缓存 + 内存缓存。"""
    ck = _io_event_cache_key(event_id, sport_key)
    _SESSION_EVENT_CACHE[ck] = (time.time(), data)
    _IO_EVENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _IO_EVENT_CACHE_DIR / f"{sport_key}_{event_id}.json"
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _clear_io_event_cache():
    """清空所有缓存（调试用）。"""
    _SESSION_EVENT_CACHE.clear()
    import shutil
    if _IO_EVENT_CACHE_DIR.exists():
        shutil.rmtree(_IO_EVENT_CACHE_DIR)


def _get_sport_api_keys(sport_key: str):
    """获取多级备用 API Key，自动轮转。"""
    if sport_key == 'basketball_nba':
        primary_keys = [BASKETBALL_API_KEY, ODDS_API_KEY]
    else:
        primary_keys = [FOOTBALL_ODDS_API_KEY, ODDS_API_KEY]
    backup_keys = [ODDS_API_KEY_2, ODDS_API_KEY_3, ODDS_API_IO_KEY]
    return [k for k in primary_keys + backup_keys if k]


# 剩余调用次数预警阈值
_QUOTA_WARNING_THRESHOLD = 50
_QUOTA_CRITICAL_THRESHOLD = 10
_QUOTA_HAS_WARNED = set()
_QUOTA_HAS_CRITICAL = set()


def _check_quota(sport_key: str, remaining: str, api_key: str):
    """检查 API 配额并发出预警。"""
    try:
        remaining_int = int(remaining)
    except (ValueError, TypeError):
        return

    key_suffix = api_key[-6:]
    if remaining_int <= _QUOTA_CRITICAL_THRESHOLD and key_suffix not in _QUOTA_HAS_CRITICAL:
        _QUOTA_HAS_CRITICAL.add(key_suffix)
        logger.warning("🚨 API 配额严重不足 (%s): key=%s 仅剩 %s 次调用",
                       sport_key, key_suffix, remaining)
    elif remaining_int <= _QUOTA_WARNING_THRESHOLD and key_suffix not in _QUOTA_HAS_WARNED:
        _QUOTA_HAS_WARNED.add(key_suffix)
        logger.warning("⚠️ API 配额不足 (%s): key=%s 仅剩 %s 次调用",
                       sport_key, key_suffix, remaining)


def _fetch_via_curl(url: str, timeout: int) -> dict:
    """使用 curl 作为 HTTPS 降级方案（绕过 LibreSSL / proxy 兼容性问题）。"""
    import subprocess, json
    cmd = ['curl', '-s', '--max-time', str(timeout), url]
    # 如果环境有代理设置，传给 curl
    for env_var in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        val = __import__('os').environ.get(env_var)
        if val:
            cmd.extend(['-x', val])
            break
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        if result.returncode != 0:
            raise RuntimeError(f'curl 请求失败 (exit {result.returncode}): {result.stderr[:200]}')
        data = json.loads(result.stdout)
        return data
    except json.JSONDecodeError as e:
        raise RuntimeError(f'curl 响应解析失败: {e}')


def _fetch_the_odds_api(sport_key: str, api_key: str, markets: str, regions: str, _timeout: int = None):
    url = f'https://api.the-odds-api.com/v4/sports/{sport_key}/odds'
    params = {
        'apiKey': api_key,
        'regions': regions,
        'markets': markets,
        'oddsFormat': 'decimal',
    }
    timeout = _timeout if _timeout is not None else SPORTS_API_TIMEOUT
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f'the-odds-api 请求失败: {resp.status_code} {resp.text}')
        return resp.json(), resp.headers.get('x-requests-remaining', 'unknown')
    except Exception as req_err:
        logger.warning('⚠️ requests 失败 (%s), 尝试 curl 降级...', req_err)
        # 构建带 query params 的完整 URL
        import urllib.parse
        param_str = urllib.parse.urlencode(params)
        full_url = f'{url}?{param_str}'
        data = _fetch_via_curl(full_url, timeout)
        # curl 可能返回 API 错误 JSON（dict 而非 list），此时应抛出而非返回
        if isinstance(data, dict):
            err_msg = data.get("message", data.get("error", str(data)))
            raise RuntimeError(f'the-odds-api curl 降级返回错误: {err_msg}')
        return data, 'unknown'


def _fetch_odds_api_io(sport_key: str, api_key: str, markets: str, regions: str):
    """从 Odds-API.io (api2.odds-api.io/v3) 获取赔率。

    注意：足球赔率已改用 BSD API（更多博彩公司，无限免费）。
    此函数现在主要服务 NBA 赔率需求。
    """
    base = "https://api2.odds-api.io/v3"

    # 足球已迁移到 BSD API，直接跳过
    if sport_key.startswith("soccer_"):
        raise RuntimeError("足球赔率已使用 BSD API，无需 odds-api.io")

    # 单联赛拉取（仅 NBA）
    slug_map = _build_io_slug_map()
    mapping = slug_map.get(sport_key)
    if not mapping:
        raise RuntimeError(f"odds-api.io 不支持的联赛: {sport_key}")

    sport_slug, league_slug = mapping

    # 1. 获取事件列表
    events_url = f"{base}/events?apiKey={api_key}&sport={sport_slug}&league={league_slug}"
    resp = requests.get(events_url, timeout=SPORTS_API_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"odds-api.io events 请求失败: {resp.status_code} {resp.text[:200]}")

    events = resp.json()
    if not events:
        return [], "N/A"

    # 预过滤：只跳过已结算赛事，保留最多 96h 内的未来赛事
    now_ts = time.time()
    _max_future = 96 * 3600
    filtered = []
    for ev in events:
        status = ev.get("status", "")
        if status == "settled":
            continue
        date_str = ev.get("date", "")
        if date_str:
            try:
                ev_ts = datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp()
                if ev_ts - now_ts > _max_future:
                    continue
            except (ValueError, TypeError):
                pass
        filtered.append(ev)

    if not filtered:
        logger.info("  ⏭️ %s: 无近期需要赔率的赛事", sport_key)
        return [], "N/A"

    # 2. 获取选定博彩公司（简短超时，失败则用默认值）
    bookmaker = "Bet365"
    try:
        sel_resp = requests.get(f"{base}/bookmakers/selected?apiKey={api_key}", timeout=5)
        if sel_resp.status_code == 200:
            sel_data = sel_resp.json()
            selected = sel_data.get("bookmakers", [])
            if selected:
                bookmaker = selected[0]
    except Exception as e:
        logger.warning("  ⚠️ odds-api.io 请求异常: %s", e)
        pass

    # 3. 逐事件获取赔率（带缓存），转换为兼容格式
    result = []
    n_cached = 0
    n_fetched = 0
    for event in filtered:
        eid = event.get("id")
        if not eid:
            continue

        cached_odds = _load_io_event_cache(eid, sport_key)
        if cached_odds is not None:
            converted = _convert_io_to_odds_api_format(
                event, cached_odds, bookmaker,
                cached_odds.get("bookmakers", {}).get(bookmaker, []),
                sport_key,
            )
            if converted:
                result.append(converted)
                n_cached += 1
            continue

        odds_url = f"{base}/odds?apiKey={api_key}&eventId={eid}&bookmakers={bookmaker}"
        try:
            odds_resp = requests.get(odds_url, timeout=SPORTS_API_TIMEOUT)
            if odds_resp.status_code != 200:
                continue
        except Exception:
            logger.warning("  ⚠️ odds-api.io 赔率请求失败 (eid=%s)", eid)
            continue

        odds_data = odds_resp.json()
        bm_data = odds_data.get("bookmakers", {}).get(bookmaker, [])
        if not bm_data:
            continue

        _save_io_event_cache(eid, sport_key, odds_data)

        converted = _convert_io_to_odds_api_format(
            event, odds_data, bookmaker, bm_data, sport_key
        )
        if converted:
            result.append(converted)
            n_fetched += 1

    if n_cached > 0:
        logger.info("  📦 %s: 缓存命中 %d 场, 新拉取 %d 场", sport_key, n_cached, n_fetched)
    return result, "N/A"


def _build_io_slug_map():
    """构建 sport_key → (sport_slug, league_slug) 映射。"""
    return {
        "basketball_nba": ("basketball", "usa-nba"),
        "basketball_wnba": ("basketball", "wnba"),
        "soccer_epl": ("football", "eng-premier-league"),
        "soccer_spain_la_liga": ("football", "spain-la-liga"),
        "soccer_germany_bundesliga": ("football", "germany-bundesliga"),
        "soccer_italy_serie_a": ("football", "italy-serie-a"),
        "soccer_france_ligue_one": ("football", "france-ligue-1"),
        "soccer_brazil_campeonato": ("football", "brazil-serie-a"),
        "soccer_netherlands_eredivisie": ("football", "netherlands-eredivisie"),
        "soccer_portugal_primeira_liga": ("football", "portugal-primeira-liga"),
        "soccer_spain_segunda_division": ("football", "spain-segunda-division"),
        "soccer_brazil_serie_b": ("football", "brazil-serie-b"),
        "soccer_china_superleague": ("football", "china-superleague"),
        "soccer_sweden_allsvenskan": ("football", "sweden-allsvenskan"),
        "soccer_norway_eliteserien": ("football", "norway-eliteserien"),
        "soccer_chile_campeonato": ("football", "chile-campeonato"),
        "soccer_finland_veikkausliiga": ("football", "finland-veikkausliiga"),
        "soccer_league_of_ireland": ("football", "ireland-premier"),
        "soccer_sweden_superettan": ("football", "sweden-superettan"),
        "soccer_germany_dfb_pokal": ("football", "germany-dfb-pokal"),
        "soccer_england_championship": ("football", "england-championship"),
        "soccer_italy_serie_b": ("football", "italy-serie-b"),
        "soccer_conmebol_copa_sudamericana": ("football", "conmebol-sudamericana"),
        "basketball_euroleague": ("basketball", "euroleague"),
    }


# 模块级哨兵：同一次运行中只批量拉取一次足球联赛
_IO_FOOTBALL_BATCHED = False
_IO_FOOTBALL_BATCHED_TIME = 0.0
_IO_BATCH_TTL = 300  # 5分钟后可重新批量拉取


def _io_batch_football_leagues(api_key, base, slug_map):
    """一次调用批量拉取所有足球联赛事件并分发到各联赛缓存。"""
    global _IO_FOOTBALL_BATCHED, _IO_FOOTBALL_BATCHED_TIME

    now = time.time()
    if _IO_FOOTBALL_BATCHED and now - _IO_FOOTBALL_BATCHED_TIME < _IO_BATCH_TTL:
        return
    _IO_FOOTBALL_BATCHED = True
    _IO_FOOTBALL_BATCHED_TIME = now

    # 只处理足球联赛
    fb_slug_map = {k: v for k, v in slug_map.items() if k.startswith("soccer_")}
    if not fb_slug_map:
        return

    # 跳过已有缓存且未过期的联赛
    leagues_to_fetch = {}
    for sk, (s_slug, l_slug) in fb_slug_map.items():
        cache = _load_cache(sk.replace("/", "_"))
        if cache is None:
            leagues_to_fetch[sk] = (s_slug, l_slug)
    if not leagues_to_fetch:
        return  # 所有联赛缓存均有效

    # 尝试无 league 过滤的一次调用（sport=football 返回所有足球赛事）
    first_sk = next(iter(leagues_to_fetch))
    first_s_slug = leagues_to_fetch[first_sk][0]
    batch_ok = False

    try:
        events_url = f"{base}/events?apiKey={api_key}&sport={first_s_slug}"
        resp = requests.get(events_url, timeout=SPORTS_API_TIMEOUT)
        if resp.status_code == 200:
            all_events = resp.json()
            if all_events:
                batch_ok = _io_distribute_football_events(
                    all_events, fb_slug_map, api_key, base,
                )
    except Exception as e:
        logger.warning("  ⚠️ odds-api.io 请求异常: %s", e)
        pass

    if batch_ok:
        logger.info("✅ 批量拉取所有足球联赛成功（1次API调用代替 %d 次）", len(leagues_to_fetch))
    else:
        # 批量失败，各联赛独立拉取由调用方兜底
        logger.warning("⚠️ 批量拉取足球联赛失败，将逐个拉取")


def _io_distribute_football_events(all_events, fb_slug_map, api_key, base):
    """将 football events 按联赛分组、拉取赔率、分发到各联赛缓存。"""
    # 构建 league_slug → sport_key 映射
    league_slug_to_sk = {l_slug: sk for sk, (_, l_slug) in fb_slug_map.items()}

    # 从事件中提取联赛信息
    events_by_league = {}
    for ev in all_events:
        league_info = ev.get("league", {})
        ls = league_info.get("slug", "") if isinstance(league_info, dict) else ""
        sport_key = league_slug_to_sk.get(ls)
        if not sport_key:
            continue
        if ev.get("status") == "settled":
            continue
        events_by_league.setdefault(sport_key, []).append(ev)

    if not events_by_league:
        return False

    # 获取博彩公司
    bookmaker = "Bet365"
    try:
        sel_resp = requests.get(f"{base}/bookmakers/selected?apiKey={api_key}", timeout=5)
        if sel_resp.status_code == 200:
            selected = sel_resp.json().get("bookmakers", [])
            if selected:
                bookmaker = selected[0]
    except Exception as e:
        logger.warning("  ⚠️ odds-api.io 请求异常: %s", e)
        pass

    # 逐事件拉取赔率（带缓存），按联赛归集
    league_results = {sk: [] for sk in fb_slug_map}
    seen_eids = set()
    n_cached = 0
    n_fetched = 0

    for event in all_events:
        eid = event.get("id")
        if not eid or eid in seen_eids:
            continue
        seen_eids.add(eid)

        league_info = event.get("league", {})
        ls = league_info.get("slug", "") if isinstance(league_info, dict) else ""
        sport_key = league_slug_to_sk.get(ls)
        if not sport_key:
            continue

        # 检查逐事件缓存
        cached_odds = _load_io_event_cache(eid, sport_key)
        if cached_odds is not None:
            converted = _convert_io_to_odds_api_format(
                event, cached_odds, bookmaker,
                cached_odds.get("bookmakers", {}).get(bookmaker, []),
                sport_key,
            )
            if converted:
                league_results[sport_key].append(converted)
                n_cached += 1
            continue

        odds_url = f"{base}/odds?apiKey={api_key}&eventId={eid}&bookmakers={bookmaker}"
        try:
            odds_resp = requests.get(odds_url, timeout=SPORTS_API_TIMEOUT)
            if odds_resp.status_code != 200:
                continue
            odds_data = odds_resp.json()
            bm_data = odds_data.get("bookmakers", {}).get(bookmaker, [])
            if not bm_data:
                continue
            _save_io_event_cache(eid, sport_key, odds_data)
            converted = _convert_io_to_odds_api_format(
                event, odds_data, bookmaker, bm_data, sport_key,
            )
            if converted:
                league_results[sport_key].append(converted)
                n_fetched += 1
        except Exception:
            logger.warning("  ⚠️ odds-api.io 事件处理失败 (eid=%s)", eid)
            continue

    if n_cached > 0 or n_fetched > 0:
        logger.info("  📦 足球批量: 缓存命中 %d 场, 新拉取 %d 场", n_cached, n_fetched)

    # 写入各联赛缓存
    for sk, games in league_results.items():
        if games:
            _save_cache(sk.replace("/", "_"), games)

    return True


def _convert_io_to_odds_api_format(event, odds_data, bookmaker, bm_data, sport_key):
    """将 odds-api.io 格式转换为 the-odds-api.com 兼容格式。"""
    home = event.get("home", "")
    away = event.get("away", "")
    date_str = event.get("date", "")

    outcomes_h2h = []
    outcomes_spread = []
    outcomes_total = []

    for market in bm_data:
        mname = market.get("name", "")
        odds_list = market.get("odds", [])

        if mname == "ML" and odds_list:
            o = odds_list[0]
            outcomes_h2h = [
                {"name": home, "price": float(o.get("home", 0))},
                {"name": away, "price": float(o.get("away", 0))},
            ]
        elif mname == "Spread" and odds_list:
            o = odds_list[0]
            hdp = o.get("hdp", 0)
            outcomes_spread = [
                {"name": home, "price": float(o.get("home", 0)), "point": float(hdp)},
                {"name": away, "price": float(o.get("away", 0)), "point": float(hdp)},
            ]
        elif mname == "Totals" and odds_list:
            o = odds_list[0]
            hdp = o.get("hdp", 0)
            outcomes_total = [
                {"name": "Over", "price": float(o.get("over", 0)), "point": float(hdp)},
                {"name": "Under", "price": float(o.get("under", 0)), "point": float(hdp)},
            ]

    markets_list = []
    if outcomes_h2h:
        markets_list.append({"key": "h2h", "outcomes": outcomes_h2h})
    if outcomes_spread:
        markets_list.append({"key": "spreads", "outcomes": outcomes_spread})
    if outcomes_total:
        markets_list.append({"key": "totals", "outcomes": outcomes_total})

    if not markets_list:
        return None

    return {
        "id": str(event.get("id", "")),
        "sport_key": sport_key,
        "sport_title": odds_data.get("sport", {}).get("name", ""),
        "commence_time": date_str,
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": bookmaker.lower(),
                "title": bookmaker,
                "last_update": date_str,
                "markets": markets_list,
            }
        ],
    }


def fetch_odds_api(sport_key: str, force: bool = False, markets: str = 'h2h,spreads,totals', regions: str = 'us,uk,eu', _timeout: int = None):
    api_keys = [k for k in _get_sport_api_keys(sport_key) if k]
    if not api_keys:
        raise ValueError(f'未配置赔率 API Key: {sport_key}')

    cache_name = sport_key.replace('/', '_')
    if not force:
        cached = _load_cache(cache_name)
        if cached is not None:
            return cached

    last_exc = None
    timeout = _timeout if _timeout is not None else SPORTS_API_TIMEOUT

    for api_key in api_keys:
        try:
            if api_key == ODDS_API_IO_KEY:
                data, remaining = _fetch_odds_api_io(sport_key, api_key, markets, regions)
            else:
                data, remaining = _fetch_the_odds_api(sport_key, api_key, markets, regions, _timeout=timeout)
            if data:
                _save_cache(cache_name, data)
                _check_quota(sport_key, remaining, api_key)
                logger.info('✅ 拉取成功：%s，剩余次数: %s (key=%s)', sport_key, remaining, api_key[-6:])
                return data
            # IO 72h 过滤器可能返回空，不覆盖已有缓存
            logger.info('  ⏭️ %s: 无近期赛事数据，保留已有缓存', sport_key)
            cached = _load_cache(cache_name, max_age_hours=4)
            if cached is not None:
                return cached
            return data
        except Exception as exc:
            last_exc = exc
            logger.warning('⚠️ %s key=%s failed: %s', sport_key, api_key[-6:], exc)
            continue

    # 所有 key 均失败：尝试读取缓存
    cached = _load_cache(cache_name, max_age_hours=24)
    if cached is not None:
        logger.warning('⚠️ %s 所有 API Key 均耗尽，使用缓存 (max 24h)', sport_key)
        return cached
    # 尝试 curl 降级（绕过 proxy/SSL 兼容性问题）
    import urllib.parse
    api_key = api_keys[0]
    param_str = 'apiKey=%s&regions=%s&markets=%s&oddsFormat=decimal' % (api_key, regions, markets)
    url = 'https://api.the-odds-api.com/v4/sports/%s/odds?%s' % (sport_key, param_str)
    try:
        logger.info('🔧 尝试 curl 降级获取 %s...', sport_key)
        data = _fetch_via_curl(url, timeout)
        if data:
            # 防御：curl 可能返回 API 错误 JSON（dict 而非 list）
            if isinstance(data, dict):
                err_msg = data.get("message", data.get("error", str(data)))
                raise RuntimeError(f'curl 降级返回错误: {err_msg}')
            _save_cache(cache_name, data)
            logger.info('✅ curl 降级成功：%s', sport_key)
            return data
    except Exception as curl_err:
        logger.warning('⚠️ curl 降级也失败: %s', curl_err)
    raise RuntimeError(last_exc)


def check_cache_health() -> dict:
    """检查所有赔率缓存的状态，返回每项缓存的时效报告。"""
    report = {"cached_leagues": 0, "fresh": 0, "stale_max_hours": 0, "uncached_leagues": 0,
              "details": {}, "overall": "ok"}

    league_keys = set()
    for sk in SPORTS_LEAGUES:
        league_keys.add(sk.replace("/", "_"))
    league_keys.add("football_all")
    league_keys.add("basketball_nba")
    league_keys.add("basketball_wnba")

    now = datetime.now()
    for name in sorted(league_keys):
        path = _cache_path(name)
        if not path.exists():
            report["uncached_leagues"] += 1
            report["details"][name] = {"status": "uncached", "age_hours": None}
            continue
        age = (now - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 3600
        report["cached_leagues"] += 1
        if age <= 4:
            report["fresh"] += 1
            status = "fresh"
        elif age <= 24:
            status = "stale"
        else:
            status = "expired"
        report["details"][name] = {"status": status, "age_hours": round(age, 1)}
        report["stale_max_hours"] = max(report["stale_max_hours"], round(age, 1))

    fresh_ratio = report["fresh"] / max(report["cached_leagues"], 1)
    if fresh_ratio < 0.5:
        report["overall"] = "degraded"
    elif report["stale_max_hours"] > 12:
        report["overall"] = "warning"
    return report


def fetch_basketball_odds(force: bool = False, sport_key: str = 'basketball_nba', cache_name: str = None):
    """获取篮球赔率 — 优先 odds-api.io（免费、已验证通）。

    Args:
        force: 是否强制刷新
        sport_key: 'basketball_nba' / 'basketball_wnba'
        cache_name: 缓存名，默认从 sport_key 派生
    """
    if cache_name is None:
        cache_name = sport_key.replace('/', '_')
    if not force:
        cached = _load_cache(cache_name)
        if cached is not None:
            return cached

    # 1. odds-api.io（免费，已验证有 Bet365 赔率）
    if ODDS_API_IO_KEY:
        try:
            data, _ = _fetch_odds_api_io(sport_key, ODDS_API_IO_KEY, '', '')
            if data:
                _save_cache(cache_name, data)
                logger.info("✅ %s 赔率: odds-api.io %d 场", sport_key, len(data))
                return data
        except Exception as e:
            logger.warning("⚠️ odds-api.io NBA 不可用: %s", e)

    # 2. the-odds-api.com 兜底
    return fetch_odds_api(sport_key, force=force, _timeout=8)


def fetch_football_odds(force: bool = False, leagues=None):
    """获取足球赔率 — 优先 BSD API（免费无限量，10+ 博彩公司），
    其次 the-odds-api.com（如配额未耗尽）。
    """
    if leagues is None:
        leagues = SPORTS_LEAGUES

    if not force:
        cached = _load_cache('football_all')
        if cached is not None:
            return cached

    # 1. BSD 免费无限量足球赔率
    try:
        from fetchers.bsd_fetcher import fetch_football_odds as bsd_fetch
        bsd_data = bsd_fetch(force=force)
        if bsd_data:
            logger.info("✅ 足球赔率: BSD API %d 场 (含 Pinnacle/Bet365 等)", len(bsd_data))
            _save_cache('football_all', bsd_data)
            return bsd_data
    except Exception as e:
        logger.warning("⚠️ BSD 足球赔率不可用: %s", e)

    # 2. the-odds-api.com 兜底（短超时）
    all_games = []
    for league in leagues:
        try:
            games = fetch_odds_api(league, force=force, _timeout=8)
            all_games.extend(games)
        except Exception as e:
            logger.warning('⚠️ 拉取 %s 赔率失败: %s', league, e)

    if not all_games:
        cached = _load_cache('football_all', max_age_hours=24)
        if cached is not None:
            return cached
    if all_games:
        _save_cache('football_all', all_games)
    return all_games
