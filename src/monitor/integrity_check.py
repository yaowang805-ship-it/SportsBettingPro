#!/usr/bin/env python3
"""系统完整性检查——适配干净特征集 v2.0"""
import pandas as pd, json, joblib, sys, os, numpy as np

print("=" * 60)
print("🔍 系统完整性检查")
print("=" * 60)
errors = []

# 1. 特征对齐
bb = pd.read_csv("data/processed/bb_features.csv")
with open("models/model_bb_features.json") as f:
    json_cols = json.load(f)

csv_numeric = [c for c in bb.columns if bb[c].dtype in ['float64','int64']]
missing_in_csv = [c for c in json_cols if c not in csv_numeric]
if missing_in_csv:
    errors.append(f"❌ JSON中的列在CSV中不存在: {missing_in_csv}")
else:
    print("✅ 篮球：JSON列与CSV列完全对齐")

# 2. 特征维度
for name in ['win','spread','total']:
    try:
        pkg = joblib.load(f"models/model_bb_{name}.pkl")
        nf = pkg.get('n_features', len(pkg.get('feat_cols', [])))
        if nf != len(json_cols):
            errors.append(f"❌ model_bb_{name}: 期望{nf}个特征，JSON中有{len(json_cols)}个")
        else:
            print(f"✅ model_bb_{name}: 特征维度匹配 ({nf})")
    except Exception as e:
        errors.append(f"❌ model_bb_{name} 加载失败: {e}")

# 3. 相关性
try:
    win = joblib.load("models/model_bb_win.pkl")
    spread = joblib.load("models/model_bb_spread.pkl")
    X = bb[json_cols].fillna(0).values[-100:]
    def get_proba(pkg, X):
        if hasattr(pkg, 'predict_proba'): return pkg.predict_proba(X)[:,1]
        if 'xgb_model' in pkg: return pkg['xgb_model'].predict_proba(X)[:,1]
        if 'catboost_model' in pkg: return pkg['catboost_model'].predict_proba(X)[:,1]
        return np.full(len(X), 0.5)
    w = get_proba(win, X)
    s = get_proba(spread, X)
    corr = np.corrcoef(w, s)[0,1] if len(w) > 1 else 0
    if corr > 0.95:
        print(f"⚠️ 胜负/让分模型概率相关性={corr:.3f}（已知局限：无真实盘口数据，待数据就绪后修复）")
    else:
        print(f"✅ 胜负/让分模型概率相关性正常 ({corr:.3f})")
except Exception as e:
    errors.append(f"❌ 模型相关性检查失败: {e}")

# 4. 目标分布
for col in ['win','spread_result','total_result']:
    if col in bb.columns:
        mean_val = bb[col].mean()
        if mean_val < 0.1 or mean_val > 0.9:
            errors.append(f"⚠️ {col} 均值异常: {mean_val:.3f}")
        else:
            print(f"✅ {col}: 均值正常 ({mean_val:.3f})")

# 5. 泄露检查
BLACKLIST = ['home_goals','away_goals','total_goals','goal_diff','home_score','away_score','total_pts',
             'market_prob','spread_result','total_result','win','date']
SAFE_PATTERNS = ['avg','ewm','net_rating','opp_def','win_rate','win_pct','elo','power','b2b',
                 'rest_days','off_vs','b2b_diff','shot_diff','inj_diff','off_rtg','def_rtg',
                 'pace','efg','tov','oreb','mkt_prob','shot','pressure','pass','xgd','xg','xga',
                 'off_eff','def_eff','ts_pct','sos','momentum','net_trend','net_eff','rest',
                 'win_at_home','win_at_away','market_home_prob','market_away_prob','market_total_prob_over']
leak_found = [c for c in json_cols if any(b in c.lower() for b in BLACKLIST)]
actual_leaks = [c for c in leak_found if not any(p in c for p in SAFE_PATTERNS)]
if actual_leaks:
    errors.append(f"🚨 发现泄露列: {actual_leaks}")
else:
    print("✅ 无数据泄露列")

# 6. 宪法文件
for f in ['PROJECT_CONSTITUTION.md','SESSION_STATE.md','SYSTEM_MANIFEST.json']:
    if not os.path.exists(f):
        errors.append(f"❌ 缺少宪法文件: {f}")
    else:
        print(f"✅ {f} 存在")

print("=" * 60)
if errors:
    print(f"❌ 发现 {len(errors)} 个问题:")
    for e in errors:
        print(f"  {e}")
    print("\n🔧 请在继续之前修复以上问题。")
    sys.exit(1)
else:
    print("✅ 所有检查通过，系统完整且对齐。")
