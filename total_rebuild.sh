#!/bin/bash
set -e
source venv/bin/activate
export PYTHONPATH="$PWD"

echo "🔧 终极重建：基于 V3 特征重训所有模型..."

# 1. 重训 NBA 模型（使用 bb_features_v3.csv）
python3 << 'BB'
import pandas as pd, joblib, json
from sklearn.model_selection import TimeSeriesSplit
from sklearn.calibration import CalibratedClassifierCV
from lightgbm import LGBMClassifier

df = pd.read_csv('data/processed/bb_features_v3.csv')
df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
exclude = ['date','home','away','win','spread_result','total_result']
feat_cols = [c for c in df.columns if c not in exclude and df[c].dtype in ['float64','int64']]
print(f"NBA 特征数: {len(feat_cols)}")
X = df[feat_cols].fillna(0)
tscv = TimeSeriesSplit(n_splits=5)
base = dict(n_estimators=200, max_depth=5, random_state=42, verbosity=-1)
for target, name in [('win','win'),('spread_result','spread'),('total_result','total')]:
    y = df[target]
    model = CalibratedClassifierCV(LGBMClassifier(**base), method='isotonic', cv=tscv)
    model.fit(X, y)
    joblib.dump(model, f'models/model_bb_{name}.pkl')
    print(f"  model_bb_{name}.pkl 已保存")
with open('models/model_bb_features.json','w') as f:
    json.dump(feat_cols, f)
BB

# 2. 重训足球模型（使用 fb_features_v3.csv）
python3 << 'FB'
import pandas as pd, joblib, json
from sklearn.model_selection import TimeSeriesSplit
from sklearn.calibration import CalibratedClassifierCV
from lightgbm import LGBMClassifier

df = pd.read_csv('data/processed/fb_features_v3.csv')
# 修复日期列
try:
    df['date'] = pd.to_datetime(df['date'], format='%Y-%m-%d')
except:
    df['date'] = pd.to_datetime(df['date'], format='mixed', utc=True).dt.tz_localize(None)

exclude = ['date','home','away','win','spread_result','total_result']
feat_cols = [c for c in df.columns if c not in exclude and df[c].dtype in ['float64','int64']]
print(f"足球特征数: {len(feat_cols)}")
X = df[feat_cols].fillna(0)
tscv = TimeSeriesSplit(n_splits=5)
base = dict(n_estimators=200, max_depth=5, random_state=42, verbosity=-1)
for target, name in [('win','win'),('spread_result','spread'),('total_result','total')]:
    y = df[target]
    model = CalibratedClassifierCV(LGBMClassifier(**base), method='isotonic', cv=tscv)
    model.fit(X, y)
    joblib.dump(model, f'models/model_fb_{name}.pkl')
    print(f"  model_fb_{name}.pkl 已保存")
with open('models/model_fb_features.json','w') as f:
    json.dump(feat_cols, f)
FB

echo "✅ 重训完成，启动系统..."
./run_all.sh
