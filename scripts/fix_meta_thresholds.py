#!/usr/bin/env python3
"""为所有现有集成模型计算最优阈值并更新 meta JSON。

无需重训练，直接加载已有模型 + 校准集数据计算。
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score

from config.logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger(__name__)

from config.settings import MODEL_DIR

MODEL_DIR_PATH = Path(MODEL_DIR) if isinstance(MODEL_DIR, str) else MODEL_DIR


def _load_training_data(sport: str):
    """与 ensemble_trainer._load_data 逻辑一致。"""
    if sport == 'bb':
        csv_path = ROOT / 'data/processed/bb_features.csv'
        feat_json = MODEL_DIR_PATH / 'model_bb_features.json'
    else:
        csv_path = ROOT / 'data/processed/fb_features.csv'
        feat_json = MODEL_DIR_PATH / 'model_fb_features.json'

    df = pd.read_csv(csv_path)
    df['date'] = pd.to_datetime(df['date'], utc=True, errors='coerce')

    with open(feat_json) as f:
        feat_cols = json.load(f)
    feat_cols = [c for c in feat_cols if c in df.columns]
    return df, feat_cols


def _compute_optimal_threshold(y_true, y_prob):
    """搜索 0.30~0.70 范围内使准确率最大的阈值。"""
    thresholds = np.arange(0.30, 0.71, 0.02)
    best_acc = 0.0
    best_thresh = 0.5
    for t in thresholds:
        preds = (y_prob >= t).astype(int)
        acc = accuracy_score(y_true, preds)
        if acc > best_acc:
            best_acc = acc
            best_thresh = t
    return float(best_thresh), float(best_acc)


def fix_meta(sport: str):
    """为指定运动的所有目标修复 meta 文件。"""
    prefix = 'model_bb' if sport == 'bb' else 'model_fb'
    logger.info("=" * 50)
    logger.info("  修复 %s meta 文件", sport.upper())
    logger.info("=" * 50)

    df, feat_cols = _load_training_data(sport)

    for target in ['win', 'spread_result', 'total_result']:
        meta_path = MODEL_DIR_PATH / f"{prefix}_{target}_ensemble_meta.json"
        model_path = MODEL_DIR_PATH / f"{prefix}_{target}_ensemble.pkl"

        if not meta_path.exists():
            logger.warning("  跳过 %s: meta 不存在", target)
            continue
        if not model_path.exists():
            logger.warning("  跳过 %s: 模型不存在", target)
            continue

        # 加载 meta
        with open(meta_path) as f:
            meta = json.load(f)

        # 跳过已有 optimal_threshold 的
        if 'optimal_threshold' in meta:
            logger.info("  %s 已有 optimal_threshold，跳过", target)
            continue

        # 准备标签
        if target == 'win':
            target_col = 'win'
        elif target == 'spread_result':
            target_col = 'spread_result'
        else:
            target_col = 'total_result'

        train_df = df.dropna(subset=[target_col]).copy()
        if len(train_df) < 200:
            logger.warning("  跳过 %s: 样本不足 (%d)", target, len(train_df))
            continue

        X = train_df[feat_cols].fillna(0)
        y = train_df[target_col].astype(int)

        # 60/40 时间序分割（与 ensemble_trainer 一致）
        dates = train_df['date'].values
        sorted_order = np.argsort(pd.to_datetime(dates))
        split_point = int(len(sorted_order) * 0.6)
        split_point = max(1, min(split_point, len(sorted_order) - 1))
        cal_idx = sorted_order[split_point:]

        X_cal = X.iloc[cal_idx]
        y_cal = y.iloc[cal_idx]

        # 加载模型并预测
        try:
            model = joblib.load(model_path)
            cal_probs = model.predict_proba(X_cal)[:, 1]
        except Exception as e:
            logger.warning("  跳过 %s: 模型加载/预测失败: %s", target, e)
            continue

        # 计算最优阈值
        best_thresh, best_acc = _compute_optimal_threshold(y_cal, cal_probs)
        logger.info("  %s: 最优阈值=%.2f, 校准集准确率=%.4f", target, best_thresh, best_acc)

        # 更新 meta
        meta['optimal_threshold'] = {
            'threshold': best_thresh,
            'test_accuracy': best_acc,
        }
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)
        logger.info("  ✅ 已更新 %s", meta_path.name)

    logger.info("=" * 50)
    logger.info("  %s 修复完成", sport.upper())
    logger.info("=" * 50)


if __name__ == '__main__':
    fix_meta('bb')
    fix_meta('fb')
    logger.info("\n✅ 全部 meta 文件修复完成")
