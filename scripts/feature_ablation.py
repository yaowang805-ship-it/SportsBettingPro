#!/usr/bin/env python3
"""特征 Ablation Study — 逐组验证预测能力

对每个运动项目：
  1. 时间序列分割（按日期排序，前80%训练，后20%测试）
  2. Baseline: 用全部特征训练 XGBoost
  3. 逐组移除特征，观察 Brier score 变化
  4. 输出谁有用、谁是噪音

用法:
    python3 scripts/feature_ablation.py          # 跑全部
    python3 scripts/feature_ablation.py --sport bb   # 只跑篮球
    python3 scripts/feature_ablation.py --sport wc   # 只跑足球
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss
from sklearn.model_selection import train_test_split
import xgboost as xgb


# ── BB 特征分组 ──
BB_GROUPS = {
    "ELO": [
        "home_elo", "away_elo", "elo_diff",
        "home_elo_residual_5", "away_elo_residual_5", "elo_residual_diff",
    ],
    "Home_Rolling": [
        "home_gf_avg_3", "home_gf_avg_10", "home_ga_avg_3", "home_ga_avg_10",
        "home_net_rating_3", "home_net_rating_10", "home_gf_ewm5", "home_ga_ewm5",
        "home_win_rate_3", "home_win_rate_10", "home_form_regression",
        "home_net_rating_slope_5", "home_net_rating_slope_10",
        "home_total_pts_avg_5", "home_total_pts_ewm5",
        "home_pts_scored_avg_5", "home_pts_allowed_avg_5",
        "home_total_pts_volatility_5", "home_high_score_rate_10",
        "home_avg_margin_5", "home_avg_margin_10", "home_margin_volatility_5",
        "home_home_win_rate_5", "home_away_win_rate_5",
        "home_last_game_won", "home_streak",
        "home_momentum_quality",
    ],
    "Away_Rolling": [
        "away_gf_avg_3", "away_gf_avg_10", "away_ga_avg_3", "away_ga_avg_10",
        "away_net_rating_3", "away_net_rating_10", "away_gf_ewm5", "away_ga_ewm5",
        "away_win_rate_3", "away_win_rate_10", "away_form_regression",
        "away_net_rating_slope_5", "away_net_rating_slope_10",
        "away_total_pts_avg_5", "away_total_pts_ewm5",
        "away_pts_scored_avg_5", "away_pts_allowed_avg_5",
        "away_total_pts_volatility_5", "away_high_score_rate_10",
        "away_avg_margin_5", "away_avg_margin_10", "away_margin_volatility_5",
        "away_home_win_rate_5", "away_away_win_rate_5",
        "away_last_game_won", "away_streak",
        "away_momentum_quality",
    ],
    "Rest_B2B_Travel": [
        "home_rest_days", "home_b2b", "away_rest_days", "away_b2b",
        "home_travel_distance", "home_travel_distance_3", "home_travel_distance_5",
        "away_travel_distance", "away_travel_distance_3", "away_travel_distance_5",
        "b2b_diff", "rest_diff", "travel_diff", "travel_3_diff",
        "total_rest_sum",
        "home_b2b_density", "away_b2b_density", "b2b_density_diff",
        "rest_advantage",
    ],
    "H2H": [
        "h2h_home_wins", "h2h_away_wins", "h2h_avg_total_pts",
        "h2h_net_wins", "h2h_total", "h2h_dominance", "h2h_form_x",
    ],
    "OppStrength_League": [
        "home_sos_5", "home_sos_10", "away_sos_5", "away_sos_10",
        "sos_diff",
        "league_home_win_rate", "league_avg_total_pts", "league_home_adv",
        "season_stage",
    ],
    "Interaction": [
        "off_vs_def", "injured_diff", "home_injured", "away_injured",
        "combined_total_avg_5", "combined_pts_scored_avg_5", "combined_pts_allowed_avg_5",
        "total_volatility_interaction", "high_score_rate_sum",
        "pace_proxy", "margin_diff",
        "home_away_win_diff", "margin_volatility_interaction", "streak_diff",
        "form_regression_diff",
        "home_odds", "away_odds",
    ],
}

# ── WC 特征分组 ──
WC_GROUPS = {
    "ELO": [
        "home_elo", "away_elo", "elo_diff",
        "home_opp_elo", "away_opp_elo", "opp_elo_diff",
    ],
    "Home_Rolling": [
        "home_gf_avg_3", "home_gf_avg_5", "home_gf_avg_10",
        "home_ga_avg_3", "home_ga_avg_5", "home_ga_avg_10",
        "home_win_rate_5", "home_win_rate_10",
        "home_draw_rate_5", "home_draw_rate_10",
        "home_net_5", "home_margin_5", "home_margin_10",
        "home_margin_volatility_5", "home_gf_volatility_5",
        "home_scoring_streak", "home_last_game_won",
        "home_form_trend_3_10",
        "home_momentum_quality",
    ],
    "Away_Rolling": [
        "away_gf_avg_3", "away_gf_avg_5", "away_gf_avg_10",
        "away_ga_avg_3", "away_ga_avg_5", "away_ga_avg_10",
        "away_win_rate_5", "away_win_rate_10",
        "away_draw_rate_5", "away_draw_rate_10",
        "away_net_5", "away_margin_5", "away_margin_10",
        "away_margin_volatility_5", "away_gf_volatility_5",
        "away_scoring_streak", "away_last_game_won",
        "away_form_trend_3_10",
        "away_momentum_quality",
    ],
    "Rest_Context": [
        "home_rest_days", "away_rest_days", "rest_diff",
        "is_neutral",
    ],
    "Matchup": [
        "form_diff_5", "net_5_diff", "gf_avg_5_diff", "ga_avg_5_diff",
        "total_avg_5", "total_avg_10",
        "margin_diff_5", "sos_elo_diff", "gf_vol_diff",
        "attack_vs_defence", "defence_vs_attack",
    ],
    "SOS_Strength": [
        "home_sos_elo_5", "away_sos_elo_5", "sos_elo_diff",
    ],
}


def _exclude_cols(sport: str) -> list:
    """标签 / ID / 字符串列，不应该作为特征。"""
    base = ["date", "season", "game_id"]
    if sport == "bb":
        return base + [
            "home", "away", "win",
            "home_goals", "away_goals",
            "teamsprd", "ovrundr",
            "home_score", "away_score",
            "spread_result", "total_result",
        ]
    elif sport == "wc":
        return base + [
            "home_team", "away_team", "tournament",
            "home_score", "away_score",
            "home_win", "draw", "total_goals", "over_2.5",
        ]
    return base


def _get_target(sport: str) -> str:
    return "win" if sport == "bb" else "home_win"


def _get_groups(sport: str) -> dict:
    return BB_GROUPS if sport == "bb" else WC_GROUPS


def _collect_features(df: pd.DataFrame, sport: str) -> list:
    """返回数值型且不在排除列表中的特征列。"""
    excl = set(_exclude_cols(sport))
    feats = []
    for c in df.columns:
        if c in excl:
            continue
        if df[c].dtype in ("object", "string", "category"):
            continue
        feats.append(c)
    return feats


def run_ablation(sport: str, data_path: Path):
    print(f"\n{'='*60}")
    print(f"  📊 特征 Ablation Study: {sport.upper()}")
    print(f"  数据: {data_path}")
    print(f"{'='*60}")

    df = pd.read_csv(data_path)
    print(f"  行数: {len(df)}")
    print(f"  原始列数: {len(df.columns)}")

    target = _get_target(sport)
    groups = _get_groups(sport)

    # 收集全部候选特征
    all_feats = _collect_features(df, sport)
    print(f"  有效特征数: {len(all_feats)}")

    # 检查每组中哪些特征实际存在
    available_groups = {}
    for gname, cols in groups.items():
        avail = [c for c in cols if c in all_feats]
        if avail:
            available_groups[gname] = avail
        missing = [c for c in cols if c not in all_feats]
        if missing:
            print(f"  ⚠️ 组 {gname} 缺少特征: {missing}")

    # 清理缺失值 — 先按特征子集处理
    df = df.dropna(subset=[target] + all_feats, how="any").copy()
    y = df[target].values
    print(f"  清理后行数: {len(df)} (目标均值: {y.mean():.3f})")

    # 时间序列分割
    dates = pd.to_datetime(df["date"], errors="coerce")
    sorted_idx = np.argsort(dates.values)
    split = int(len(sorted_idx) * 0.8)
    train_idx = sorted_idx[:split]
    test_idx = sorted_idx[split:]

    X_all = df[all_feats].values.astype(np.float64)
    X_train, X_test = X_all[train_idx], X_all[test_idx]
    y_train, y_test = y[train_idx], y[test_idx]
    print(f"  训练集: {len(X_train)} | 测试集: {len(X_test)}")
    print(f"  测试集目标均值: {y_test.mean():.3f}")

    # ── 基线 ──
    print(f"\n  ▶ 训练 Baseline (全部 {len(all_feats)} 个特征)...")
    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.1,
        eval_metric="logloss", early_stopping_rounds=20,
        random_state=42, verbosity=0,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    y_prob = model.predict_proba(X_test)[:, 1]
    baseline_brier = brier_score_loss(y_test, y_prob)
    baseline_acc = (y_prob.round() == y_test).mean()
    print(f"  ✅ Baseline: 准确率 {baseline_acc:.4f} | Brier {baseline_brier:.4f}")

    # ── 逐组移除 ──
    print(f"\n  ▶ 逐组移除结果:")
    print(f"  {'组名':<22} {'Brier':>8} {'ΔBrier':>8} {'Δ%':>7} {'准确率':>8} {'决策':>6}")
    print(f"  {'-'*22} {'-'*8} {'-'*8} {'-'*7} {'-'*8} {'-'*6}")

    results = {}
    best_delta = 0
    worst_delta = 0
    for gname in sorted(available_groups.keys()):
        cols = available_groups[gname]
        remaining = [c for c in all_feats if c not in cols]
        if not remaining:
            print(f"  ⚠️ 移除 {gname} 后无剩余特征，跳过")
            continue

        X_rem = df[remaining].values.astype(np.float64)
        Xr_train = X_rem[train_idx]
        Xr_test = X_rem[test_idx]

        m = xgb.XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.1,
            eval_metric="logloss", early_stopping_rounds=20,
            random_state=42, verbosity=0,
        )
        m.fit(Xr_train, y_train, eval_set=[(Xr_test, y_test)], verbose=False)
        prob = m.predict_proba(Xr_test)[:, 1]
        brier = brier_score_loss(y_test, prob)
        acc = (prob.round() == y_test).mean()
        delta = brier - baseline_brier  # 正值 = 移除后更差 = 该组有用
        delta_pct = delta / baseline_brier * 100 if baseline_brier > 0 else 0

        if delta > best_delta:
            best_delta = delta
        if delta < worst_delta:
            worst_delta = delta

        if delta > 0.005:
            verdict = "✅有用"
        elif delta > 0.001:
            verdict = "轻微"
        elif delta > -0.001:
            verdict = "🟡中性"
        else:
            verdict = "❌噪音"

        results[gname] = {"brier": brier, "delta": delta, "delta_pct": delta_pct, "acc": acc, "verdict": verdict}
        print(f"  {gname:<22} {brier:>8.4f} {delta:>+8.4f} {delta_pct:>+6.1f}% {acc:>8.4f} {verdict:>6}")

    # ── 总结 ──
    print(f"\n  {'='*60}")
    print(f"  📋 Ablation 总结 — {sport.upper()}")
    print(f"  Baseline Brier: {baseline_brier:.4f}")
    print(f"  Baseline 准确率: {baseline_acc:.4f}")
    print(f"  {'='*60}")

    useful = [k for k, v in results.items() if v["delta"] > 0.005]
    neutral = [k for k, v in results.items() if -0.001 <= v["delta"] <= 0.005]
    noise = [k for k, v in results.items() if v["delta"] < -0.001]

    if useful:
        print(f"\n  ✅ 有用特征组 (移除后Brier↑>{0.005:.3f}):")
        for g in useful:
            print(f"     - {g} (ΔBrier={results[g]['delta']:+.4f})")
    if neutral:
        print(f"\n  🟡 中性特征组 (可保留可移除):")
        for g in neutral:
            print(f"     - {g} (ΔBrier={results[g]['delta']:+.4f})")
    if noise:
        print(f"\n  ❌ 噪音特征组 (移除后Brier反而↓):")
        for g in noise:
            print(f"     - {g} (ΔBrier={results[g]['delta']:+.4f})")

    return {"sport": sport, "baseline_brier": baseline_brier, "baseline_acc": baseline_acc, "results": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["bb", "wc", "all"], default="all")
    args = parser.parse_args()

    DATA_DIR = ROOT / "data" / "processed"
    configs = []
    if args.sport in ("bb", "all"):
        configs.append(("bb", DATA_DIR / "bb_features.csv"))
    if args.sport in ("wc", "all"):
        configs.append(("wc", DATA_DIR / "wc_features.csv"))

    all_results = {}
    for sport, path in configs:
        if not path.exists():
            print(f"❌ 数据文件不存在: {path}")
            continue
        try:
            all_results[sport] = run_ablation(sport, path)
        except Exception as e:
            print(f"❌ {sport} ablation 失败: {e}")
            import traceback
            traceback.print_exc()

    # 跨运动对比
    if len(all_results) > 1:
        print(f"\n\n{'='*60}")
        print(f"  📊 跨运动对比")
        print(f"{'='*60}")
        print(f"  {'运动':<8} {'Baseline Brier':<16} {'有用组':<20} {'噪音组':<20}")
        print(f"  {'-'*8} {'-'*16} {'-'*20} {'-'*20}")
        for sport, r in all_results.items():
            useful = [k for k, v in r["results"].items() if v["delta"] > 0.005]
            noise = [k for k, v in r["results"].items() if v["delta"] < -0.001]
            print(f"  {sport:<8} {r['baseline_brier']:<16.4f} {str(useful):<20} {str(noise):<20}")


if __name__ == "__main__":
    main()
