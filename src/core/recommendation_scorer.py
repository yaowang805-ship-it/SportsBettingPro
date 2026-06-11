#!/usr/bin/env python3
"""推荐质量评分 — 整合模型置信度、市场效率、Smart Money、校准可靠性。

用法:
    scorer = RecommendationScorer()
    result = scorer.score(pred_dict)
    # => {"score": 78.5, "tier": "medium",
    #     "breakdown": {"model_confidence": 22, "market_efficiency": 18, ...}}
"""
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

EFFICIENCY_PATH = ROOT / "data" / "storage" / "market_efficiency.json"


class RecommendationScorer:
    """推荐质量评分器 (0-100)。

    维度权重:
      - 模型置信度 (30%): 模型概率偏离市场的程度
      - 市场效率 (25%): 该(联赛,盘口)的历史Sharpe/胜率
      - Smart Money一致性 (20%): 模型方向 vs sharp money 方向
      - 校准可靠性 (15%): Brier score / cal_error
      - 冷启动惩罚 (10%): 数据不足时降分
    """

    # 维度权重
    WEIGHTS = {
        "model_confidence": 0.30,
        "market_efficiency": 0.25,
        "smart_money": 0.20,
        "calibration": 0.15,
        "cold_start": 0.10,
    }

    def __init__(self):
        self._efficiency: dict = self._load_efficiency()
        self._total_settled = self._efficiency.get("total_settled", 0) if self._efficiency else 0

    def _load_efficiency(self) -> dict:
        """加载市场效率数据。"""
        if not EFFICIENCY_PATH.exists():
            return {}
        try:
            return json.loads(EFFICIENCY_PATH.read_text())
        except Exception as e:
            logger.warning("  ⚠️ 加载市场效率数据失败: %s", e)
            return {}

    def _score_model_confidence(self, model_prob: float, mkt_prob: float) -> float:
        """模型置信度分 (0-30)。

        模型概率与市场概率的偏差越大 → 模型有更强的信号。
        公式: min(abs(model_prob - mkt_prob) * 200, 30)
        偏差 15pp → 满分 30, 偏差 5pp → 10分
        """
        if mkt_prob <= 0:
            return 0.0
        deviation = abs(model_prob - mkt_prob)
        return min(deviation * 200, 30.0)

    def _score_market_efficiency(self, sport: str, league: str, market_type: str) -> float:
        """市场效率分 (0-25)。

        从 market_efficiency.json 读取该(sport, league, market_type)的历史表现。
        Sharpe > 0.5 → 满分, Sharpe < 0 → 0分
        confidence_score > 80 → 满分, < 20 → 0分
        取两项的加权平均。
        """
        details = self._efficiency.get("details", {}) if self._efficiency else {}
        key = f"{sport}/{league}/{market_type}"
        entry = details.get(key)
        if not entry:
            # 尝试宽匹配：只匹配 sport + market_type
            for k, v in details.items():
                if k.startswith(f"{sport}/") and v.get("market_type") == market_type:
                    entry = v
                    break
        if not entry:
            return 0.0

        n = entry.get("n", 0)
        sharpe = entry.get("sharpe", 0)
        conf_score = entry.get("confidence_score", 0)

        # Sharpe 分 (0-12.5)
        sharpe_score = max(0, min(sharpe, 1.0)) / 1.0 * 12.5

        # confidence_score 分 (0-12.5)
        conf_score_portion = max(0, min(conf_score, 100)) / 100 * 12.5

        # 样本量惩罚：< 30 时线性降分
        sample_multiplier = min(n / 30, 1.0)

        return (sharpe_score + conf_score_portion) * sample_multiplier

    @staticmethod
    def _compute_smart_money_index(sharpe_home_prob: Optional[float],
                                    market_home_prob: float) -> float:
        """Smart Money 指数 (-100 ~ +100)。

        sharp books 的共识概率 vs 全市场平均概率。
        正 = sharp 指向主队, 负 = sharp 指向客队。
        """
        if sharpe_home_prob is None or market_home_prob <= 0:
            return 0.0
        diff = sharpe_home_prob - market_home_prob
        return max(-100.0, min(100.0, diff / max(market_home_prob, 0.01) * 100))

    def _score_smart_money(self, model_prob: float, mkt_prob: float,
                            sharp_home_prob: Optional[float],
                            model_is_home: bool = True) -> float:
        """Smart Money 一致性分 (0-20)。

        模型方向 vs smart money 方向：
        - 一致 (+10~20): 模型和sharp money都看好主队/客队
        - 中立 (~10): 无sharp数据
        - 相反 (0~5): 模型与sharp money方向相反
        """
        sm_index = self._compute_smart_money_index(sharp_home_prob, mkt_prob)

        # 无sharp数据 → 中立的10分
        if sharp_home_prob is None or abs(sm_index) < 5:
            return 10.0

        # 模型偏差方向: 正 = 模型看好主队
        model_deviation = model_prob - mkt_prob if model_is_home else mkt_prob - model_prob

        # 一致性检查
        if (sm_index > 0 and model_deviation > 0) or (sm_index < 0 and model_deviation < 0):
            # 方向一致: 力度越大分越高
            agreement = abs(sm_index) / 100.0  # 0~1
            return 10.0 + agreement * 10.0  # 10~20
        else:
            # 方向相反: 降分
            disagreement = abs(sm_index) / 100.0
            return max(0, 10.0 - disagreement * 10.0)

    def _score_calibration(self, sport: str, league: str, market_type: str) -> float:
        """校准可靠性分 (0-15)。

        从 market_efficiency.json 读取 Brier score 和 cal_error。
        Brier < 0.2 → 15分, Brier > 0.3 → 0分
        """
        details = self._efficiency.get("details", {}) if self._efficiency else {}
        key = f"{sport}/{league}/{market_type}"
        entry = details.get(key)
        if not entry:
            for k, v in details.items():
                if k.startswith(f"{sport}/") and v.get("market_type") == market_type:
                    entry = v
                    break
        if not entry:
            return 0.0

        brier = entry.get("brier", 0.5)
        cal_error = entry.get("cal_error", 0.5)

        # Brier 分 (0~10): 0.2→10分, 0.3→5分, 0.5→0分
        brier_score = max(0, min(10, (0.5 - brier) / 0.3 * 10))

        # 校准误差分 (0~5): 0→5分, 0.1→0分
        cal_score = max(0, min(5, (0.1 - cal_error) / 0.1 * 5))

        return brier_score + cal_score

    def _score_cold_start(self) -> float:
        """冷启动惩罚分 (0-10)。

        总结算数 < 20 时线性降分。
        < 5 → 0分（满惩罚）
        5~20 → 线性递增
        > 20 → 10分（无惩罚）
        """
        if self._total_settled >= 20:
            return 10.0
        if self._total_settled < 5:
            return 0.0
        return (self._total_settled - 5) / 15.0 * 10.0

    def score(self, pred: dict, market_type: str = "胜负",
              model_is_home: bool = True) -> dict:
        """计算推荐质量分。

        Args:
            pred: 预测字典，需包含:
                - model_prob, market_home_prob (或 mkt_prob)
                - sharp_home_prob (可选)
                - sport, league
                - odds (可选, 用于计算实际mkt_prob)
            market_type: 盘口类型 (胜负/让分/大小球)
            model_is_home: 模型概率是否是主胜方向

        Returns:
            {"score": float, "tier": str, "breakdown": dict, "smart_money_index": float}
        """
        sport = pred.get("sport", "unknown")
        league = pred.get("league", "")
        model_prob = pred.get("model_prob", 0.5)
        odds = pred.get("odds", 2.0)

        # 市场概率：优先使用传入的 mkt_prob，兜底用 1/odds
        mkt_prob = pred.get("mkt_prob", pred.get("market_home_prob", 0))
        if not mkt_prob or mkt_prob <= 0:
            mkt_prob = 1.0 / odds if odds > 1 else 0.5

        sharp_home_prob = pred.get("sharp_home_prob", None)

        # 各维度得分
        scores = {}
        scores["model_confidence"] = self._score_model_confidence(model_prob, mkt_prob)
        scores["market_efficiency"] = self._score_market_efficiency(sport, league, market_type)
        scores["smart_money"] = self._score_smart_money(model_prob, mkt_prob, sharp_home_prob, model_is_home)
        scores["calibration"] = self._score_calibration(sport, league, market_type)
        scores["cold_start"] = self._score_cold_start()

        # 加权总分（归一化到 0–100）
        raw = sum(scores[k] * self.WEIGHTS[k] for k in self.WEIGHTS)
        MAX_RAW = sum({
            "model_confidence": 30, "market_efficiency": 25,
            "smart_money": 20, "calibration": 15, "cold_start": 10,
        }[k] * self.WEIGHTS[k] for k in self.WEIGHTS)  # = 22.5
        total = raw / MAX_RAW * 100.0

        # 分档
        if total >= 80:
            tier = "high"
        elif total >= 60:
            tier = "medium"
        else:
            tier = "low"

        return {
            "score": round(total, 1),
            "tier": tier,
            "breakdown": {k: round(v, 1) for k, v in scores.items()},
            "smart_money_index": round(
                self._compute_smart_money_index(sharp_home_prob, mkt_prob), 1
            ),
        }

    def reload_efficiency(self):
        """重新加载市场效率数据（每日流水线中调用）。"""
        self._efficiency = self._load_efficiency()
        self._total_settled = self._efficiency.get("total_settled", 0) if self._efficiency else 0
