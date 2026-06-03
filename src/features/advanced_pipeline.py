import pandas as pd, numpy as np, json, os

def build_features(scores_csv='data/storage/basketball_scores_combined.csv',
                   injuries_json='data/odds/nba_injuries.json',
                   output_csv='data/processed/bb_features_final.csv'):
    df = pd.read_csv(scores_csv)
    df.columns = [c.strip().lower() for c in df.columns]
    df['date'] = pd.to_datetime(df['date'])
    df['win'] = (df['home_score'] > df['away_score']).astype(int)

    home = df[['date','home','home_score','away_score']].copy()
    home.columns = ['date','team','pts_for','pts_against']; home['home']=1
    away = df[['date','away','away_score','home_score']].copy()
    away.columns = ['date','team','pts_for','pts_against']; away['home']=0
    team = pd.concat([home,away]).sort_values(['team','date'])

    for w in [5,10,20]:
        team[f'pts_avg_{w}'] = team.groupby('team')['pts_for'].transform(lambda x: x.shift(1).rolling(w,min_periods=1).mean())
        team[f'pts_against_avg_{w}'] = team.groupby('team')['pts_against'].transform(lambda x: x.shift(1).rolling(w,min_periods=1).mean())
        team[f'net_avg_{w}'] = team[f'pts_avg_{w}'] - team[f'pts_against_avg_{w}']
    team['pts_ewm10'] = team.groupby('team')['pts_for'].transform(lambda x: x.shift(1).ewm(span=10,adjust=False).mean())
    team['pts_against_ewm10'] = team.groupby('team')['pts_against'].transform(lambda x: x.shift(1).ewm(span=10,adjust=False).mean())
    team['is_win'] = (team['pts_for'] > team['pts_against']).astype(int)
    team['win_rate_10'] = team.groupby('team')['is_win'].transform(lambda x: x.shift(1).rolling(10,min_periods=1).mean())
    team['rest_days'] = team.groupby('team')['date'].diff().dt.days.fillna(3)
    team['b2b'] = (team['rest_days'] == 1).astype(int)

    # 伤病特征
    if os.path.exists(injuries_json):
        with open(injuries_json) as f:
            inj_data = json.load(f)
        # 构建日期->球队->伤病球员数的映射
        from collections import defaultdict
        inj_map = defaultdict(lambda: defaultdict(int))
        for team_name, records in inj_data.items():
            for rec in records:
                d = rec.get('date', '')
                inj_map[d][team_name] += 1
        def get_inj(team_name, date_str):
            return inj_map.get(date_str, {}).get(team_name, 0)
        team['injuries'] = team.apply(lambda row: get_inj(row['team'], row['date'].strftime('%Y-%m-%d')), axis=1)
    else:
        team['injuries'] = 0

    feat_cols = ['date','team','pts_avg_5','pts_avg_10','pts_avg_20',
                 'pts_against_avg_5','pts_against_avg_10','pts_against_avg_20',
                 'net_avg_5','net_avg_10','net_avg_20',
                 'pts_ewm10','pts_against_ewm10','win_rate_10','rest_days','b2b','injuries','home']
    team_feats = team[feat_cols]

    match_df = df[['date','home','away','win']].copy()
    for side, col in [('home','home'), ('away','away')]:
        sf = team_feats.add_prefix(f'{side}_')
        sf.rename(columns={f'{side}_date':'date', f'{side}_team':col}, inplace=True)
        match_df = pd.merge_asof(match_df.sort_values('date'), sf.sort_values('date'),
                                 by=col, left_on='date', right_on='date', direction='backward')
    match_df['off_vs_def'] = match_df['home_pts_ewm10'] - match_df['away_pts_against_ewm10']
    match_df['inj_diff'] = match_df['home_injuries'] - match_df['away_injuries']
    match_df['b2b_diff'] = match_df['home_b2b'] - match_df['away_b2b']
    match_df = match_df.ffill().fillna(0)

    match_df['spread_result'] = ((df['home_score'] - df['away_score']) > -2.0).astype(int)
    match_df['total_result'] = ((df['home_score'] + df['away_score']) > 220).astype(int)

    match_df.to_csv(output_csv, index=False)
    print(f"🏀 最终特征已保存 ({output_csv})，包含伤病信息")

if __name__ == '__main__':
    build_features()
