"""集成模型训练器 — Optuna 超参调优 + 概率校准。

用法:
    from src.models.ensemble_trainer import train_sport_ensemble
    train_sport_ensemble('bb')    # 篮球
    train_sport_ensemble('fb')    # 足球
"""
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import joblib
import numpy as np
import optuna
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier, VotingClassifier
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import TimeSeriesSplit

warnings.filterwarnings('ignore', category=UserWarning, module='optuna')

from config.settings import MODEL_DIR

MODEL_DIR_PATH = Path(MODEL_DIR) if isinstance(MODEL_DIR, str) else MODEL_DIR
MODEL_DIR_PATH.mkdir(parents=True, exist_ok=True)

N_TRIALS = 50       # Optuna trials per model (single phase)
CV_SPLITS = 5       # TimeSeriesSplit folds for CV evaluation during Optuna

# 根据运动类型和数据量自适应
SPORT_CONFIG = {
    'bb': {  # 篮球：24427 样本（↑ 12倍，原 1891），4 模型全量搜索
        'model_types': ['lgbm', 'xgb', 'cat', 'rf'],
        'n_trials': 20,
        'rf_trials': 10,  # RF 大规模数据慢，减少搜索
        'min_samples': 200,
    },
    'fb': {  # 足球：~1700 样本，简化模型防过拟合
        'model_types': ['lgbm', 'xgb'],
        'n_trials': 20,
        'min_samples': 200,
    },
}


def _tscv(n_splits=CV_SPLITS):
    return TimeSeriesSplit(n_splits=n_splits)


def _load_data(sport):
    if sport == 'bb':
        csv_path = 'data/processed/bb_features.csv'
        feat_json = MODEL_DIR_PATH / 'model_bb_features.json'
    else:
        csv_path = 'data/processed/fb_features.csv'
        feat_json = MODEL_DIR_PATH / 'model_fb_features.json'

    df = pd.read_csv(csv_path)
    df['date'] = pd.to_datetime(df['date'], utc=True, errors='coerce')

    with open(feat_json) as f:
        feat_cols = json.load(f)

    feat_cols = [c for c in feat_cols if c in df.columns]
    return df, feat_cols


def _objective(trial, X, y, model_type, tscv, scale_pos_weight=1.0, n_samples=None):
    """Optuna objective: 最小化 log_loss（带正则化偏置）。"""
    n_s = n_samples or len(X)
    # 小数据集时增强正则化
    is_small_data = n_s < 1000

    if model_type == 'cat':
        params = {'random_seed': 42}
        params.update({
            'iterations': trial.suggest_int('iterations', 100, 400),
            'depth': trial.suggest_int('depth', 3, 6),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
            'l2_leaf_reg': trial.suggest_float('l2_leaf_reg', 3, 20),
            'border_count': trial.suggest_int('border_count', 32, 128),
            'random_strength': trial.suggest_float('random_strength', 0, 2),
        })
        from catboost import CatBoostClassifier
        if scale_pos_weight > 1.5:
            params['auto_class_weights'] = 'Balanced'
        model = CatBoostClassifier(**params, verbose=0, allow_writing_files=False)
    else:
        base = {'random_state': 42}
        if model_type != 'rf':
            base['verbosity'] = -1 if model_type != 'xgb' else 0

        if model_type == 'lgbm':
            base.update({
                'n_estimators': trial.suggest_int('n_estimators', 100, 500),
                'max_depth': trial.suggest_int('max_depth', 3, 5 if is_small_data else 7),
                'num_leaves': trial.suggest_int('num_leaves', 8, 24 if is_small_data else 48),
                'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
                'subsample': trial.suggest_float('subsample', 0.6, 1.0),
                'colsample_bytree': trial.suggest_float('colsample_bytree', 0.6, 1.0),
                'min_child_samples': trial.suggest_int('min_child_samples', 30 if is_small_data else 20, 100),
                'reg_alpha': trial.suggest_float('reg_alpha', 1 if is_small_data else 0, 5),
                'reg_lambda': trial.suggest_float('reg_lambda', 1 if is_small_data else 0, 5),
            })
            from lightgbm import LGBMClassifier
            if scale_pos_weight > 1.5:
                base['class_weight'] = 'balanced'
            model = LGBMClassifier(**base)
        elif model_type == 'xgb':
            base.update({
                'n_estimators': trial.suggest_int('n_estimators', 100, 400),
                'max_depth': trial.suggest_int('max_depth', 3, 5 if is_small_data else 7),
                'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
                'subsample': trial.suggest_float('subsample', 0.6, 1.0),
                'colsample_bytree': trial.suggest_float('colsample_bytree', 0.6, 1.0),
                'min_child_weight': trial.suggest_int('min_child_weight', 5 if is_small_data else 3, 15),
                'gamma': trial.suggest_float('gamma', 1 if is_small_data else 0, 5),
                'reg_alpha': trial.suggest_float('reg_alpha', 1 if is_small_data else 0, 5),
                'reg_lambda': trial.suggest_float('reg_lambda', 1 if is_small_data else 0, 5),
            })
            import xgboost as xgb
            if scale_pos_weight > 1.5:
                base['scale_pos_weight'] = scale_pos_weight
            model = xgb.XGBClassifier(**base)
        else:  # rf
            base.pop('verbosity', None)
            base['verbose'] = 0
            base.update({
                'n_estimators': trial.suggest_int('n_estimators', 100, 400),
                'max_depth': trial.suggest_int('max_depth', 4, 15),
                'min_samples_split': trial.suggest_int('min_samples_split', 5, 30),
                'min_samples_leaf': trial.suggest_int('min_samples_leaf', 2, 15),
                'max_features': trial.suggest_float('max_features', 0.3, 0.8),
            })
            if scale_pos_weight > 1.5:
                base['class_weight'] = 'balanced'
            model = RandomForestClassifier(**base, n_jobs=-1)

    losses = []
    for train_idx, val_idx in tscv.split(X):
        X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]
        model.fit(X_tr, y_tr)
        proba = model.predict_proba(X_val)[:, 1]
        losses.append(log_loss(y_val, proba))
    return np.mean(losses)


def _train_base_model(X, y, model_type, scale_pos_weight=1.0, n_trials=None):
    """用 Optuna 最优参数训练单模型。"""
    if n_trials is None:
        n_trials = N_TRIALS
    tscv = _tscv()

    study = optuna.create_study(direction='minimize', sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(lambda t: _objective(t, X, y, model_type, tscv, scale_pos_weight, n_samples=len(X)),
                   n_trials=n_trials, show_progress_bar=False)
    best_params = study.best_params
    best_value = study.best_value
    print(f"   {model_type}: log_loss={best_value:.4f} ({len(study.trials)} trials)")

    params = {k: v for k, v in best_params.items() if k != 'random_state'}
    params['random_state'] = 42

    if model_type == 'lgbm':
        from lightgbm import LGBMClassifier
        params['verbosity'] = -1
        if scale_pos_weight > 1.5:
            params['class_weight'] = 'balanced'
        model = LGBMClassifier(**params)
    elif model_type == 'xgb':
        import xgboost as xgb
        params['verbosity'] = 0
        if scale_pos_weight > 1.5:
            params['scale_pos_weight'] = scale_pos_weight
        model = xgb.XGBClassifier(**params)
    elif model_type == 'cat':
        from catboost import CatBoostClassifier
        cat_params = {
            'iterations': params.pop('iterations', 200),
            'depth': params.pop('depth', 6),
            'learning_rate': params.pop('learning_rate', 0.1),
            'l2_leaf_reg': params.pop('l2_leaf_reg', 3),
            'border_count': params.pop('border_count', 64),
            'random_strength': params.pop('random_strength', 0),
            'random_seed': 42,
            'verbose': 0,
            'allow_writing_files': False,
        }
        if scale_pos_weight > 1.5:
            cat_params['auto_class_weights'] = 'Balanced'
        model = CatBoostClassifier(**cat_params)
    else:
        params.pop('verbosity', None)
        params['verbose'] = 0
        if scale_pos_weight > 1.5:
            params['class_weight'] = 'balanced'
        model = RandomForestClassifier(**params, n_jobs=-1)

    model.fit(X, y)
    return model, params


def _scale_pos_weight(y):
    """计算 scale_pos_weight 用于处理类别不平衡。"""
    n_pos = y.sum()
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 1.0
    return n_neg / n_pos


def train_ensemble(df, target_col, prefix, feat_cols, model_types=None, n_trials=None, rf_trials=None):
    """为指定目标训练完整集成模型（单阶段 Optuna + Voting + 概率校准）。

    Args:
        model_types: 使用的基模型列表，如 ['lgbm', 'xgb', 'cat', 'rf']
        n_trials: Optuna 搜索次数 (默认 N_TRIALS)
        rf_trials: RF 专用 trials (大数据集时 RF 慢，可单独减少)
    """
    if model_types is None:
        model_types = ['lgbm', 'xgb', 'cat', 'rf']
    if n_trials is None:
        n_trials = N_TRIALS

    print(f"\n{'=' * 50}")
    print(f"  训练集成模型: {prefix} (目标={target_col})")
    print(f"  基模型: {model_types} | trials: {n_trials}" + (f" | RF trials: {rf_trials}" if rf_trials else ""))
    print(f"{'=' * 50}")

    train_df = df.dropna(subset=[target_col]).copy()
    if len(train_df) < 200:
        print(f"  跳过: 样本不足 ({len(train_df)})")
        return None, None

    X = train_df[feat_cols].fillna(0)
    y = train_df[target_col].astype(int)
    pos_ratio = y.mean()
    print(f"  样本: {len(X)}, 特征: {len(feat_cols)}, 正例: {pos_ratio:.2%}")

    # ── 类别不平衡处理 ──
    spw = _scale_pos_weight(y)
    use_class_weight = pos_ratio < 0.30 or pos_ratio > 0.70
    if use_class_weight:
        print(f"  类别不平衡: 应用 scale_pos_weight={spw:.2f}")

    # ── 阶段1: 用 Optuna 训练基模型 ──
    trained_models = {}
    for mt in model_types:
        try:
            mt_trials = rf_trials if (mt == 'rf' and rf_trials is not None) else n_trials
            m, params = _train_base_model(X, y, mt, scale_pos_weight=spw, n_trials=mt_trials)
            trained_models[mt] = m
            model_path = MODEL_DIR_PATH / f"{prefix}_{mt}.pkl"
            joblib.dump(m, model_path)
        except Exception as e:
            print(f"  ⚠️ {mt} 训练失败: {e}")

    if len(trained_models) < 2:
        print("  集成模型不足 2 个，跳过")
        return None, None

    # ── 阶段2: 按时间排序，前 60% 训练 Voting，后 40% 校准 ──
    # 用 train_df 的 date 列排序，确保时序正确
    dates = train_df['date'].values
    sorted_order = np.argsort(pd.to_datetime(dates))
    split_point = int(len(sorted_order) * 0.6)
    split_point = max(1, min(split_point, len(sorted_order) - 1))
    meta_idx = sorted_order[:split_point]
    cal_idx = sorted_order[split_point:]

    X_meta = X.iloc[meta_idx]
    y_meta = y.iloc[meta_idx]
    X_cal = X.iloc[cal_idx]
    y_cal = y.iloc[cal_idx]
    print(f"  Voting 训练: {len(X_meta)} 样本 (至 {str(dates[meta_idx[-1]])[:10]}) "
          f"| 校准: {len(X_cal)} 样本 (自 {str(dates[cal_idx[0]])[:10]} 起)")

    # ── 阶段3: 在训练集上拟合 VotingClassifier ──
    estimators = [(name, trained_models[name]) for name in model_types if name in trained_models]
    ensemble = VotingClassifier(estimators=estimators, voting='soft')
    ensemble.fit(X_meta, y_meta)

    # ── 阶段4: 概率校准 ──
    # win 用 isotonic（样本充足，不假设分布形状）
    # spread/total 用 sigmoid（50/50 小样本下更鲁棒）
    cal_method = 'isotonic' if target_col == 'win' else 'sigmoid'
    calibrated = CalibratedClassifierCV(ensemble, method=cal_method, cv='prefit')
    calibrated.fit(X_cal, y_cal)

    # 评估校准结果
    cal_probs = calibrated.predict_proba(X_cal)[:, 1]
    brier = brier_score_loss(y_cal, cal_probs)
    ll = log_loss(y_cal, cal_probs)
    print(f"  校准完成: Brier={brier:.4f}, LogLoss={ll:.4f} (校准集)")

    # ── SHAP 特征重要性分析 ──
    try:
        from src.core.interpretability import report_feature_importance
        report_feature_importance(
            calibrated, X_cal, feat_cols,
            save_dir=str(MODEL_DIR_PATH / "shap"),
        )
    except Exception as e:
        print(f"  ⚠️ SHAP 分析跳过: {e}")

    # ── 保存 ──
    ensemble_path = MODEL_DIR_PATH / f"{prefix}_ensemble.pkl"
    joblib.dump(calibrated, ensemble_path)
    print(f"  已保存 {ensemble_path}")

    meta_info = {
        'base_models': list(trained_models.keys()),
        'metrics': {'brier': brier, 'log_loss': ll},
        'target': target_col,
        'n_samples': len(X),
        'n_features': len(feat_cols),
    }
    meta_path = MODEL_DIR_PATH / f"{prefix}_ensemble_meta.json"
    with open(meta_path, 'w') as f:
        json.dump(meta_info, f, indent=2)

    return calibrated, {'brier': brier, 'log_loss': ll}


def train_sport_ensemble(sport='bb'):
    """为指定运动训练所有目标的集成模型。"""
    config = SPORT_CONFIG.get(sport, SPORT_CONFIG['bb'])
    print(f"\n{'#' * 55}")
    print(f"  {'🏀' if sport == 'bb' else '⚽'} {sport.upper()} 集成模型训练")
    print(f"  配置: 基模型={config['model_types']} | trials={config['n_trials']}" +
          (f" | RF trials={config['rf_trials']}" if 'rf_trials' in config else "") +
          f" | min_samples={config['min_samples']}")
    print(f"{'#' * 55}")

    df, feat_cols = _load_data(sport)
    targets = ['win', 'spread_result', 'total_result']
    prefix_map = {'bb': 'model_bb', 'fb': 'model_fb'}
    prefix = prefix_map[sport]

    results = {}
    for target in targets:
        ensemble, metrics = train_ensemble(
            df, target, f"{prefix}_{target}", feat_cols,
            model_types=config['model_types'],
            n_trials=config['n_trials'],
            rf_trials=config.get('rf_trials'),
        )
        results[target] = metrics

    print(f"\n{'=' * 55}")
    print(f"  {sport.upper()} 集成训练完成")
    for t, m in results.items():
        if m:
            print(f"  {t}: Brier={m['brier']:.4f}, LogLoss={m['log_loss']:.4f}")
    print(f"{'=' * 55}")
    return results


if __name__ == '__main__':
    sport = sys.argv[1] if len(sys.argv) > 1 else 'bb'
    train_sport_ensemble(sport)
