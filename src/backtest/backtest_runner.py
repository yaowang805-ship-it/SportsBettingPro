#!/usr/bin/env python3
"""职业级回测评估：时间序列分割 + Bootstrap置信区间 + 交易成本模拟。

增强功能:
  1. Bootstrap 置信区间 — 对准确率/Brier/Sharpe 重采样估计不确定性
  2. 交易成本模拟 — 按市场类型应用滑点/佣金
  3. Walk-Forward 分析 — 滚动扩展窗口验证
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger, setup_logging
setup_logging(log_level="INFO", log_to_file=False, log_to_console=True)
logger = get_logger(__name__)

import joblib
import numpy as np
import pandas as pd

# 轻量级重训练用
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier
from catboost import CatBoostClassifier
from sklearn.calibration import CalibratedClassifierCV

# 兼容已 pickle 的 Stage2Stacking / WeightedEnsemble
import src.models.stacking as _stacking_mod
sys.modules['__main__'].Stage2Stacking = _stacking_mod.Stage2Stacking
sys.modules['__main__'].WeightedEnsemble = _stacking_mod.WeightedEnsemble

# SimpleEnsemble 用于快速训练保存的模型（不依赖 Optuna）
class SimpleEnsemble:
    """Lightweight ensemble of 3 calibrated classifiers."""
    def __init__(self):
        self.models = []
        self.feat_cols = []
    def set_params(self, models, feat_cols):
        self.models = models
        self.feat_cols = feat_cols
    def predict_proba(self, X):
        import numpy as np
        all_probs = np.zeros((X.shape[0], len(self.models)))
        for i, m in enumerate(self.models):
            all_probs[:, i] = m.predict_proba(X)[:, 1]
        avg = all_probs.mean(axis=1)
        return np.column_stack([1 - avg, avg])
sys.modules['__main__'].SimpleEnsemble = SimpleEnsemble
from sklearn.metrics import (precision_score,
                             recall_score, f1_score, confusion_matrix)

# 抑制 sklearn 特征名警告（_quick_train_ensemble 使用 numpy arrays 而非 DataFrame）
import warnings
warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

from config.settings import DATA_DIR
from src.core.evaluation import brier_score, safe_log_loss

DATA_DIR_PATH = ROOT / DATA_DIR if isinstance(DATA_DIR, str) else DATA_DIR
OUTPUT_PATH = DATA_DIR_PATH / 'model_backtest_summary.json'

# 职业滑点模型参数
SLIPPAGE_BASE = {"h2h": 0.015, "spread": 0.025, "total": 0.025, "default": 0.020}
SLIPPAGE_STAKE_SCALE = 0.001  # 每单位注额的滑点加成（1单位=1%本金）

N_BOOTSTRAP = 1000      # Bootstrap 重采样次数
BOOTSTRAP_CI = 0.95     # 置信区间水平


def bootstrap_ci(metrics_fn, y_true, y_prob, n_samples=N_BOOTSTRAP, ci=BOOTSTRAP_CI):
    """Bootstrap 重采样计算指标置信区间。

    Args:
        metrics_fn: 接受 (y_true_subset, y_prob_subset) 返回 float 的函数
        y_true: 真实标签数组
        y_prob: 预测概率数组
        n_samples: 重采样次数
        ci: 置信区间水平 (默认 0.95)

    Returns:
        {"mean": float, "std": float, "ci_lower": float, "ci_upper": float, "n_samples": int}
    """
    np.random.seed(42)
    n = len(y_true)
    boot_vals = np.zeros(n_samples)

    for i in range(n_samples):
        idx = np.random.randint(0, n, size=n)
        boot_vals[i] = metrics_fn(y_true[idx], y_prob[idx])

    alpha = (1 - ci) / 2
    return {
        "mean": float(np.mean(boot_vals)),
        "std": float(np.std(boot_vals)),
        "ci_lower": float(np.percentile(boot_vals, alpha * 100)),
        "ci_upper": float(np.percentile(boot_vals, (1 - alpha) * 100)),
        "n_samples": n_samples,
    }


def simulate_transaction_cost(probs, prices, market_type="default", stake_ratio=0.0):
    """模拟交易成本对实际盈亏的影响 — 职业级滑点模型。

    滑点 = base_slippage(market) + min(stake_ratio * SLIPPAGE_STAKE_SCALE, 0.02)
    大额注单承受更多滑点，模拟真实市场深度影响。

    Args:
        probs: 模型预测概率
        prices: 名义市场赔率 (decimal)
        market_type: h2h / spread / total / default
        stake_ratio: 注额占本金比例 (0.0~1.0)，用于计算规模相关滑点

    Returns:
        avg_edge_loss: 平均 edge 损失
        cost_rate: 实际使用的总成本率
    """
    base = SLIPPAGE_BASE.get(market_type, SLIPPAGE_BASE["default"])
    stake_adder = min(stake_ratio * SLIPPAGE_STAKE_SCALE, 0.02)
    cost_rate = base + stake_adder
    prices = np.asarray(prices, dtype=float)

    # 有效赔率 = 名义赔率 * (1 - cost_rate)
    effective_prices = prices * (1 - cost_rate)
    market_probs_effective = 1.0 / effective_prices

    market_probs_raw = 1.0 / prices
    edge_after_cost = probs - market_probs_effective
    edge_before_cost = probs - market_probs_raw
    edge_loss = edge_before_cost - edge_after_cost

    return {
        "avg_edge_loss": float(np.mean(edge_loss)),
        "cost_rate": cost_rate,
        "base_slippage": base,
        "stake_adder": stake_adder,
    }


def _quick_train_ensemble(X_tr, y_tr, X_te, y_te, verbose=False):
    """轻量级集成训练（无 Optuna），用于 Walk-Forward 每折重训练。

    返回 (probs, metrics_dict)。
    """
    models = []
    configs = [
        ('lgbm', LGBMClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                                random_state=42, n_jobs=-1, verbose=-1)),
        ('xgb', XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                              random_state=42, n_jobs=-1)),
        ('catb', CatBoostClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                                    random_state=42, verbose=0, allow_writing_files=False)),
    ]
    all_probs = np.zeros((len(X_te), len(configs)))
    for i, (name, m) in enumerate(configs):
        cal = CalibratedClassifierCV(m, cv=min(3, len(np.unique(y_tr))), method='sigmoid')
        cal.fit(X_tr, y_tr)
        all_probs[:, i] = cal.predict_proba(X_te)[:, 1]

    probs = all_probs.mean(axis=1)
    acc = float(((probs > 0.5).astype(int) == y_te).mean())
    brier = float(((probs - y_te) ** 2).mean())
    ll = float(-np.mean(y_te * np.log(np.clip(probs, 1e-15, 1)) +
                         (1 - y_te) * np.log(np.clip(1 - probs, 1e-15, 1))))
    return probs, {'accuracy': acc, 'brier': brier, 'logloss': ll}


def walk_forward_retrain(df, feat_cols, target_col, n_windows=5, test_size=0.075):
    """Walk-Forward 分析：每折重训练，无 lookahead bias。

    每次窗口用之前所有数据训练，在后续测试集上评估。
    使用轻量级集成（LGBM+XGB+CatBoost 平均），约 3 个基模型，无需 Optuna。

    Returns:
        [{window, train_range, test_range, train_samples, test_samples,
          accuracy, brier, logloss, mean_prob}, ...] 最后一项是 avg 汇总。
    """
    df = df.dropna(subset=[target_col]).sort_values('date').reset_index(drop=True)
    X = df[feat_cols].fillna(0).values
    y = df[target_col].astype(int).values
    dates = df['date'].values

    total = len(df)
    step = int(total * test_size)
    init_train = total - step * n_windows
    if init_train < 200:
        n_windows = max(2, (total - 200) // step)
        init_train = total - step * n_windows
    if init_train < 100:
        init_train = 100
        n_windows = max(2, (total - init_train) // step)

    results = []
    for w in range(n_windows):
        train_end = init_train + w * step
        test_start = train_end
        test_end = min(test_start + step, total)

        if test_start >= test_end or train_end < 100:
            break

        X_tr, y_tr = X[:train_end], y[:train_end]
        X_te, y_te = X[test_start:test_end], y[test_start:test_end]

        _, metrics = _quick_train_ensemble(X_tr, y_tr, X_te, y_te)
        results.append({
            "window": w + 1,
            "train_range": f"{dates[0]} to {dates[train_end-1]}",
            "test_range": f"{dates[test_start]} to {dates[test_end-1]}",
            "train_samples": int(train_end),
            "test_samples": int(len(y_te)),
            **metrics,
            "mean_prob": float(y_te.mean()),
        })

    if results:
        accs = [r["accuracy"] for r in results]
        results.append({
            "window": "avg",
            "avg_accuracy": float(np.mean(accs)),
            "std_accuracy": float(np.std(accs)),
            "min_accuracy": float(min(accs)),
            "max_accuracy": float(max(accs)),
            "n_windows": len(results),
        })
    return results


def _load_feature_columns(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _load_model(path):
    return joblib.load(path)


def _time_split(df, test_frac=0.2):
    """按时间顺序分割训练集/测试集，返回 (train_df, test_df, split_date)。"""
    df = df.sort_values('date').reset_index(drop=True)
    split_idx = int(len(df) * (1 - test_frac))
    split_idx = max(split_idx, 1)  # 至少留1行
    split_idx = min(split_idx, len(df) - 1)  # 测试集至少1行
    split_date = df.iloc[split_idx]['date']
    train_df = df.iloc[:split_idx].copy()
    test_df = df.iloc[split_idx:].copy()
    # 验证时序正确性
    train_max = train_df['date'].max()
    test_min = test_df['date'].min()
    if isinstance(train_max, pd.Timestamp) and isinstance(test_min, pd.Timestamp):
        if train_max <= test_min:
            pass  # 正确
        else:
            logger.warning("  ⚠️ 时间顺序异常: 训练集最大日期 %s > 测试集最小日期 %s", train_max, test_min)
    return train_df, test_df, split_date


def _find_optimal_threshold(y_true, y_prob, n_thresholds=50):
    """搜索最优概率阈值，最大化净盈利（假设赔率 1.91，即 -110 美式赔率）。

    对每个阈值，模拟等额定注：
      - prob >= thresh → 下注主胜，赢=+1单位，输=-1单位
      - 假设赔率 1.91，盈亏平衡点精度 = 1/1.91 ≈ 52.4%
    返回最优阈值及其对应的 Sharpe Ratio / 总盈利。
    """
    thresholds = np.linspace(0.35, 0.75, n_thresholds)
    best_profit = -np.inf
    best_threshold = 0.5
    odds = 1.91

    for thresh in thresholds:
        bets = y_prob >= thresh
        if bets.sum() < 5:
            continue
        pred = (y_prob >= thresh).astype(int)
        correct = pred == y_true
        wins = correct & bets
        losses = (~correct) & bets
        profit = wins.sum() * (odds - 1) - losses.sum() * 1.0
        if profit > best_profit:
            best_profit = profit
            best_threshold = thresh

    return float(best_threshold), float(best_profit)


def _classification_metrics(y_true, y_prob, threshold=0.5):
    """计算完整分类指标。"""
    y_pred = (y_prob > threshold).astype(int)

    # 处理单类别情况
    n_pos = int(y_true.sum())
    n_neg = int((1 - y_true).sum())

    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()

    precision = float(precision_score(y_true, y_pred, zero_division=0))
    recall = float(recall_score(y_true, y_pred, zero_division=0))
    f1 = float(f1_score(y_true, y_pred, zero_division=0))

    return {
        'threshold': threshold,
        'accuracy': float((y_pred == y_true).mean()),
        'precision': precision,
        'recall': recall,
        'f1_score': f1,
        'confusion_matrix': {'tn': int(tn), 'fp': int(fp), 'fn': int(fn), 'tp': int(tp)},
        'n_pos': n_pos,
        'n_neg': n_neg,
    }


# ── 指标合理性校验（防止虚高准确率/Brier 等系统性问题） ──
# 各市场类型合理上限（超出即警告）
_SANITY_THRESHOLDS = {
    "h2h": {"max_acc": 0.82, "min_brier": 0.08},   # 胜负: >82% 或 Brier<0.08 可疑
    "spread": {"max_acc": 0.78, "min_brier": 0.12}, # 让分: >78% 可疑
    "total": {"max_acc": 0.78, "min_brier": 0.12},  # 大小: >78% 可疑
    "default": {"max_acc": 0.80, "min_brier": 0.10},
}

# 各运动的基准准确率（合理区间）
_SPORT_BASELINE = {
    "bb": "h2h ~60-75%, spread ~55-68%, total ~55-68%",
    "fb": "h2h ~55-70%, spread/total ~50-65%",
    "nfl": "h2h ~60-72%, spread ~55-65%",
    "wc": "h2h ~55-68%, total ~50-65%",
}


def sanity_check_metrics(metrics: dict, market_type: str = "default",
                          dataset_name: str = "", model_name: str = "") -> list:
    """校验指标是否在合理范围内，返回警告列表。

    在体育博彩领域，过高的准确率通常意味着 lookahead bias 或数据泄漏。
    """
    thresholds = _SANITY_THRESHOLDS.get(market_type, _SANITY_THRESHOLDS["default"])
    warnings_list = []
    acc = metrics.get("accuracy", 0)
    brier = metrics.get("brier", 1)

    if acc > thresholds["max_acc"]:
        warnings_list.append(
            f"⚠️  RED FLAG [{model_name} {dataset_name}] "
            f"准确率 {acc:.1%} 超过合理上限 {thresholds['max_acc']:.0%}！"
            f"可能原因: lookahead bias / 数据泄漏 / 过拟合"
        )
    elif acc > thresholds["max_acc"] - 0.05:
        warnings_list.append(
            f"⚠️  WARNING [{model_name} {dataset_name}] "
            f"准确率 {acc:.1%} 接近合理上限 {thresholds['max_acc']:.0%}，建议验证"
        )

    if brier < thresholds["min_brier"]:
        warnings_list.append(
            f"⚠️  RED FLAG [{model_name} {dataset_name}] "
            f"Brier {brier:.4f} 低于合理下限 {thresholds['min_brier']}！"
            f"可能原因: 概率校准过度 / 数据泄漏"
        )

    if warnings_list:
        logger.warning("=" * 60)
        for w in warnings_list:
            logger.warning(w)
        sport_key = dataset_name.split("_")[0] if "_" in dataset_name else dataset_name
        baseline = _SPORT_BASELINE.get(sport_key, "")
        if baseline:
            logger.warning("  该运动参考基准: %s", baseline)
        logger.warning("  建议: 使用 walk-forward 重训练模式获取真实 OOS 指标")
        logger.warning("=" * 60)

    return warnings_list


def evaluate_model(df, feat_cols, model_path, target_col, dataset_name: str,
                   threshold: float = 0.5, test_frac: float = 0.2,
                   market_type: str = "default", stake_ratio: float = 0.02):
    """评估模型：时间序列分割 + Bootstrap置信区间 + 交易成本。

    返回 dict，包含训练集和测试集指标、置信区间、交易成本影响。
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'], utc=True, format='mixed')

    model = _load_model(model_path)
    feat_cols_actual = model.get('feat_cols', feat_cols) if isinstance(model, dict) else feat_cols

    valid_cols = [col for col in feat_cols_actual if col in df.columns]
    if not valid_cols:
        raise ValueError(f"模型特征列与数据集不匹配: {model_path}")

    df_valid = df.dropna(subset=[target_col]).sort_values('date').reset_index(drop=True)
    X = df_valid[valid_cols].fillna(0).values
    y = df_valid[target_col].astype(int).values
    dates = df_valid['date'].values

    proba_fn = getattr(model, 'predict_proba', None)
    if proba_fn is None:
        probs = np.full(len(X), 0.5)
    else:
        probs = model.predict_proba(X)[:, 1]

    brier = brier_score(y, probs)
    logloss = safe_log_loss(y, probs)

    # ── 时间序列分割 ──
    split_idx = int(len(df_valid) * (1 - test_frac))
    split_idx = max(split_idx, 1)
    split_idx = min(split_idx, len(df_valid) - 1)
    split_date = dates[split_idx - 1]

    # 训练集
    train_y = y[:split_idx]
    train_probs = probs[:split_idx]
    train_metrics = _classification_metrics(train_y, train_probs, threshold)
    train_metrics['brier'] = brier_score(train_y, train_probs)
    train_metrics['logloss'] = safe_log_loss(train_y, train_probs)
    train_metrics['samples'] = int(len(train_y))
    train_metrics['mean_prob'] = float(np.mean(train_probs))
    train_metrics['date_range'] = {
        'start': str(dates[0]),
        'end': str(dates[split_idx - 1]),
    }

    # 测试集
    test_y = y[split_idx:]
    test_probs = probs[split_idx:]
    test_metrics = _classification_metrics(test_y, test_probs, threshold)
    test_metrics['brier'] = brier_score(test_y, test_probs)
    test_metrics['logloss'] = safe_log_loss(test_y, test_probs)
    test_metrics['samples'] = int(len(test_y))
    test_metrics['mean_prob'] = float(np.mean(test_probs))
    test_metrics['date_range'] = {
        'start': str(dates[split_idx]),
        'end': str(dates[-1]),
    }

    # ── 最优阈值搜索（在训练集上找最大化 Sharpe 的阈值，避免未来信息泄露） ──
    optimal_thresh, opt_sharpe = _find_optimal_threshold(train_y, train_probs)
    # 用最优阈值重新评估测试集
    test_opt_metrics = _classification_metrics(test_y, test_probs, optimal_thresh)

    # ── Bootstrap 置信区间（测试集） ──
    def _accuracy_fn(yt, yp): return float(( (yp > threshold).astype(int) == yt).mean())
    def _brier_fn(yt, yp): return float(brier_score(yt, yp))
    def _logloss_fn(yt, yp): return float(safe_log_loss(yt, yp))

    test_boot = {
        "accuracy": bootstrap_ci(_accuracy_fn, test_y, test_probs),
        "brier": bootstrap_ci(_brier_fn, test_y, test_probs),
        "logloss": bootstrap_ci(_logloss_fn, test_y, test_probs),
    }

    # ── 交易成本模拟 ──
    # 假设名义赔率为 1/probs 的近似 (用于 edge 计算)
    nominal_prices = 1.0 / np.clip(test_probs, 0.05, 0.95)
    cost_analysis = simulate_transaction_cost(test_probs, nominal_prices, market_type, stake_ratio)

    # 全量指标（历史兼容）
    all_metrics = _classification_metrics(y, probs, threshold)

    # 验证时序
    train_end_ts = pd.Timestamp(dates[split_idx - 1])
    test_start_ts = pd.Timestamp(dates[split_idx])
    chronological_valid = bool(train_end_ts <= test_start_ts)

    # ── True OOS: 在训练集上重训练**轻量级集成**，在测试集上评估（无 lookahead）──
    # ⚠️ 此快速重训练使用 LGBM+XGB+CatBoost 各 200 棵树（无 Optuna 调参），
    #   结果是**真实但偏悲观**的下界。经 Optuna 调参的生产模型通常更好。
    #   Walk-Forward（下方）的多窗口平均是更可靠的 OOS 评估。
    test_oos = None
    try:
        oos_probs, oos_metrics = _quick_train_ensemble(
            X[:split_idx], y[:split_idx],
            X[split_idx:], y[split_idx:],
        )
        test_oos = _classification_metrics(test_y, oos_probs, threshold)
        test_oos['brier'] = brier_score(test_y, oos_probs)
        test_oos['logloss'] = safe_log_loss(test_y, oos_probs)
        test_oos['samples'] = int(len(test_y))
        test_oos['date_range'] = test_metrics['date_range']
        test_oos['mean_prob'] = float(np.mean(oos_probs))
    except Exception as e:
        logger.debug("True OOS 重训练跳过: %s", e)

    # ── 指标合理性校验（优先用 test_oos）──
    lookahead_note = (
        "⚠️ 该模型使用全量数据训练（含测试集时间段），chronological_split 的测试集指标为近似值，"
        "并非真实 OOS 性能。请参考下方 True OOS 或 walk-forward(重训练) 结果。"
    )
    sanity_target = test_oos if test_oos else test_metrics
    test_check = sanity_check_metrics(sanity_target, market_type, dataset_name, Path(model_path).name)
    train_check = sanity_check_metrics(train_metrics, market_type, dataset_name, Path(model_path).name)
    if test_check and not test_oos:
        logger.warning("  → %s", lookahead_note)

    result = {
        'model': Path(model_path).name,
        'dataset': dataset_name,
        'target': target_col,
        'market_type': market_type,
        'chronological_split': chronological_valid,
        'split_date': str(dates[split_idx - 1]),
        'split_info': f"训练集: {len(train_y)} 样本 (至 {dates[split_idx - 1]}) | 测试集: {len(test_y)} 样本 (自 {dates[split_idx]} 起)",
        'lookahead_bias_warning': lookahead_note,
        'test_oos': test_oos,
        'sanity_checks': {'train': train_check, 'test': test_check},
        'overall': {
            'brier': brier,
            'logloss': logloss,
            'accuracy': all_metrics['accuracy'],
            'precision': all_metrics['precision'],
            'recall': all_metrics['recall'],
            'f1_score': all_metrics['f1_score'],
            'mean_prob': float(np.mean(probs)),
            'samples': int(len(probs)),
            'confusion_matrix': all_metrics['confusion_matrix'],
        },
        'train': train_metrics,
        'test': test_metrics,
        'optimal_threshold': {
            'threshold': optimal_thresh,
            'net_profit': opt_sharpe,
            'test_accuracy': test_opt_metrics['accuracy'],
            'test_precision': test_opt_metrics['precision'],
            'test_recall': test_opt_metrics['recall'],
        },
        'bootstrap_ci': test_boot,
        'transaction_cost': cost_analysis,
        'prob_distribution': {
            'min': float(np.min(test_probs)),
            'max': float(np.max(test_probs)),
            'median': float(np.median(test_probs)),
            'p25': float(np.percentile(test_probs, 25)),
            'p75': float(np.percentile(test_probs, 75)),
        },
    }

    # ── Walk-Forward 分析（每折重训练，无 lookahead bias）──
    try:
        wf_results = walk_forward_retrain(
            df, valid_cols, target_col,
            n_windows=5, test_size=test_frac / 3
        )
        if wf_results:
            result['walk_forward_retrain'] = wf_results
    except Exception as e:
        logger.debug("Walk-Forward(retrain) 分析跳过: %s", e)

    return result


def _print_bootstrap_ci(label, boot_dict):
    """打印 Bootstrap 置信区间。"""
    logger.info("      %s Bootstrap 95%% CI: [%.4f, %.4f] (mean=%.4f, std=%.4f)",
                label,
                boot_dict["ci_lower"], boot_dict["ci_upper"],
                boot_dict["mean"], boot_dict["std"])


def _print_eval_rename(result, metrics_dict, label, threshold=0.5):
    """用自定义 metrics dict 打印（用于 test_oos 等非标准键）。"""
    cm = metrics_dict.get('confusion_matrix', {})
    logger.info("    %s: 样本=%s Brier=%.4f LogLoss=%.4f Acc=%.3f Prec=%.3f Recall=%.3f F1=%.3f (阈值=%s)",
                label, metrics_dict.get('samples', '?'), metrics_dict.get('brier', 0),
                metrics_dict.get('logloss', 0), metrics_dict.get('accuracy', 0),
                metrics_dict.get('precision', 0), metrics_dict.get('recall', 0),
                metrics_dict.get('f1_score', 0), threshold)
    if cm:
        logger.info("      混淆矩阵: TN=%s FP=%s FN=%s TP=%s | 正例=%s 负例=%s",
                    cm.get('tn', '?'), cm.get('fp', '?'), cm.get('fn', '?'),
                    cm.get('tp', '?'), metrics_dict.get('n_pos', '?'), metrics_dict.get('n_neg', '?'))
    dr = metrics_dict.get('date_range', {})
    if dr:
        logger.info("      日期范围: %s ~ %s", dr.get('start', '?'), dr.get('end', '?'))


def _print_eval(result, label, threshold=0.5):
    """打印评估结果（含 Bootstrap CI）。"""
    r = result[label]
    cm = r['confusion_matrix']
    logger.info("    %s: 样本=%s Brier=%.4f LogLoss=%.4f Acc=%.3f Prec=%.3f Recall=%.3f F1=%.3f (阈值=%s)",
                label, r['samples'], r['brier'], r['logloss'], r['accuracy'], r['precision'], r['recall'], r['f1_score'], threshold)
    logger.info("      混淆矩阵: TN=%s FP=%s FN=%s TP=%s | 正例=%s 负例=%s",
                cm['tn'], cm['fp'], cm['fn'], cm['tp'], r['n_pos'], r['n_neg'])
    logger.info("      日期范围: %s ~ %s", r['date_range']['start'], r['date_range']['end'])


def run_backtest():
    """运行完整回测：时序分割 + Bootstrap CI + 交易成本 + Walk-Forward。"""
    report = []

    # ── 市场类型映射 ──
    market_map = {"win": "h2h", "spread_result": "spread", "total_result": "total"}

    # 篮球模型回测
    bb_csv = ROOT / 'data/processed/bb_features.csv'
    if bb_csv.exists():
        df_bb = pd.read_csv(bb_csv)
        feat_cols = _load_feature_columns(ROOT / 'models/model_bb_features.json')
        logger.info("\n%s", "=" * 60)
        logger.info("篮球模型回测 (%s 样本)", len(df_bb))
        logger.info("%s", "=" * 60)
        for target in ['win', 'spread_result', 'total_result']:
            model_file = ROOT / f'models/model_bb_{target}_ensemble.pkl'
            if model_file.exists():
                thresh = 0.5
                logger.info("\n  模型: %s (阈值=%s, 特征=%d)", model_file.name, thresh, len(feat_cols))
                result = evaluate_model(
                    df_bb, feat_cols, model_file, target, 'bb_features',
                    threshold=thresh, market_type=market_map.get(target, "default"),
                )
                report.append(result)

                # ── True OOS（优先展示）──
                oos = result.get('test_oos')
                if oos:
                    _print_eval_rename(result, oos, "test_oos(快速重训练·真实OOS)", thresh)
                    logger.info("      (轻量级 LGBM+XGB+CatBoost, 无 Optuna 调参, 结果为悲观下界)")
                    for w in result.get('sanity_checks', {}).get('test', []):
                        logger.warning("    ⚠️  %s", w.split('] ')[-1] if '] ' in w else w)
                else:
                    logger.info("  时序分割: %s | %s", result['chronological_split'], result['split_info'])
                    _print_eval(result, 'test', thresh)
                    if result.get('lookahead_bias_warning'):
                        logger.warning("    ⚠️  [LOOKAHEAD] 测试集指标为近似值（模型已见未来数据），仅供参考")
                    for w in result.get('sanity_checks', {}).get('test', []):
                        logger.warning("    ⚠️  %s", w.split('] ')[-1] if '] ' in w else w)

                _print_eval(result, 'train', thresh)

                # Bootstrap CI（基于 contaminated test，仅供参考）
                boot = result.get('bootstrap_ci', {})
                if boot:
                    _print_bootstrap_ci("准确率(Bootstrap)", boot.get("accuracy", {}))

                # 交易成本
                tc = result.get('transaction_cost', {})
                if tc:
                    logger.info("    交易成本: %s | 平均 Edge 损失: %.4f",
                                tc.get("cost_rate", 0), tc.get("avg_edge_loss", 0))

                # Walk-Forward（每折重训练·真实OOS）
                wf = result.get('walk_forward_retrain', [])
                if wf:
                    n_windows = len(wf) - 1  # 最后一个是 avg
                    if n_windows > 0:
                        avg_entry = wf[-1]
                        logger.info("    ✅ Walk-Forward(重训练·真实OOS): %d 窗口 | 平均准确率 %.3f ± %.3f",
                                    n_windows, avg_entry.get("avg_accuracy", 0),
                                    avg_entry.get("std_accuracy", 0))

                pd_dist = result['prob_distribution']
                logger.info("    测试集概率分布: 中位数=%.3f P25=%.3f P75=%.3f 范围=[%.3f, %.3f]",
                            pd_dist['median'], pd_dist['p25'], pd_dist['p75'],
                            pd_dist['min'], pd_dist['max'])

    # 足球模型回测
    fb_csv = ROOT / 'data/processed/fb_features.csv'
    if fb_csv.exists():
        df_fb = pd.read_csv(fb_csv)
        feat_cols = _load_feature_columns(ROOT / 'models/model_fb_features.json')
        logger.info("\n%s", "=" * 60)
        logger.info("足球模型回测 (%s 样本)", len(df_fb))
        logger.info("%s", "=" * 60)
        for target in ['win', 'spread_result', 'total_result']:
            model_file = ROOT / f'models/model_fb_{target}_ensemble.pkl'
            if model_file.exists():
                thresh = 0.5
                logger.info("\n  模型: %s (阈值=%s)", model_file.name, thresh)
                result = evaluate_model(
                    df_fb, feat_cols, model_file, target, 'fb_features',
                    threshold=thresh, market_type=market_map.get(target, "default"),
                )
                report.append(result)

                oos = result.get('test_oos')
                if oos:
                    _print_eval_rename(result, oos, "test_oos(快速重训练·真实OOS)", thresh)
                    logger.info("      (轻量级 LGBM+XGB+CatBoost, 无 Optuna 调参, 结果为悲观下界)")
                    for w in result.get('sanity_checks', {}).get('test', []):
                        logger.warning("    ⚠️  %s", w.split('] ')[-1] if '] ' in w else w)
                else:
                    logger.info("  时序分割: %s | %s", result['chronological_split'], result['split_info'])
                    _print_eval(result, 'test', thresh)
                    if result.get('lookahead_bias_warning'):
                        logger.warning("    ⚠️  [LOOKAHEAD] 测试集指标为近似值（模型已见未来数据），仅供参考")
                    for w in result.get('sanity_checks', {}).get('test', []):
                        logger.warning("    ⚠️  %s", w.split('] ')[-1] if '] ' in w else w)

                _print_eval(result, 'train', thresh)

                boot = result.get('bootstrap_ci', {})
                if boot:
                    _print_bootstrap_ci("准确率(Bootstrap)", boot.get("accuracy", {}))

                tc = result.get('transaction_cost', {})
                if tc:
                    logger.info("    交易成本: %s | 平均 Edge 损失: %.4f",
                                tc.get("cost_rate", 0), tc.get("avg_edge_loss", 0))

                wf = result.get('walk_forward_retrain', [])
                if wf:
                    n_windows = len(wf) - 1
                    if n_windows > 0:
                        avg_entry = wf[-1]
                        logger.info("    ✅ Walk-Forward(重训练·真实OOS): %d 窗口 | 平均准确率 %.3f ± %.3f",
                                    n_windows, avg_entry.get("avg_accuracy", 0),
                                    avg_entry.get("std_accuracy", 0))

                pd_dist = result['prob_distribution']
                logger.info("    测试集概率分布: 中位数=%.3f P25=%.3f P75=%.3f 范围=[%.3f, %.3f]",
                            pd_dist['median'], pd_dist['p25'], pd_dist['p75'],
                            pd_dist['min'], pd_dist['max'])

    # 美式足球模型回测（NFL 淡季无新数据，但模型已训练，需验证历史表现）
    nfl_csv = ROOT / 'data/processed/nfl_features.csv'
    if nfl_csv.exists():
        df_nfl = pd.read_csv(nfl_csv)
        feat_cols = _load_feature_columns(ROOT / 'models/model_nfl_features.json')
        logger.info("\n%s", "=" * 60)
        logger.info("美式足球模型回测 (%s 样本)", len(df_nfl))
        logger.info("%s", "=" * 60)
        for target in ['win', 'spread_result', 'total_result']:
            model_file = ROOT / f'models/model_nfl_{target}_ensemble.pkl'
            if model_file.exists():
                thresh = 0.5
                logger.info("\n  模型: %s (阈值=%s, 特征=%d)", model_file.name, thresh, len(feat_cols))
                result = evaluate_model(
                    df_nfl, feat_cols, model_file, target, 'nfl_features',
                    threshold=thresh, market_type=market_map.get(target, "default"),
                )
                report.append(result)

                oos = result.get('test_oos')
                if oos:
                    _print_eval_rename(result, oos, "test_oos(快速重训练·真实OOS)", thresh)
                    logger.info("      (轻量级 LGBM+XGB+CatBoost, 无 Optuna 调参, 结果为悲观下界)")
                    for w in result.get('sanity_checks', {}).get('test', []):
                        logger.warning("    ⚠️  %s", w.split('] ')[-1] if '] ' in w else w)
                else:
                    logger.info("  时序分割: %s | %s", result['chronological_split'], result['split_info'])
                    _print_eval(result, 'test', thresh)
                    if result.get('lookahead_bias_warning'):
                        logger.warning("    ⚠️  [LOOKAHEAD] 测试集指标为近似值（模型已见未来数据），仅供参考")
                    for w in result.get('sanity_checks', {}).get('test', []):
                        logger.warning("    ⚠️  %s", w.split('] ')[-1] if '] ' in w else w)

                _print_eval(result, 'train', thresh)

                boot = result.get('bootstrap_ci', {})
                if boot:
                    _print_bootstrap_ci("准确率(Bootstrap)", boot.get("accuracy", {}))

                tc = result.get('transaction_cost', {})
                if tc:
                    logger.info("    交易成本: %s | 平均 Edge 损失: %.4f",
                                tc.get("cost_rate", 0), tc.get("avg_edge_loss", 0))

                wf = result.get('walk_forward_retrain', [])
                if wf:
                    n_windows = len(wf) - 1
                    if n_windows > 0:
                        avg_entry = wf[-1]
                        logger.info("    ✅ Walk-Forward(重训练·真实OOS): %d 窗口 | 平均准确率 %.3f ± %.3f",
                                    n_windows, avg_entry.get("avg_accuracy", 0),
                                    avg_entry.get("std_accuracy", 0))

                pd_dist = result['prob_distribution']
                logger.info("    测试集概率分布: 中位数=%.3f P25=%.3f P75=%.3f 范围=[%.3f, %.3f]",
                            pd_dist['median'], pd_dist['p25'], pd_dist['p75'],
                            pd_dist['min'], pd_dist['max'])

    DATA_DIR_PATH.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump({'updated': pd.Timestamp.now().isoformat(), 'report': report},
                  f, ensure_ascii=False, indent=2)
    logger.info("\n回测评估结果已保存至 %s", OUTPUT_PATH)

    # 汇总
    if report:
        logger.info("\n" + "=" * 60)
        logger.info("回测汇总")
        logger.info("=" * 60)
        all_flags = []
        for r in report:
            oos = r.get('test_oos')
            test = oos if oos else r.get("test", {})
            boot = r.get("bootstrap_ci", {}).get("accuracy", {})
            ci_str = f" [95%% CI: {boot.get('ci_lower', 0):.3f}-{boot.get('ci_upper', 0):.3f}]" if boot else ""
            oos_tag = " [快速重训练·悲观下界]" if oos else " [含lookahead]"
            logger.info("  %-30s 测试 Acc=%.3f%s  Brier=%.4f  n=%s%s",
                        f"{r['model'][:26]}", test.get("accuracy", 0),
                        ci_str, test.get("brier", 0), test.get("samples", 0), oos_tag)

            # 附加 Walk-Forward（最可靠的 OOS）
            wf = r.get('walk_forward_retrain', [])
            if wf and len(wf) > 1:
                avg = wf[-1]
                logger.info("  ╰─Walk-Forward(5窗平均) → Acc=%.1f%%±%.1f%%",
                            avg.get("avg_accuracy", 0) * 100,
                            avg.get("std_accuracy", 0) * 100)

            # 收集 RED FLAG（训练集和测试集）
            for side in ('train', 'test'):
                for w in r.get('sanity_checks', {}).get(side, []):
                    if 'RED FLAG' in w:
                        all_flags.append(w)

        if all_flags:
            logger.warning("\n" + "!" * 60)
            logger.warning("  ⛔ 指标异常汇总（%d 个 RED FLAG）", len(all_flags))
            logger.warning("!" + "!" * 59)
            for w in all_flags:
                logger.warning("  %s", w)
            logger.warning("!" * 60)


def run_portfolio_backtest(bet_log_csv: str = None, bankroll: float = 10000) -> dict:
    """投资组合级回测：对比顺序分配 vs Kelly 优化分配。

    读取下注历史 CSV（date, prob, odds, actual_win），
    按天分组模拟同时下注场景，用 QuantStats 计算专业指标。

    Args:
        bet_log_csv: 下注历史 CSV 路径，默认 BET_LOG_FILE
        bankroll: 初始本金

    Returns:
        {strategy_a: Sequential, strategy_b: Portfolio, comparison: {...}}
    """
    import quantstats as qs

    if bet_log_csv is None:
        from src.risk.manager import BET_LOG_FILE
        bet_log_csv = str(BET_LOG_FILE)

    bet_log = Path(bet_log_csv)
    if not bet_log.exists():
        logger.warning("下注历史不存在: %s", bet_log_csv)
        return {"error": "no_bet_log"}

    df = pd.read_csv(bet_log_csv)
    if df.empty or 'win' not in df.columns:
        return {"error": "invalid_bet_log"}
    if 'odds' not in df.columns or 'model_prob' not in df.columns:
        return {"error": "缺少 odds 或 model_prob 列"}

    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df = df.dropna(subset=['date', 'win', 'odds', 'model_prob'])
    df = df.sort_values('date').reset_index(drop=True)
    df['day'] = df['date'].dt.date

    from src.risk.portfolio import KellyPortfolioOptimizer
    from src.risk.manager import RiskManager

    # 策略 A: 顺序 Kelly（现有方式）
    rm_a = RiskManager(initial_budget=bankroll)
    eq_a = [float(bankroll)]

    # 策略 B: 组合 Kelly 优化
    bk_b = float(bankroll)
    eq_b = [bk_b]
    opt = KellyPortfolioOptimizer(max_single=0.05, max_total=0.30)

    for day, day_bets in df.groupby('day'):
        daily_inputs = []
        for _, row in day_bets.iterrows():
            daily_inputs.append({
                'prob': float(row['model_prob']),
                'odds': float(row['odds']),
                'actual': int(row['win']),
            })

        if not daily_inputs:
            continue

        # 策略 A
        day_exp_a = 0.0
        day_pnl_a = 0.0
        for b in daily_inputs:
            stake = rm_a.get_max_stake(
                b['prob'], b['odds'],
                current_exposure_pct=day_exp_a,
                input_is_prob=True,
            )
            if stake > 0:
                pnl = stake * (b['odds'] - 1) if b['actual'] else -stake
                day_pnl_a += pnl
                day_exp_a += stake / max(rm_a.current_balance, 1)
        eq_a.append(eq_a[-1] + day_pnl_a)

        # 策略 B
        weights, meta = opt.solve(daily_inputs)
        day_pnl_b = 0.0
        for i, b in enumerate(daily_inputs):
            if i < len(weights) and weights[i] > 0:
                stake_b = weights[i] * bk_b
                if stake_b > 0:
                    pnl = stake_b * (b['odds'] - 1) if b['actual'] else -stake_b
                    day_pnl_b += pnl
        bk_b += day_pnl_b
        eq_b.append(bk_b)

    # QuantStats 指标
    eq_a_series = pd.Series(eq_a, name='Sequential')
    eq_b_series = pd.Series(eq_b, name='Portfolio_Optimized')
    ret_a = eq_a_series.pct_change().dropna()
    ret_b = eq_b_series.pct_change().dropna()

    metrics_a = {
        'final_balance': round(float(eq_a[-1]), 2),
        'total_return': round(float(eq_a[-1] / bankroll - 1), 4),
        'sharpe': round(float(qs.stats.sharpe(ret_a)), 4) if len(ret_a) > 1 else 0.0,
        'max_drawdown': round(float(qs.stats.max_drawdown(eq_a_series)), 4),
        'sortino': round(float(qs.stats.sortino(ret_a)), 4) if len(ret_a) > 1 else 0.0,
    }
    metrics_b = {
        'final_balance': round(float(eq_b[-1]), 2),
        'total_return': round(float(eq_b[-1] / bankroll - 1), 4),
        'sharpe': round(float(qs.stats.sharpe(ret_b)), 4) if len(ret_b) > 1 else 0.0,
        'max_drawdown': round(float(qs.stats.max_drawdown(eq_b_series)), 4),
        'sortino': round(float(qs.stats.sortino(ret_b)), 4) if len(ret_b) > 1 else 0.0,
    }

    comparison = {}
    for k in metrics_a:
        if isinstance(metrics_a[k], (int, float)):
            diff = metrics_b[k] - metrics_a[k]
            pct_improvement = (diff / abs(metrics_a[k]) * 100) if metrics_a[k] != 0 else 0
            comparison[k] = round(pct_improvement, 2)

    result = {
        'strategy_a_sequential': metrics_a,
        'strategy_b_portfolio_optimized': metrics_b,
        'comparison_pct_improvement': comparison,
        'n_days': len(df['day'].unique()),
        'n_bets': len(df),
    }

    logger.info("\n%s", "=" * 60)
    logger.info("📊 投资组合回测结果")
    logger.info("%s", "=" * 60)
    logger.info("  策略 A (顺序): 终值=¥%.2f, Sharpe=%.3f, 回撤=%.1f%%",
                metrics_a['final_balance'], metrics_a['sharpe'], metrics_a['max_drawdown'] * 100)
    logger.info("  策略 B (优化): 终值=¥%.2f, Sharpe=%.3f, 回撤=%.1f%%",
                metrics_b['final_balance'], metrics_b['sharpe'], metrics_b['max_drawdown'] * 100)
    logger.info("  Sharpe 改善: %+.1f%%", comparison.get('sharpe', 0))

    # 追加到回测输出
    output_path = Path(OUTPUT_PATH)
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text())
            existing['portfolio_backtest'] = result
            output_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        except Exception:
            pass

    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--portfolio', action='store_true', help='运行投资组合回测')
    args = parser.parse_args()
    if args.portfolio:
        run_portfolio_backtest()
    else:
        run_backtest()
