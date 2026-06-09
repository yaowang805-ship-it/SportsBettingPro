#!/usr/bin/env python3
"""市场效率分析 — 按 (运动, 联赛, 盘口类型) 分组回测历史表现。

功能：
  1. 从 prediction_log.csv 读取已结算记录
  2. 按 (sport, league, market_type) 分组统计（含 Sharpe、置信区间）
  3. 输出效率评分卡 + 联赛置信度排名
  4. 输出允许名单和屏蔽名单供 RiskManager 使用
"""
import sys, json
from pathlib import Path
from collections import defaultdict
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np

from config.logging_config import get_logger
logger = get_logger(__name__)

LOG_PATH = ROOT / "data" / "storage" / "prediction_log.csv"
EFFICIENCY_PATH = ROOT / "data" / "storage" / "market_efficiency.json"
BLOCKLIST_PATH = ROOT / "models" / "market_blocklist.json"


def _bootstrap_roi(profits: np.ndarray, n_iter: int = 2000) -> tuple:
    """Bootstrap ROI 置信区间。"""
    n = len(profits)
    if n < 3:
        return (0.0, 0.0)
    rois = []
    for _ in range(n_iter):
        sample = np.random.choice(profits, size=n, replace=True)
        rois.append(sample.mean())
    return float(np.percentile(rois, 5)), float(np.percentile(rois, 95))


def _sharpe_ratio(profits: np.ndarray, stakes: np.ndarray) -> float:
    """计算投注层面的 Sharpe ratio。

    每笔 return = profit / stake（收益率），
    Sharpe = mean(return) / std(return) * sqrt(n)。
    Sharpe > 0.5 表示有持续edge，> 1.0 表示非常可靠。
    """
    valid = stakes > 0
    if valid.sum() < 3:
        return 0.0
    returns = profits[valid] / stakes[valid]
    std = returns.std()
    if std == 0:
        return 0.0
    return float(returns.mean() / std * np.sqrt(len(returns)))


def _confidence_score(n: int, sharpe: float, roi: float,
                       roi_ci_low: float, roi_ci_high: float) -> float:
    """联赛置信度评分 (0~100)。

    权重：
      - 样本量 (40%)：n < 20 线性递增，20+ 满分
      - Sharpe (30%)：0→0分, 0.5→15分, 1.0→30分
      - ROI 稳定性 (30%)：置信区间宽度越窄越高
    """
    # 样本量分
    n_score = min(n / 20, 1.0) * 40

    # Sharpe 分
    s_score = min(max(sharpe, 0), 2.0) / 2.0 * 30

    # 稳定性分：CI 越窄越可靠
    ci_width = roi_ci_high - roi_ci_low
    stability = max(0, 1.0 - ci_width / 0.5) * 30  # CI宽度50pp = 0分

    return round(min(n_score + s_score + stability, 100), 1)


def analyze_efficiency(min_samples: int = 5) -> dict:
    """分析各市场效率，返回评分卡（含 Sharpe + 置信区间）。"""
    if not LOG_PATH.exists():
        logger.warning("⚠️ 无预测记录: %s", LOG_PATH)
        return {}

    df = pd.read_csv(LOG_PATH)
    df = df[df["status"].isin(["won", "lost"])].copy()

    if df.empty:
        logger.info("📭 无已结算记录")
        return {}

    # 标准化字段
    df["profit"] = np.where(df["status"] == "won", df["odds"] * df["stake"] - df["stake"], -df["stake"])

    groups = df.groupby(["sport", "league", "market_type"])
    results = {}
    for (sport, league, mtype), grp in groups:
        n = len(grp)
        if n < min_samples:
            continue

        wins = (grp["status"] == "won").sum()
        win_rate = wins / n
        total_stake = grp["stake"].sum()
        total_profit = grp["profit"].sum()
        roi = total_profit / total_stake if total_stake > 0 else 0.0
        avg_ev = grp["ev"].mean()
        avg_odds = grp["odds"].mean()

        # Brier score
        y_true = (grp["status"] == "won").astype(int).values
        y_prob = grp["model_prob"].values
        brier = float(np.mean((y_true - y_prob) ** 2))

        # 校准误差
        cal_error = abs(win_rate - y_prob.mean()) if n > 0 else 0

        # Sharpe ratio
        profits_arr = grp["profit"].values
        stakes_arr = grp["stake"].values
        sharpe = _sharpe_ratio(profits_arr, stakes_arr)

        # Bootstrap ROI 置信区间
        roi_ci_low, roi_ci_high = _bootstrap_roi(profits_arr / stakes_arr.clip(min=1))

        # 置信度评分
        conf = _confidence_score(n, sharpe, roi, roi_ci_low, roi_ci_high)

        # 显著性：90% CI 全部 > 0
        significant = roi_ci_low > 0

        results[f"{sport}/{league}/{mtype}"] = {
            "sport": sport,
            "league": league,
            "market_type": mtype,
            "n": n,
            "wins": int(wins),
            "win_rate": round(float(win_rate), 4),
            "roi": round(float(roi), 4),
            "sharpe": round(float(sharpe), 4),
            "roi_ci_90": [round(float(roi_ci_low), 4), round(float(roi_ci_high), 4)],
            "significant": bool(significant),
            "confidence_score": conf,
            "avg_ev": round(float(avg_ev), 4),
            "avg_odds": round(float(avg_odds), 4),
            "brier": round(float(brier), 4),
            "cal_error": round(float(cal_error), 4),
            "total_profit": round(float(total_profit), 2),
            "total_stake": round(float(total_stake), 2),
        }

    # 生成屏蔽名单（增强版：考虑置信度）
    blocklist = []
    for key, info in results.items():
        if info["roi"] < -0.05 or info["win_rate"] < 0.35:
            blocklist.append({
                "sport": info["sport"],
                "league": info["league"],
                "market_type": info["market_type"],
                "reason": f"ROI={info['roi']:.1%} WinRate={info['win_rate']:.1%} Sharpe={info['sharpe']:.2f}",
                "n": info["n"],
            })

    output = {
        "generated_at": datetime.now().isoformat(),
        "total_settled": int(len(df)),
        "categories": len(results),
        "details": results,
        "blocklist": blocklist,
    }

    # Save
    EFFICIENCY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EFFICIENCY_PATH, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    with open(BLOCKLIST_PATH, "w") as f:
        json.dump(blocklist, f, ensure_ascii=False, indent=2)

    return output


def print_report(output: dict):
    """打印效率评分卡（含排名）。"""
    if not output:
        print("📭 无数据")
        return

    print(f"\n{'='*70}")
    print(f"  市场效率分析 ({output['generated_at'][:10]})")
    print(f"  已结算: {output['total_settled']} 条, {output['categories']} 个类别")
    print(f"{'='*70}")

    details = output.get("details", {})
    if not details:
        print("📭 无满足最低样本量的类别")
        return

    # ── 按置信度评分排名 ──
    ranked = sorted(details.items(), key=lambda x: -x[1]["confidence_score"])

    print(f"\n  📊 联赛置信度排名（综合评分）")
    print(f"  {'评分':<6} {'类别':<30} {'单数':<5} {'ROI':<8} {'Sharpe':<8} {'胜率':<7} {'90%CI':<14}")
    print(f"  {'-'*78}")
    for key, info in ranked:
        score = info["confidence_score"]
        sport = info["sport"]
        league = info["league"][:10]
        mtype = info["market_type"]
        label = f"{sport}/{league}/{mtype}"[:29]
        wr = f"{info['win_rate']:.0%}"
        roi = f"{info['roi']:+.1%}"
        sh = f"{info['sharpe']:.2f}"
        ci = f"[{info['roi_ci_90'][0]:+.0%}, {info['roi_ci_90'][1]:+.0%}]"
        sig = " ✅" if info["significant"] else ""
        print(f"  {score:<5.0f} {label:<30} {info['n']:<5} {roi:<8} {sh:<8} {wr:<7} {ci:<14}{sig}")

    # 显著性摘要
    sig_count = sum(1 for v in details.values() if v.get("significant"))
    print(f"\n  📈 显著正EV类别: {sig_count}/{len(details)}")

    # 屏蔽列表
    blocklist = output.get("blocklist", [])
    if blocklist:
        print(f"\n  ⛔ 屏蔽列表（ROI<-5% 或 WinRate<35%）:")
        for b in blocklist:
            print(f"    {b['sport']}/{b['league']}/{b['market_type']} — {b['reason']} ({b['n']}单)")
    else:
        print(f"\n  ✅ 无类别需要屏蔽")


if __name__ == "__main__":
    print_report(analyze_efficiency(min_samples=3))
