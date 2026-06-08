"""MLP 神经网络模型封装 — 用 sklearn MLPClassifier 作为集成基模型。

用法:
    from src.models.mlp_model import MLPWrapper
    model = MLPWrapper()
    model.fit(X_train, y_train)
    probs = model.predict_proba(X_test)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler


class MLPWrapper:
    """MLP 封装 — 保持与 EnsemblePredictor 的兼容性。

    自动执行 StandardScaler 标准化（MLP 需要）。
    """

    _estimator_type = 'classifier'

    def __init__(self, *, random_state=42, **kwargs):
        self.scaler = StandardScaler()
        self.fitted = False
        self.classes_ = None

        # 默认参数
        params = {
            'hidden_layer_sizes': (128, 64),
            'activation': 'relu',
            'solver': 'adam',
            'alpha': 0.001,
            'learning_rate': 'adaptive',
            'learning_rate_init': 0.001,
            'max_iter': 500,
            'shuffle': False,
            'random_state': random_state,
            'tol': 1e-4,
            'early_stopping': True,
            'validation_fraction': 0.15,
            'n_iter_no_change': 20,
            'verbose': False,
        }
        params.update(kwargs)
        self.model = MLPClassifier(**params)

    def fit(self, X, y):
        if isinstance(X, np.ndarray):
            X_scaled = self.scaler.fit_transform(X)
        else:
            X_scaled = self.scaler.fit_transform(X.values)
        self.model.fit(X_scaled, y)
        self.fitted = True
        self.classes_ = self.model.classes_
        return self

    def predict_proba(self, X):
        if isinstance(X, np.ndarray):
            X_scaled = self.scaler.transform(X)
        else:
            X_scaled = self.scaler.transform(X.values)
        return self.model.predict_proba(X_scaled)

    def predict(self, X):
        if isinstance(X, np.ndarray):
            X_scaled = self.scaler.transform(X)
        else:
            X_scaled = self.scaler.transform(X.values)
        return self.model.predict(X_scaled)

    def get_params(self, deep=True):
        params = self.model.get_params(deep)
        params['random_state'] = params.get('random_state', 42)
        return params

    def set_params(self, **params):
        self.model.set_params(**params)
        return self
