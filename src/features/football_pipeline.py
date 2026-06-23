import json
import pandas as pd
import numpy as np
from pathlib import Path
import sys
from math import radians, sin, cos, sqrt, asin

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR

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


def _current_streak(s):
    """计算当前连胜/连败长度。正数=连胜，负数=连败。"""
    streak = 0
    for v in s:
        if v > 0:
            if streak >= 0:
                streak += 1
            else:
                break
        elif v < 0:
            if streak <= 0:
                streak -= 1
            else:
                break
        else:
            break
    return streak


def _process_team_stats(df):
    """球队级滚动特征（与 ensemble_predictor 共享）"""
    elo_avail = all(c in df.columns for c in ['home_elo', 'away_elo'])
    if elo_avail:
        home = df[['date', 'home', 'home_goals', 'away_goals', 'home_elo', 'away_elo']].copy()
        home.columns = ['date', 'team', 'gf', 'ga', 'team_elo', 'opp_elo']
        home['is_home'] = 1
        away = df[['date', 'away', 'away_goals', 'home_goals', 'away_elo', 'home_elo']].copy()
        away.columns = ['date', 'team', 'gf', 'ga', 'team_elo', 'opp_elo']
        away['is_home'] = 0
    else:
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
    team['is_win'] = (team['gf'] > team['ga']).astype(int)
    team['is_win_int'] = team['is_win'].copy()  # for home/away split
    team['win_rate_10'] = team.groupby('team')['is_win'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    team['rest_days'] = team.groupby('team')['date'].diff().dt.days.fillna(3)
    team['total_goals'] = team['gf'] + team['ga']

    # 趋势斜率特征（动量）
    for w in [5, 10]:
        team[f'net_rating_slope_{w}'] = team.groupby('team')['net_rating_3'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=2).apply(_slope, raw=True))

    # ── 主客场分别统计滚动平均（足球主客场差异显著）──
    # 按 is_home 分离后计算，避免 h_gf 和 a_gf 结果相同
    team['home_gf_sep'] = team['gf'].where(team['is_home'] == 1)
    team['away_gf_sep'] = team['gf'].where(team['is_home'] == 0)
    team['home_ga_sep'] = team['ga'].where(team['is_home'] == 1)
    team['away_ga_sep'] = team['ga'].where(team['is_home'] == 0)
    for w in [5, 10]:
        team[f'h_gf_avg_{w}'] = team.groupby('team')['home_gf_sep'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'a_gf_avg_{w}'] = team.groupby('team')['away_gf_sep'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'h_ga_avg_{w}'] = team.groupby('team')['home_ga_sep'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'a_ga_avg_{w}'] = team.groupby('team')['away_ga_sep'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())

    # ── 球队近期状态（最近5场得分）──
    team['points_5'] = team.groupby('team')['is_win'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).sum())

    # ── 连续进球/失球场次（进攻/防守 momentum）──
    team['scoring_streak'] = team.groupby('team')['gf'].transform(
        lambda x: (x.shift(1) > 0).astype(int).rolling(10, min_periods=1).sum())
    team['conceding_streak'] = team.groupby('team')['ga'].transform(
        lambda x: (x.shift(1) > 0).astype(int).rolling(10, min_periods=1).sum())

    # ── 连胜/连败特征（动量延续强度）──
    team['win_streak'] = team.groupby('team')['is_win'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).apply(
            lambda y: _current_streak(y[::-1]) if len(y) > 0 else 0, raw=True))

    # ── 进球波动率（一致性指标）──
    team['gf_volatility_5'] = team.groupby('team')['gf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=3).std())
    team['ga_volatility_5'] = team.groupby('team')['ga'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=3).std())
    team['total_goals_volatility_5'] = team.groupby('team')['total_goals'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=3).std())

    # ── 形态回归：短窗口 vs 长窗口胜率差（过热/过冷信号）──
    team['win_rate_3'] = team.groupby('team')['is_win'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).mean())
    team['form_regression'] = team['win_rate_3'] - team['win_rate_10']

    # ── 净胜球 EWMA 额外窗口 ──
    team['net_rating_ewm8'] = team.groupby('team')['net_rating_3'].transform(
        lambda x: x.shift(1).ewm(span=8, adjust=False).mean())

    # ── 主客场分离胜率 ──
    team['home_win'] = team['is_win_int'].where(team['is_home'] == 1)
    team['home_win_rate_5'] = team.groupby('team')['home_win'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    team['home_win_rate_10'] = team.groupby('team')['home_win'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    team['away_win'] = team['is_win_int'].where(team['is_home'] == 0)
    team['away_win_rate_5'] = team.groupby('team')['away_win'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    team['away_win_rate_10'] = team.groupby('team')['away_win'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    # ── 动量延续：上一场是否赢球 ──
    team['last_game_won'] = team.groupby('team')['is_win_int'].transform(
        lambda x: x.shift(1))

    # ── 赛程强度 Strength of Schedule (ELO-based) ──
    if 'opp_elo' in team.columns:
        team['sos_elo_5'] = team.groupby('team')['opp_elo'].transform(
            lambda x: x.shift(1).rolling(5, min_periods=1).mean())
        team['sos_elo_10'] = team.groupby('team')['opp_elo'].transform(
            lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    # ── 平均净胜球 margin ──
    team['avg_margin_5'] = team.groupby('team')['net_rating_3'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    team['avg_margin_10'] = team.groupby('team')['net_rating_3'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    # ── ELO 残差：实际净胜球 vs ELO 预期 ──
    if 'opp_elo' in team.columns and 'team_elo' in team.columns:
        team['margin'] = team['gf'] - team['ga']
        team['elo_residual'] = team['margin'] - (team['team_elo'] - team['opp_elo']) / 400
        team['elo_residual_5'] = team.groupby('team')['elo_residual'].transform(
            lambda x: x.shift(1).rolling(5, min_periods=2).mean())

    feat_cols = ['date', 'team', 'gf_avg_3', 'gf_avg_10',
                 'ga_avg_3', 'ga_avg_10',
                 'net_rating_3', 'net_rating_10',
                 'gf_ewm5', 'ga_ewm5', 'win_rate_10', 'rest_days',
                 'net_rating_slope_5', 'net_rating_slope_10',
                 'h_gf_avg_5', 'h_gf_avg_10', 'a_gf_avg_5', 'a_gf_avg_10',
                 'h_ga_avg_5', 'h_ga_avg_10', 'a_ga_avg_5', 'a_ga_avg_10',
                 'points_5', 'scoring_streak', 'conceding_streak',
                 'win_streak', 'gf_volatility_5', 'ga_volatility_5',
                 'total_goals_volatility_5',
                 'win_rate_3', 'form_regression', 'net_rating_ewm8',
                 'home_win_rate_5', 'home_win_rate_10',
                 'away_win_rate_5', 'away_win_rate_10',
                 'last_game_won', 'avg_margin_5', 'avg_margin_10', 'elo_residual_5']
    if 'sos_elo_5' in team.columns:
        feat_cols += ['sos_elo_5', 'sos_elo_10']
    return team[feat_cols]


def _compute_rolling_poisson_probs(df: pd.DataFrame, block_size: int = 300) -> np.ndarray:
    """用滚动窗口泊松模型计算每场比赛的 over/under 概率（零泄漏）。

    对第 N 块比赛(第 N*block_size 到 (N+1)*block_size 场)：
      训练: 用 0..N*block_size-1 的所有历史比赛
      预测: 当前块的每场比赛的 over_{2.5} 概率
    训练集不足 block_size 的初始段返回 0.5。
    """
    df_sorted = df.sort_values('date').reset_index(drop=True)
    n = len(df_sorted)
    probs = np.full(n, 0.5)
    from src.models.poisson_model import PoissonGoalModel

    for start in range(0, n, block_size):
        train_end = start + block_size
        if train_end >= n:
            break
        train_df = df_sorted.iloc[:train_end]
        try:
            model = PoissonGoalModel(alpha=1.0, decay_halflife_days=365)
            model.fit(train_df[['date', 'home', 'away', 'home_goals', 'away_goals']])
        except Exception:
            continue
        next_end = min(train_end + block_size, n)
        for j in range(train_end, next_end):
            row = df_sorted.iloc[j]
            try:
                pred = model.predict_proba(row['home'], row['away'])
                probs[j] = pred.get('over_2.5', 0.5)
            except Exception:
                probs[j] = 0.5
    return probs


def build_football_features(input_csv=None, output_csv=None):
    if input_csv is None:
        input_csv = str(Path(DATA_DIR) / "football_history.csv")
    if output_csv is None:
        output_csv = str(ROOT / "data" / "processed" / "fb_features.csv")
    df = pd.read_csv(input_csv)
    df.columns = [c.strip().lower() for c in df.columns]
    df['date'] = pd.to_datetime(df['date'], format='mixed', utc=True).dt.tz_localize(None)
    df = df.rename(columns={'home_score': 'home_goals', 'away_score': 'away_goals'})
    df['win'] = (df['home_goals'] > df['away_goals']).astype(int)

    # ── ELO 评级特征 ──
    from src.features.elo import compute_elo
    df = compute_elo(df, K=30)

    # ── 球队级滚动特征（复用 _process_team_stats，训练/预测共享）──
    team_feats = _process_team_stats(df)

    competition_col = 'competition' if 'competition' in df.columns else None
    match_cols = ['date', 'home', 'away', 'win', 'home_goals', 'away_goals',
                  'home_elo', 'away_elo', 'elo_diff']
    if competition_col:
        match_cols.append('competition')
    match_df = df[match_cols].copy()

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

    # ── 新交叉特征（SoS / 主客场 / 动量） ──
    if 'home_sos_elo_5' in match_df.columns and 'away_sos_elo_5' in match_df.columns:
        match_df['sos_elo_diff'] = match_df['home_sos_elo_5'] - match_df['away_sos_elo_5']
    if 'home_home_win_rate_5' in match_df.columns and 'away_away_win_rate_5' in match_df.columns:
        match_df['home_away_win_diff'] = match_df['home_home_win_rate_5'] - match_df['away_away_win_rate_5']

    # ── 动量质量（连胜幅度） ──
    if 'home_win_streak' in match_df.columns and 'home_avg_margin_5' in match_df.columns:
        match_df['home_momentum_quality'] = match_df['home_win_streak'] * match_df['home_avg_margin_5'].clip(-3, 3) / 3
        match_df['away_momentum_quality'] = match_df['away_win_streak'] * match_df['away_avg_margin_5'].clip(-3, 3) / 3
        match_df['momentum_quality_diff'] = match_df['home_momentum_quality'] - match_df['away_momentum_quality']

    # ── 波动率交互 ──
    if 'home_gf_volatility_5' in match_df.columns and 'away_ga_volatility_5' in match_df.columns:
        match_df['margin_volatility_interaction'] = match_df['home_gf_volatility_5'] * match_df['away_ga_volatility_5']

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
    team_away_matches['cum_travel_3'] = team_away_matches.groupby('away')['away_travel_km'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=1).sum())
    team_away_matches = team_away_matches[['date', 'away', 'cum_travel_3']]
    match_df = pd.merge_asof(
        match_df.sort_values('date'), team_away_matches.sort_values('date'),
        by='away', left_on='date', right_on='date', direction='backward')
    match_df['away_cum_travel_3'] = match_df['cum_travel_3'].fillna(0)
    match_df = match_df.drop(columns=['cum_travel_3'])

    # ── 休息综合优势（休息差 + 客场累计行程 + 周中比赛） ──
    r_diff = match_df['rest_diff'].clip(-3, 3) / 3
    t_3 = match_df['away_cum_travel_3'].clip(0, 3000) / 3000
    mw = match_df['midweek'] * 0.3
    match_df['rest_advantage'] = r_diff - t_3 + mw

    # ── 对手近期状态（Strength of Schedule）──
    # 为每场比赛构建 "对手近期得分"（用于评估比赛难度）
    team_latest_form = team_feats.sort_values('date').groupby('team').last().reset_index()
    form_lookup = dict(zip(team_latest_form['team'], team_latest_form['points_5']))
    match_df['home_opp_points_5'] = match_df['away'].map(form_lookup).fillna(0)  # 主队对手得分
    match_df['away_opp_points_5'] = match_df['home'].map(form_lookup).fillna(0)  # 客队对手得分

    # ── 历史交锋 H2H 特征 ──
    # 从原始比赛数据构建 H2H 历史缓存
    h2h_cache = {}
    for _, r in df.sort_values('date').iterrows():
        h, a = r['home'], r['away']
        key = tuple(sorted([h, a]))
        if key not in h2h_cache:
            h2h_cache[key] = []
        h2h_cache[key].append({
            'home': h, 'away': a,
            'home_goals': r['home_goals'], 'away_goals': r['away_goals'],
            'date': r['date'],
        })
    h2h_rows = []
    for _, row in match_df.iterrows():
        h, a = row['home'], row['away']
        key = tuple(sorted([h, a]))
        meetings = h2h_cache.get(key, [])
        prior = [m for m in meetings if m['date'] < row['date']]
        last_5 = prior[-5:]
        if len(last_5) >= 2:
            h_wins = 0
            a_wins = 0
            for m in last_5:
                if m['home'] == h:  # home team is the current home team
                    if m['home_goals'] > m['away_goals']:
                        h_wins += 1
                    elif m['away_goals'] > m['home_goals']:
                        a_wins += 1
                else:  # home team was the away team
                    if m['away_goals'] > m['home_goals']:
                        h_wins += 1
                    elif m['home_goals'] > m['away_goals']:
                        a_wins += 1
            draws = len(last_5) - h_wins - a_wins
            avg_total = sum(m['home_goals'] + m['away_goals'] for m in last_5) / len(last_5)
        else:
            h_wins = a_wins = draws = 0
            avg_total = 0.0
        h2h_rows.append({
            'h2h_home_wins': h_wins, 'h2h_away_wins': a_wins,
            'h2h_draws': draws, 'h2h_avg_total_goals': avg_total,
        })
    if h2h_rows:
        h2h_df = pd.DataFrame(h2h_rows, index=match_df.index)
        match_df = pd.concat([match_df, h2h_df], axis=1)

    # ── H2H 派生特征（dominance + form interaction）──
    if 'h2h_home_wins' in match_df.columns and 'h2h_away_wins' in match_df.columns:
        match_df['h2h_total'] = match_df['h2h_home_wins'] + match_df['h2h_away_wins'] + match_df.get('h2h_draws', 0)
        match_df['h2h_dominance'] = (match_df['h2h_home_wins'] - match_df['h2h_away_wins']) / match_df['h2h_total'].clip(lower=1)
    if 'h2h_dominance' in match_df.columns and 'home_home_win_rate_5' in match_df.columns:
        match_df['h2h_form_x'] = match_df['h2h_dominance'] * match_df['home_home_win_rate_5']

    # ── 联赛宏观趋势特征（使用 expanding window 防止泄漏）──
    if 'competition' in df.columns:
        # 按日期排序后，对每个联赛计算 expanding 统计量，shift(1) 排除当前场次
        comp_df = df.sort_values('date').copy()
        comp_df['_goals_total'] = comp_df['home_goals'] + comp_df['away_goals']
        comp_df['_home_adv'] = comp_df['home_goals'] - comp_df['away_goals']

        # 使用 shift(1) 确保当前行不参与自身统计
        for col, source in [('league_home_win_rate', 'win'),
                            ('league_avg_home_goals', 'home_goals'),
                            ('league_avg_away_goals', 'away_goals')]:
            comp_df[col] = comp_df.groupby('competition')[source].transform(
                lambda x: x.shift(1).expanding(min_periods=5).mean())

        comp_df['league_avg_goals'] = comp_df.groupby('competition')['_goals_total'].transform(
            lambda x: x.shift(1).expanding(min_periods=5).mean())
        comp_df['league_home_adv_raw'] = comp_df.groupby('competition')['_home_adv'].transform(
            lambda x: x.shift(1).expanding(min_periods=5).mean())

        # 去掉内部列
        comp_df = comp_df.drop(columns=['_goals_total', '_home_adv'])

        # 合并回 match_df
        merge_cols = ['date', 'home', 'away', 'league_home_win_rate', 'league_avg_goals',
                      'league_home_adv_raw', 'league_avg_home_goals', 'league_avg_away_goals']
        match_df = match_df.merge(
            comp_df[merge_cols], on=['date', 'home', 'away'], how='left')
        match_df['league_home_adv'] = match_df['league_home_adv_raw']
        match_df = match_df.drop(columns=['league_home_adv_raw'])

    # ── xG 预期进球特征 ──
    from src.features.xg_pipeline import build_xg_features, merge_xg_into_match
    try:
        xg_df = build_xg_features(seasons=[2024, 2023])
        if not xg_df.empty:
            match_df = merge_xg_into_match(match_df, xg_df)
            print("  ✅ xG 特征合并完成")
    except Exception as e:
        print(f"  ⚠️ xG 特征合并失败: {e}")

    # ── xG 残差交叉特征 ──
    if 'home_xg_residual_5' in match_df.columns and 'away_xg_residual_5' in match_df.columns:
        match_df['xg_residual_diff'] = match_df['home_xg_residual_5'] - match_df['away_xg_residual_5']

    # ── 赛季阶段（足球赛季 8月~5月） ──
    match_df['_month'] = pd.to_datetime(match_df['date']).dt.month
    match_df['season_stage'] = match_df['_month'].map({
        8: 0, 9: 0, 10: 0,  # 开季
        11: 1, 12: 1, 1: 1,  # 中期
        2: 2, 3: 2, 4: 2,    # 冲刺
        5: 3,                 # 收官
    }).fillna(-1)
    match_df.drop(columns=['_month'], inplace=True)

    match_df = match_df.ffill().fillna(0)
    match_df['total_result'] = ((match_df['home_goals'] + match_df['away_goals']) > 2.5).astype(int)
    # date 列归一化，避免带时间戳导致再读取解析失败
    match_df['date'] = pd.to_datetime(match_df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
    if output_csv:
        match_df.to_csv(output_csv, index=False)
    print(f"⚽ 足球特征已生成，列数：{match_df.shape[1]}（+15 新特征：H2H/联赛宏观/波动率/连胜/形态回归）")
    return match_df
