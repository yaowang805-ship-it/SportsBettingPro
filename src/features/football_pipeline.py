import json, os, pandas as pd, numpy as np
from pathlib import Path
import sys
from math import radians, sin, cos, sqrt, asin

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

try:
    from src.features.transfermarkt_client import get_team_market_value
    _TM_AVAILABLE = True
except ImportError:
    get_team_market_value = None  # type: ignore
    _TM_AVAILABLE = False

# ── Transfermarkt 球队名称规范化 ──────────────────────────────────
_TM_NAME_MAP = {
    # Italian: year suffix → common name
    "AC Pisa 1909": "Pisa Sporting Club", "Bologna FC 1909": "Bologna",
    "Cagliari Calcio": "Cagliari", "Como 1907": "Como",
    "Genoa CFC": "Genoa", "Hellas Verona FC": "Hellas Verona",
    "Parma Calcio 1913": "Parma", "SS Lazio": "Lazio",
    "SSC Napoli": "Napoli", "Torino FC": "Torino",
    "Udinese Calcio": "Udinese", "US Cremonese": "Cremonese",
    "US Lecce": "Lecce", "US Sassuolo Calcio": "Sassuolo",
    "FC Internazionale Milano": "Inter Milan",
    # German: year suffix → clean
    "1. FC Heidenheim 1846": "FC Heidenheim",
    "1. FC Union Berlin": "Union Berlin",
    "1. FSV Mainz 05": "Mainz 05",
    "FC St. Pauli 1910": "FC St. Pauli",
    "FC Bayern München": "FC Bayern Munich",
    # French
    "AS Monaco FC": "AS Monaco", "Stade Brestois 29": "Stade Brest",
    "Stade Rennais FC 1901": "Stade Rennais",
    "RC Strasbourg Alsace": "RC Strasbourg",
    "Paris Saint-Germain FC": "Paris Saint-Germain",
    # Spanish
    "Club Atlético de Madrid": "Atletico Madrid",
    "RC Celta de Vigo": "Celta Vigo",
    "RCD Espanyol de Barcelona": "Espanyol",
    "RCD Mallorca": "Mallorca", "CA Osasuna": "Osasuna",
    "Rayo Vallecano de Madrid": "Rayo Vallecano",
    "Real Betis Balompié": "Real Betis",
    "Real Sociedad de Fútbol": "Real Sociedad",
    "Elche CF": "Elche", "Getafe CF": "Getafe",
    "Valencia CF": "Valencia", "Villarreal CF": "Villarreal",
    "Deportivo Alavés": "Alavés", "Levante UD": "Levante",
    # English
    "Arsenal FC": "Arsenal", "Aston Villa FC": "Aston Villa",
    "Brighton & Hove Albion FC": "Brighton",
    "Chelsea FC": "Chelsea", "Crystal Palace FC": "Crystal Palace",
    "Everton FC": "Everton", "Fulham FC": "Fulham",
    "Leeds United FC": "Leeds United",
    "Leicester City FC": "Leicester City",  # may not be in data but safe
    "Liverpool FC": "Liverpool",
    "Manchester City FC": "Manchester City",
    "Manchester United FC": "Manchester United",
    "Newcastle United FC": "Newcastle United",
    "Nottingham Forest FC": "Nottingham Forest",
    "Sevilla FC": "Sevilla", "Sunderland AFC": "Sunderland",
    "Tottenham Hotspur FC": "Tottenham Hotspur",
    "West Ham United FC": "West Ham United",
    "Wolverhampton Wanderers FC": "Wolverhampton Wanderers",
    "Athletic Club": "Athletic Bilbao",
    # French extra
    "Olympique Lyonnais": "Olympique Lyon",
    "Olympique de Marseille": "Olympique Marseille",
    "FC Lorient": "Lorient", "FC Metz": "Metz",
    "FC Nantes": "Nantes", "AJ Auxerre": "Auxerre",
    "Le Havre AC": "Le Havre", "Lille OSC": "Lille",
    "Paris FC": "Paris FC", "Toulouse FC": "Toulouse",
    "Angers SCO": "Angers", "OGC Nice": "Nice",
}

_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "storage" / "tm_market_values.json"


# ── 欧洲球队坐标（用于行程距离计算）──
_TEAM_COORDS = {
    # English Premier League
    "Arsenal": (51.555, -0.108), "Aston Villa": (52.509, -1.885), "Bournemouth": (50.735, -1.838),
    "Brentford": (51.488, -0.289), "Brighton": (50.861, -0.084), "Burnley": (53.789, -2.230),
    "Chelsea": (51.482, -0.191), "Crystal Palace": (51.398, -0.086), "Everton": (53.439, -2.966),
    "Fulham": (51.475, -0.221), "Leeds United": (53.778, -1.572), "Leicester City": (52.620, -1.142),
    "Liverpool": (53.431, -2.961), "Luton": (51.885, -0.365), "Manchester City": (53.483, -2.200),
    "Manchester United": (53.463, -2.291), "Newcastle United": (54.975, -1.622), "Norwich": (52.622, 1.309),
    "Nottingham Forest": (52.942, -1.133), "Sheffield United": (53.370, -1.471), "Southampton": (50.906, -1.391),
    "Tottenham Hotspur": (51.603, -0.066), "West Ham United": (51.539, 0.017), "Wolverhampton Wanderers": (52.590, -2.130),
    "Ipswich Town": (52.055, 1.145),
    # La Liga
    "Atletico Madrid": (40.437, -3.599), "Athletic Bilbao": (43.263, -2.948), "Barcelona": (41.381, 2.123),
    "Celta Vigo": (42.212, -8.739), "Deportivo Alavés": (42.847, -2.688), "Eibar": (43.333, -2.474),
    "Elche": (38.267, -0.668), "Espanyol": (41.348, 2.075), "Getafe": (40.326, -3.715),
    "Girona": (41.962, 2.828), "Granada": (37.153, -3.605), "Las Palmas": (28.100, -15.456),
    "Levante": (39.495, -0.364), "Mallorca": (39.590, 2.630), "Osasuna": (42.797, -1.638),
    "Rayo Vallecano": (40.392, -3.658), "Real Betis": (37.356, -5.981), "Real Madrid": (40.453, -3.688),
    "Real Sociedad": (43.301, -1.974), "Sevilla": (37.384, -5.970), "Valencia": (39.475, -0.358),
    "Valladolid": (41.645, -4.764), "Villarreal": (39.944, -0.103), "Almería": (36.839, -2.436),
    "Cádiz": (36.503, -6.273),
    # Serie A
    "AC Milan": (45.478, 9.124), "Atalanta": (45.699, 9.744), "Bologna": (44.493, 11.350),
    "Cagliari": (39.195, 9.135), "Como": (45.810, 9.085), "Empoli": (43.720, 10.961),
    "Fiorentina": (43.771, 11.282), "Frosinone": (41.642, 13.350), "Genoa": (44.413, 8.952),
    "Hellas Verona": (45.438, 10.969), "Inter Milan": (45.478, 9.124), "Juventus": (45.110, 7.641),
    "Lazio": (41.935, 12.455), "Lecce": (40.364, 18.173), "Monza": (45.575, 9.310),
    "Napoli": (40.828, 14.193), "Parma": (44.796, 10.327), "Roma": (41.935, 12.455),
    "Salernitana": (40.680, 14.769), "Sassuolo": (44.549, 10.786), "Torino": (45.042, 7.649),
    "Udinese": (46.081, 13.201), "Venezia": (45.437, 12.323),
    # Bundesliga
    "FC Bayern Munich": (48.219, 11.625), "Borussia Dortmund": (51.492, 7.415),
    "RB Leipzig": (51.345, 12.348), "Bayer Leverkusen": (51.038, 7.002),
    "Eintracht Frankfurt": (50.069, 8.644), "VfB Stuttgart": (48.792, 9.232),
    "Borussia Mönchengladbach": (51.175, 6.385), "VfL Wolfsburg": (52.432, 10.805),
    "SC Freiburg": (48.022, 7.849), "1. FC Köln": (50.934, 6.875),
    "Mainz 05": (49.985, 8.224), "TSG Hoffenheim": (49.239, 8.888),
    "Werder Bremen": (53.066, 8.838), "FC Augsburg": (48.323, 10.886),
    "Union Berlin": (52.457, 13.568), "FC Heidenheim": (48.678, 10.153),
    "FC St. Pauli": (53.555, 9.968), "VfL Bochum": (51.490, 7.234),
    "Holstein Kiel": (54.345, 10.122), "Darmstadt 98": (49.858, 8.673),
    # Ligue 1
    "Paris Saint-Germain": (48.841, 2.253), "Olympique Marseille": (43.270, 5.396),
    "Olympique Lyon": (45.724, 4.832), "AS Monaco": (43.727, 7.415),
    "Lille": (50.612, 3.130), "Nice": (43.704, 7.194), "Rennes": (48.109, -1.710),
    "Strasbourg": (48.573, 7.752), "Lorient": (47.749, -3.370), "Nantes": (47.255, -1.525),
    "Montpellier": (43.622, 3.812), "Toulouse": (43.619, 1.419), "Angers": (47.465, -0.530),
    "Reims": (49.247, 3.935), "Brest": (48.405, -4.461), "Le Havre": (49.498, 0.169),
    "Metz": (49.110, 6.160), "Auxerre": (47.792, 3.583), "Clermont": (45.787, 3.086),
    "Saint-Étienne": (45.461, 4.390),
}

# Simplified coords for non-top5 leagues / international
_FALLBACK_COORDS = (50.0, 10.0)  # Central Europe


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points in km."""
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * asin(sqrt(a))


def _team_distance(home: str, away: str) -> float:
    """Return travel distance (km) from away team to home team (home advantage proxy)."""
    c1 = _TEAM_COORDS.get(home, _FALLBACK_COORDS)
    c2 = _TEAM_COORDS.get(away, _FALLBACK_COORDS)
    return _haversine(c1[0], c1[1], c2[0], c2[1])


def _load_tm_cache() -> dict:
    if _CACHE_FILE.exists():
        return json.loads(_CACHE_FILE.read_text())
    return {}


def _save_tm_cache(cache: dict):
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _lookup_market_value(team_csv_name: str, cache: dict, save_after: bool = False) -> float:
    """查询球队市值，优先使用缓存，其次实时查询 Transfermarkt。"""
    if team_csv_name in cache:
        return cache[team_csv_name]
    if not _TM_AVAILABLE:
        cache[team_csv_name] = 0.0
        return 0.0
    search_name = _TM_NAME_MAP.get(team_csv_name, team_csv_name)
    val = get_team_market_value(search_name)
    if val is None:
        val = 0.0
    cache[team_csv_name] = val
    if save_after:
        _save_tm_cache(cache)
    return val


def _slope(y):
    """Linear regression slope. >0 = improving trend."""
    if len(y) < 2:
        return 0.0
    return np.polyfit(np.arange(len(y)), y, 1)[0]


def build_football_features(input_csv="data/storage/football_history.csv", output_csv="data/processed/fb_features.csv"):
    df = pd.read_csv(input_csv)
    df.columns = [c.strip().lower() for c in df.columns]
    df['date'] = pd.to_datetime(df['date'], format='mixed', utc=True).dt.tz_localize(None)
    df = df.rename(columns={'home_score': 'home_goals', 'away_score': 'away_goals'})
    df['win'] = (df['home_goals'] > df['away_goals']).astype(int)

    # ── ELO 评级特征 ──
    from src.features.elo import compute_elo
    df = compute_elo(df, K=30)

    home = df[['date', 'home', 'home_goals', 'away_goals']].copy()
    home.columns = ['date', 'team', 'gf', 'ga']; home['is_home'] = 1
    away = df[['date', 'away', 'away_goals', 'home_goals']].copy()
    away.columns = ['date', 'team', 'gf', 'ga']; away['is_home'] = 0
    team = pd.concat([home, away]).sort_values(['team', 'date'])

    for w in [3, 10]:
        team[f'gf_avg_{w}'] = team.groupby('team')['gf'].transform(lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'ga_avg_{w}'] = team.groupby('team')['ga'].transform(lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'net_rating_{w}'] = team[f'gf_avg_{w}'] - team[f'ga_avg_{w}']

    team['gf_ewm5'] = team.groupby('team')['gf'].transform(lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    team['ga_ewm5'] = team.groupby('team')['ga'].transform(lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    team['opp_def_strength'] = team['ga_ewm5']
    team['is_win'] = (team['gf'] > team['ga']).astype(int)
    team['win_rate_10'] = team.groupby('team')['is_win'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    team['rest_days'] = team.groupby('team')['date'].diff().dt.days.fillna(3)
    team['b2b'] = (team['rest_days'] == 1).astype(int)

    # 趋势斜率特征（动量）
    for w in [5, 10]:
        team[f'net_rating_slope_{w}'] = team.groupby('team')['net_rating_3'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=2).apply(_slope, raw=True))

    # ── 主客场分别统计滚动平均（足球主客场差异显著）──
    for w in [5, 10]:
        team[f'h_gf_avg_{w}'] = team.groupby('team')['gf'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'a_gf_avg_{w}'] = team.groupby('team')['gf'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())

    # ── 球队近期状态（最近5场得分）──
    team['points_5'] = team.groupby('team')['is_win'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).sum())

    # ── 连续进球/失球场次（进攻/防守 momentum）──
    team['scoring_streak'] = team.groupby('team')['gf'].transform(
        lambda x: (x.shift(1) > 0).astype(int).rolling(10, min_periods=1).sum())
    team['conceding_streak'] = team.groupby('team')['ga'].transform(
        lambda x: (x.shift(1) > 0).astype(int).rolling(10, min_periods=1).sum())

    feat_cols = ['date', 'team', 'gf_avg_3', 'gf_avg_10',
                 'ga_avg_3', 'ga_avg_10',
                 'net_rating_3', 'net_rating_10',
                 'gf_ewm5', 'ga_ewm5', 'opp_def_strength', 'win_rate_10', 'rest_days',
                 'net_rating_slope_5', 'net_rating_slope_10',
                 'h_gf_avg_5', 'h_gf_avg_10', 'a_gf_avg_5', 'a_gf_avg_10',
                 'points_5', 'scoring_streak', 'conceding_streak']
    team_feats = team[feat_cols]

    match_df = df[['date', 'home', 'away', 'win', 'home_goals', 'away_goals',
                   'home_elo', 'away_elo', 'elo_diff']].copy()

    # ── Transfermarkt 球队市值特征 ──
    mv_cache = _load_tm_cache()
    unique_teams = set(match_df['home'].unique()) | set(match_df['away'].unique())
    # 只查询缓存中尚未有的球队（首次运行慢，后续秒出）
    missing = [t for t in unique_teams if t not in mv_cache]
    if missing:
        print(f"🔍 查询 Transfermarkt 市值（{len(missing)} 支球队）...")
    for i, t in enumerate(sorted(unique_teams), 1):
        _lookup_market_value(t, mv_cache, save_after=(i % 10 == 0))
    _save_tm_cache(mv_cache)  # final save

    match_df['home_market_value'] = match_df['home'].map(mv_cache).fillna(0.0).astype(float)
    match_df['away_market_value'] = match_df['away'].map(mv_cache).fillna(0.0).astype(float)
    match_df['market_value_diff'] = match_df['home_market_value'] - match_df['away_market_value']

    for side, team_col in [('home', 'home'), ('away', 'away')]:
        sf = team_feats.add_prefix(f'{side}_')
        sf.rename(columns={f'{side}_date': 'date', f'{side}_team': team_col}, inplace=True)
        match_df = pd.merge_asof(match_df.sort_values('date'), sf.sort_values('date'),
                                 by=team_col, left_on='date', right_on='date', direction='backward')
    match_df['off_vs_def'] = match_df['home_gf_ewm5'] - match_df['away_ga_ewm5']
    match_df['rest_diff'] = match_df['home_rest_days'] - match_df['away_rest_days']
    match_df['midweek'] = (pd.to_datetime(match_df['date']).dt.dayofweek >= 4).astype(int)

    # ── 行程距离特征 ──
    distance_cache = {}
    for side in ['home', 'away']:
        match_df[f'{side}_travel_km'] = 0.0
    for i, row in match_df.iterrows():
        key = (row['home'], row['away'])
        if key not in distance_cache:
            distance_cache[key] = _team_distance(row['home'], row['away'])
        match_df.at[i, 'away_travel_km'] = distance_cache[key]
    # 累计行程（近3场客队行程之和，用于疲劳评估）
    team_away_matches = match_df[['date', 'away', 'away_travel_km']].copy()
    team_away_matches.columns = ['date', 'team', 'travel_km']
    team_away_matches['cum_travel_3'] = team_away_matches.groupby('team')['travel_km'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).sum())
    team_away_matches = team_away_matches[['date', 'team', 'cum_travel_3']]
    match_df = pd.merge_asof(
        match_df.sort_values('date'), team_away_matches.sort_values('date'),
        by='away', left_on='date', right_on='date', direction='backward',
        suffixes=('', '_cum'))
    match_df['away_cum_travel_3'] = match_df['cum_travel_3'].fillna(0)
    match_df = match_df.drop(columns=['cum_travel_3'])

    # ── 对手近期状态（Strength of Schedule）──
    # 为每场比赛构建 "对手近期得分"（用于评估比赛难度）
    team_latest_form = team.sort_values('date').groupby('team').last().reset_index()
    form_lookup = dict(zip(team_latest_form['team'], team_latest_form['points_5']))
    match_df['home_opp_points_5'] = match_df['away'].map(form_lookup).fillna(0)  # 主队对手得分
    match_df['away_opp_points_5'] = match_df['home'].map(form_lookup).fillna(0)  # 客队对手得分

    # ── xG 预期进球特征 ──
    from src.features.xg_pipeline import build_xg_features, merge_xg_into_match
    try:
        xg_df = build_xg_features(seasons=[2024, 2023])
        if not xg_df.empty:
            match_df = merge_xg_into_match(match_df, xg_df)
            print(f"  ✅ xG 特征合并完成")
    except Exception as e:
        print(f"  ⚠️ xG 特征合并失败: {e}")

    match_df = match_df.ffill().fillna(0)
    match_df['total_result'] = ((match_df['home_goals'] + match_df['away_goals']) > 2.5).astype(int)
    # date 列归一化，避免带时间戳导致再读取解析失败
    match_df['date'] = pd.to_datetime(match_df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
    if output_csv:
        match_df.to_csv(output_csv, index=False)
    print(f"⚽ 足球特征已生成，列数：{match_df.shape[1]}（+8 新特征：行程距离/主客场滚动/对手状态/周中赛）")

if __name__ == '__main__':
    build_football_features()
