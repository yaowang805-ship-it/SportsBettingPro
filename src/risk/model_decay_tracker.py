"""模型退化追踪 — 滑动窗口准确率 → 置信度乘数。

连接模型监控与风控系统：每个模型的近期准确率决定其推荐的下注权重。
若模型准确率从 65% 降至 55%，该模型的推荐自动获得更低乘数。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

DECAY_FILE = DATA_DIR / "model_decay_state.json"

# 每个模型在各自特点下的基准准确率（低于此值 → 降权）
ACCURACY_BASELINES = {
    "bb": 0.55,    # NBA 基准
    "fb": 0.52,    # 足球基准（更难预测）
    "nfl": 0.53,   # NFL 基准
    "ensemble": 0.56,
    "dc": 0.50,
    "poisson": 0.50,
}


class ModelDecayTracker:
    """每个预测流水线的滑动窗口准确率追踪器。

    用法:
        tracker = ModelDecayTracker()
        tracker.record_prediction("bb", 0.65, True)  # 模型名, 概率, 是否猜对
        mult = tracker.get_confidence_multiplier("bb")  # 0.3~1.0
    """

    def __init__(self, window_sizes: List[int] = None):
        self.window_sizes = window_sizes or [30, 100]
        # model_name → [(timestamp_iso, correct: bool)]
        self.history: Dict[str, List[Tuple[str, bool]]] = {}
        self._load()

    # ── 核心接口 ─────────────────────────────────────

    def record_prediction(self, model_name: str, prob: float,
                          actual_result: bool, outcome: Optional[str] = None):
        """记录一次预测结果。

        Args:
            model_name: 模型标识（bb/fb/nfl/ensemble/dc/poisson）
            prob: 模型输出的概率（0~1）
            actual_result: 实际结果（True=赢, False=输）
            outcome: 可选，'won'/'lost'/'pending'，兼容外部系统
        """
        correct = actual_result
        if model_name not in self.history:
            self.history[model_name] = []
        self.history[model_name].append((datetime.now().isoformat(), correct))
        self._trim(model_name, max_len=2000)
        self._save()

    def get_confidence_multiplier(self, model_name: str,
                                  min_records: int = 20) -> float:
        """基于最近准确率返回置信度乘数（0.3~1.0）。

        逻辑:
          - 记录 < min_records: 1.0（无足够数据，不作调整）
          - 准确率 < baseline: 等比降权至最低 0.3
          - 准确率 baseline ~ baseline+5%: 0.8（轻微降权）
          - 准确率 > baseline+5%: 1.0（正常下注）
        """
        if model_name not in self.history or len(self.history[model_name]) < min_records:
            return 1.0

        recent = self._recent(model_name, 50)
        if len(recent) < min_records:
            return 1.0

        accuracy = sum(1 for _, c in recent if c) / len(recent)
        baseline = ACCURACY_BASELINES.get(model_name, 0.50)

        if accuracy < baseline:
            # 低于基准: 等比降权，最低 0.3
            return max(0.3, accuracy / baseline * 0.6)
        elif accuracy < baseline + 0.05:
            return 0.8
        else:
            return 1.0

    def get_multi_window_multiplier(self, model_name: str) -> float:
        """多窗口综合乘数 — 取各窗口的最小值（最悲观原则）。"""
        multipliers = []
        for ws in self.window_sizes:
            m = self._window_multiplier(model_name, ws)
            if m < 1.0:
                multipliers.append(m)
        return min(multipliers) if multipliers else 1.0

    def get_all_health(self) -> Dict[str, dict]:
        """获取所有模型的退化健康度报告。"""
        report = {}
        for model_name in self.history:
            records = self.history[model_name]
            if len(records) < 10:
                continue
            recent = self._recent(model_name, 50)
            accuracy = (sum(1 for _, c in recent if c) / len(recent)
                        if recent else 0.0)
            baseline = ACCURACY_BASELINES.get(model_name, 0.50)
            report[model_name] = {
                "total_records": len(records),
                "recent_50_accuracy": round(accuracy, 4),
                "baseline": baseline,
                "multiplier": round(self.get_confidence_multiplier(model_name), 4),
                "degraded": accuracy < baseline - 0.02,
            }
        return report

    # ── 内部方法 ─────────────────────────────────────

    def _recent(self, model_name: str, n: int) -> List[Tuple[str, bool]]:
        return self.history.get(model_name, [])[-n:]

    def _window_multiplier(self, model_name: str, window: int) -> float:
        if model_name not in self.history:
            return 1.0
        records = self.history[model_name][-window:]
        if len(records) < 10:
            return 1.0
        accuracy = sum(1 for _, c in records if c) / len(records)
        baseline = ACCURACY_BASELINES.get(model_name, 0.50)
        if accuracy < baseline - 0.05:
            return 0.5
        elif accuracy < baseline:
            return 0.8
        return 1.0

    def _trim(self, model_name: str, max_len: int = 2000):
        if model_name in self.history and len(self.history[model_name]) > max_len:
            self.history[model_name] = self.history[model_name][-max_len:]

    def _save(self):
        try:
            DECAY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(DECAY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("model decay state 保存失败: %s", e)

    def _load(self):
        if DECAY_FILE.exists():
            try:
                raw = json.loads(DECAY_FILE.read_text())
                self.history = {
                    k: [(ts, bool(c)) for ts, c in v]
                    for k, v in raw.items()
                }
            except Exception as e:
                logger.warning("model decay state 加载失败: %s", e)

    def clear_history(self, model_name: Optional[str] = None):
        """清空指定模型或全部历史（用于测试）。"""
        if model_name:
            self.history.pop(model_name, None)
        else:
            self.history.clear()
        self._save()
