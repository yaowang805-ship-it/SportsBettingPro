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

FOOTBALL_HISTORY_FILE = DATA_DIR / 'football_history.csv'
BASKETBALL_HISTORY_FILE = DATA_DIR / 'basketball_history.csv'

FOOTBALL_LEAGUES = {
    'PL': '英超',
    'PD': '西甲',
    'BL1': '德甲',
    'SA': '意甲',
    'FL1': '法甲',
}


def _to_date(value):
    return pd.to_datetime(value, utc=True, errors='coerce').dt.tz_localize(None)


def _fetch_json(url, headers=None, params=None):
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=SPORTS_API_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.SSLError:
        print(f'⚠️ SSL 连接失败，尝试 verify=False 重试：{url}')
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


def update_football_history(days_back: int = 120, leagues: dict = None):
    if not FOOTBALL_API_KEY:
        raise RuntimeError('未配置 football-data.org API Key，请设置 FOOTBALL_API_KEY')
    leagues = leagues or FOOTBALL_LEAGUES
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)

    rows = []
    for code in leagues:
        url = f'https://api.football-data.org/v4/competitions/{code}/matches'
        params = {
            'dateFrom': start_date.isoformat(),
            'dateTo': end_date.isoformat(),
        }
        headers = {'X-Auth-Token': FOOTBALL_API_KEY}
        data = _fetch_json(url, headers=headers, params=params)
        for match in data.get('matches', []):
            status = match.get('status')
            if status != 'FINISHED':
                continue
            score = match.get('score', {}).get('fullTime', {})
            home_goals = score.get('home')
            away_goals = score.get('away')
            if home_goals is None or away_goals is None:
                continue
            rows.append({
                'date': match.get('utcDate'),
                'home': match.get('homeTeam', {}).get('name', ''),
                'away': match.get('awayTeam', {}).get('name', ''),
                'home_goals': home_goals,
                'away_goals': away_goals,
                'competition': code,
            })

    if not rows:
        raise RuntimeError('football-data.org 未获取到有效比赛结果')

    df = pd.DataFrame(rows)
    df['date'] = _to_date(df['date'])
    df = df.drop_duplicates(subset=['date', 'home', 'away'])
    df = df.sort_values('date').reset_index(drop=True)
    df.to_csv(FOOTBALL_HISTORY_FILE, index=False)
    print(f'✅ 已同步足球历史数据：{len(df)} 条，保存至 {FOOTBALL_HISTORY_FILE}')
    return df


def update_basketball_history(days_back: int = 120):
    end_date = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days_back)
    rows = []

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
                    print(f'⚠️ BallDontLie 速率限制，等待 {retry_after}s...')
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
                print(f'⚠️ BallDontLie 请求失败 ({e}), {wait}s 后重试...')
                time.sleep(wait)
            else:
                raise RuntimeError(f'BallDontLie 拉取失败: {e}')

    if not rows:
        raise RuntimeError('BallDontLie 未获取到有效比赛结果')

    df = pd.DataFrame(rows)
    df['date'] = _to_date(df['date'])
    df = df.drop_duplicates(subset=['date', 'home', 'away'])
    df = df.sort_values('date').reset_index(drop=True)
    df.to_csv(BASKETBALL_HISTORY_FILE, index=False)
    print(f'✅ 已同步篮球历史数据：{len(df)} 条，保存至 {BASKETBALL_HISTORY_FILE}')
    return df
