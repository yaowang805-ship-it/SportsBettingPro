"""概率校准工具 — 动态置信度加权与模型校准辅助。

专业博彩团队的模型输出从来不直接使用，而是经过：
  1. 校准（Calibration）— 确保概率反映真实频率
  2. 收缩（Shrinkage）— 向市场先验回归，防止过拟合
  3. 置信度加权（Confidence Weighting）— 不确定时更依赖市场
"""
import numpy as np
from typing import Optional


def dynamic_shrinkage(
    model_prob: float,
    market_prob: float,
    min_model_weight: float = 0.20,
    max_model_weight: float = 0.50,
    confidence_power: float = 0.7,
) -> float:
    """动态概率融合 — 模型越自信，模型权重越高。

    Args:
        model_prob: 模型输出的概率 (0~1)
        market_prob: 市场隐含概率 (1/odds)
        min_model_weight: 模型权重下限（最不确定时）
        max_model_weight: 模型权重上限（最确定时）
        confidence_power: 置信度映射曲线的幂次（<1 更激进，>1 更保守）

    Returns:
        融合后的概率
    """
    # 置信度: 模型离 0.5 越远越自信，0~1 范围
    confidence = abs(model_prob - 0.5) * 2.0
    confidence = np.clip(confidence, 0.0, 1.0)

    # 幂次映射：低幂次 → 低置信度时给模型更少权重
    mapped = confidence ** confidence_power

    # 动态权重
    model_weight = min_model_weight + (max_model_weight - min_model_weight) * mapped
    model_weight = np.clip(model_weight, 0.0, 1.0)

    return model_weight * model_prob + (1.0 - model_weight) * market_prob


def adjust_for_sample_size(
    model_prob: float,
    market_prob: float,
    n_samples: int = 100,
    min_samples: int = 50,
) -> float:
    """根据训练样本量调整融合权重。

    样本越少，越依赖市场先验。
    """
    if n_samples >= min_samples * 4:
        model_weight = 0.40
    elif n_samples >= min_samples * 2:
        model_weight = 0.30
    elif n_samples >= min_samples:
        model_weight = 0.20
    else:
        model_weight = 0.10
    return model_weight * model_prob + (1.0 - model_weight) * market_prob


def calibrate_ensemble(
    model_prob: float,
    market_prob: float,
    confidence: Optional[float] = None,
    n_samples: Optional[int] = None,
) -> float:
    """综合校准管道：置信度 + 样本量 + 市场先验。

    这是推荐的统一入口，替代所有固定比例的软代码。
    """
    prob = dynamic_shrinkage(model_prob, market_prob)
    if n_samples is not None:
        prob = adjust_for_sample_size(prob, market_prob, n_samples)
    return prob


def brier_score(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """计算 Brier 分数（越小越好，0=完美）。"""
    return float(np.mean((y_true - y_prob) ** 2))


def calibration_curve(y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10):
    """计算校准曲线数据。"""
    bin_edges = np.linspace(0, 1, bins + 1)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    fraction_positives = np.zeros(bins)
    for i in range(bins):
        mask = (y_prob >= bin_edges[i]) & (y_prob < bin_edges[i + 1])
        if mask.sum() > 0:
            fraction_positives[i] = y_true[mask].mean()
        else:
            fraction_positives[i] = np.nan
    return bin_centers, fraction_positives
