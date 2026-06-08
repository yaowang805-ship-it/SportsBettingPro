#!/usr/bin/env python3
"""NFL 特征工程管线 — ELO + 滚动统计 + 天气 + 标签。

输出 data/processed/nfl_features.csv，用于 ensemble_trainer。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import DATA_DIR

NFL_HISTORY_CSV = DATA_DIR / "nfl_history.csv"
OUTPUT_CSV = ROOT / "data" / "processed" / "nfl_features.csv"
FEATURES_JSON = ROOT / "models" / "model_nfl_features.json"

# ELO 参数
ELO_K = 40
ELO_HOME_ADV = 55  # NFL 主场优势约 2.5 分 → ELO 分
# 盘口标签阈值（2020+ 数据中 spread_line 普遍存在）
MIN_SPREAD_YEAR = 2015


def _elo_expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def _update_elo(winner_elo: float, loser_elo: float, margin: float) -> tuple:
    """NFL ELO 更新，考虑分差加权（blowout 权重更高）。"""
    expected = _elo_expected(winner_elo, loser_elo)
    # margin_of_victory multiplier: log(margin + 1) * 2.2 / (elo_diff * 0.001 + 2.2)
    mov_mult = np.log(max(margin, 1) + 1.0) * 2.2 / (abs(winner_elo - loser_elo) * 0.001 + 2.2)
    k = ELO_K * mov_mult
    new_winner = winner_elo + k * (1.0 - expected)
    new_loser = loser_elo + k * (0.0 - (1.0 - expected))
    return new_winner, new_loser


def _slope(y) -> float:
    """线性回归斜率（最近 n 场的动量）。"""
    if len(y) < 2:
        return 0.0
    x = np.arange(len(y))
    return np.polyfit(x, y, 1)[0]


def _is_dome(roof: str) -> bool:
    if not roof or roof == 'nan':
        return np.nan
    return 1.0 if roof.strip().lower() in ('dome', 'closed') else 0.0


def build_nfl_features(
    input_csv: Path = None,
    output_csv: Path = None,
) -> pd.DataFrame:
    """构建 NFL 特征矩阵。

    Args:
        input_csv: NFL 历史 CSV 路径
        output_csv: 输出特征 CSV 路径

    Returns:
        特征 DataFrame
    """
    input_csv = input_csv or NFL_HISTORY_CSV
    output_csv = output_csv or OUTPUT_CSV

    if not input_csv.exists():
        logger.warning("NFL 历史数据不存在: %s", input_csv)
        return pd.DataFrame()

    df = pd.read_csv(input_csv)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)
    logger.info("  NFL 历史: %d 行（%s ~ %s）", len(df), df["date"].min(), df["date"].max())

    # ── ELO ──
    elo_ratings = {}
    home_elos, away_elos = [], []
    for _, row in df.iterrows():
        home, away = row["home"], row["away"]
        home_elo = elo_ratings.get(home, 1500.0) + ELO_HOME_ADV
        away_elo = elo_ratings.get(away, 1500.0)
        home_elos.append(home_elo)
        away_elos.append(away_elo)
        hs, aws = row["home_score"], row["away_score"]
        if pd.isna(hs) or pd.isna(aws):
            continue
        margin = abs(hs - aws)
        if hs > aws:
            new_home, new_away = _update_elo(home_elo, away_elo, margin)
            elo_ratings[home] = new_home
            elo_ratings[away] = new_away
        elif aws > hs:
            new_away, new_home = _update_elo(away_elo, home_elo, margin)
            elo_ratings[home] = new_home
            elo_ratings[away] = new_away
        # draw not possible in NFL

    df["home_elo"] = home_elos
    df["away_elo"] = away_elos
    df["elo_diff"] = df["home_elo"] - df["away_elo"]
    logger.info("  ELO 完成: %d 支球队", len(elo_ratings))

    # ── 球队日视图（展开每行变成 2 行，主客各一）──
    team_records = []
    for _, row in df.iterrows():
        for side, team in [("home", row["home"]), ("away", row["away"])]:
            pf = row["home_score"] if side == "home" else row["away_score"]
            pa = row["away_score"] if side == "home" else row["home_score"]
            win = 1.0 if (side == "home" and row["home_score"] > row["away_score"]) or \
                         (side == "away" and row["away_score"] > row["home_score"]) else 0.0
            team_records.append({
                "date": row["date"],
                "team": team,
                "pf": pf,
                "pa": pa,
                "win": win,
                "net": pf - pa,
                "side": side,
            })

    tr = pd.DataFrame(team_records)
    tr = tr.sort_values(["team", "date"]).reset_index(drop=True)

    # 滚动统计
    for window in [3, 5]:
        tr[f"pf_avg_{window}"] = tr.groupby("team")["pf"].transform(
            lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        tr[f"pa_avg_{window}"] = tr.groupby("team")["pa"].transform(
            lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        tr[f"net_rating_{window}"] = tr[f"pf_avg_{window}"] - tr[f"pa_avg_{window}"]

    tr["pf_ewm5"] = tr.groupby("team")["pf"].transform(
        lambda x: x.shift(1).ewm(span=5, min_periods=1).mean())
    tr["pa_ewm5"] = tr.groupby("team")["pa"].transform(
        lambda x: x.shift(1).ewm(span=5, min_periods=1).mean())

    tr["win_rate_5"] = tr.groupby("team")["win"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=1).mean())

    # 动量斜率（最近 5 场净胜分）
    tr["net_slope_5"] = tr.groupby("team")["net"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=2).apply(_slope, raw=False))

    # ── 主场统计 ──
    home_stats = tr[tr["side"] == "home"].rename(columns={
        "team": "home",
        "pf_avg_3": "home_pf_avg_3", "pf_avg_5": "home_pf_avg_5",
        "pa_avg_3": "home_pa_avg_3", "pa_avg_5": "home_pa_avg_5",
        "net_rating_3": "home_net_rating_3", "net_rating_5": "home_net_rating_5",
        "pf_ewm5": "home_pf_ewm5", "pa_ewm5": "home_pa_ewm5",
        "win_rate_5": "home_win_rate_5",
        "net_slope_5": "home_net_slope_5",
    }).drop(columns=["pf", "pa", "win", "net", "side"])

    # ── 客场统计 ──
    away_stats = tr[tr["side"] == "away"].rename(columns={
        "team": "away",
        "pf_avg_3": "away_pf_avg_3", "pf_avg_5": "away_pf_avg_5",
        "pa_avg_3": "away_pa_avg_3", "pa_avg_5": "away_pa_avg_5",
        "net_rating_3": "away_net_rating_3", "net_rating_5": "away_net_rating_5",
        "pf_ewm5": "away_pf_ewm5", "pa_ewm5": "away_pa_ewm5",
        "win_rate_5": "away_win_rate_5",
        "net_slope_5": "away_net_slope_5",
    }).drop(columns=["pf", "pa", "win", "net", "side"])

    # ── 合并 ──
    # 使用 merge_asof: 对每场比赛，取主队和客队在比赛前的最新统计
    df = df.sort_values("date")
    home_stats = home_stats.sort_values("date")
    away_stats = away_stats.sort_values("date")

    df = pd.merge_asof(df, home_stats, on="date", by="home", direction="backward")
    df = pd.merge_asof(df, away_stats, on="date", by="away", direction="backward")

    # ── 天气特征 ──
    df["is_dome"] = df["roof"].apply(_is_dome)
    df["temp"] = pd.to_numeric(df["temp"], errors="coerce")
    df["wind"] = pd.to_numeric(df["wind"], errors="coerce")
    # 室外比赛填补天气
    outdoor = df["is_dome"] == 0
    df.loc[outdoor & df["temp"].isna(), "temp"] = df.loc[outdoor, "temp"].median()
    df.loc[outdoor & df["wind"].isna(), "wind"] = df.loc[outdoor, "wind"].median()
    # 室内比赛天气设为 0（无影响）
    df.loc[df["is_dome"] == 1, "temp"] = 20.0
    df.loc[df["is_dome"] == 1, "wind"] = 0.0

    # ── 休息天数 ──
    df["rest_diff"] = df["home_rest"] - df["away_rest"]

    # ── 交叉特征 ──
    df["off_vs_def"] = df["home_pf_avg_5"] - df["away_pa_avg_5"]
    df["elo_net"] = df["home_elo"] - df["away_elo"]

    # ── 标签 ──
    has_spread = df["spread_line"].notna()
    has_total = df["total_line"].notna()

    df["spread_result"] = np.where(
        has_spread,
        ((df["home_score"] + df["spread_line"]) > df["away_score"]).astype(int),
        np.nan,
    )
    df["total_result"] = np.where(
        has_total,
        (df["home_score"] + df["away_score"] > df["total_line"]).astype(int),
        np.nan,
    )

    # win 标签（主胜=1）
    df["win"] = (df["home_score"] > df["away_score"]).astype(float)

    # ── 选择特征列 ──
    base_cols = ["date", "home", "away", "win", "spread_result", "total_result",
                 "home_score", "away_score", "season", "week"]
    feat_cols = [
        "home_elo", "away_elo", "elo_diff",
        "home_pf_avg_3", "home_pf_avg_5", "home_pa_avg_3", "home_pa_avg_5",
        "home_net_rating_3", "home_net_rating_5",
        "home_pf_ewm5", "home_pa_ewm5",
        "home_win_rate_5", "home_net_slope_5",
        "away_pf_avg_3", "away_pf_avg_5", "away_pa_avg_3", "away_pa_avg_5",
        "away_net_rating_3", "away_net_rating_5",
        "away_pf_ewm5", "away_pa_ewm5",
        "away_win_rate_5", "away_net_slope_5",
        "off_vs_def", "rest_diff", "is_dome", "temp", "wind",
    ]
    all_cols = base_cols + feat_cols
    # 只保留实际存在的列
    all_cols = [c for c in all_cols if c in df.columns]
    result = df[all_cols].copy()

    # ── 清理 NaN ──
    before = len(result)
    result = result.dropna(subset=["win"])
    after = len(result)
    if after < before:
        logger.info("  删除 %d 行无比分记录", before - after)

    # 填充特征 NaN
    float_cols = result.select_dtypes(include=[np.float64, np.float32]).columns
    result[float_cols] = result[float_cols].fillna(0.0)

    result = result.sort_values("date").reset_index(drop=True)
    result.to_csv(output_csv, index=False)
    logger.info("✅ NFL 特征: %d 行, %d 列 → %s", len(result), len(result.columns), output_csv.name)

    # 保存特征列列表
    import json
    feat_list = [c for c in result.columns if c not in ("date", "home", "away", "home_score", "away_score")]
    FEATURES_JSON.parent.mkdir(parents=True, exist_ok=True)
    FEATURES_JSON.write_text(json.dumps(feat_list, indent=2))
    logger.info("  特征列已保存至 %s (%d 个)", FEATURES_JSON.name, len(feat_list))

    return result


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    build_nfl_features()
