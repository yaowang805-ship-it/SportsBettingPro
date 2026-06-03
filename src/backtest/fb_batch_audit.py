import pandas as pd, numpy as np, os, glob, json
from lightgbm import LGBMClassifier

ODDS_DIR = "data/free_odds"
LEAGUES = {'E0':'英超','D1':'德甲','I1':'意甲','SP1':'西甲','F1':'法甲'}
all_results = []
audit_log = []

with open("models/model_fb_features.json") as f:
    FB_COLS = json.load(f)
print(f"使用深度特征: {len(FB_COLS)} 维")

for fp in sorted(glob.glob(f"{ODDS_DIR}/*.csv")):
    fname = os.path.basename(fp)
    parts = fname.replace('.csv','').split('_')
    if len(parts) < 2: continue
    code, season = parts[0], parts[1]
    if code not in LEAGUES: continue
    
    try:
        raw = pd.read_csv(fp, encoding='latin-1')
        if 'B365H' not in raw.columns: continue
        
        # 使用深度数据列（如果存在）
        use_deep = all(c in raw.columns for c in ['HS','AS','HST','AST','HC','AC'])
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
        if len(data) < 100: continue
        
        data['total_result'] = ((data['hg'] + data['ag']) > 2.5).astype(int)
        data['home_win'] = (data['hg'] > data['ag']).astype(int)
        
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
            hs = get_stats(ht, d, feat_count)
            asc = get_stats(at, d, feat_count)
            if use_deep:
                keys = ['h_gf5','h_gf10','h_ga5','h_ga10','h_net5','h_net10','h_ewm5','h_ewma5',
                        'h_shots5','h_shots_on5','h_corners5','h_shots10','h_shots_on10','h_corners10']
                for k, v in zip(keys, hs): data.at[i, k] = v
                keys2 = ['a_gf5','a_gf10','a_ga5','a_ga10','a_net5','a_net10','a_ewm5','a_ewma5',
                         'a_shots5','a_shots_on5','a_corners5','a_shots10','a_shots_on10','a_corners10']
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
                    pd.Series(all_gf).ewm(span=5,adjust=False).mean().iloc[-1],
                    pd.Series(all_ga).ewm(span=5,adjust=False).mean().iloc[-1],
                ]
                if use_deep:
                    stats += [
                        np.mean(all_shots[-5:]) if n>=5 else np.mean(all_shots),
                        np.mean(all_shots_on[-5:]) if n>=5 else np.mean(all_shots_on),
                        np.mean(all_corners[-5:]) if n>=5 else np.mean(all_corners),
                        np.mean(all_shots[-10:]) if n>=10 else np.mean(all_shots),
                        np.mean(all_shots_on[-10:]) if n>=10 else np.mean(all_shots_on),
                        np.mean(all_corners[-10:]) if n>=10 else np.mean(all_corners),
                    ]
                team_stats[team].append((d, stats, gf, ga, shots, shots_on, corners))
            update(ht, data.at[i,'hg'], data.at[i,'ag'], d,
                   row.get('hs',0) or 0, row.get('hst',0) or 0, row.get('hc',0) or 0)
            update(at, data.at[i,'ag'], data.at[i,'hg'], d,
                   row.get('as',0) or 0, row.get('ast',0) or 0, row.get('ac',0) or 0)
        
        data = data.fillna(0)
        avail_cols = [c for c in FB_COLS if c in data.columns]
        if len(avail_cols) < 6: continue
        X = data[avail_cols].values
        
        for target_name, y_col, odds_col in [('大小球','total_result','over'), ('胜负','home_win','oh')]:
            y = data[y_col].values.astype(int)
            odds_arr = data[odds_col].values
            capital = 1000; bets = wins = 0
            max_odds_seen = 0
            for i in range(80, len(data)):
                model = LGBMClassifier(n_estimators=50, max_depth=2, num_leaves=6,
                                       min_child_samples=100, random_state=42, verbosity=-1)
                model.fit(X[:i], y[:i])
                prob = model.predict_proba(X[i:i+1])[:,1][0]
                val = odds_arr[i]
                if val > max_odds_seen: max_odds_seen = val
                mp = 1.0/val
                prob_mix = prob*0.3 + mp*0.7
                edge = prob_mix - mp
                if edge > 0.02:
                    kelly = (prob_mix*val-1)/(val-1)
                    stake = capital * min(max(kelly*0.25,0), 0.02)
                    # 审计：超过50倍赔率则记录
                    if val > 50:
                        audit_log.append({
                            '联赛': LEAGUES[code], '赛季': f"20{season[:2]}-{season[2:]}",
                            '玩法': target_name, '日期': data.iloc[i]['date'].strftime('%Y-%m-%d'),
                            '主队': data.iloc[i]['home'], '客队': data.iloc[i]['away'],
                            '赔率': f"{val:.2f}", '模型概率': f"{prob:.1%}",
                            '下注比例': f"{stake/capital*100:.1f}%",
                        })
                else: stake = 0
                if stake > 0:
                    bets += 1
                    if y[i] == 1: capital += stake*(val-1); wins += 1
                    else: capital -= stake
            ret = (capital - 1000)/1000
            wr = wins/bets if bets else 0
            
            # 审计异常赛季（回报率超过 +50%）
            if abs(ret) > 0.50:
                audit_log.append({
                    '联赛': LEAGUES[code], '赛季': f"20{season[:2]}-{season[2:]}",
                    '玩法': f"{target_name} (异常)", '日期': '—',
                    '主队': f'回报率 {ret:+.1%}', '客队': f'投注 {bets} 次',
                    '赔率': f'最大赔率 {max_odds_seen:.1f}',
                    '模型概率': f'胜率 {wr:.1%}',
                    '下注比例': f'终值 {capital:.0f}',
                })
            
            all_results.append({
                '联赛': LEAGUES[code], '赛季': f"20{season[:2]}-{season[2:]}",
                '玩法': target_name, '特征': '深度' if use_deep else '基础',
                '比赛': len(data), '投注': bets,
                '胜率': f"{wr:.1%}", '回报': f"{ret:+.2%}"
            })
    except Exception as e:
        pass

if all_results:
    df = pd.DataFrame(all_results)
    df.to_csv("batch_backtest_v2.csv", index=False)
    ret_nums = df['回报'].str.rstrip('%').astype(float)
    
    print("\n📊 审计发现的异常赛季 (回报率 > |50%|):")
    if audit_log:
        audit_df = pd.DataFrame(audit_log).drop_duplicates()
        print(audit_df.to_string(index=False))
    else:
        print("  ✅ 未发现异常赛季")
    
    print(f"\n📊 30维深度特征回测汇总:")
    print(f"总赛季: {len(df)} | 平均回报: {ret_nums.mean()/100:+.2%} | 盈利占比: {(ret_nums>0).mean():.0%}")
    
    # 按特征类型分组
    for feat_type in df['特征'].unique():
        sub = df[df['特征'] == feat_type]
        sub_ret = sub['回报'].str.rstrip('%').astype(float)
        print(f"  {feat_type}特征: {len(sub)}赛季, 平均回报 {sub_ret.mean()/100:+.2%}, 盈利 {(sub_ret>0).mean():.0%}")
    
    print(df.to_string(index=False))
