#!/usr/bin/env python3
"""世界杯特征流水线 — 基于 results.csv 国家队历史数据。

用法:
    from src.features.wc_pipeline import build_wc_features
    df = build_wc_features()  # 训练: 全量历史特征
    feat = compute_team_features(team_name, date)  # 推理: 单队特征
"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from config.logging_config import get_logger
logger = get_logger(__name__)

# ── Odds API 队名 → results.csv 队名映射 ──
WC_TEAM_MAP = {
    "USA": "United States",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    "Curaçao": "Curacao",  # results.csv may have either
}
# 反向映射（results.csv → Odds API）
_REVERSE_MAP = {v: k for k, v in WC_TEAM_MAP.items()}

# 48 支世界杯参赛队（Odds API 名称）
WC_TEAMS_ODDS = [
    "Algeria", "Argentina", "Australia", "Austria", "Belgium", "Bosnia & Herzegovina",
    "Brazil", "Canada", "Cape Verde", "Colombia", "Croatia", "Curaçao",
    "Czech Republic", "DR Congo", "Ecuador", "Egypt", "England", "France",
    "Germany", "Ghana", "Haiti", "Iran", "Iraq", "Ivory Coast", "Japan", "Jordan",
    "Mexico", "Morocco", "Netherlands", "New Zealand", "Norway", "Panama",
    "Paraguay", "Portugal", "Qatar", "Saudi Arabia", "Scotland", "Senegal",
    "South Africa", "South Korea", "Spain", "Sweden", "Switzerland", "Tunisia",
    "Turkey", "USA", "Uruguay", "Uzbekistan",
]


def _odds_to_csv_name(name: str) -> str:
    """Odds API 名称 → results.csv 名称。"""
    return WC_TEAM_MAP.get(name, name)


def _csv_to_odds_name(name: str) -> str:
    """results.csv 名称 → Odds API 名称。"""
    return _REVERSE_MAP.get(name, name)


def _elo_rating(home_rating, away_rating, home_goals, away_goals, K=50):
    """计算 ELO 变动。国际比赛 K 值更高（队伍变动大）。"""
    home_win_prob = 1.0 / (1.0 + 10.0 ** ((away_rating - home_rating) / 400.0))
    goal_diff = home_goals - away_goals
    if goal_diff > 0:
        actual = 1.0
    elif goal_diff == 0:
        actual = 0.5
    else:
        actual = 0.0
    # 进球差加权（大比分获胜获得更多 ELO）
    margin = min(abs(goal_diff), 5) ** 0.5  # sqrt capped at 5
    return K * margin * (actual - home_win_prob)


def _tournament_weight(tournament: str) -> float:
    """不同赛事权重，用于加权特征。"""
    t = tournament.lower()
    if 'world cup' in t and 'qualification' not in t:
        return 3.0
    if 'world cup qualification' in t:
        return 2.0
    if 'euro' in t or 'copa am' in t or 'african cup' in t or 'asian cup' in t or 'gold cup' in t:
        return 2.0
    if 'nations league' in t:
        return 1.5
    if 'friendly' in t:
        return 0.5
    return 1.0


def build_wc_features(
    input_csv="data/storage/results.csv",
    output_csv="data/processed/wc_features.csv",
    lookback_years=8,
    min_matches=10,
) -> pd.DataFrame:
    """为世界杯模型构建特征。

    Args:
        lookback_years: 回溯年数（世界杯周期 4 年，但用 8 年增加样本）
        min_matches: 球队最小比赛数才纳入（排除数据极少球队）
    """
    df = pd.read_csv(input_csv)
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date', 'home_score', 'away_score']).copy()

    # 归一化队名（处理 accented characters）
    df['home_team'] = df['home_team'].str.normalize('NFKD').str.encode('ascii', errors='ignore').str.decode('utf-8')
    df['away_team'] = df['away_team'].str.normalize('NFKD').str.encode('ascii', errors='ignore').str.decode('utf-8')
    # Apply special mappings
    df['home_team'] = df['home_team'].map(_REVERSE_MAP).fillna(df['home_team'])
    df['home_team'] = df['home_team'].map(WC_TEAM_MAP).fillna(df['home_team'])
    df['away_team'] = df['away_team'].map(_REVERSE_MAP).fillna(df['away_team'])
    df['away_team'] = df['away_team'].map(WC_TEAM_MAP).fillna(df['away_team'])

    # 过滤：涉及世界杯参赛队的比赛（或对手是世界杯队）
    wc_csv_name = [_odds_to_csv_name(t) for t in WC_TEAMS_ODDS]
    mask = df['home_team'].isin(wc_csv_name) | df['away_team'].isin(wc_csv_name)
    df = df[mask].copy()

    # 时间过滤
    cutoff = pd.Timestamp.now() - pd.DateOffset(years=lookback_years)
    df = df[df['date'] >= cutoff].copy()
    df = df.sort_values('date').reset_index(drop=True)

    logger.info(f"📂 国家队历史数据: {len(df)} 场 ({df['date'].min().date()} ~ {df['date'].max().date()})")

    # ── 计算 ELO ──
    elo_ratings = {}
    elo_records = []

    for _, row in df.iterrows():
        home, away = row['home_team'], row['away_team']
        home_elo = elo_ratings.get(home, 1500)
        away_elo = elo_ratings.get(away, 1500)
        hg, ag = float(row['home_score']), float(row['away_score'])
        K = _tournament_weight(row.get('tournament', 'Friendly'))

        margin = abs(hg - ag)
        m = min(margin, 5) ** 0.5
        K_adjusted = K * 20 * m  # 标准国际足球 K≈20-30

        if hg > ag:
            home_result, away_result = 1.0, 0.0
        elif hg == ag:
            home_result, away_result = 0.5, 0.5
        else:
            home_result, away_result = 0.0, 1.0

        home_exp = 1.0 / (1.0 + 10.0 ** ((away_elo - home_elo) / 400.0))
        away_exp = 1.0 / (1.0 + 10.0 ** ((home_elo - away_elo) / 400.0))

        new_home_elo = home_elo + K_adjusted * (home_result - home_exp)
        new_away_elo = away_elo + K_adjusted * (away_result - away_exp)

        elo_ratings[home] = new_home_elo
        elo_ratings[away] = new_away_elo

        elo_records.append({
            'date': row['date'],
            'home': home,
            'away': away,
            'home_elo': home_elo,
            'away_elo': away_elo,
            'elo_diff': home_elo - away_elo,
        })

    elo_df = pd.DataFrame(elo_records)

    # ── 构建球队级滚动特征 ──
    home = df[['date', 'home_team', 'away_team', 'home_score', 'away_score']].copy()
    home.columns = ['date', 'team', 'opponent', 'gf', 'ga']
    home['is_home'] = 1
    away = df[['date', 'away_team', 'home_team', 'away_score', 'home_score']].copy()
    away.columns = ['date', 'team', 'opponent', 'gf', 'ga']
    away['is_home'] = 0
    team = pd.concat([home, away], ignore_index=True).sort_values(['team', 'date'])

    for w in [3, 5, 10]:
        team[f'gf_avg_{w}'] = team.groupby('team')['gf'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'ga_avg_{w}'] = team.groupby('team')['ga'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())

    team['is_win'] = (team['gf'] > team['ga']).astype(int)
    team['is_draw'] = (team['gf'] == team['ga']).astype(int)
    for w in [5, 10]:
        team[f'win_rate_{w}'] = team.groupby('team')['is_win'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'draw_rate_{w}'] = team.groupby('team')['is_draw'].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())

    team['rest_days'] = team.groupby('team')['date'].diff().dt.days.fillna(14)

    # ── 关键指标：近5场净胜球 ──
    team['net_5'] = team.groupby('team')['gf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean()
    ) - team.groupby('team')['ga'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())

    # ── 对手强度加权评分 ──
    elo_lookup = {}
    for _, er in elo_df.iterrows():
        elo_lookup[(er['date'], er['home'], er['away'])] = (er['home_elo'], er['away_elo'])

    def _opponent_strength(team_df, elo_lookup):
        vals = []
        for _, r in team_df.iterrows():
            key = (r['date'], r['team'], r['opponent']) if r['is_home'] else (r['date'], r['opponent'], r['team'])
            elos = elo_lookup.get(key)
            opp_elo = elos[1] if r['is_home'] else elos[0] if elos else 1500
            vals.append(opp_elo)
        return pd.Series(vals, index=team_df.index)

    team['opp_elo'] = team.groupby('team', group_keys=False).apply(
        lambda g: _opponent_strength(g, elo_lookup))

    # ── 合并特征到比赛行 ──
    match_df = df[['date', 'home_team', 'away_team', 'home_score', 'away_score',
                   'tournament', 'neutral']].copy()
    match_df['home_win'] = (match_df['home_score'] > match_df['away_score']).astype(int)
    match_df['draw'] = (match_df['home_score'] == match_df['away_score']).astype(int)
    match_df['total_goals'] = match_df['home_score'] + match_df['away_score']
    match_df['over_2.5'] = (match_df['total_goals'] > 2.5).astype(int)

    # 加入 ELO 特征
    match_df = match_df.merge(elo_df, left_on=['date', 'home_team', 'away_team'],
                               right_on=['date', 'home', 'away'], how='left')
    match_df.drop(columns=['home', 'away'], inplace=True)

    # 加入球队特征
    feat_cols = ['date', 'team', 'gf_avg_3', 'gf_avg_5', 'gf_avg_10',
                 'ga_avg_3', 'ga_avg_5', 'ga_avg_10',
                 'win_rate_5', 'win_rate_10', 'draw_rate_5', 'draw_rate_10',
                 'rest_days', 'net_5', 'opp_elo']

    team_feats = team[feat_cols]
    for side, team_col in [('home', 'home_team'), ('away', 'away_team')]:
        sf = team_feats.add_prefix(f'{side}_')
        sf.rename(columns={f'{side}_date': 'date', f'{side}_team': team_col}, inplace=True)
        match_df = pd.merge_asof(
            match_df.sort_values('date'), sf.sort_values('date'),
            by=team_col, on='date', direction='backward')

    # 交互特征
    match_df['elo_diff'] = match_df['home_elo'].fillna(0) - match_df['away_elo'].fillna(0)
    match_df['form_diff_5'] = match_df['home_win_rate_5'].fillna(0.5) - match_df['away_win_rate_5'].fillna(0.5)
    match_df['net_5_diff'] = match_df['home_net_5'].fillna(0) - match_df['away_net_5'].fillna(0)
    match_df['rest_diff'] = match_df['home_rest_days'].fillna(7) - match_df['away_rest_days'].fillna(7)
    match_df['gf_avg_5_diff'] = match_df['home_gf_avg_5'].fillna(1) - match_df['away_gf_avg_5'].fillna(1)
    match_df['ga_avg_5_diff'] = match_df['home_ga_avg_5'].fillna(1) - match_df['away_ga_avg_5'].fillna(1)
    match_df['total_avg_5'] = match_df['home_gf_avg_5'].fillna(1) + match_df['away_gf_avg_5'].fillna(1)
    match_df['opp_elo_diff'] = match_df['home_opp_elo'].fillna(1500) - match_df['away_opp_elo'].fillna(1500)

    # neutral 标志
    match_df['is_neutral'] = match_df['neutral'].fillna(False).astype(int)

    # 填充 NaN
    match_df = match_df.ffill().fillna(0)

    if output_csv:
        # date 归一化
        match_df['date'] = pd.to_datetime(match_df['date']).dt.strftime('%Y-%m-%d')
        match_df.to_csv(output_csv, index=False)

    # 过滤未开始的比赛（score=0,0 可能是 futuro 比赛）
    played = match_df[match_df['total_goals'] > 0].copy() if len(match_df) > 0 else match_df
    logger.info(f"✅ 世界杯特征: {len(played)} 场已完赛, {len(match_df) - len(played)} 场未开始")
    logger.info(f"   特征列: {match_df.shape[1]}")

    return match_df


def compute_team_features(team_name: str, reference_date=None) -> dict:
    """为指定球队计算当前特征（用于推理时）。"""
    wc = build_wc_features(output_csv=None)
    wc['date'] = pd.to_datetime(wc['date'])

    if reference_date is None:
        reference_date = pd.Timestamp.now()

    past = wc[(wc['date'] < reference_date) &
              ((wc['home_team'] == team_name) | (wc['away_team'] == team_name))].copy()

    if len(past) == 0:
        return {}

    last = past.iloc[-1]
    prefix = 'home_' if last['home_team'] == team_name else 'away_'
    features = {}
    for col in ['elo', 'win_rate_5', 'win_rate_10', 'gf_avg_5', 'ga_avg_5',
                'net_5', 'rest_days', 'opp_elo']:
        val = last.get(f'{prefix}{col}', last.get(col, 0))
        features[col] = val
    return features


def save_feature_columns(feat_cols: list, path: str = None):
    """保存特征列列表（供训练/预测使用）。"""
    from config.settings import MODEL_DIR
    save_path = path or Path(MODEL_DIR) / 'model_wc_features.json'
    with open(save_path, 'w') as f:
        json.dump(feat_cols, f)
    logger.info(f"💾 特征列已保存: {save_path} ({len(feat_cols)} 列)")


if __name__ == '__main__':
    df = build_wc_features()
    print(f"特征列: {list(df.columns)}")
