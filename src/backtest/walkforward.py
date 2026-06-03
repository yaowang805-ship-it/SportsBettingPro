#!/usr/bin/env python3
"""职业级全量时序回测 (Walk-Forward Backtest)，无语法错误版"""
import pandas as pd, numpy as np, json, joblib, sys, os
from pathlib import Path
from datetime import datetime, timedelta
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier
from lightgbm import LGBMClassifier
from catboost import CatBoostClassifier
from sklearn.metrics import brier_score_loss, log_loss
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, str(Path.cwd()))

print("=" * 60)
print("📊 全量时间序列回测 (Walk-Forward Backtest)")
print("=" * 60)

master = pd.read_csv("data/processed/bb_features_master.csv", parse_dates=['date']).sort_values('date')
with open("models/model_bb_features.json") as f:
    feat_cols = json.load(f)

trainable = master.dropna(subset=['spread_home', 'total_line']).copy()
trainable['spread_target'] = ((trainable['spread_diff'] + trainable['spread_home']) > 0).astype(int)
trainable['total_target'] = (trainable['total_score'] > trainable['total_line']).astype(int)

trainable['season'] = trainable['date'].dt.year
seasons = sorted(trainable['season'].unique())

backtest_records = []
for test_season in seasons[5:]:
    train = trainable[trainable['season'] < test_season]
    test = trainable[trainable['season'] == test_season]

    if len(test) == 0:
        continue

    X_train = train[feat_cols].fillna(0).values
    X_test = test[feat_cols].fillna(0).values

    for target, name in [('win', '胜负'), ('spread_target', '让分'), ('total_target', '大小分')]:
        y_train = train[target].values
        y_test = test[target].values

        xgb = CalibratedClassifierCV(XGBClassifier(n_estimators=150, max_depth=3, learning_rate=0.05), cv=2).fit(X_train, y_train)
        lgb = LGBMClassifier(n_estimators=150, max_depth=3, learning_rate=0.05, verbose=-1).fit(X_train, y_train)
        cat = CatBoostClassifier(iterations=150, depth=3, learning_rate=0.05, verbose=0).fit(X_train, y_train)

        xgb_prob = xgb.predict_proba(X_test)[:, 1]
        lgb_prob = lgb.predict_proba(X_test)[:, 1]
        cat_prob = cat.predict_proba(X_test)[:, 1]

        ensemble_prob = (xgb_prob + lgb_prob + cat_prob) / 3
        ensemble_prob = np.clip(ensemble_prob, 0.05, 0.95)

        if 'market_home_prob' in feat_cols and target == 'win':
            mkt_prob = X_test[:, feat_cols.index('market_home_prob')]
        else:
            mkt_prob = np.full(len(y_test), 0.5)

        shrunk_prob = 0.7 * mkt_prob + 0.3 * ensemble_prob

        brier_model = brier_score_loss(y_test, ensemble_prob)
        brier_shrunk = brier_score_loss(y_test, shrunk_prob)
        acc_model = (y_test == (ensemble_prob > 0.5)).mean()
        acc_shrunk = (y_test == (shrunk_prob > 0.5)).mean()

        # 安全计算 logloss
        logloss_val = float('nan')
        try:
            logloss_val = log_loss(y_test, shrunk_prob)
        except ValueError:
            pass

        test_odds = test['home_odds'].values if target == 'win' else np.full(len(test), 2.0)
        kelly_stakes = np.maximum(0, (shrunk_prob * test_odds - 1) / (test_odds - 1)) * 0.25 * 1000
        kelly_stakes = np.minimum(kelly_stakes, 50)
        profits = np.where(shrunk_prob > 0.5,
                           np.where(y_test == 1, kelly_stakes * (test_odds - 1), -kelly_stakes),
                           0)
        total_profit = profits.sum()

        backtest_records.append({
            'season': test_season, 'target': name, 'n_games': len(test),
            'brier_model': brier_model, 'brier_shrunk': brier_shrunk,
            'acc_model': acc_model, 'acc_shrunk': acc_shrunk,
            'logloss': logloss_val, 'kelly_profit': total_profit
        })

    print(f'✅ {test_season} 赛季完成')

bt_df = pd.DataFrame(backtest_records)
bt_df.to_csv('data/storage/backtest_history.csv', index=False)

print("\n📊 回测汇总报告")
summary = bt_df.groupby('target').agg({
    'brier_shrunk': 'mean', 'acc_shrunk': 'mean', 'kelly_profit': 'sum',
    'n_games': 'sum'
}).round(4)
print(summary)
print(f"\n总投注利润: {bt_df['kelly_profit'].sum():.1f} 元")

benchmark = {
    'date': datetime.now().isoformat(),
    'total_profit': float(bt_df['kelly_profit'].sum()),
    'summary': summary.to_dict()
}
with open('data/storage/performance_benchmark.json', 'w') as f:
    json.dump(benchmark, f, indent=2)
print("\n✅ 回测完成，基准已保存")
