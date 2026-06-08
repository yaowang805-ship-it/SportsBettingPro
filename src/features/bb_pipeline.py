import pandas as pd, numpy as np
from pathlib import Path
from datetime import datetime, timezone
import sys

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)


def _load_injury_features(injury_df=None):
    """将当前伤病数据转为球队级特征字典。

    返回: {team_abbr: {'injured_count': n, 'injured_stars': n}}
    或空字典（无伤病数据时）。
    """
    if injury_df is None:
        try:
            from src.features.nba_injuries import get_nba_injuries
            injuries = get_nba_injuries()
            injury_df = pd.DataFrame(injuries)
        except Exception:
            return {}

    if injury_df is None or injury_df.empty:
        return {}

    # 按球队统计缺阵人数（只统计 Out / Doubtful）
    confirmed = injury_df[injury_df['status'].str.lower().isin(['out', 'doubtful'])] if 'status' in injury_df.columns else injury_df
    team_counts = confirmed.groupby('team').size().to_dict()
    return {team: {'injured_count': team_counts.get(team, 0), 'injured_stars': 0} for team in set(injury_df.get('team', []))}


def _injury_to_features(team_name, injury_map, team_abbr_map):
    """将球队全名映射为伤病特征值。"""
    if not injury_map:
        return 0, 0
    abbr = team_abbr_map.get(team_name.strip().lower(), '')
    info = injury_map.get(abbr, {})
    return info.get('injured_count', 0), info.get('injured_stars', 0)


def _slope(y):
    """Linear regression slope of a window. >0 = improving trend."""
    if len(y) < 2:
        return 0.0
    x = np.arange(len(y))
    return np.polyfit(x, y, 1)[0]


# NBA 球队主场城市坐标（纬度, 经度），用于计算行程距离
_NBA_CITY_COORDS = {
    "Atlanta Hawks": (33.7490, -84.3880),
    "Boston Celtics": (42.3601, -71.0589),
    "Brooklyn Nets": (40.6782, -73.9442),
    "Charlotte Hornets": (35.2271, -80.8431),
    "Chicago Bulls": (41.8781, -87.6298),
    "Cleveland Cavaliers": (41.4993, -81.6944),
    "Dallas Mavericks": (32.7767, -96.7970),
    "Denver Nuggets": (39.7392, -104.9903),
    "Detroit Pistons": (42.3314, -83.0458),
    "Golden State Warriors": (37.7749, -122.4194),
    "Houston Rockets": (29.7604, -95.3698),
    "Indiana Pacers": (39.7684, -86.1581),
    "LA Clippers": (34.0522, -118.2437),
    "Los Angeles Lakers": (34.0522, -118.2437),
    "Memphis Grizzlies": (35.1495, -90.0490),
    "Miami Heat": (25.7617, -80.1918),
    "Milwaukee Bucks": (43.0389, -87.9065),
    "Minnesota Timberwolves": (44.9778, -93.2650),
    "New Orleans Pelicans": (29.9511, -90.0715),
    "New York Knicks": (40.7128, -74.0060),
    "Oklahoma City Thunder": (35.4676, -97.5164),
    "Orlando Magic": (28.5383, -81.3792),
    "Philadelphia 76ers": (39.9526, -75.1652),
    "Phoenix Suns": (33.4484, -112.0740),
    "Portland Trail Blazers": (45.5152, -122.6784),
    "Sacramento Kings": (38.5816, -121.4944),
    "San Antonio Spurs": (29.4241, -98.4936),
    "Toronto Raptors": (43.6532, -79.3832),
    "Utah Jazz": (40.7608, -111.8910),
    "Washington Wizards": (38.9072, -77.0369),
}


def _haversine(lat1, lon1, lat2, lon2):
    """计算两点间球面距离（单位：英里）。"""
    R = 3958.8  # 地球半径（英里）
    dlat = np.radians(lat2 - lat1)
    dlon = np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(np.radians(lat1)) * np.cos(np.radians(lat2)) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def _compute_travel_distances(team_df):
    """为单支球队的比赛序列计算行程距离。

    输入 team_df 必须包含: date, opponent, is_home, team 列，按 date 排序。
    返回: 每场比赛到上一场比赛场地的距离（第一场为 0）。
    """
    distances = [0.0]
    for i in range(1, len(team_df)):
        prev = team_df.iloc[i - 1]
        curr = team_df.iloc[i]

        # 上一场比赛结束后球队所在位置
        if prev["is_home"]:
            prev_loc = _NBA_CITY_COORDS.get(prev["team"], (0, 0))
        else:
            prev_loc = _NBA_CITY_COORDS.get(prev["opponent"], (0, 0))

        # 当前比赛球队所在位置
        if curr["is_home"]:
            curr_loc = _NBA_CITY_COORDS.get(curr["team"], (0, 0))
        else:
            curr_loc = _NBA_CITY_COORDS.get(curr["opponent"], (0, 0))

        if prev_loc != (0, 0) and curr_loc != (0, 0):
            d = _haversine(prev_loc[0], prev_loc[1], curr_loc[0], curr_loc[1])
        else:
            d = 0.0
        distances.append(d)
    return distances


def _process_team_stats(df):
    """从比赛记录构建球队级滚动统计。"""
    home = df[['date', 'home', 'away', 'home_score', 'away_score']].copy()
    home.columns = ['date', 'team', 'opponent', 'gf', 'ga']
    home['is_home'] = 1
    away = df[['date', 'away', 'home', 'away_score', 'home_score']].copy()
    away.columns = ['date', 'team', 'opponent', 'gf', 'ga']
    away['is_home'] = 0
    team = pd.concat([home, away], ignore_index=True).sort_values(['team', 'date'])

    for w in [3, 10]:
        team[f'gf_avg_{w}'] = team.groupby('team')['gf'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'ga_avg_{w}'] = team.groupby('team')['ga'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'net_rating_{w}'] = team[f'gf_avg_{w}'] - team[f'ga_avg_{w}']

    team['gf_ewm5'] = team.groupby('team')['gf'].transform(
        lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    team['ga_ewm5'] = team.groupby('team')['ga'].transform(
        lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    team['opp_def_strength'] = team['ga_ewm5']
    team['is_win'] = (team['gf'] > team['ga']).astype(int)
    team['win_rate_10'] = team.groupby('team')['is_win'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    team['rest_days'] = team.groupby('team')['date'].diff().dt.days.fillna(3)
    team['b2b'] = (team['rest_days'] == 1).astype(int)

    # 趋势斜率特征（动量）：net_rating 的线性趋势，正值表示状态上升
    for w in [5, 10]:
        team[f'net_rating_slope_{w}'] = team.groupby('team')['net_rating_3'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=2).apply(_slope, raw=True))

    # ── 总分预测专用特征 ──
    team['total_pts'] = team['gf'] + team['ga']
    # 滚动5场平均总分（team's games average total points）
    team['total_pts_avg_5'] = team.groupby('team')['total_pts'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    # EWMA 总分（偏重新近比赛）
    team['total_pts_ewm5'] = team.groupby('team')['total_pts'].transform(
        lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    # 滚动5场平均得分和失分（窗口5，介于3/10之间）
    team['pts_scored_avg_5'] = team.groupby('team')['gf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    team['pts_allowed_avg_5'] = team.groupby('team')['ga'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    # 总分波动率（高波动 → 更可能极端值）
    team['total_pts_volatility_5'] = team.groupby('team')['total_pts'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=3).std())
    # 大分率：近期比赛总分高于联盟平均(210分)的比率
    LEAGUE_AVG_TOTAL = 210.0
    team['is_high_scoring'] = (team['total_pts'] > LEAGUE_AVG_TOTAL).astype(int)
    team['high_score_rate_10'] = team.groupby('team')['is_high_scoring'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    # ── 行程距离特征 ──
    team['travel_distance'] = team.groupby('team', group_keys=True).apply(
        lambda g: pd.Series(_compute_travel_distances(g), index=g.index)
    ).droplevel(0) if len(team) > 0 else 0.0
    team['travel_distance'] = team['travel_distance'].fillna(0).astype(float)
    # 累计行程（过去 3 场），捕捉长途奔波累积疲劳
    team['travel_distance_3'] = team.groupby('team')['travel_distance'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).sum())
    team['travel_distance_5'] = team.groupby('team')['travel_distance'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).sum())

    feat_cols = ['date', 'team',
                 'gf_avg_3', 'gf_avg_10',
                 'ga_avg_3', 'ga_avg_10',
                 'net_rating_3', 'net_rating_10',
                 'gf_ewm5', 'ga_ewm5', 'opp_def_strength', 'win_rate_10', 'rest_days', 'b2b',
                 'net_rating_slope_5', 'net_rating_slope_10',
                 'total_pts_avg_5', 'total_pts_ewm5',
                 'pts_scored_avg_5', 'pts_allowed_avg_5',
                 'total_pts_volatility_5', 'high_score_rate_10',
                 'travel_distance', 'travel_distance_3', 'travel_distance_5']
    return team[feat_cols]


# NBA 球队名称缩写 → 全名（用于 nba_betting_*.csv）
NBA_ABBR_TO_FULL = {
    'atl': 'Atlanta Hawks', 'bkn': 'Brooklyn Nets', 'bos': 'Boston Celtics',
    'cha': 'Charlotte Hornets', 'chi': 'Chicago Bulls', 'cle': 'Cleveland Cavaliers',
    'dal': 'Dallas Mavericks', 'den': 'Denver Nuggets', 'det': 'Detroit Pistons',
    'gs': 'Golden State Warriors', 'hou': 'Houston Rockets', 'ind': 'Indiana Pacers',
    'lac': 'LA Clippers', 'lal': 'Los Angeles Lakers', 'mem': 'Memphis Grizzlies',
    'mia': 'Miami Heat', 'mil': 'Milwaukee Bucks', 'min': 'Minnesota Timberwolves',
    'no': 'New Orleans Pelicans', 'ny': 'New York Knicks', 'okc': 'Oklahoma City Thunder',
    'orl': 'Orlando Magic', 'phi': 'Philadelphia 76ers', 'phx': 'Phoenix Suns',
    'por': 'Portland Trail Blazers', 'sa': 'San Antonio Spurs', 'sac': 'Sacramento Kings',
    'tor': 'Toronto Raptors', 'utah': 'Utah Jazz', 'wsh': 'Washington Wizards',
}

# NBA 球队名称简称映射（用于伤病查询）
NBA_TEAM_ABBR_MAP = {
    "atlanta hawks": "ATL", "boston celtics": "BOS", "brooklyn nets": "BKN",
    "charlotte hornets": "CHA", "chicago bulls": "CHI", "cleveland cavaliers": "CLE",
    "dallas mavericks": "DAL", "denver nuggets": "DEN", "detroit pistons": "DET",
    "golden state warriors": "GSW", "houston rockets": "HOU", "indiana pacers": "IND",
    "los angeles clippers": "LAC", "los angeles lakers": "LAL", "memphis grizzlies": "MEM",
    "miami heat": "MIA", "milwaukee bucks": "MIL", "minnesota timberwolves": "MIN",
    "new orleans pelicans": "NOP", "new york knicks": "NYK", "oklahoma city thunder": "OKC",
    "orlando magic": "ORL", "philadelphia 76ers": "PHI", "phoenix suns": "PHX",
    "portland trail blazers": "POR", "sacramento kings": "SAC", "san antonio spurs": "SAS",
    "toronto raptors": "TOR", "utah jazz": "UTA", "washington wizards": "WAS",
}

# nba_scores.csv 旧版短名 → 全名（用于统一名称、去重）
NBA_LEGACY_NAME_MAP = {
    'atlanta': 'Atlanta Hawks', 'boston': 'Boston Celtics', 'brooklyn': 'Brooklyn Nets',
    'charlotte': 'Charlotte Hornets', 'chicago': 'Chicago Bulls', 'cleveland': 'Cleveland Cavaliers',
    'dallas': 'Dallas Mavericks', 'denver': 'Denver Nuggets', 'detroit': 'Detroit Pistons',
    'golden state': 'Golden State Warriors', 'houston': 'Houston Rockets',
    'indiana': 'Indiana Pacers', 'l.a. clippers': 'LA Clippers',
    'l.a. lakers': 'Los Angeles Lakers', 'memphis': 'Memphis Grizzlies',
    'miami': 'Miami Heat', 'milwaukee': 'Milwaukee Bucks',
    'minnesota': 'Minnesota Timberwolves', 'new orleans': 'New Orleans Pelicans',
    'new york': 'New York Knicks', 'oklahoma city': 'Oklahoma City Thunder',
    'orlando': 'Orlando Magic', 'philadelphia': 'Philadelphia 76ers',
    'phoenix': 'Phoenix Suns', 'portland': 'Portland Trail Blazers',
    'sacramento': 'Sacramento Kings', 'san antonio': 'San Antonio Spurs',
    'toronto': 'Toronto Raptors', 'utah': 'Utah Jazz', 'washington': 'Washington Wizards',
}


def build_bb_features(
    legacy_csv="data/storage/nba_scores.csv",
    modern_csv="data/storage/basketball_history.csv",
    spread_csv="data/storage/nba_spread_results.csv",
    betting_csv="data/raw/nba_betting_2007_2026.csv",
    output_csv="data/processed/bb_features.csv"
):
    """增强的篮球特征管道。

    从历史数据（nba_scores.csv，含盘口）、现代数据（basketball_history.csv，不含盘口）
    和盘口匹配数据（nba_spread_results.csv）三个来源构建特征。

    盘口匹配数据通过将 Odds API 的 spreads/totals 与实际比分匹配得到，
    填补了现代数据中 spread_result / total_result 的空白。
    """
    # ── 1. 加载伤病数据（实时，用于注入特征名称） ──
    injury_map = _load_injury_features()

    # ── 2. 加载历史数据（含盘口） ──
    legacy_path = Path(legacy_csv)
    if legacy_path.exists():
        old = pd.read_csv(legacy_csv)
        old.columns = [c.strip().lower() for c in old.columns]
        old['date'] = pd.to_datetime(old['dateslash'])
        old['home'] = old['team'].str.strip()
        old['away'] = old['oppteam'].str.strip()
        # 统一旧版短名为全名（"Utah" → "Utah Jazz"）以支持去重
        old['home'] = old['home'].str.lower().map(NBA_LEGACY_NAME_MAP).fillna(old['home'])
        old['away'] = old['away'].str.lower().map(NBA_LEGACY_NAME_MAP).fillna(old['away'])
        old['home_score'] = pd.to_numeric(old['teampts'], errors='coerce')
        old['away_score'] = pd.to_numeric(old['opppts'], errors='coerce')
        old['teamsprd'] = pd.to_numeric(old['teamsprd'], errors='coerce')
        old['ovrundr'] = pd.to_numeric(old['ovrundr'], errors='coerce')
        old['win'] = (old['home_score'] > old['away_score']).astype(int)
        old['home_goals'] = old['home_score']
        old['away_goals'] = old['away_score']
        # 自动注入伤病特征（历史数据无法获取实时伤病，填0）
        old['home_injured'] = 0
        old['away_injured'] = 0
        old['home_injured_stars'] = 0
        old['away_injured_stars'] = 0
    else:
        old = pd.DataFrame()

    # ── 3. 加载现代数据（2026 赛季，通过 BallDontLie 获取） ──
    modern_path = Path(modern_csv)
    if modern_path.exists():
        new = pd.read_csv(modern_csv)
        new.columns = [c.strip().lower() for c in new.columns]
        new['date'] = pd.to_datetime(new['date'], utc=True, format='mixed').dt.tz_localize(None)
        new['home'] = new['home'].str.strip()
        new['away'] = new['away'].str.strip()
        new['home_score'] = pd.to_numeric(new['home_score'], errors='coerce')
        new['away_score'] = pd.to_numeric(new['away_score'], errors='coerce')
        new['win'] = (new['home_score'] > new['away_score']).astype(int)
        new['home_goals'] = new['home_score']
        new['away_goals'] = new['away_score']
        # 现代数据无盘口信息，标记为 NaN
        new['teamsprd'] = np.nan
        new['ovrundr'] = np.nan

        # ── 3a. 从 nba_spread_results.csv 注入盘口数据（自动构建） ──
        spread_path = Path(spread_csv)
        if not spread_path.exists():
            try:
                from src.features.nba_spread_tracker import build_spread_results
                build_spread_results(output_csv=spread_csv)
            except Exception as e:
                print(f"  ⚠️ 自动构建 nba_spread_results 失败: {e}")

        if spread_path.exists():
            spr = pd.read_csv(spread_csv)
            spr.columns = [c.strip().lower() for c in spr.columns]
            spr['match_key'] = (spr['date'].astype(str) + '|'
                                + spr['home'].str.strip().str.lower() + '|'
                                + spr['away'].str.strip().str.lower())
            spr_lookup = spr.set_index('match_key')[['spread_point', 'total_point']].to_dict('index')

            new['match_key'] = (new['date'].dt.strftime('%Y-%m-%d') + '|'
                                + new['home'].str.lower() + '|'
                                + new['away'].str.lower())
            matched = new['match_key'].isin(spr_lookup)
            new.loc[matched, 'teamsprd'] = new.loc[matched, 'match_key'].map(
                lambda k: spr_lookup[k]['spread_point'])
            new.loc[matched, 'ovrundr'] = new.loc[matched, 'match_key'].map(
                lambda k: spr_lookup[k]['total_point'])
            n_matched = matched.sum()
            if n_matched > 0:
                print(f"   ├─ 盘口匹配数据: {n_matched} 场（2025-26 赛季 spreads/totals）")
            new.drop(columns='match_key', inplace=True)
        # 如果当前有伤病数据，注入
        if injury_map:
            new['home_injured'] = new['home'].str.lower().map(
                lambda x: injury_map.get(NBA_TEAM_ABBR_MAP.get(x, ''), {}).get('injured_count', 0))
            new['away_injured'] = new['away'].str.lower().map(
                lambda x: injury_map.get(NBA_TEAM_ABBR_MAP.get(x, ''), {}).get('injured_count', 0))
            new['home_injured_stars'] = new['home'].str.lower().map(
                lambda x: injury_map.get(NBA_TEAM_ABBR_MAP.get(x, ''), {}).get('injured_stars', 0))
            new['away_injured_stars'] = new['away'].str.lower().map(
                lambda x: injury_map.get(NBA_TEAM_ABBR_MAP.get(x, ''), {}).get('injured_stars', 0))
        else:
            new['home_injured'] = 0
            new['away_injured'] = 0
            new['home_injured_stars'] = 0
            new['away_injured_stars'] = 0
    else:
        new = pd.DataFrame()

    # ── 3b. 加载 nba_betting 历史数据集（2007-2026, 24K+ 行, 含盘口） ──
    betting_path = Path(betting_csv)
    if betting_path.exists():
        bet = pd.read_csv(betting_csv)
        bet.columns = [c.strip().lower() for c in bet.columns]
        bet['date'] = pd.to_datetime(bet['date'])
        # 统一队名：缩写（atl → Atlanta Hawks）或已全名 → 标准全名
        bet['home'] = bet['home'].map(NBA_ABBR_TO_FULL).fillna(bet['home'])
        bet['away'] = bet['away'].map(NBA_ABBR_TO_FULL).fillna(bet['away'])
        bet = bet.dropna(subset=['home', 'away']).copy()
        bet['home_score'] = pd.to_numeric(bet['score_home'], errors='coerce')
        bet['away_score'] = pd.to_numeric(bet['score_away'], errors='coerce')
        bet['home_goals'] = bet['home_score']
        bet['away_goals'] = bet['away_score']
        bet['teamsprd'] = pd.to_numeric(bet['spread'], errors='coerce')
        bet['ovrundr'] = pd.to_numeric(bet['total'], errors='coerce')
        bet['win'] = (bet['home_score'] > bet['away_score']).astype(int)
        bet['home_injured'] = 0
        bet['away_injured'] = 0
        bet['home_injured_stars'] = 0
        bet['away_injured_stars'] = 0
        print(f"  📊 nba_betting: {len(bet)} 场 ({bet['date'].dt.year.min()}~{bet['date'].dt.year.max()}, 含盘口 {bet['teamsprd'].notna().sum()} 场)")
    else:
        bet = pd.DataFrame()

    # ── 4. 合并三个来源（优先级: new > old > bet, 按 (date, home, away) 去重） ──
    if old.empty and new.empty and bet.empty:
        raise FileNotFoundError(f"未找到任何数据源：{legacy_csv}, {modern_csv}, {betting_csv}")

    common_cols = ['date', 'home', 'away', 'win', 'home_score', 'away_score',
                   'home_goals', 'away_goals', 'teamsprd', 'ovrundr',
                   'home_injured', 'away_injured',
                   'home_injured_stars', 'away_injured_stars',
                   'home_odds', 'away_odds']
    parts = []
    for src in [new, old, bet]:
        if not src.empty:
            for c in common_cols:
                if c not in src.columns:
                    src[c] = 0 if c in ('home_injured', 'away_injured', 'home_injured_stars', 'away_injured_stars') else np.nan
            parts.append(src[common_cols].copy())

    df = pd.concat(parts, ignore_index=True)
    df = df.drop_duplicates(subset=['date', 'home', 'away'], keep='first')
    df = df.dropna(subset=['date']).copy()
    df = df.sort_values('date').reset_index(drop=True)

    # ── 5. ELO 评级特征 ──
    from src.features.elo import compute_elo
    nba_teams = set(df['home'].unique()) | set(df['away'].unique())
    df = compute_elo(df, K=20)

    # ── 6. 构建球队级统计特征 ──
    team_feats = _process_team_stats(df)

    # ── 6. 将特征合并到比赛行 ──
    match_df = df[['date', 'home', 'away', 'win', 'home_goals', 'away_goals',
                   'teamsprd', 'ovrundr', 'home_injured', 'away_injured',
                   'home_injured_stars', 'away_injured_stars',
                   'home_elo', 'away_elo', 'elo_diff',
                   'home_odds', 'away_odds']].copy()

    for side, team_col in [('home', 'home'), ('away', 'away')]:
        sf = team_feats.copy()
        sf.columns = [f'{side}_{c}' if c not in ['date', 'team'] else c for c in sf.columns]
        sf.rename(columns={'date': 'date', 'team': team_col}, inplace=True)
        match_df = pd.merge_asof(match_df.sort_values('date'), sf.sort_values('date'),
                                 by=team_col, on='date', direction='backward')

    match_df['off_vs_def'] = match_df['home_gf_ewm5'] - match_df['away_ga_ewm5']
    match_df['b2b_diff'] = match_df['home_b2b'] - match_df['away_b2b']
    match_df['injured_diff'] = match_df['home_injured'] - match_df['away_injured']
    match_df['rest_diff'] = match_df['home_rest_days'] - match_df['away_rest_days']
    match_df['travel_diff'] = match_df['home_travel_distance'] - match_df['away_travel_distance']
    match_df['travel_3_diff'] = match_df['home_travel_distance_3'] - match_df['away_travel_distance_3']

    # ── 总分预测交互特征 ──
    match_df['combined_total_avg_5'] = match_df['home_total_pts_avg_5'] + match_df['away_total_pts_avg_5']
    match_df['combined_pts_scored_avg_5'] = match_df['home_pts_scored_avg_5'] + match_df['away_pts_scored_avg_5']
    match_df['combined_pts_allowed_avg_5'] = match_df['home_pts_allowed_avg_5'] + match_df['away_pts_allowed_avg_5']
    match_df['total_volatility_interaction'] = match_df['home_total_pts_volatility_5'] * match_df['away_total_pts_volatility_5']
    match_df['high_score_rate_sum'] = (match_df['home_high_score_rate_10'] + match_df['away_high_score_rate_10']).clip(0, 2)
    match_df['total_rest_sum'] = match_df['home_rest_days'] + match_df['away_rest_days']
    # pace 代理特征：两队场均得分之和反映了比赛节奏
    match_df['pace_proxy'] = (match_df['home_gf_ewm5'] + match_df['away_gf_ewm5'] +
                              match_df['home_ga_ewm5'] + match_df['away_ga_ewm5']) / 2

    # ── 历史市场概率特征（来自 nba_betting 盘口数据） ──
    odds_h = pd.to_numeric(match_df['home_odds'], errors='coerce')
    odds_a = pd.to_numeric(match_df['away_odds'], errors='coerce')
    match_df['hist_market_home_prob'] = (1.0 / odds_h.clip(lower=1.01)).fillna(0.5)
    match_df['hist_market_away_prob'] = (1.0 / odds_a.clip(lower=1.01)).fillna(0.5)
    match_df['hist_market_edge'] = match_df['hist_market_home_prob'] - 0.5

    # ── 预测 Edge 特征（CLV 代理） ──
    try:
        from src.monitor.clv_tracker import compute_team_edge_features
        from src.core.team_names import cn_to_feature_name

        edges = compute_team_edge_features(sport="nba")
        if edges:
            # 将中文名映射为特征名
            eng_edges = {}
            for cn, e in edges.items():
                en = cn_to_feature_name(cn, sport="nba")
                eng_edges[en] = e

            # 按比赛时间对齐（用 asof merge 避免未来信息泄露）
            edge_df = pd.DataFrame({
                'date': match_df['date'],
                'home_edge': match_df['home'].str.lower().map(eng_edges),
                'away_edge': match_df['away'].str.lower().map(eng_edges),
            })
            match_df['home_pred_edge'] = edge_df['home_edge'].fillna(0)
            match_df['away_pred_edge'] = edge_df['away_edge'].fillna(0)
            match_df['pred_edge_diff'] = match_df['home_pred_edge'] - match_df['away_pred_edge']
    except Exception as e:
        match_df['home_pred_edge'] = 0.0
        match_df['away_pred_edge'] = 0.0
        match_df['pred_edge_diff'] = 0.0

    # ── 天气与行程距离特征 ──
    from src.features.weather_features import add_weather_to_df, get_weather_feature_names
    try:
        match_df = add_weather_to_df(match_df)
        for wf in get_weather_feature_names():
            if wf not in match_df.columns:
                match_df[wf] = 0.0
    except Exception as e:
        print(f"  ⚠️ 天气特征注入失败: {e}")
        for wf in get_weather_feature_names():
            match_df[wf] = 0.0

    # 仅填充特征列，不填充盘口原始列（避免将2015年的盘口前向填充到2026年）
    # 注意：ovrundr 作为 total 模型的特征保留（但 teamsprd 仍排除）
    feature_like = [c for c in match_df.columns if c not in ('teamsprd',)]
    match_df[feature_like] = match_df[feature_like].fillna(0)

    # 盘口结果（仅在盘口数据存在时有效）
    has_spread = match_df['teamsprd'].notna() & (match_df['teamsprd'] != 0)
    match_df['spread_result'] = np.where(
        has_spread,
        (match_df['home_goals'] + match_df['teamsprd'] > match_df['away_goals']).astype(int),
        np.nan
    )
    has_total = match_df['ovrundr'].notna() & (match_df['ovrundr'] > 0)
    match_df['total_result'] = np.where(
        has_total,
        (match_df['home_goals'] + match_df['away_goals'] > match_df['ovrundr']).astype(int),
        np.nan
    )

    if output_csv:
        match_df.to_csv(output_csv, index=False)
    n_with_spread = match_df['teamsprd'].notna().sum()
    n_with_total = match_df['ovrundr'].notna().sum()
    print(f"✅ 篮球特征生成完成：{len(match_df)} 场，{match_df.shape[1]} 列")
    if not old.empty:
        print(f"   ├─ legacy: {len(old)} 场（nba_scores.csv）")
    if not new.empty:
        print(f"   ├─ modern: {len(new)} 场（basketball_history.csv）")
    if bet is not None and not bet.empty:
        print(f"   ├─ betting: {len(bet)} 场（nba_betting_2007_2026.csv）")
    print(f"   ├─ 含 spread 标签: {n_with_spread} 场")
    print(f"   └─ 含 total 标签:  {n_with_total} 场")
    return match_df


if __name__ == '__main__':
    build_bb_features()
