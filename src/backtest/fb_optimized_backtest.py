import pandas as pd
import numpy as np
import os
import glob
from lightgbm import LGBMClassifier
from collections import deque

ODDS_DIR = "data/free_odds"
LEAGUES = {'E0':'英超','D1':'德甲','I1':'意甲','SP1':'西甲','F1':'法甲'}
MAX_ODDS = 8.0          # 赔率上限，超过的不参与投注
BASE_KELLY_FRAC = 0.25   # 基础凯利分数
KELLY_WINDOW = 20        # 动态凯利窗口（最近N场）
MIN_TRAIN = 80

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
        
        use_deep = all(c in raw.columns for c in ['HS','AS'])
        cols = ['Date','HomeTeam','AwayTeam','FTHG','FTAG','B365H','B365>2.5']
        if use_deep: cols += ['HS','AS','HST','AST','HC','AC']
        
        data = raw[cols].copy()
        data.columns = (['date','home','away','hg','ag','oh','over'] +
                       (['hs','as','hst','ast','hc','ac'] if use_deep else []))
        data['date'] = pd.to_datetime(data['date'], dayfirst=True)
        data['home'] = data['home'].str.lower()
        data['away'] = data['away'].str.lower()
        data = data.dropna()
        for c in ['hg','ag','over','oh']:
            data[c] = pd.to_numeric(data[c], errors='coerce')
        data = data.dropna()
        data = data.query('over > 1 and oh > 1').sort_values('date').reset_index(drop=True)
        if len(data) < MIN_TRAIN + 20: continue
        
        data['total_result'] = ((data['hg'] + data['ag']) > 2.5).astype(int)
        data['home_win'] = (data['hg'] > data['ag']).astype(int)
        
        # ==== 优化1: 将赔率隐含概率作为特征加入 ====
        data['market_prob_home'] = 1.0 / data['oh']
        data['market_prob_over'] = 1.0 / data['over']
        
        team_stats = {}
        def get_stats(team, d, n=14):
            if team not in team_stats or not team_stats[team]: return [0]*n
            best = None
            for rec in team_stats[team]:
                if rec[0] < d: best = rec
            return best[1] if best else [0]*n
        
        feat_count = 14 if use_deep else 6
        for i, row in data.iterrows():
            d = row['date']; ht = row['home']; at = row['away']
            hs = get_stats(ht, d, feat_count); asc = get_stats(at, d, feat_count)
            if use_deep:
                keys = ['h_gf5','h_gf10','h_ga5','h_ga10','h_net5','h_net10',
                        'h_shots5','h_shots_on5','h_corners5',
                        'h_shots10','h_shots_on10','h_corners10','h_ewm5','h_ewma5']
                for k, v in zip(keys, hs): data.at[i, k] = v
                keys2 = ['a_gf5','a_gf10','a_ga5','a_ga10','a_net5','a_net10',
                         'a_shots5','a_shots_on5','a_corners5',
                         'a_shots10','a_shots_on10','a_corners10','a_ewm5','a_ewma5']
                for k, v in zip(keys2, asc): data.at[i, k] = v
            else:
                keys = ['h_gf5','h_ga5','h_net5','h_ewm5','h_ewma5','h_win5']
                for k, v in zip(keys, hs): data.at[i, k] = v
                keys2 = ['a_gf5','a_ga5','a_net5','a_ewm5','a_ewma5','a_win5']
                for k, v in zip(keys2, asc): data.at[i, k] = v
            data.at[i, 'off_def'] = data.at[i, 'h_ewm5'] - data.at[i, 'a_ewma5']
            
            def update(team, gf, ga, d, shots=0, shots_on=0, corners=0):
                if team not in team_stats: team_stats[team] = []
                all_gf = [r[2] for r in team_stats[team]] + [gf]
                all_ga = [r[3] for r in team_stats[team]] + [ga]
                all_shots = [r[4] for r in team_stats[team]] + [shots]
                all_shots_on = [r[5] for r in team_stats[team]] + [shots_on]
                all_corners = [r[6] for r in team_stats[team]] + [corners]
                n = len(all_gf)
                stats = [
                    np.mean(all_gf[-5:]) if n>=5 else np.mean(all_gf),
                    np.mean(all_gf[-10:]) if n>=10 else np.mean(all_gf),
                    np.mean(all_ga[-5:]) if n>=5 else np.mean(all_ga),
                    np.mean(all_ga[-10:]) if n>=10 else np.mean(all_ga),
                    np.mean([a-b for a,b in zip(all_gf,all_ga)][-5:]) if n>=5 else 0,
                    np.mean([a-b for a,b in zip(all_gf,all_ga)][-10:]) if n>=10 else 0,
                ]
                if use_deep:
                    stats += [
                        np.mean(all_shots[-5:]) if n>=5 else np.mean(all_shots),
                        np.mean(all_shots_on[-5:]) if n>=5 else np.mean(all_shots_on),
                        np.mean(all_corners[-5:]) if n>=5 else np.mean(all_corners),
                        np.mean(all_shots[-10:]) if n>=10 else np.mean(all_shots),
                        np.mean(all_shots_on[-10:]) if n>=10 else np.mean(all_shots_on),
                        np.mean(all_corners[-10:]) if n>=10 else np.mean(all_corners),
                        pd.Series(all_gf).ewm(span=5,adjust=False).mean().iloc[-1],
                        pd.Series(all_ga).ewm(span=5,adjust=False).mean().iloc[-1],
                    ]
                team_stats[team].append((d, stats, gf, ga, shots, shots_on, corners))
            update(ht, data.at[i,'hg'], data.at[i,'ag'], d,
                   row.get('hs',0) or 0, row.get('hst',0) or 0, row.get('hc',0) or 0)
            update(at, data.at[i,'ag'], data.at[i,'hg'], d,
                   row.get('as',0) or 0, row.get('ast',0) or 0, row.get('ac',0) or 0)
        
        data = data.fillna(0)
        feat_cols = keys + keys2 + ['off_def', 'market_prob_home', 'market_prob_over']
        avail_cols = [c for c in feat_cols if c in data.columns]
        if len(avail_cols) < 6: continue
        X = data[avail_cols].values
        
        for target_name, y_col, odds_col, market_prob_col in [
            ('大小球','total_result','over','market_prob_over'),
            ('胜负','home_win','oh','market_prob_home')
        ]:
            y = data[y_col].values.astype(int)
            odds_arr = data[odds_col].values
            capital = 1000; bets = wins = 0
            recent_results = deque(maxlen=KELLY_WINDOW)  # 存储最近N场结果
            
            for i in range(MIN_TRAIN, len(data)):
                model = LGBMClassifier(n_estimators=50, max_depth=2, num_leaves=6,
                                       min_child_samples=100, random_state=42, verbosity=-1)
                model.fit(X[:i], y[:i])
                prob = model.predict_proba(X[i:i+1])[:,1][0]
                val = odds_arr[i]
                
                # ==== 优化2: 赔率上限过滤 ====
                if val > MAX_ODDS:
                    continue
                
                mp = 1.0/val
                prob_mix = prob*0.3 + mp*0.7
                edge = prob_mix - mp
                
                if edge > 0.02:
                    # ==== 优化3: 动态凯利分数 ====
                    if len(recent_results) >= 10:
                        recent_win_rate = sum(recent_results) / len(recent_results)
                        # 近期胜率映射到凯利倍率 (0.4~1.6)
                        kelly_multiplier = 0.4 + recent_win_rate * 1.2
                    else:
                        kelly_multiplier = 1.0
                    
                    kelly = (prob_mix*val - 1)/(val - 1)
                    stake = capital * min(max(kelly * BASE_KELLY_FRAC * kelly_multiplier, 0), 0.02)
                else:
                    stake = 0
                
                if stake > 0:
                    bets += 1
                    if y[i] == 1:
                        capital += stake*(val-1)
                        wins += 1
                        recent_results.append(1)
                    else:
                        capital -= stake
                        recent_results.append(0)
            
            ret = (capital - 1000)/1000
            wr = wins/bets if bets else 0
            all_results.append({
                '联赛': LEAGUES[code], '赛季': f"20{season[:2]}-{season[2:]}",
                '玩法': target_name, '比赛': len(data), '投注': bets,
                '胜率': f"{wr:.1%}", '回报': f"{ret:+.2%}"
            })
    except Exception:
        pass

if all_results:
    df = pd.DataFrame(all_results)
    df.to_csv("optimized_backtest.csv", index=False)
    ret_nums = df['回报'].str.rstrip('%').astype(float)
    
    print("\n📊 三重优化后回测汇总:")
    print(f"总赛季: {len(df)} | 平均回报: {ret_nums.mean()/100:+.2%} | 盈利占比: {(ret_nums>0).mean():.0%}")
    
    for play in df['玩法'].unique():
        sub = df[df['玩法'] == play]
        sub_ret = sub['回报'].str.rstrip('%').astype(float)
        print(f"  {play}: {len(sub)}赛季, 平均回报 {sub_ret.mean()/100:+.2%}, 盈利 {(sub_ret>0).mean():.0%}")
    
    print(df.to_string(index=False))
