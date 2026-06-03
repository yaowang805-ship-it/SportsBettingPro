import pandas as pd
import numpy as np
import json
from pathlib import Path

DATA_RAW = Path("data/raw")

def load_json(path, default={}):
    if Path(path).exists():
        with open(path) as f:
            return json.load(f)
    return default

TEAM_ELO = load_json(DATA_RAW / "team_elo.json", {})
PLAYER_CACHE = load_json(DATA_RAW / "player_avg_cache.json", {})
TEAM_POWER = {t: s.get('avg_pts',0)+1.5*s.get('avg_ast',0)+1.2*s.get('avg_trb',0) 
              for t,s in PLAYER_CACHE.items()}
FOOTBALL_ELO = load_json(DATA_RAW / "football_elo.json", {})

def to_naive_utc(dt):
    if isinstance(dt, str):
        dt = pd.to_datetime(dt, utc=True)
    elif isinstance(dt, pd.Timestamp):
        dt = dt.tz_convert('UTC') if dt.tzinfo else dt.tz_localize('UTC')
    else:
        dt = pd.Timestamp(dt).tz_localize('UTC')
    return dt.tz_localize(None)

def compute_rolling_stats(df, team, date, n_games=10):
    date = to_naive_utc(date)
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'], utc=True).dt.tz_localize(None)
    mask = (df['date'] < date) & ((df['home'] == team) | (df['away'] == team))
    recent = df[mask].sort_values('date', ascending=False).head(n_games)
    if recent.empty:
        return None
    stats = {'gf': [], 'ga': [], 'margin': [], 'total': [], 'wins': 0}
    for _, row in recent.iterrows():
        if row['home'] == team:
            gf, ga = row['home_score'], row['away_score']
            if gf > ga: stats['wins'] += 1
        else:
            gf, ga = row['away_score'], row['home_score']
            if gf > ga: stats['wins'] += 1
        stats['gf'].append(gf)
        stats['ga'].append(ga)
        stats['margin'].append(gf - ga)
        stats['total'].append(gf + ga)
    res = {k: np.mean(v) if isinstance(v, list) else v for k, v in stats.items()}
    res['win_pct'] = res['wins'] / len(recent)
    last_game = recent.iloc[0]['date']
    res['b2b'] = 1 if (date - last_game).days == 1 else 0
    return res

def build_basketball_features(match_row, df):
    home, away, date = match_row['home'], match_row['away'], match_row['date']
    home_stats = compute_rolling_stats(df, home, date) or {}
    away_stats = compute_rolling_stats(df, away, date) or {}
    
    feats = [
        home_stats.get('gf', 112.0),                # 0 主队近期场均得分
        away_stats.get('gf', 112.0),                # 1 客队近期场均得分
        home_stats.get('ga', 112.0),                # 2 主队近期场均失分
        away_stats.get('ga', 112.0),                # 3 客队近期场均失分
        home_stats.get('margin', 0.0),              # 4 主队近期净胜分
        away_stats.get('margin', 0.0),              # 5 客队近期净胜分
        home_stats.get('win_pct', 0.5),             # 6 主队近期胜率
        away_stats.get('win_pct', 0.5),             # 7 客队近期胜率
        home_stats.get('total', 224.0),             # 8 主队近期场均总得分
        away_stats.get('total', 224.0),             # 9 客队近期场均总得分
        home_stats.get('b2b', 0),                   # 10 主队背靠背
        away_stats.get('b2b', 0),                   # 11 客队背靠背
        TEAM_ELO.get(home, 1500),               # 12 主队ELO
        TEAM_ELO.get(away, 1500),               # 13 客队ELO
        TEAM_POWER.get(home, 110.0),             # 14 主队球员战力
        TEAM_POWER.get(away, 110.0),             # 15 客队球员战力
    ]
    return np.array(feats, dtype=np.float32)

def build_football_features(match_row, df):
    home, away, date = match_row['home'], match_row['away'], match_row['date']
    home_stats = compute_rolling_stats(df, home, date, n_games=6) or {}
    away_stats = compute_rolling_stats(df, away, date, n_games=6) or {}
    
    feats = [
        home_stats.get('gf', 1.4),                  # 0 主队近期场均进球
        away_stats.get('gf', 1.4),                  # 1 客队近期场均进球
        home_stats.get('ga', 1.4),                  # 2 主队近期场均失球
        away_stats.get('ga', 1.4),                  # 3 客队近期场均失球
        home_stats.get('total', 2.8),               # 4 主队近期场均总进球
        away_stats.get('total', 2.8),               # 5 客队近期场均总进球
        home_stats.get('win_pct', 0.5),             # 6 主队近期胜率
        away_stats.get('win_pct', 0.5),             # 7 客队近期胜率
        FOOTBALL_ELO.get(home, 1500)/100,           # 8 主队ELO
        FOOTBALL_ELO.get(away, 1500)/100,           # 9 客队ELO
    ]
    return np.array(feats, dtype=np.float32)
