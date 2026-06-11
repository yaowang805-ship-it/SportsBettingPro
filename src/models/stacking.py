"""可 pickle 的集成模型包装器 — Stage-2 Stacking + 加权平均。"""

import numpy as np


class Stage2Stacking:
    """Stage-2 Stacking: 用预训练基模型的概率作为 meta 特征训练 LR。"""
    _estimator_type = "classifier"

    def __init__(self, base_models, meta_model):
        self.base_models = base_models if base_models else []
        self.meta_model = meta_model
        self.classes_ = meta_model.classes_

    def predict_proba(self, X):
        meta_X = np.column_stack([
            m.predict_proba(X)[:, 1] for _, m in self.base_models
        ])
        return self.meta_model.predict_proba(meta_X)


class WeightedEnsemble:
    """加权平均集成 — 可 pickle 的加权集成替代。"""
    _estimator_type = 'classifier'
    def __init__(self, estimators, weights):
        self.estimators_ = [(n, m) for n, m in estimators]
        self.weights = weights
        self.classes_ = np.array([0, 1])
    def fit(self, X, y):
        return self
    def predict_proba(self, X):
        probs = np.zeros((X.shape[0], 2))
        for (_, m), w in zip(self.estimators_, self.weights):
            probs += w * m.predict_proba(X)
        probs /= sum(self.weights)
        return probs
    def get_params(self, deep=True):
        return {}
