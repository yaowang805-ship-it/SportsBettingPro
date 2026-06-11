"""集成模型训练器 — Optuna 超参调优 + 概率校准。

用法:
    from src.models.ensemble_trainer import train_sport_ensemble
    train_sport_ensemble('bb')    # 篮球
    train_sport_ensemble('fb')    # 足球
    train_sport_ensemble('nfl')   # NFL
"""
import json
import shutil
import sys
import warnings
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import joblib
import numpy as np
import optuna
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.frozen import FrozenEstimator
from sklearn.model_selection import TimeSeriesSplit

warnings.filterwarnings('ignore', category=UserWarning, module='optuna')

from config.settings import MODEL_DIR

MODEL_DIR_PATH = Path(MODEL_DIR) if isinstance(MODEL_DIR, str) else MODEL_DIR
MODEL_DIR_PATH.mkdir(parents=True, exist_ok=True)

N_TRIALS = 50       # Optuna trials per model (single phase)


from src.models.stacking import Stage2Stacking, WeightedEnsemble
CV_SPLITS = 5       # TimeSeriesSplit folds for CV evaluation during Optuna

# 根据运动类型和数据量自适应
SPORT_CONFIG = {
    'bb': {  # 篮球：24427 样本，3 模型（RF 在 24k 样本上内存不足去掉）
        'model_types': ['lgbm', 'xgb', 'cat'],
        'n_trials': 20,
        'min_samples': 200,
    },
    'fb': {  # 足球：~5200 样本，2 模型防过拟合
        'model_types': ['lgbm', 'xgb'],
        'n_trials': 20,
        'min_samples': 200,
        'targets': ['win', 'total_result'],  # 无真实让分盘数据，不训练 spread_result
    },
    'nfl': {  # NFL：~1400 样本，2 模型防过拟合
        'model_types': ['lgbm', 'xgb'],
        'n_trials': 20,
        'min_samples': 100,
    },
    'wc': {  # 世界杯：~3500 国家队比赛样本，2 模型
        'model_types': ['lgbm', 'xgb'],
        'n_trials': 25,
        'min_samples': 200,
        'targets': ['home_win', 'over_2.5'],
    },
}


def _tscv(n_splits=CV_SPLITS):
    return TimeSeriesSplit(n_splits=n_splits)


def _load_data(sport):
    if sport == 'bb':
        csv_path = 'data/processed/bb_features.csv'
        feat_json = MODEL_DIR_PATH / 'model_bb_features.json'
    elif sport == 'nfl':
        csv_path = 'data/processed/nfl_features.csv'
        feat_json = MODEL_DIR_PATH / 'model_nfl_features.json'
    elif sport == 'wc':
        csv_path = 'data/processed/wc_features.csv'
        feat_json = MODEL_DIR_PATH / 'model_wc_features.json'
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




def _compare_models(champion, challenger, X_holdout, y_holdout, threshold=0.5) -> dict:
    """对比 champion vs challenger 在 holdout 集上的表现。

    Args:
        champion: 当前生产模型
        challenger: 新训练模型
        X_holdout: holdout 特征
        y_holdout: holdout 标签
        threshold: 分类阈值

    Returns:
        {"winner": "champion"|"challenger", "details": "...",
         "champion_metrics": {...}, "challenger_metrics": {...}}
    """
    from sklearn.metrics import accuracy_score

    def _eval(model):
        probs = model.predict_proba(X_holdout)[:, 1]
        preds = (probs >= threshold).astype(int)
        return {
            "accuracy": round(float(accuracy_score(y_holdout, preds)), 4),
            "brier": round(float(brier_score_loss(y_holdout, probs)), 4),
            "log_loss": round(float(log_loss(y_holdout, probs)), 4),
        }

    champ_metrics = _eval(champion)
    chall_metrics = _eval(challenger)

    # 逐个指标比较：accuracy 越高越好，brier 越低越好，log_loss 越低越好
    champ_wins = 0
    chall_wins = 0
    details = []

    if champ_metrics["accuracy"] > chall_metrics["accuracy"]:
        champ_wins += 1
        details.append(f"acc: champ={champ_metrics['accuracy']:.4f} > chall={chall_metrics['accuracy']:.4f}")
    else:
        chall_wins += 1
        details.append(f"acc: chall={chall_metrics['accuracy']:.4f} >= champ={champ_metrics['accuracy']:.4f}")

    if champ_metrics["brier"] < chall_metrics["brier"]:
        champ_wins += 1
        details.append(f"brier: champ={champ_metrics['brier']:.4f} < chall={chall_metrics['brier']:.4f}")
    else:
        chall_wins += 1
        details.append(f"brier: chall={chall_metrics['brier']:.4f} <= champ={champ_metrics['brier']:.4f}")

    if champ_metrics["log_loss"] < chall_metrics["log_loss"]:
        champ_wins += 1
        details.append(f"log_loss: champ={champ_metrics['log_loss']:.4f} < chall={chall_metrics['log_loss']:.4f}")
    else:
        chall_wins += 1
        details.append(f"log_loss: chall={chall_metrics['log_loss']:.4f} <= champ={champ_metrics['log_loss']:.4f}")

    if chall_wins > champ_wins:
        winner = "challenger"
    elif champ_wins > chall_wins:
        winner = "champion"
    else:
        # 平局：保留 champion（保守策略）
        winner = "champion"
        details.append("平局，保留 champion")

    return {
        "winner": winner,
        "details": "; ".join(details),
        "champion_metrics": champ_metrics,
        "challenger_metrics": chall_metrics,
        "champion_wins": champ_wins,
        "challenger_wins": chall_wins,
    }


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

    # ── 阶段2: 按时间排序，50% 训练 Voting，30% 校准，20% 真实留出（不接触模型）──
    # 严格时序分割确保校准集不溢出到留出集
    dates = train_df['date'].values
    sorted_order = np.argsort(pd.to_datetime(dates))
    n_total = len(sorted_order)
    train_split = int(n_total * 0.5)
    cal_split = int(n_total * 0.8)  # 50% + 30%
    train_split = max(1, min(train_split, n_total - 2))
    cal_split = max(train_split + 1, min(cal_split, n_total - 1))
    meta_idx = sorted_order[:train_split]
    cal_idx = sorted_order[train_split:cal_split]
    holdout_idx = sorted_order[cal_split:]

    X_holdout = X.iloc[holdout_idx] if len(holdout_idx) > 0 else None
    y_holdout = y.iloc[holdout_idx] if len(holdout_idx) > 0 else None

    X_meta = X.iloc[meta_idx]
    X_cal = X.iloc[cal_idx]
    y_cal = y.iloc[cal_idx]
    print(f"  Voting 训练: {len(X_meta)} 样本 (至 {str(dates[meta_idx[-1]])[:10]}) "
          f"| 校准: {len(X_cal)} 样本 ({str(dates[cal_idx[0]])[:10]} ~ {str(dates[cal_idx[-1]])[:10]})"
          f" | 留出: {len(holdout_idx)} 样本 (自 {str(dates[holdout_idx[0]])[:10]} 起)")

    # ── 阶段3: 计算每个基模型在校准集上的表现 → 动态权重 ──
    per_model_metrics = {}
    model_weights = []
    valid_estimators = []

    for name in model_types:
        if name not in trained_models:
            continue
        m = trained_models[name]
        try:
            cal_probs = m.predict_proba(X_cal)[:, 1]
            ll = log_loss(y_cal, cal_probs)
            brier = brier_score_loss(y_cal, cal_probs)
            # 负log_loss保护（防止极端值）
            weight = 1.0 / (max(ll, 0.05))
            per_model_metrics[name] = {'cal_log_loss': round(ll, 4), 'cal_brier': round(brier, 4), 'weight': round(weight, 4)}
            model_weights.append(weight)
            valid_estimators.append((name, m))
            print(f"   {name}: log_loss={ll:.4f} weight={weight:.2f}")
        except Exception as e:
            print(f"   {name}: 校准集评估失败 ({e}), 跳过")
            continue

    if len(valid_estimators) < 2:
        print("  有效基模型不足 2 个，跳过")
        return None, None

    # 归一化权重
    total_w = sum(model_weights)
    norm_weights = [w / total_w for w in model_weights]
    for i, (name, _) in enumerate(valid_estimators):
        per_model_metrics[name]['norm_weight'] = round(norm_weights[i], 4)
    print(f"  动态权重: {[(n, round(w, 3)) for n, w in zip([e[0] for e in valid_estimators], norm_weights)]}")

    # ── 阶段4: Stacking 集成（默认）或 Voting（回退） ──
    best_base_ll = min([per_model_metrics[n]['cal_log_loss']
                        for n, _ in valid_estimators])

    ensemble = None
    try:
        # Stage-2 Stacking: 用已训练的基模型在校准集上做 meta 训练
        # （避免 StackingClassifier 内部重新训练 + 打乱时序）
        cal_meta_X = np.column_stack([
            m.predict_proba(X_cal)[:, 1] for _, m in valid_estimators
        ])
        hold_meta_X = np.column_stack([
            m.predict_proba(X_holdout)[:, 1] for _, m in valid_estimators
        ]) if X_holdout is not None else None

        meta_learner = LogisticRegression(
            penalty='l2', C=0.5, solver='lbfgs', max_iter=3000,
            random_state=42, class_weight='balanced',
        )
        # 在 calibration 集上训练，在 holdout 集上评估
        meta_learner.fit(cal_meta_X, y_cal)
        hold_probs = meta_learner.predict_proba(hold_meta_X)[:, 1]
        stack_ll = log_loss(y_holdout, hold_probs)
        print(f"  Stage-2 Stacking: holdout log_loss={stack_ll:.4f} (基线 best={best_base_ll:.4f})")
        if stack_ll < best_base_ll - 0.005:
            # 用校准+留出全量数据重新训练 meta-learner
            full_meta_X = np.column_stack([
                m.predict_proba(pd.concat([X_cal, X_holdout]))[:, 1] for _, m in valid_estimators
            ])
            meta_learner.fit(full_meta_X, pd.concat([y_cal, y_holdout]))
            # 包装为类模型接口以便与现有 pipeline 兼容
            ensemble = Stage2Stacking(valid_estimators, meta_learner)
            print("  ✅ Stage-2 Stacking 优于最佳基模型，采用 Stacking")
        else:
            print("  Stage-2 Stacking 未优于最佳基模型，回退到 Voting")
    except Exception as e:
        print(f"  ⚠️ Stage-2 Stacking 失败 ({e})，回退到 Voting")

    if ensemble is None:
        # 自定义加权平均，避免 VotingClassifier 重拟合 degrade 基模型
        ensemble = WeightedEnsemble(valid_estimators, norm_weights)

    # ── 阶段5: 概率校准 ──
    # win 用 isotonic（样本充足，不假设分布形状）
    # spread/total 用 sigmoid（50/50 小样本下更鲁棒）
    cal_method = 'isotonic' if target_col == 'win' else 'sigmoid'
    # sklearn 1.6+: FrozenEstimator 防止误重拟合，cv='prefit' 跳过交叉验证
    # （FrozenEstimator 仅控制 ensemble flag，cv='prefit' 仍必须）
    import warnings
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', message="The `cv='prefit'` option is deprecated")
        calibrated = CalibratedClassifierCV(FrozenEstimator(ensemble), method=cal_method, cv='prefit')
    calibrated.fit(X_cal, y_cal)

    # 评估校准结果
    cal_probs = calibrated.predict_proba(X_cal)[:, 1]
    brier = brier_score_loss(y_cal, cal_probs)
    ll = log_loss(y_cal, cal_probs)
    print(f"  校准完成: Brier={brier:.4f}, LogLoss={ll:.4f} (校准集)")

    # ── 最优阈值搜索 ──
    from sklearn.metrics import accuracy_score
    thresholds = np.arange(0.30, 0.71, 0.02)
    best_acc_val = 0.0
    best_thresh = 0.5
    for t in thresholds:
        preds = (cal_probs >= t).astype(int)
        acc = accuracy_score(y_cal, preds)
        if acc > best_acc_val:
            best_acc_val = acc
            best_thresh = t
    print(f"  最优阈值: {best_thresh:.2f} | 校准集准确率: {best_acc_val:.4f}")

    # ── SHAP 特征重要性分析 ──
    try:
        from src.core.interpretability import report_feature_importance
        report_feature_importance(
            calibrated, X_cal, feat_cols,
            save_dir=str(MODEL_DIR_PATH / "shap"),
        )
    except Exception as e:
        print(f"  ⚠️ SHAP 分析跳过: {e}")

    # ── Champion/Challenger 对比部署 ──
    challenger_dir = MODEL_DIR_PATH / "challengers"
    challenger_dir.mkdir(parents=True, exist_ok=True)
    challenger_path = challenger_dir / f"{prefix}_ensemble.pkl"
    champion_path = MODEL_DIR_PATH / f"{prefix}_ensemble.pkl"

    # 先保存 challenger（新模型）
    joblib.dump(calibrated, challenger_path)
    print(f"  已保存 challenger: {challenger_path}")

    # 检查 champion 是否存在
    champion_exists = champion_path.exists()
    promote = False
    compare_report = None

    # ── 版本备份：覆盖前保存旧版 ──
    versions_dir = MODEL_DIR_PATH / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _backup_champion():
        """将当前 champion 备份到 versions/ 目录，仅保留最近 3 版。"""
        if not champion_path.exists():
            return
        backup_path = versions_dir / f"{prefix}_ensemble_{ts}.pkl"
        shutil.copy2(champion_path, backup_path)
        print(f"  📦 备份旧版: {backup_path.name}")
        # 清理旧备份，仅保留最近 3 个
        backups = sorted(versions_dir.glob(f"{prefix}_ensemble_*.pkl"))
        for old in backups[:-3]:
            old.unlink()
            # 同时删除对应的 meta（如果有）
            old_meta = versions_dir / old.name.replace(".pkl", "_meta.json")
            if old_meta.exists():
                old_meta.unlink()

    if not champion_exists:
        # 首次训练：直接部署
        shutil.copy2(challenger_path, champion_path)
        print(f"  ✅ 首次训练，直接部署为 champion: {champion_path}")
        promote = True
    elif X_holdout is not None and len(X_holdout) >= 10:
        # 在 holdout 集上对比 champion vs challenger
        try:
            champion_model = joblib.load(champion_path)
            compare_report = _compare_models(
                champion_model, calibrated,
                X_holdout, y_holdout, best_thresh,
            )
            if compare_report["winner"] == "challenger":
                _backup_champion()
                shutil.copy2(challenger_path, champion_path)
                print(f"  ✅ Challenger 胜出 ({compare_report['details']})，已更新 champion")
                promote = True
            else:
                print(f"  ℹ️ Champion 仍更优 ({compare_report['details']})，保留现有模型")
        except Exception as e:
            print(f"  ⚠️ 模型对比失败 ({e})，直接部署 challenger")
            _backup_champion()
            shutil.copy2(challenger_path, champion_path)
            promote = True
    else:
        # 无 holdout 数据（样本太少），直接部署
        _backup_champion()
        shutil.copy2(challenger_path, champion_path)
        n_holdout = len(X_holdout) if X_holdout is not None else 0
        print(f"  样本不足 ({n_holdout})，跳过对比，直接部署")
        promote = True

    # 保存对比报告
    if compare_report:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = challenger_dir / f"compare_{prefix}_{ts}.json"
        with open(report_path, 'w') as f:
            json.dump(compare_report, f, indent=2)
        print(f"  对比报告: {report_path}")

    # ── 保存元数据（记录部署决策） ──
    meta_info = {
        'base_models': [e[0] for e in valid_estimators],
        'metrics': {'brier': brier, 'log_loss': ll},
        'per_model_metrics': per_model_metrics,
        'ensemble_weights': {e[0]: round(w, 4) for e, w in zip(valid_estimators, norm_weights)},
        'target': target_col,
        'n_samples': len(X),
        'n_features': len(feat_cols),
        'optimal_threshold': {
            'threshold': float(best_thresh),
            'test_accuracy': float(best_acc_val),
        },
        'holdout': {
            'start_idx': int(cal_split),
            'start_date': str(dates[holdout_idx[0]]) if len(holdout_idx) > 0 else None,
            'n_samples': int(len(holdout_idx)),
        },
        'deployment': {
            'promoted': promote,
            'champion_existed': champion_exists,
            'winner': compare_report["winner"] if compare_report else ("champion" if champion_exists else "challenger_first_train"),
        },
    }
    meta_path = MODEL_DIR_PATH / f"{prefix}_ensemble_meta.json"
    with open(meta_path, 'w') as f:
        json.dump(meta_info, f, indent=2)

    # 版本 meta 备份（与 .pkl 备份对应）
    if promote and champion_exists:
        version_meta_path = versions_dir / f"{prefix}_ensemble_{ts}_meta.json"
        with open(version_meta_path, 'w') as f:
            json.dump(meta_info, f, indent=2)

    return calibrated, {'brier': brier, 'log_loss': ll}


def train_sport_ensemble(sport='bb', quick=False):
    """为指定运动训练所有目标的集成模型。

    Args:
        sport: 运动类型 ('bb', 'fb', 'nfl', 'wc')
        quick: 快速模式，大幅减少 Optuna trials 用于日常流水线
    """
    config = SPORT_CONFIG.get(sport, SPORT_CONFIG['bb'])
    if quick:
        config = {**config, 'n_trials': 3, 'rf_trials': 2}
    print(f"\n{'#' * 55}")
    print(f"  {'🏀' if sport == 'bb' else '⚽'} {sport.upper()} 集成模型训练")
    print(f"  配置: 基模型={config['model_types']} | trials={config['n_trials']}" +
          (f" | RF trials={config['rf_trials']}" if 'rf_trials' in config else "") +
          f" | min_samples={config['min_samples']}")
    print(f"{'#' * 55}")

    df, feat_cols = _load_data(sport)
    targets = config.get('targets', ['win', 'spread_result', 'total_result'])
    prefix_map = {'bb': 'model_bb', 'fb': 'model_fb', 'nfl': 'model_nfl', 'wc': 'model_wc'}
    prefix = prefix_map[sport]

    results = {}
    for target in targets:
        target_feat_cols = feat_cols

        ensemble, metrics = train_ensemble(
            df, target, f"{prefix}_{target}", target_feat_cols,
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
    if len(sys.argv) > 2 and sys.argv[2] == 'quick':
        for cfg in SPORT_CONFIG.values():
            cfg['n_trials'] = 3
    train_sport_ensemble(sport)
