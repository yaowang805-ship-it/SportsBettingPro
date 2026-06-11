import pandas as pd
import numpy as np
import os
import glob
from lightgbm import LGBMClassifier

ODDS_DIR = "data/free_odds"
LEAGUES = {'E0':'英超','D1':'德甲','I1':'意甲','SP1':'西甲','F1':'法甲'}
all_results = []

for fp in sorted(glob.glob(f"{ODDS_DIR}/*.csv")):
    fname = os.path.basename(fp)
    parts = fname.replace('.csv','').split('_')
    if len(parts) < 2: continue
    code, season = parts[0], parts[1]
    if code not in LEAGUES: continue
    
    try:
        raw = pd.read_csv(fp, encoding='latin-1')
        if 'B365H' not in raw.columns: continue
        
        data = raw[['Date','HomeTeam','AwayTeam','FTHG','FTAG','B365H','B365>2.5']].copy()
        data.columns = ['date','home','away','hg','ag','oh','over']
        data['date'] = pd.to_datetime(data['date'], dayfirst=True)
        data['home'] = data['home'].str.lower()
        data['away'] = data['away'].str.lower()
        data = data.dropna()
        data['hg'] = pd.to_numeric(data['hg'], errors='coerce')
        data['ag'] = pd.to_numeric(data['ag'], errors='coerce')
        data['over'] = pd.to_numeric(data['over'], errors='coerce')
        data['oh'] = pd.to_numeric(data['oh'], errors='coerce')
        data = data.dropna()
        data = data.query('over > 1').sort_values('date').reset_index(drop=True)
        if len(data) < 100: continue
        
        data['total_result'] = ((data['hg'] + data['ag']) > 2.5).astype(int)
        data['home_win'] = (data['hg'] > data['ag']).astype(int)
        
        team_stats = {}
        def get_stats(team, d):
            if team not in team_stats or not team_stats[team]: return [0]*6
            best = None
            for rec in team_stats[team]:
                if rec[0] < d: best = rec
            return best[1] if best else [0]*6
        
        for i, row in data.iterrows():
            d = row['date']; ht = row['home']; at = row['away']
            hs = get_stats(ht, d); asc = get_stats(at, d)
            keys = ['h_gf5','h_ga5','h_net5','h_ewm5','h_ewma5','h_win5']
            for k, v in zip(keys, hs): data.at[i, k] = v
            keys2 = ['a_gf5','a_ga5','a_net5','a_ewm5','a_ewma5','a_win5']
            for k, v in zip(keys2, asc): data.at[i, k] = v
            data.at[i, 'off_def'] = data.at[i, 'h_ewm5'] - data.at[i, 'a_ewma5']
            
            def update(team, gf, ga, d):
                if team not in team_stats: team_stats[team] = []
                all_gf = [r[2] for r in team_stats[team]] + [gf]
                all_ga = [r[3] for r in team_stats[team]] + [ga]
                n = len(all_gf)
                stats = [
                    np.mean(all_gf[-5:]) if n>=5 else np.mean(all_gf),
                    np.mean(all_ga[-5:]) if n>=5 else np.mean(all_ga),
                    np.mean([a-b for a,b in zip(all_gf,all_ga)][-5:]) if n>=5 else 0,
                    pd.Series(all_gf).ewm(span=5,adjust=False).mean().iloc[-1],
                    pd.Series(all_ga).ewm(span=5,adjust=False).mean().iloc[-1],
                    np.mean([1 if a>b else 0 for a,b in zip(all_gf[-5:],all_ga[-5:])]) if n>=5 else 0.5,
                ]
                team_stats[team].append((d, stats, gf, ga))
            update(ht, data.at[i,'hg'], data.at[i,'ag'], d)
            update(at, data.at[i,'ag'], data.at[i,'hg'], d)
        
        data = data.fillna(0)
        feat_cols = keys + keys2 + ['off_def']
        X = data[feat_cols].values
        
        for target_name, y_col in [('大小球','total_result'), ('胜负','home_win')]:
            y = data[y_col].values.astype(int)
            odds_arr = data['over'].values if target_name == '大小球' else data['oh'].values
            capital = 1000; bets = wins = 0
            for i in range(80, len(data)):
                model = LGBMClassifier(n_estimators=50, max_depth=2, num_leaves=6,
                                       min_child_samples=100, random_state=42, verbosity=-1)
                model.fit(X[:i], y[:i])
                prob = model.predict_proba(X[i:i+1])[:,1][0]
                val = odds_arr[i]; mp = 1.0/val
                prob_mix = prob*0.3 + mp*0.7; edge = prob_mix - mp
                if edge > 0.02:
                    kelly = (prob_mix*val-1)/(val-1)
                    stake = capital * min(max(kelly*0.25,0), 0.02)
                else: stake = 0
                if stake > 0:
                    bets += 1
                    if y[i] == 1: capital += stake*(val-1); wins += 1
                    else: capital -= stake
            ret = (capital - 1000)/1000
            wr = wins/bets if bets else 0
            all_results.append({
                '联赛': LEAGUES[code], '赛季': f"20{season[:2]}-{season[2:]}",
                '玩法': target_name, '比赛': len(data), '投注': bets,
                '胜率': f"{wr:.1%}", '回报': f"{ret:+.2%}"
            })
        short_season = season[:2] + '/' + season[2:]
        print(f"✅ {LEAGUES[code]} {short_season} 完成")
    except Exception as e:
        print(f"❌ {fname}: {e.__class__.__name__}")

if all_results:
    df = pd.DataFrame(all_results)
    df.to_csv("batch_backtest_results.csv", index=False)
    ret_nums = df['回报'].str.rstrip('%').astype(float)
    print(f"\n📊 回测汇总 | 总赛季: {len(df)} | 平均回报: {ret_nums.mean()/100:+.2%} | 盈利占比: {(ret_nums>0).mean():.0%}")
    print(df.to_string(index=False))
