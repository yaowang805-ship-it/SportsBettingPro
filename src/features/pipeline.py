import pandas as pd, numpy as np

def build_features(input_csv="data/storage/basketball_scores_combined.csv", output_csv="data/processed/bb_features.csv"):
    df = pd.read_csv(input_csv)
    df.columns = [c.strip().lower() for c in df.columns]
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    df = df.rename(columns={'home_score': 'home_goals', 'away_score': 'away_goals'})
    df['win'] = (df['home_goals'] > df['away_goals']).astype(int)

    home = df[['date', 'home', 'home_goals', 'away_goals']].copy()
    home.columns = ['date', 'team', 'gf', 'ga']
    home['is_home'] = 1
    away = df[['date', 'away', 'away_goals', 'home_goals']].copy()
    away.columns = ['date', 'team', 'gf', 'ga']
    away['is_home'] = 0
    team = pd.concat([home, away]).sort_values(['team', 'date'])

    for w in [3,5,7,10]:
        team[f'gf_avg_{w}'] = team.groupby('team')['gf'].transform(lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'ga_avg_{w}'] = team.groupby('team')['ga'].transform(lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        team[f'net_rating_{w}'] = team[f'gf_avg_{w}'] - team[f'ga_avg_{w}']

    team['gf_ewm5'] = team.groupby('team')['gf'].transform(lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    team['ga_ewm5'] = team.groupby('team')['ga'].transform(lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    team_def = team.groupby('team')['ga_ewm5'].last().to_dict()
    team['opp_def_strength'] = team['team'].map(team_def)
    team['is_win'] = (team['gf'] > team['ga']).astype(int)
    team['win_rate_10'] = team.groupby('team')['is_win'].transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    team['rest_days'] = team.groupby('team')['date'].diff().dt.days.fillna(3)
    team['b2b'] = (team['rest_days'] == 1).astype(int)

    feat_cols = ['date', 'team', 'gf_avg_3', 'gf_avg_5', 'gf_avg_7', 'gf_avg_10',
                 'ga_avg_3', 'ga_avg_5', 'ga_avg_7', 'ga_avg_10',
                 'net_rating_3', 'net_rating_5', 'net_rating_7', 'net_rating_10',
                 'gf_ewm5', 'ga_ewm5', 'opp_def_strength', 'win_rate_10', 'rest_days', 'b2b', 'is_home']
    team_feats = team[feat_cols]

    match_df = df[['date', 'home', 'away', 'win', 'home_goals', 'away_goals']].copy()
    for side, team_col in [('home', 'home'), ('away', 'away')]:
        sf = team_feats.add_prefix(f'{side}_')
        sf.rename(columns={f'{side}_date': 'date', f'{side}_team': team_col}, inplace=True)
        match_df = pd.merge_asof(
            match_df.sort_values('date'),
            sf.sort_values('date'),
            by=team_col, left_on='date', right_on='date', direction='backward'
        )

    match_df['off_vs_def'] = match_df['home_gf_ewm5'] - match_df['away_ga_ewm5']
    match_df['b2b_diff'] = match_df['home_b2b'] - match_df['away_b2b']
    match_df = match_df.ffill().fillna(0)

    match_df['spread_result'] = ((match_df['home_goals'] - match_df['away_goals']) > 0.5).astype(int)
    match_df['total_result'] = ((match_df['home_goals'] + match_df['away_goals']) > 2.5).astype(int)

    match_df.to_csv(output_csv, index=False)
    print(f"✅ 特征已保存至 {output_csv}，列数：{match_df.shape[1]}")

if __name__ == '__main__':
    build_features()
