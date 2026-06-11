#!/usr/bin/env python3
"""
绝对干净的回测：逐场严格时序，杜绝任何未来信息泄露。
只信任宪法规定的 features_stable.py 作为特征入口。
"""
import pandas as pd
import numpy as np
import sys
import warnings
from pathlib import Path

warnings.filterwarnings('ignore')
sys.path.insert(0, str(Path.cwd()))
from features_stable import build_basketball_features

# ── 1. 加载最原始的比赛数据（仅比分，不含任何预先计算的特征） ──
print("📂 加载原始比分数据...")
# 主数据源：2007-2025 博彩数据
raw = pd.read_csv("data/raw/nba_betting_2007_2025.csv", low_memory=False)
raw = raw.rename(columns={
    'date': 'date', 'home': 'home', 'away': 'away',
    'score_home': 'home_score', 'score_away': 'away_score',
    'spread': 'spread_home', 'total': 'total_line',
    'moneyline_home': 'home_odds', 'moneyline_away': 'away_odds'
})
# 只保留我们需要的列
keep_cols = ['date','home','away','home_score','away_score','spread_home','total_line','home_odds','away_odds']
raw = raw[[c for c in keep_cols if c in raw.columns]]
raw['date'] = pd.to_datetime(raw['date'])
raw = raw.dropna(subset=['home_score','away_score']).sort_values('date').reset_index(drop=True)
print(f"   原始比赛场次: {len(raw)}")

# ── 2. 计算基础目标列 ──
raw['win'] = (raw['home_score'] > raw['away_score']).astype(int)
raw['total_score'] = raw['home_score'] + raw['away_score']
raw['spread_diff'] = raw['home_score'] - raw['away_score']

# ── 3. 逐场严格回测 ──
MIN_TRAIN = 500   # 最少需要 500 场比赛才能开始预测
PREDICT_EVERY = 100  # 每 100 场更新一次模型

all_predictions = []
unique_dates = raw['date'].unique()
print(f"🔄 开始逐场回测（从第 {MIN_TRAIN} 场开始）...")

for i in range(MIN_TRAIN, len(unique_dates), PREDICT_EVERY):
    cutoff_date = unique_dates[i]
    # 训练集：截止日期之前的所有比赛
    train = raw[raw['date'] < cutoff_date]
    # 测试集：未来 N 天的比赛
    next_cutoff = unique_dates[min(i + PREDICT_EVERY, len(unique_dates)-1)]
    test = raw[(raw['date'] >= cutoff_date) & (raw['date'] < next_cutoff)]
    
    if len(test) == 0:
        continue
    
    # ── 特征构建（严格使用过去数据） ──
    train_features = []
    train_targets = []
    for _, row in train.iterrows():
        past = train[train['date'] < row['date']]
        feat = build_basketball_features(row, past)
        train_features.append(feat)
        train_targets.append(row['win'])
    
    X_train = np.array(train_features, dtype=np.float32)
    y_train = np.array(train_targets)
    
    test_features = []
    for _, row in test.iterrows():
        past = raw[raw['date'] < row['date']]  # 使用全历史数据计算特征
        feat = build_basketball_features(row, past)
        test_features.append(feat)
    X_test = np.array(test_features, dtype=np.float32)
    
    # 有真实赔率的测试集才参与投注评估
    test['has_odds'] = test['home_odds'].notna() & (test['home_odds'] != 2.0)
    
    # ── 训练简单模型 ──
    from sklearn.calibration import CalibratedClassifierCV
    from xgboost import XGBClassifier
    from sklearn.metrics import brier_score_loss
    
    try:
        model = CalibratedClassifierCV(
            XGBClassifier(n_estimators=100, max_depth=3, learning_rate=0.05),
            cv=2
        ).fit(X_train, y_train)
    except Exception as e:
        print(f"   ⚠️ 模型训练失败 @ {cutoff_date.date()}: {e}")
        continue
    
    # ── 预测 ──
    model_prob = model.predict_proba(X_test)[:, 1]
    model_prob = np.clip(model_prob, 0.05, 0.95)
    
    # 市场概率（去抽水）
    test_home_odds = test['home_odds'].fillna(2.0).clip(1.01, 21.0).values
    test_away_odds = test['away_odds'].fillna(2.0).clip(1.01, 21.0).values
    imp_home = 1 / test_home_odds
    imp_away = 1 / test_away_odds
    overround = imp_home + imp_away
    mkt_prob = imp_home / overround
    
    # 收缩概率（保守权重：市场 0.70, 模型 0.30）
    shrunk_prob = np.clip(0.70 * mkt_prob + 0.30 * model_prob, 0.05, 0.95)
    
    # ── 记录结果 ──
    for j in range(len(test)):
        all_predictions.append({
            'date': test.iloc[j]['date'],
            'home': test.iloc[j]['home'],
            'away': test.iloc[j]['away'],
            'model_prob': float(model_prob[j]),
            'market_prob': float(mkt_prob[j]),
            'shrunk_prob': float(shrunk_prob[j]),
            'actual': int(test.iloc[j]['win']),
            'home_odds': float(test_home_odds[j]),
            'away_odds': float(test_away_odds[j]),
            'has_real_odds': bool(test.iloc[j]['has_odds'])
        })
    
    # 进度报告
    n_real_odds = test['has_odds'].sum()
    if n_real_odds > 0:
        test_real = test[test['has_odds']]
        brier = brier_score_loss(
            test_real['win'].values,
            np.array([all_predictions[-len(test)+k]['shrunk_prob'] for k in range(len(test)) if test.iloc[k]['has_odds']])
        )
        acc = (model_prob > 0.5) == test['win'].values
        print(f"   📅 {cutoff_date.date()}: 训练{len(train)}场, 测试{len(test)}场, 准确率{acc.mean():.3f}")

# ── 4. 汇总分析 ──
pred_df = pd.DataFrame(all_predictions)
pred_df.to_csv('data/storage/strict_eval_predictions.csv', index=False)
print(f"\n✅ 严格回测完成，共 {len(pred_df)} 条预测")

# 只分析有真实赔率的场次
real = pred_df[pred_df['has_real_odds']].copy()
print(f"   其中有真实赔率的场次: {len(real)}")

if len(real) > 0:
    # Brier 分数
    from sklearn.metrics import brier_score_loss
    brier_model = brier_score_loss(real['actual'], real['model_prob'])
    brier_market = brier_score_loss(real['actual'], real['market_prob'])
    brier_shrunk = brier_score_loss(real['actual'], real['shrunk_prob'])
    acc_model = (real['model_prob'] > 0.5).eq(real['actual']).mean()
    acc_shrunk = (real['shrunk_prob'] > 0.5).eq(real['actual']).mean()
    
    print("\n📊 性能报告 (仅含真实赔率场次):")
    print(f"  Brier (模型): {brier_model:.4f}")
    print(f"  Brier (市场): {brier_market:.4f}")
    print(f"  Brier (收缩): {brier_shrunk:.4f}")
    print(f"  准确率 (模型): {acc_model:.4f}")
    print(f"  准确率 (收缩): {acc_shrunk:.4f}")
    
    # 按赔率区间分析
    real['odds_group'] = pd.cut(real['home_odds'], bins=[1.0, 1.4, 1.8, 2.2, 3.0, 5.0, 10.0])
    print("\n📊 按赔率区间分析:")
    print(real.groupby('odds_group', observed=False).agg(
        n=('actual', 'count'),
        model_acc=('model_prob', lambda x: ((x > 0.5) == real.loc[x.index, 'actual']).mean()),
        shrunk_acc=('shrunk_prob', lambda x: ((x > 0.5) == real.loc[x.index, 'actual']).mean()),
        actual_winrate=('actual', 'mean')
    ).round(4))
    
    # 凯利模拟（保守参数）
    kelly_stakes = np.maximum(0, (real['shrunk_prob'].values * real['home_odds'].values.clip(1.01,21) - 1) / (real['home_odds'].values.clip(1.01,21) - 1)) * 250
    kelly_stakes = np.minimum(kelly_stakes, 50)
    bets = (real['shrunk_prob'] > 0.5) & (real['home_odds'] >= 1.4)
    profits = np.where(bets, np.where(real['actual']==1, kelly_stakes*(real['home_odds'].values.clip(1.01,21)-1), -kelly_stakes), 0)
    print("\n💰 凯利投注模拟:")
    print(f"  投注数: {bets.sum()}")
    print(f"  总利润: {profits.sum():.1f}")
    print(f"  胜率: {real.loc[bets, 'actual'].mean():.4f}")
    
    # 按赛季汇总
    real['season'] = real['date'].dt.year
    seasonal = real.groupby('season').agg(
        n_games=('actual', 'count'),
        brier=('shrunk_prob', lambda x: brier_score_loss(real.loc[x.index, 'actual'], x)),
        acc=('actual', lambda x: ((real.loc[x.index, 'shrunk_prob'] > 0.5) == x).mean())
    )
    print("\n📊 按赛季汇总:")
    print(seasonal.round(4))
else:
    print("\n⚠️ 无真实赔率场次，无法评估投注性能")

print("\n✅ 严格回测报告完成")
