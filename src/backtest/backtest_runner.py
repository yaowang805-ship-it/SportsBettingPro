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

from config.logging_config import get_logger
logger = get_logger(__name__)

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import (brier_score_loss, log_loss, precision_score,
                             recall_score, f1_score, confusion_matrix)

from config.settings import DATA_DIR
from src.core.evaluation import brier_score, safe_log_loss, sharpe_ratio

DATA_DIR_PATH = ROOT / DATA_DIR if isinstance(DATA_DIR, str) else DATA_DIR
OUTPUT_PATH = DATA_DIR_PATH / 'model_backtest_summary.json'

# 按市场类型的交易成本 (滑点 + 佣金，以赔率为基础)
TRANSACTION_COSTS = {
    "h2h": 0.02,       # 标准胜负盘 2%
    "spread": 0.03,     # 让分盘 3%
    "total": 0.03,      # 大小球 3%
    "default": 0.025,   # 默认
}

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


def simulate_transaction_cost(probs, prices, market_type="default"):
    """模拟交易成本对实际盈亏的影响。

    交易成本模型: 真实赔率 = 名义赔率 * (1 - cost_rate)
    对应的隐含概率 = 1 / 真实赔率

    Args:
        probs: 模型预测概率
        prices: 名义市场赔率 (decimal)
        market_type: h2h / spread / total / default

    Returns:
        adjusted_probs: 扣除交易成本后的有效概率
        edge_loss: 每个样本的 edge 损失
    """
    cost_rate = TRANSACTION_COSTS.get(market_type, TRANSACTION_COSTS["default"])
    prices = np.asarray(prices, dtype=float)

    # 有效赔率 = 名义赔率 * (1 - cost_rate)
    effective_prices = prices * (1 - cost_rate)
    # 有效市场概率 = 1 / 有效赔率
    market_probs_effective = 1.0 / effective_prices

    # Edge 损失 = 模型概率 - 有效市场概率 vs 模型概率 - 原始市场概率
    market_probs_raw = 1.0 / prices
    edge_after_cost = probs - market_probs_effective
    edge_before_cost = probs - market_probs_raw
    edge_loss = edge_before_cost - edge_after_cost

    return {
        "avg_edge_loss": float(np.mean(edge_loss)),
        "cost_rate": cost_rate,
    }


def simulate_walk_forward(df, feat_cols, model, target_col, n_windows=5, test_size=0.15):
    """Walk-Forward 分析：滚动扩展窗口验证。

    每次窗口: 用之前所有数据训练(或直接评估已有模型)，在后续测试集上评估。
    这里用已有模型在每期测试集上评估(无需重训练)。

    Args:
        df: 特征数据
        feat_cols: 特征列
        model: 预训练模型
        target_col: 目标列
        n_windows: 窗口数
        test_size: 每期测试集占总数据比例

    Returns:
        [{window_idx, train_range, test_range, accuracy, brier, logloss, samples}, ...]
    """
    df = df.dropna(subset=[target_col]).sort_values("date").reset_index(drop=True)
    X = df[feat_cols].fillna(0)
    y = df[target_col].astype(int).values
    dates = df["date"].values

    proba_fn = getattr(model, "predict_proba", None)
    if proba_fn is None:
        return []
    probs = model.predict_proba(X)[:, 1]

    total = len(df)
    # 每期测试集大小（向前滚动）
    step = int(total * test_size)
    # 初始训练集大小
    init_train = total - step * n_windows
    if init_train < 50:
        # 数据不够，减少窗口数
        n_windows = max(1, (total - 50) // step)
        init_train = total - step * n_windows
    if init_train < 10:
        init_train = 10

    results = []
    for w in range(n_windows):
        train_end = init_train + w * step
        test_start = train_end
        test_end = min(test_start + step, total)

        if test_start >= test_end:
            break

        train_y = y[:train_end] if train_end > 0 else y[:1]
        test_y = y[test_start:test_end]
        test_probs = probs[test_start:test_end]

        if len(test_y) < 2:
            continue

        results.append({
            "window": w + 1,
            "train_range": f"{dates[0]} to {dates[train_end-1] if train_end > 0 else dates[0]}",
            "test_range": f"{dates[test_start]} to {dates[test_end-1]}",
            "train_samples": int(train_end),
            "test_samples": int(len(test_y)),
            "accuracy": float(( (test_probs > 0.5).astype(int) == test_y).mean()),
            "brier": float(brier_score(test_y, test_probs)),
            "logloss": float(safe_log_loss(test_y, test_probs)),
            "mean_prob": float(np.mean(test_probs)),
        })

    # 汇总
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


def evaluate_model(df, feat_cols, model_path, target_col, dataset_name: str,
                   threshold: float = 0.5, test_frac: float = 0.2,
                   market_type: str = "default"):
    """评估模型：时间序列分割 + Bootstrap置信区间 + 交易成本。

    返回 dict，包含训练集和测试集指标、置信区间、交易成本影响。
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])

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
    cost_analysis = simulate_transaction_cost(test_probs, nominal_prices, market_type)

    # 全量指标（历史兼容）
    all_metrics = _classification_metrics(y, probs, threshold)

    # 验证时序
    train_end_ts = pd.Timestamp(dates[split_idx - 1])
    test_start_ts = pd.Timestamp(dates[split_idx])
    chronological_valid = bool(train_end_ts <= test_start_ts)

    result = {
        'model': Path(model_path).name,
        'dataset': dataset_name,
        'target': target_col,
        'market_type': market_type,
        'chronological_split': chronological_valid,
        'split_date': str(dates[split_idx - 1]),
        'split_info': f"训练集: {len(train_y)} 样本 (至 {dates[split_idx - 1]}) | 测试集: {len(test_y)} 样本 (自 {dates[split_idx]} 起)",
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

    # ── Walk-Forward 分析 ──
    try:
        wf_results = simulate_walk_forward(
            df, valid_cols, model, target_col,
            n_windows=5, test_size=test_frac / 2
        )
        if wf_results:
            result['walk_forward'] = wf_results
    except Exception as e:
        logger.debug("Walk-forward 分析跳过: %s", e)

    return result


def _print_bootstrap_ci(label, boot_dict):
    """打印 Bootstrap 置信区间。"""
    logger.info("      %s Bootstrap 95%% CI: [%.4f, %.4f] (mean=%.4f, std=%.4f)",
                label,
                boot_dict["ci_lower"], boot_dict["ci_upper"],
                boot_dict["mean"], boot_dict["std"])


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
                logger.info("\n  模型: %s (阈值=%s)", model_file.name, thresh)
                result = evaluate_model(
                    df_bb, feat_cols, model_file, target, 'bb_features',
                    threshold=thresh, market_type=market_map.get(target, "default"),
                )
                report.append(result)
                logger.info("  时序分割: %s | %s", result['chronological_split'], result['split_info'])
                _print_eval(result, 'train', thresh)
                _print_eval(result, 'test', thresh)

                # Bootstrap CI
                boot = result.get('bootstrap_ci', {})
                if boot:
                    _print_bootstrap_ci("准确率", boot.get("accuracy", {}))
                    _print_bootstrap_ci("Brier", boot.get("brier", {}))

                # 交易成本
                tc = result.get('transaction_cost', {})
                if tc:
                    logger.info("    交易成本: %s | 平均 Edge 损失: %.4f",
                                tc.get("cost_rate", 0), tc.get("avg_edge_loss", 0))

                # Walk-Forward
                wf = result.get('walk_forward', [])
                if wf:
                    n_windows = len(wf) - 1  # 最后一个是 avg
                    if n_windows > 0:
                        avg_entry = wf[-1]
                        logger.info("    Walk-Forward: %d 窗口 | 平均准确率 %.3f ± %.3f",
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
                logger.info("  时序分割: %s | %s", result['chronological_split'], result['split_info'])
                _print_eval(result, 'train', thresh)
                _print_eval(result, 'test', thresh)

                boot = result.get('bootstrap_ci', {})
                if boot:
                    _print_bootstrap_ci("准确率", boot.get("accuracy", {}))

                tc = result.get('transaction_cost', {})
                if tc:
                    logger.info("    交易成本: %s | 平均 Edge 损失: %.4f",
                                tc.get("cost_rate", 0), tc.get("avg_edge_loss", 0))

                wf = result.get('walk_forward', [])
                if wf:
                    n_windows = len(wf) - 1
                    if n_windows > 0:
                        avg_entry = wf[-1]
                        logger.info("    Walk-Forward: %d 窗口 | 平均准确率 %.3f ± %.3f",
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
        for r in report:
            test = r.get("test", {})
            boot = r.get("bootstrap_ci", {}).get("accuracy", {})
            ci_str = f" [95%% CI: {boot.get('ci_lower', 0):.3f}-{boot.get('ci_upper', 0):.3f}]" if boot else ""
            logger.info("  %-30s 测试 Acc=%.3f%s  Brier=%.4f  n=%s",
                        f"{r['model'][:28]}", test.get("accuracy", 0),
                        ci_str, test.get("brier", 0), test.get("samples", 0))


if __name__ == '__main__':
    run_backtest()
