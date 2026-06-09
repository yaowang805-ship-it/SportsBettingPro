import json, os, pandas as pd, numpy as np
from pathlib import Path
import sys

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

    feat_cols = ['date', 'team', 'gf_avg_3', 'gf_avg_10',
                 'ga_avg_3', 'ga_avg_10',
                 'net_rating_3', 'net_rating_10',
                 'gf_ewm5', 'ga_ewm5', 'opp_def_strength', 'win_rate_10', 'rest_days',
                 'net_rating_slope_5', 'net_rating_slope_10']
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
    print(f"⚽ 足球特征已生成，列数：{match_df.shape[1]}")

if __name__ == '__main__':
    build_football_features()
