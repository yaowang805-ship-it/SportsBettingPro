"""ESPN 足球数据抓取器 — 使用 sports-skills (免费, 无需 API Key)。

数据源:
  - sports-skills → ESPN API, 覆盖 30+ 足球联赛
  - 完全免费, 零配置, 无请求次数限制
  - 提供: 赛程/赛果/进球/xG/阵容/伤病(FPL)

用法:
    from fetchers.espn_fb import fetch_league_schedule, fetch_all_leagues, fetch_xg
    df = fetch_league_schedule('premier-league', '2025')
    all_df = fetch_all_leagues()
"""
import sys, time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import DATA_DIR

# ESPN 联赛 ID → 内部联赛代码
# 当前 football-data.org 联赛 + 新增免费联赛
ESPN_LEAGUES = {
    # 五大联赛（已覆盖）
    'premier-league': 'PL',
    'la-liga': 'PD',
    'bundesliga': 'BL1',
    'serie-a': 'SA',
    'ligue-1': 'FL1',
    # 二级联赛（已覆盖）
    'championship': 'ELC',
    'serie-a-brazil': 'BSA',
    'primeira-liga': 'PPL',
    'eredivisie': 'DED',
    'champions-league': 'CL',
    'europa-league': 'EL',
    # 新增免费联赛
    'mls': 'MLS',
    'liga-mx': 'LMX',
    'belgian-pro-league': 'BEL',
    'super-lig': 'TUR',
    'scottish-premiership': 'SCO',
    'j-league': 'JLG',
    'a-league': 'ALE',
    'liga-argentina': 'ARG',
    'copa-libertadores': 'LIB',
}

# 反向映射: 内部代码 → ESPN ID
CODE_TO_ESPN = {v: k for k, v in ESPN_LEAGUES.items()}


def _extract_competitors(event: dict) -> tuple:
    """从 event 中提取主客队信息。"""
    competitors = event.get('competitors', [])
    home = away = None
    home_score = away_score = None
    for c in competitors:
        qualifier = c.get('qualifier', '')
        team_name = c.get('team', {}).get('name', '')
        score = c.get('score')
        if qualifier == 'home':
            home = team_name
            home_score = score
        elif qualifier == 'away':
            away = team_name
            away_score = score

    # 如果 qualifier 不标准, 按顺序: 第一个=home, 第二个=away
    if not home and competitors:
        home = competitors[0].get('team', {}).get('name', '')
        home_score = competitors[0].get('score')
    if not away and len(competitors) > 1:
        away = competitors[1].get('team', {}).get('name', '')
        away_score = competitors[1].get('score')

    return home, away, home_score, away_score


def _event_to_row(event: dict, league_code: str) -> Optional[dict]:
    """将 ESPN 事件转换为标准格式行。"""
    if event.get('status') != 'closed':
        return None

    scores = event.get('scores', {})
    home_score = scores.get('home')
    away_score = scores.get('away')
    if home_score is None or away_score is None:
        # 尝试从 competitors 提取
        _, _, hs, aw = _extract_competitors(event)
        home_score = home_score if home_score is not None else hs
        away_score = away_score if away_score is not None else aw

    if home_score is None or away_score is None:
        return None

    home, away, _, _ = _extract_competitors(event)
    if not home or not away:
        return None

    return {
        'date': event.get('start_time', ''),
        'home': home,
        'away': away,
        'home_goals': int(home_score) if home_score is not None else None,
        'away_goals': int(away_score) if away_score is not None else None,
        'competition': league_code,
    }


def fetch_league_schedule(league_id: str, season_year: str = '2025') -> pd.DataFrame:
    """获取指定联赛整个赛季的比赛数据。

    Args:
        league_id: ESPN 联赛 ID, 如 'premier-league', 'la-liga'
        season_year: 赛季年份, 如 '2025' 代表 2025/26 赛季

    Returns:
        DataFrame: date, home, away, home_goals, away_goals, competition
    """
    try:
        from sports_skills import football

        season_id = f'{league_id}-{season_year}'
        logger.info('  拉取 %s ...', season_id)
        result = football.get_season_schedule(season_id=season_id)

        if not result.get('status'):
            logger.warning('  %s 无数据: %s', season_id, result.get('message', ''))
            return pd.DataFrame()

        events = result.get('data', {}).get('schedules', [])
        if not events:
            logger.info('  %s: 空赛程', season_id)
            return pd.DataFrame()

        league_code = ESPN_LEAGUES.get(league_id, league_id)
        rows = []
        for e in events:
            row = _event_to_row(e, league_code)
            if row is not None:
                rows.append(row)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'], utc=True, errors='coerce')
        df = df.dropna(subset=['date', 'home', 'away'])
        df = df.sort_values('date').reset_index(drop=True)
        logger.info('  %s (%s): %d 场比赛', league_id, league_code, len(df))
        return df

    except ImportError:
        logger.error('  sports-skills 未安装, 请执行: pip install sports-skills')
        return pd.DataFrame()
    except Exception as e:
        logger.warning('  %s 拉取失败: %s', league_id, e)
        return pd.DataFrame()


def fetch_all_leagues(leagues: dict = None, season_year: str = '2025',
                      rate_limit: float = 1.0) -> pd.DataFrame:
    """批量拉取所有已配置联赛的历史数据。

    Args:
        leagues: 要拉取的联赛字典 {espn_id: internal_code}, 默认 ESPN_LEAGUES
        season_year: 赛季年份
        rate_limit: 每次请求间隔秒数 (ESPN 频率限制)

    Returns:
        合并后的 DataFrame
    """
    leagues = leagues or ESPN_LEAGUES
    all_dfs = []

    for espn_id in leagues:
        df = fetch_league_schedule(espn_id, season_year)
        if not df.empty:
            all_dfs.append(df)
        time.sleep(rate_limit)  # ESPN 限速

    if not all_dfs:
        logger.info('  无任何联赛数据')
        return pd.DataFrame()

    combined = pd.concat(all_dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset=['date', 'home', 'away', 'competition'])
    combined = combined.sort_values('date').reset_index(drop=True)
    return combined


def fetch_xg(event_id: str) -> Optional[dict]:
    """获取指定比赛的 xG 数据。

    Args:
        event_id: ESPN 比赛 ID (9 位数字)

    Returns:
        {home_xg, away_xg, shots} 或 None
    """
    try:
        from sports_skills import football
        result = football.get_event_xg(event_id=event_id)
        if not result.get('status'):
            return None
        data = result.get('data', {})
        teams = data.get('teams', [])
        if not teams:
            return None
        home_xg = away_xg = None
        for t in teams:
            if t.get('qualifier') == 'home':
                home_xg = t.get('xg', t.get('expectedGoals'))
            elif t.get('qualifier') == 'away':
                away_xg = t.get('xg', t.get('expectedGoals'))
        return {
            'home_xg': home_xg,
            'away_xg': away_xg,
            'shots': data.get('shots', []),
        }
    except Exception as e:
        logger.debug('  xG 获取失败: %s', e)
        return None


def fetch_missing_players(season_id: str = 'premier-league-2025') -> list:
    """获取英超伤病/缺阵名单 (数据源: FPL)。

    Args:
        season_id: 如 'premier-league-2025'

    Returns:
        [{player, team, reason, date}] 列表
    """
    try:
        from sports_skills import football
        result = football.get_missing_players(season_id=season_id)
        if not result.get('status'):
            return []
        data = result.get('data', {})
        teams = data.get('teams', [])
        players = []
        for team in teams:
            team_name = team.get('name', '')
            for p in team.get('players', []):
                players.append({
                    'player': p.get('name', ''),
                    'team': team_name,
                    'reason': p.get('reason', ''),
                    'date': p.get('date', ''),
                })
        return players
    except Exception as e:
        logger.debug('  伤病数据获取失败: %s', e)
        return []


def run_sync(output_path: str = None, leagues: dict = None,
             season_year: str = '2025', incremental: bool = True) -> pd.DataFrame:
    """增量同步 ESPN 足球数据到本地 CSV。

    策略:
      1. 拉取指定联赛的赛季数据
      2. 与已有 football_history.csv 合并去重
      3. 保留 football-data.org 已有数据, 仅补充新增

    Args:
        output_path: 输出 CSV 路径, 默认 football_history.csv
        leagues: 联赛字典, 默认 ESPN_LEAGUES
        season_year: 赛季年份
        incremental: 增量模式 (与已有数据合并)

    Returns:
        合并后的 DataFrame
    """
    output_path = output_path or str(DATA_DIR / 'football_history.csv')
    leagues = leagues or ESPN_LEAGUES

    logger.info('开始同步 ESPN 足球数据（sports-skills）...')

    # 拉取 ESPN 数据
    espn_df = fetch_all_leagues(leagues, season_year)

    if espn_df.empty:
        logger.info('  ESPN 未返回数据, 保持现有文件不变')
        existing = Path(output_path)
        if existing.exists():
            return pd.read_csv(output_path)
        return pd.DataFrame()

    # 与已有数据合并
    existing = Path(output_path)
    if incremental and existing.exists():
        old_df = pd.read_csv(output_path)
        old_df['date'] = pd.to_datetime(old_df['date'], utc=True, errors='coerce')
        logger.info('  已有数据: %d 行', len(old_df))

        combined = pd.concat([old_df, espn_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['date', 'home', 'away', 'competition'])
        combined = combined.sort_values('date').reset_index(drop=True)
        combined.to_csv(output_path, index=False)
        logger.info('✅ ESPN 足球数据: %d → %d 行', len(old_df), len(combined))
        return combined
    else:
        espn_df.to_csv(output_path, index=False)
        logger.info('✅ ESPN 足球数据: %d 行（新建）', len(espn_df))
        return espn_df


if __name__ == '__main__':
    # 测试: 使用临时路径, 不覆盖生产文件
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
    test_leagues = {
        'premier-league': 'PL',
        'la-liga': 'PD',
    }
    df = fetch_all_leagues(test_leagues)
    print(f'\n共 {len(df)} 行数据 (写入 {tmp.name})')
    if not df.empty:
        print(f'日期范围: {df["date"].min()} ~ {df["date"].max()}')
        print(f'联赛分布:\n{df["competition"].value_counts().to_string()}')
        df.to_csv(tmp.name, index=False)
        print(f'已保存到 {tmp.name}')
    else:
        tmp.close()
