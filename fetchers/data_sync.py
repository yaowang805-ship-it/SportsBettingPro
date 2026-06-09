import json
import time
import warnings
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import requests
from urllib3.exceptions import InsecureRequestWarning

from config.settings import DATA_DIR, FOOTBALL_API_KEY, BDL_API_KEY, SPORTS_API_TIMEOUT

DATA_DIR.mkdir(parents=True, exist_ok=True)

from config.logging_config import get_logger
logger = get_logger(__name__)

FOOTBALL_HISTORY_FILE = DATA_DIR / 'football_history.csv'
BASKETBALL_HISTORY_FILE = DATA_DIR / 'basketball_history.csv'
NFL_HISTORY_FILE = DATA_DIR / 'nfl_history.csv'

FOOTBALL_LEAGUES = {
    'PL': '英超',
    'PD': '西甲',
    'BL1': '德甲',
    'SA': '意甲',
    'FL1': '法甲',
}

# 二级联赛（额外数据）
FOOTBALL_LEAGUES_EXTRA = {
    'ELC': '英冠',
    'BSA': '巴甲',
    'PPL': '葡超',
    'DED': '荷甲',
    'CL': '欧冠',
    'EL': '欧联',
    'BL2': '德乙',
    'FL2': '法乙',
}


def _to_date(value):
    return pd.to_datetime(value, utc=True, errors='coerce').dt.tz_localize(None)


def _fetch_json(url, headers=None, params=None):
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=SPORTS_API_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.SSLError:
        logger.warning('SSL 连接失败，尝试 verify=False 重试：%s', url)
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', InsecureRequestWarning)
            resp = requests.get(url, headers=headers, params=params, timeout=SPORTS_API_TIMEOUT, verify=False)
            resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(str(exc))
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f'JSON 解析失败: {exc} {resp.text[:200]}')


def update_football_history(days_back: int = 365, leagues: dict = None, incremental: bool = True):
    """同步足球历史数据（增量模式，不覆盖已有数据）。"""
    if not FOOTBALL_API_KEY:
        raise RuntimeError('未配置 football-data.org API Key，请设置 FOOTBALL_API_KEY')
    leagues = leagues or {**FOOTBALL_LEAGUES, **FOOTBALL_LEAGUES_EXTRA}

    # 读取已有数据
    existing = set()
    if incremental and FOOTBALL_HISTORY_FILE.exists():
        old_df = pd.read_csv(FOOTBALL_HISTORY_FILE)
        existing = set(zip(old_df['date'], old_df['home'], old_df['away']))
        logger.info('  已有 %d 条历史数据', len(old_df))

    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)

    rows = []
    for code in leagues:
        try:
            url = f'https://api.football-data.org/v4/competitions/{code}/matches'
            params = {
                'dateFrom': start_date.isoformat(),
                'dateTo': end_date.isoformat(),
            }
            headers = {'X-Auth-Token': FOOTBALL_API_KEY}
            data = _fetch_json(url, headers=headers, params=params)
            n_fetched = 0
            for match in data.get('matches', []):
                if match.get('status') != 'FINISHED':
                    continue
                score = match.get('score', {}).get('fullTime', {})
                home_goals = score.get('home')
                away_goals = score.get('away')
                if home_goals is None or away_goals is None:
                    continue
                match_date = match.get('utcDate', '')[:10]
                home = match.get('homeTeam', {}).get('name', '')
                away = match.get('awayTeam', {}).get('name', '')
                key = (match_date, home, away)
                # 跳过已有记录
                if incremental and key in existing:
                    continue
                rows.append({
                    'date': match.get('utcDate'),
                    'home': home,
                    'away': away,
                    'home_goals': home_goals,
                    'away_goals': away_goals,
                    'competition': code,
                })
                n_fetched += 1
            logger.info('  %s (%s): %d 场（%d 新增）', code, leagues[code],
                       len([m for m in data.get('matches', []) if m.get('status') == 'FINISHED']), n_fetched)
        except Exception as e:
            logger.warning('  ⚠️ %s 拉取失败: %s', code, e)

    if not rows:
        logger.info('  无新增比赛数据')
        return pd.read_csv(FOOTBALL_HISTORY_FILE) if FOOTBALL_HISTORY_FILE.exists() else pd.DataFrame()

    df = pd.DataFrame(rows)
    df['date'] = _to_date(df['date'])
    df = df.drop_duplicates(subset=['date', 'home', 'away'])
    df = df.sort_values('date').reset_index(drop=True)

    # 合并到现有数据
    if incremental and FOOTBALL_HISTORY_FILE.exists():
        old_df = pd.read_csv(FOOTBALL_HISTORY_FILE)
        old_df['date'] = _to_date(old_df['date'])
        combined = pd.concat([old_df, df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date', 'home', 'away'])
        combined = combined.sort_values('date').reset_index(drop=True)
        combined.to_csv(FOOTBALL_HISTORY_FILE, index=False)
        logger.info('✅ 足球历史数据: %d → %d 条（%s）', len(old_df), len(combined), FOOTBALL_HISTORY_FILE.name)
    else:
        df.to_csv(FOOTBALL_HISTORY_FILE, index=False)
        logger.info('✅ 足球历史数据: %d 条（%s）', len(df), FOOTBALL_HISTORY_FILE.name)
    return df


def _try_nba_api_fallback() -> pd.DataFrame:
    """当 BallDontLie 不可用时，回退到 sports-skills（免费, 无需 API Key）。"""
    try:
        from fetchers.nba_api import run_sync
        logger.info('  回退到 sports-skills (ESPN)...')
        df = run_sync(days_back=365)
        if df is not None and not df.empty:
            logger.info('  sports-skills 获取 %d 条比赛', len(df))
            return df
    except Exception as e:
        logger.warning('  sports-skills 回退也失败: %s', e)
    return pd.DataFrame()


# ESPN 新增免费联赛（football-data.org 不覆盖）
ESPN_EXTRA_LEAGUES = {
    'MLS': '美国 MLS',
    'LMX': '墨超',
    'BEL': '比甲',
    'TUR': '土超',
    'SCO': '苏超',
    'JLG': 'J联赛',
    'ALE': '澳超',
    'ARG': '阿甲',
    'LIB': '解放者杯',
}

# ESPN 联赛 ID → 内部代码
ESPN_TO_CODE = {
    'mls': 'MLS', 'liga-mx': 'LMX', 'belgian-pro-league': 'BEL',
    'super-lig': 'TUR', 'scottish-premiership': 'SCO', 'j-league': 'JLG',
    'a-league': 'ALE', 'liga-argentina': 'ARG', 'copa-libertadores': 'LIB',
}


_ESPN_CACHE_FILE = DATA_DIR / ".espn_last_sync"


def _espn_sync_due(max_age_hours: int = 24) -> bool:
    """检查 ESPN 同步是否需要运行（缓存控制）。"""
    if not _ESPN_CACHE_FILE.exists():
        return True
    try:
        age = (datetime.utcnow() - datetime.fromtimestamp(_ESPN_CACHE_FILE.stat().st_mtime)).total_seconds()
        return age > max_age_hours * 3600
    except Exception:
        return True


def supplement_football_espn(output_file: Path = None, *, force: bool = False) -> pd.DataFrame:
    """使用 sports-skills (ESPN) 补充足球数据中的新增联赛。

    不影响 football-data.org 已有的数据，只添加新联赛。
    默认每 24 小时只同步一次，避免阻塞主流程。

    Args:
        output_file: 输出 CSV 文件路径
        force: 强制拉取，忽略缓存
    """
    if not force and not _espn_sync_due():
        logger.info('  ESPN 数据 24h 内已同步，跳过（使用 force=True 强制刷新）')
        return pd.read_csv(FOOTBALL_HISTORY_FILE) if FOOTBALL_HISTORY_FILE.exists() else pd.DataFrame()

    output_file = output_file or FOOTBALL_HISTORY_FILE

    try:
        from fetchers.espn_fb import fetch_all_leagues
    except ImportError:
        logger.warning('  espn_fb 模块不可用，跳过 ESPN 补充')
        return pd.read_csv(output_file) if output_file.exists() else pd.DataFrame()

    logger.info('补充 ESPN 足球数据（新增联赛: %s）...', ', '.join(ESPN_EXTRA_LEAGUES.keys()))
    rate = 0.3  # 缩短间隔，改为 0.3s
    espn_df = fetch_all_leagues(ESPN_TO_CODE, season_year='2025', rate_limit=rate)

    if espn_df.empty:
        logger.info('  ESPN 无新增数据')
        _ESPN_CACHE_FILE.touch()
        return pd.read_csv(output_file) if output_file.exists() else pd.DataFrame()

    # 与现有数据合并
    if output_file.exists():
        old_df = pd.read_csv(output_file)
        old_df['date'] = pd.to_datetime(old_df['date'], utc=True, errors='coerce')
        combined = pd.concat([old_df, espn_df], ignore_index=True)
    else:
        combined = espn_df

    combined = combined.drop_duplicates(subset=['date', 'home', 'away', 'competition'])
    combined = combined.sort_values('date').reset_index(drop=True)
    combined.to_csv(output_file, index=False)
    _ESPN_CACHE_FILE.touch()  # 标记同步时间
    logger.info('✅ ESPN 足球补充: %d 条新增, 总计 %d 行, %d 个联赛',
                len(espn_df), len(combined), combined['competition'].nunique())
    return combined


def update_basketball_history(days_back: int = 365, incremental: bool = True):
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)
    rows = []

    # 读取已有数据去重
    existing = set()
    if incremental and BASKETBALL_HISTORY_FILE.exists():
        old_df = pd.read_csv(BASKETBALL_HISTORY_FILE)
        existing = set(zip(old_df['date'].astype(str), old_df['home'], old_df['away']))
        logger.info('  已有 %d 条篮球历史数据', len(old_df))

    url = 'https://api.balldontlie.io/v1/games'
    headers = {'Authorization': BDL_API_KEY} if BDL_API_KEY else None
    cursor = None
    for attempt in range(3):
        try:
            while True:
                params = {
                    'start_date': start_date.isoformat(),
                    'end_date': end_date.isoformat(),
                    'per_page': 100,
                }
                if cursor:
                    params['cursor'] = cursor
                resp = requests.get(url, params=params, headers=headers, timeout=SPORTS_API_TIMEOUT)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get('Retry-After', 30))
                    logger.warning('BallDontLie 速率限制，等待 %ds...', retry_after)
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                data = resp.json()
                page_data = data.get('data', [])
                if not page_data:
                    break
                for match in page_data:
                    rows.append({
                        'date': match.get('date'),
                        'home': match.get('home_team', {}).get('full_name', ''),
                        'away': match.get('visitor_team', {}).get('full_name', ''),
                        'home_score': match.get('home_team_score'),
                        'away_score': match.get('visitor_team_score'),
                    })
                cursor = data.get('meta', {}).get('next_cursor')
                if not cursor:
                    break
            break  # success, exit retry loop
        except requests.exceptions.HTTPError as e:
            if attempt < 2:
                wait = 10 * (attempt + 1)
                logger.warning('BallDontLie 请求失败 (%s), %ds 后重试...', e, wait)
                time.sleep(wait)
            else:
                logger.warning('BallDontLie 3次重试均失败，切换到 stats.nba.com 回退')
                return _try_nba_api_fallback()

    if not rows:
        logger.info('  BallDontLie 无数据，尝试 stats.nba.com 回退')
        nba_df = _try_nba_api_fallback()
        if nba_df is not None and not nba_df.empty:
            if incremental and BASKETBALL_HISTORY_FILE.exists():
                old_df = pd.read_csv(BASKETBALL_HISTORY_FILE)
                old_df['date'] = _to_date(old_df['date'])
                combined = pd.concat([old_df, nba_df], ignore_index=True)
                combined = combined.drop_duplicates(subset=['date', 'home', 'away'])
                combined = combined.sort_values('date').reset_index(drop=True)
                combined.to_csv(BASKETBALL_HISTORY_FILE, index=False)
                logger.info('✅ 篮球历史数据: %d → %d 条', len(old_df), len(combined))
            else:
                nba_df.to_csv(BASKETBALL_HISTORY_FILE, index=False)
                logger.info('✅ 篮球历史数据: %d 条（NBA API）', len(nba_df))
            return pd.read_csv(BASKETBALL_HISTORY_FILE) if BASKETBALL_HISTORY_FILE.exists() else nba_df
        logger.info('  无新增篮球比赛数据')
        return pd.read_csv(BASKETBALL_HISTORY_FILE) if BASKETBALL_HISTORY_FILE.exists() else pd.DataFrame()

    df = pd.DataFrame(rows)
    df['date'] = _to_date(df['date'])
    df = df.drop_duplicates(subset=['date', 'home', 'away'])
    df = df.sort_values('date').reset_index(drop=True)

    if incremental and BASKETBALL_HISTORY_FILE.exists():
        old_df = pd.read_csv(BASKETBALL_HISTORY_FILE)
        old_df['date'] = _to_date(old_df['date'])
        combined = pd.concat([old_df, df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date', 'home', 'away'])
        combined = combined.sort_values('date').reset_index(drop=True)
        combined.to_csv(BASKETBALL_HISTORY_FILE, index=False)
        logger.info('✅ 篮球历史数据: %d → %d 条', len(old_df), len(combined))
    else:
        df.to_csv(BASKETBALL_HISTORY_FILE, index=False)
        logger.info('✅ 篮球历史数据: %d 条', len(df))
    return df


NFL_CSV_URL = "http://www.habitatring.com/games.csv"


def update_nfl_history(seasons: list = None, incremental: bool = True) -> pd.DataFrame:
    """同步 NFL 历史数据（直接下载 habitatring.com/games.csv，完全免费）。

    Args:
        seasons: 赛季列表，默认 2015-2025（含 spread/total 线的赛季）
        incremental: 是否增量追加（去重）

    Returns:
        NFL 历史比赛 DataFrame
    """
    if seasons is None:
        seasons = list(range(2015, 2026))

    # 读取已有数据去重
    existing = set()
    if incremental and NFL_HISTORY_FILE.exists():
        old_df = pd.read_csv(NFL_HISTORY_FILE)
        existing = set(zip(old_df['date'].astype(str), old_df['home'], old_df['away']))
        logger.info('  已有 %d 条 NFL 历史数据', len(old_df))

    logger.info('  下载 NFL 数据...')
    resp = requests.get(NFL_CSV_URL, timeout=60)
    resp.raise_for_status()

    import io
    sched = pd.read_csv(io.StringIO(resp.text))
    logger.info('  原始数据: %d 行, %d 列', len(sched), len(sched.columns))

    # 过滤赛季
    sched = sched[sched['season'].isin(seasons)].copy()

    # 只保留有比分的比赛
    sched = sched.dropna(subset=['away_score', 'home_score']).copy()
    logger.info('  赛季 %s: %d 行（有比分）', seasons, len(sched))

    rows = []
    for _, row in sched.iterrows():
        gameday = str(row.get('gameday', ''))[:10]
        home = row.get('home_team', '')
        away = row.get('away_team', '')
        key = (gameday, home, away)
        if incremental and key in existing:
            continue
        rows.append({
            'date': gameday,
            'season': row.get('season'),
            'week': row.get('week'),
            'home': home,
            'away': away,
            'home_score': row.get('home_score'),
            'away_score': row.get('away_score'),
            'spread_line': row.get('spread_line'),
            'total_line': row.get('total_line'),
            'roof': row.get('roof'),
            'surface': row.get('surface'),
            'temp': row.get('temp'),
            'wind': row.get('wind'),
            'home_rest': row.get('home_rest'),
            'away_rest': row.get('away_rest'),
            'div_game': row.get('div_game'),
            'stadium': row.get('stadium'),
        })

    if not rows:
        logger.info('  NFL 无新增比赛数据')
        return pd.read_csv(NFL_HISTORY_FILE) if NFL_HISTORY_FILE.exists() else pd.DataFrame()

    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.drop_duplicates(subset=['date', 'home', 'away'])
    df = df.sort_values('date').reset_index(drop=True)

    if incremental and NFL_HISTORY_FILE.exists():
        old_df = pd.read_csv(NFL_HISTORY_FILE)
        old_df['date'] = pd.to_datetime(old_df['date'], errors='coerce')
        combined = pd.concat([old_df, df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date', 'home', 'away'])
        combined = combined.sort_values('date').reset_index(drop=True)
        combined.to_csv(NFL_HISTORY_FILE, index=False)
        logger.info('✅ NFL 历史数据: %d → %d 条（%s）', len(old_df), len(combined), NFL_HISTORY_FILE.name)
    else:
        df.to_csv(NFL_HISTORY_FILE, index=False)
        logger.info('✅ NFL 历史数据: %d 条（%s）', len(df), NFL_HISTORY_FILE.name)

    return df
