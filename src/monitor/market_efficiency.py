#!/usr/bin/env python3
"""市场效率分析 — 按 (运动, 联赛, 盘口类型) 分组回测历史表现。

功能：
  1. 从 prediction_log.csv 读取已结算记录
  2. 按 (sport, league, market_type) 分组统计
  3. 输出效率评分卡
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


def analyze_efficiency(min_samples: int = 5) -> dict:
    """分析各市场效率，返回评分卡。"""
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

        # 校准误差 (model_prob vs actual win_rate in bins)
        cal_error = abs(win_rate - y_prob.mean()) if n > 0 else 0

        results[f"{sport}/{league}/{mtype}"] = {
            "sport": sport,
            "league": league,
            "market_type": mtype,
            "n": n,
            "wins": int(wins),
            "win_rate": round(float(win_rate), 4),
            "roi": round(float(roi), 4),
            "avg_ev": round(float(avg_ev), 4),
            "avg_odds": round(float(avg_odds), 4),
            "brier": round(float(brier), 4),
            "cal_error": round(float(cal_error), 4),
            "total_profit": round(float(total_profit), 2),
            "total_stake": round(float(total_stake), 2),
        }

    # 生成屏蔽名单
    blocklist = []
    for key, info in results.items():
        if info["roi"] < -0.05 or info["win_rate"] < 0.35:
            blocklist.append({
                "sport": info["sport"],
                "league": info["league"],
                "market_type": info["market_type"],
                "reason": f"ROI={info['roi']:.1%} WinRate={info['win_rate']:.1%}",
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
    """打印效率评分卡。"""
    if not output:
        print("📭 无数据")
        return

    print(f"\n{'='*60}")
    print(f"  市场效率分析 ({output['generated_at'][:10]})")
    print(f"  已结算: {output['total_settled']} 条, {output['categories']} 个类别")
    print(f"{'='*60}")

    details = output.get("details", {})
    if not details:
        print("📭 无满足最低样本量的类别")
        return

    # 按 ROI 排序
    sorted_items = sorted(details.items(), key=lambda x: -x[1]["roi"])

    print(f"\n  {'类别':<35} {'单数':<5} {'胜率':<8} {'ROI':<8} {'平均EV':<8} {'Brier':<8}")
    print(f"  {'-'*72}")
    for key, info in sorted_items:
        sport = info["sport"]
        league = info["league"][:12]
        mtype = info["market_type"]
        label = f"{sport}/{league}/{mtype}"[:34]
        wr = f"{info['win_rate']:.0%}"
        roi = f"{info['roi']:+.1%}"
        ev = f"{info['avg_ev']:+.2f}"
        br = f"{info['brier']:.3f}"
        print(f"  {label:<35} {info['n']:<5} {wr:<8} {roi:<8} {ev:<8} {br:<8}")

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
