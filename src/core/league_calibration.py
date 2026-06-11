"""每联赛校准 — 跟踪联赛特定预测偏差并应用 Platt 缩放。

不同联赛的预测可靠性不同（英超 vs 巴甲），
此模块对每个联赛独立校准模型概率。

用法:
    calibrator = LeagueCalibrator()
    calibrator.update("EPL", 0.65, 1)  # (league, pred_prob, actual_win)
    calibrated_prob = calibrator.calibrate("EPL", 0.65)
"""
import sys
import json
from pathlib import Path
from collections import defaultdict

import numpy as np
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import DATA_DIR


class LeagueCalibrator:
    """每联赛校准器 — 存储历史预测结果，>50 样本时自动拟合 Platt 缩放。"""

    MIN_SAMPLES_PER_LEAGUE = 50
    MAX_ROLLING = 200  # 滚动窗口大小

    def __init__(self, storage_path: str = None):
        self.storage_path = storage_path or str(DATA_DIR / "league_calibration.json")
        # {league: [(pred_prob, actual_outcome), ...]}
        self.history: dict = defaultdict(list)
        # {league: LogisticRegression model}
        self.calibrators: dict = {}
        self.load()

    def update(self, league: str, pred_prob: float, actual: int):
        """记录预测-结果对并自动重拟合（如果样本量足够）。"""
        self.history[league].append((float(pred_prob), int(actual)))
        # 裁剪到滚动窗口
        if len(self.history[league]) > self.MAX_ROLLING:
            self.history[league] = self.history[league][-self.MAX_ROLLING:]
        # 自动重拟合
        if len(self.history[league]) >= self.MIN_SAMPLES_PER_LEAGUE:
            self._refit(league)

    def _refit(self, league: str):
        """对联赛拟合 Platt 缩放（sigmoid 校准）。"""
        records = self.history[league]
        probs = np.array([p for p, _ in records]).reshape(-1, 1)
        outcomes = np.array([o for _, o in records])
        calibrator = LogisticRegression(C=1.0, solver='lbfgs', max_iter=500, random_state=42)
        try:
            calibrator.fit(probs, outcomes)
            self.calibrators[league] = calibrator
        except Exception as e:
            logger.debug("联赛 %s 校准拟合失败: %s", league, e)

    def calibrate(self, league: str, raw_prob: float) -> float:
        """如果有联赛校准器，应用校准；否则返回原始概率。"""
        if league in self.calibrators:
            try:
                p = self.calibrators[league].predict_proba(
                    np.array([[raw_prob]])
                )[0, 1]
                return float(np.clip(p, 0.02, 0.98))
            except Exception:
                return raw_prob
        return raw_prob

    def get_league_stats(self) -> dict:
        """返回每联赛统计量。"""
        stats = {}
        for league, records in self.history.items():
            n = len(records)
            outcomes = np.array([o for _, o in records])
            probs = np.array([p for p, _ in records])
            preds = (probs >= 0.5).astype(int)
            accuracy = float(np.mean(preds == outcomes))
            avg_prob = float(np.mean(probs))
            avg_outcome = float(np.mean(outcomes))
            calibrated = league in self.calibrators
            stats[league] = {
                "n_samples": n,
                "accuracy": round(accuracy, 4),
                "avg_prob": round(avg_prob, 4),
                "avg_outcome": round(avg_outcome, 4),
                "bias": round(avg_prob - avg_outcome, 4),
                "calibrated": calibrated,
            }
        return stats

    def save(self):
        """持久化到 JSON。"""
        data = {k: v for k, v in self.history.items() if len(v) >= self.MIN_SAMPLES_PER_LEAGUE}
        path = Path(self.storage_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    def load(self):
        """从 JSON 加载。"""
        path = Path(self.storage_path)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self.history = defaultdict(list, {
                    k: [tuple(p) for p in v] for k, v in data.items()
                })
            except Exception:
                pass


if __name__ == "__main__":
    # 简单测试
    c = LeagueCalibrator()
    print("联赛校准器初始化完成")
    print(f"  存储路径: {c.storage_path}")
    print(f"  已加载联赛: {list(c.history.keys())}")
    stats = c.get_league_stats()
    if stats:
        for league, s in stats.items():
            print(f"  {league}: {s['n_samples']} 样本, 准确率 {s['accuracy']:.1%}, "
                  f"偏差 {s['bias']:.3f}, 已校准={s['calibrated']}")
