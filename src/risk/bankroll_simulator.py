#!/usr/bin/env python3
"""Bankroll Monte Carlo 模拟 — 破产概率 + 最优凯利校准。

对标职业系统：不只看 ROI，要回答
  "按当前模型表现，这个凯利分数有多大概率破产？"
  "什么凯利分数能最大化长期资金增长？"

用法:
    python src/risk/bankroll_simulator.py                          # 完整模拟
    python src/risk/bankroll_simulator.py --kelly 0.25 --sims 50000  # 自定义
"""
import sys, json, random
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from config.logging_config import get_logger
logger = get_logger(__name__)

LOG_PATH = ROOT / "data" / "storage" / "prediction_log.csv"
OUTPUT_PATH = ROOT / "data" / "storage" / "bankroll_simulation.json"


def load_settled_bets() -> pd.DataFrame:
    """从 prediction_log 加载已结算注单。"""
    if not LOG_PATH.exists():
        return pd.DataFrame()
    df = pd.read_csv(LOG_PATH)
    df = df[df["status"].isin(["won", "lost"])].copy()
    if df.empty:
        return df
    df["win"] = (df["status"] == "won").astype(int)
    df["odds"] = pd.to_numeric(df["odds"], errors="coerce")
    df["stake"] = pd.to_numeric(df["stake"], errors="coerce")
    df["model_prob"] = pd.to_numeric(df["model_prob"], errors="coerce")
    return df.dropna(subset=["odds", "stake", "model_prob", "win"])


def simulate_one_path(
    bets: pd.DataFrame,
    initial_bankroll: float = 10000,
    kelly_fraction: float = 0.25,
    n_bets: int = None,
    use_model_prob: bool = False,
) -> np.ndarray:
    """模拟一条资金曲线。

    Args:
        bets: DataFrame with columns [odds, model_prob, win]
        initial_bankroll: 初始资金
        kelly_fraction: 凯利分数 (full Kelly = 1.0, 1/4 Kelly = 0.25)
        n_bets: 模拟注单数 (None = 使用全部 bets 数量)
        use_model_prob: True=用模型概率模拟胜负（非有放回采样历史结果）

    Returns:
        资金曲线数组 (长度 n_bets+1, 首元素为 initial_bankroll)
    """
    if n_bets is None:
        n_bets = len(bets)
    if n_bets == 0:
        return np.array([initial_bankroll])

    if use_model_prob:
        # 用模型概率模拟：每条注单独立按模型概率判定胜负
        sampled = bets.sample(n=n_bets, replace=True).reset_index(drop=True)
        bankroll = initial_bankroll
        curve = [bankroll]
        for _, b in sampled.iterrows():
            odds = b["odds"]
            prob = b["model_prob"]
            if odds <= 1.0 or prob <= 0:
                curve.append(bankroll)
                continue
            won = random.random() < prob
            b_val = odds - 1.0
            full_kelly = (prob * b_val - (1.0 - prob)) / b_val
            if full_kelly <= 0:
                curve.append(bankroll)
                continue
            stake_pct = full_kelly * kelly_fraction
            stake = bankroll * stake_pct
            if won:
                bankroll += stake * b_val
            else:
                bankroll -= stake
            if bankroll <= 0:
                bankroll = 0
                curve.extend([0] * (n_bets - len(curve) + 1))
                break
            curve.append(bankroll)
        return np.array(curve)

    # 有放回采样历史结果（默认）
    sampled = bets.sample(n=n_bets, replace=True).reset_index(drop=True)

    bankroll = initial_bankroll
    curve = [bankroll]
    for _, b in sampled.iterrows():
        odds = b["odds"]
        prob = b["model_prob"]
        won = b["win"]

        if odds <= 1.0 or prob <= 0:
            curve.append(bankroll)
            continue

        # 全凯利
        b_val = odds - 1.0
        full_kelly = (prob * b_val - (1.0 - prob)) / b_val
        if full_kelly <= 0:
            curve.append(bankroll)
            continue

        stake_pct = full_kelly * kelly_fraction
        stake = bankroll * stake_pct

        if won:
            bankroll += stake * b_val
        else:
            bankroll -= stake

        # 破产保护：资金 ≤ 0 则停止
        if bankroll <= 0:
            bankroll = 0
            curve.extend([0] * (n_bets - len(curve) + 1))
            break
        curve.append(bankroll)

    return np.array(curve)


def simulate(
    bets: pd.DataFrame,
    initial_bankroll: float = 10000,
    n_simulations: int = 10000,
    kelly_fractions: list = None,
    ruin_threshold: float = 0.5,
) -> dict:
    """蒙特卡洛模拟 — 多凯利分数对比。

    Args:
        bets: 已结算注单
        initial_bankroll: 初始资金
        n_simulations: 模拟次数
        kelly_fractions: 待测试的凯利分数列表
        ruin_threshold: 破产阈值 (初始资金的百分比)

    Returns:
        {
            "n_bets": N,
            "initial_bankroll": float,
            "results": {kelly_frac: {key: val}},
            "optimal_kelly": float,
        }
    """
    if bets.empty:
        return {"error": "无已结算记录", "results": {}}

    n_bets = len(bets)
    if kelly_fractions is None:
        kelly_fractions = [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5]

    results = {}
    best_kelly = kelly_fractions[0]
    best_median_final = -np.inf

    use_model_prob = len(bets) < 20  # 样本太少时用模型概率模拟

    for kf in kelly_fractions:
        final_bankrolls = []
        ruin_count = 0
        all_curves = []

        for _ in range(n_simulations):
            curve = simulate_one_path(bets, initial_bankroll, kf, n_bets, use_model_prob)
            final = curve[-1]
            final_bankrolls.append(final)
            if final <= initial_bankroll * ruin_threshold:
                ruin_count += 1
            all_curves.append(curve)

        final_arr = np.array(final_bankrolls)
        curves_arr = np.array(all_curves)

        # 百分位曲线（用于可视化）
        steps = curves_arr.shape[1]
        percentiles = {}
        for p in [10, 25, 50, 75, 90]:
            percentiles[str(p)] = np.percentile(curves_arr, p, axis=0).tolist()

        median_final = float(np.median(final_arr))
        mean_final = float(np.mean(final_arr))

        results[str(kf)] = {
            "kelly_fraction": kf,
            "median_final": round(median_final, 2),
            "mean_final": round(mean_final, 2),
            "std_final": round(float(np.std(final_arr)), 2),
            "min_final": round(float(np.min(final_arr)), 2),
            "max_final": round(float(np.max(final_arr)), 2),
            "ruin_prob": round(ruin_count / n_simulations, 4),
            "growth_rate": round(median_final / initial_bankroll - 1, 4),
            "percentiles": percentiles,
        }

        if median_final > best_median_final:
            best_median_final = median_final
            best_kelly = kf

    return {
        "n_bets": n_bets,
        "initial_bankroll": initial_bankroll,
        "n_simulations": n_simulations,
        "ruin_threshold_pct": ruin_threshold,
        "use_model_prob": n_bets < 20,
        "results": results,
        "optimal_kelly": best_kelly,
        "generated_at": datetime.now().isoformat(),
    }


def print_report(output: dict):
    """打印模拟报告。"""
    if "error" in output:
        print(f"⚠️ {output['error']}")
        return

    results = output.get("results", {})
    if not results:
        print("📭 无模拟结果")
        return

    print(f"\n{'='*70}")
    print(f"  Bankroll Monte Carlo 模拟")
    print(f"  初始资金: ¥{output['initial_bankroll']:,.0f}")
    print(f"  历史注单: {output['n_bets']} 条")
    print(f"  模拟方式: {'模型概率' if output['n_bets'] < 20 else '历史重采样'}")
    print(f"  模拟次数: {output['n_simulations']:,} 次")
    print(f"{'='*70}")

    print(f"\n  {'凯利分数':<10} {'期末中位':<14} {'破产概率':<12} {'增长率':<10} {'标准差':<10}")
    print(f"  {'-'*56}")
    for kf_str in sorted(results.keys(), key=float):
        r = results[kf_str]
        print(f"  {float(kf_str):<10.2f} ¥{r['median_final']:<10,.0f} "
              f"{r['ruin_prob']:<11.1%} {r['growth_rate']:<+9.1%} {r['std_final']:<10,.0f}")

    best = output.get("optimal_kelly", 0)
    best_r = results.get(str(best), {})
    best_ruin = best_r.get("ruin_prob", 0)
    best_median = best_r.get("median_final", 0)

    print(f"\n  🏆 最优凯利分数: {best:.2f}")
    print(f"     期末中位资金: ¥{best_median:,.0f}")
    print(f"     破产概率: {best_ruin:.1%}")

    # 当前配置评估
    default_kf = 0.25
    if str(default_kf) in results:
        cur = results[str(default_kf)]
        print(f"\n  📊 当前配置 (Kelly={default_kf:.2f}):")
        print(f"     期末中位: ¥{cur['median_final']:,.0f}")
        print(f"     破产概率: {cur['ruin_prob']:.1%}")
        print(f"     P10~P90: ¥{cur['percentiles']['10'][-1]:,.0f} ~ ¥{cur['percentiles']['90'][-1]:,.0f}")
        if default_kf != best:
            print(f"     建议: 调整至 Kelly={best:.2f} 可提升中位资金 ¥{best_median - cur['median_final']:,.0f}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--kelly", type=float, default=None, help="只模拟此凯利分数")
    parser.add_argument("--sims", type=int, default=10000)
    parser.add_argument("--bankroll", type=float, default=10000)
    args = parser.parse_args()

    bets = load_settled_bets()
    if bets.empty:
        logger.warning("❌ 无已结算记录")
        return

    logger.info("📊 Bankroll MC 模拟 (%s 条注单, %s 次)...", len(bets), args.sims)

    kelly_list = [args.kelly] if args.kelly else None
    output = simulate(bets, initial_bankroll=args.bankroll,
                      n_simulations=args.sims, kelly_fractions=kelly_list)

    # 保存
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print_report(output)
    logger.info("✅ 模拟结果已保存至 %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
