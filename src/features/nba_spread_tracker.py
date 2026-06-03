"""NBA Spread/Total 数据追踪器 — 从 Odds API 数据反推盘口结果。

将 basketball_historical_odds.csv（含 spreads/totals 盘口线）与
basketball_history.csv（含实际比分）匹配，计算 spread_result 和 total_result。
"""
import json
from pathlib import Path

import pandas as pd

from config.logging_config import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
ODDS_CSV = ROOT / "data" / "storage" / "basketball_historical_odds.csv"
SCORES_CSV = ROOT / "data" / "storage" / "basketball_history.csv"
OUTPUT_CSV = ROOT / "data" / "storage" / "nba_spread_results.csv"


def _normalize_team(name: str) -> str:
    return name.strip().lower()


def build_spread_results(odds_csv: str = None, scores_csv: str = None,
                         output_csv: str = None) -> pd.DataFrame:
    """从历史赔率与比分数据构建 spread/total 结果集。

    Returns:
        包含 home, away, date, home_score, away_score,
        spread_point, total_point, spread_result, total_result 的 DataFrame
    """
    odds_path = Path(odds_csv or ODDS_CSV)
    scores_path = Path(scores_csv or SCORES_CSV)

    if not odds_path.exists():
        logger.warning("未找到赔率数据: %s", odds_path)
        return pd.DataFrame()
    if not scores_path.exists():
        logger.warning("未找到比分数据: %s", scores_path)
        return pd.DataFrame()

    odds = pd.read_csv(odds_path)
    scores = pd.read_csv(scores_path)

    # 标准化球队名称
    odds["home_n"] = odds["home_team"].astype(str).apply(_normalize_team)
    odds["away_n"] = odds["away_team"].astype(str).apply(_normalize_team)
    odds["matchup"] = odds["home_n"] + "|" + odds["away_n"]
    odds["snap_date"] = pd.to_datetime(odds["date"])

    # 每场比赛取最新快照（即比赛日当天的盘口）
    game_date = odds.groupby("matchup")["snap_date"].max().reset_index()
    game_date.columns = ["matchup", "game_date"]
    odds2 = odds.merge(game_date, on="matchup")
    last_snap = (
        odds2[odds2["snap_date"] == odds2["game_date"]]
        .drop_duplicates(subset="matchup")
        .copy()
    )

    # 解析 spreads / totals
    def _extract(row):
        try:
            j = json.loads(row["odds_json"]) if isinstance(row["odds_json"], str) else row["odds_json"]
            spreads = next((m for m in j if m["key"] == "spreads"), None)
            totals = next((m for m in j if m["key"] == "totals"), None)
            sp = next(
                (o["point"] for o in (spreads or {}).get("outcomes", [])
                 if o["name"].strip().lower() == row["home_n"]),
                None
            )
            if sp is None and spreads:
                sp = spreads["outcomes"][0].get("point")
            tp = totals["outcomes"][0].get("point") if totals and totals.get("outcomes") else None
            return sp, tp
        except Exception:
            return None, None

    last_snap[["spread_point", "total_point"]] = last_snap.apply(
        lambda r: pd.Series(_extract(r)), axis=1
    )

    # 合并比分
    scores["home_n"] = scores["home"].astype(str).apply(_normalize_team)
    scores["away_n"] = scores["away"].astype(str).apply(_normalize_team)
    scores["matchup"] = scores["home_n"] + "|" + scores["away_n"]
    scores["game_date"] = pd.to_datetime(scores["date"])
    scores["game_only_date"] = scores["game_date"].dt.date

    last_snap["game_only_date"] = last_snap["game_date"].dt.date

    merged = last_snap.merge(
        scores[["matchup", "game_only_date", "home_score", "away_score"]],
        on=["matchup", "game_only_date"],
        how="inner",
    )

    if len(merged) == 0:
        logger.warning("赔率与比分无匹配数据")
        return pd.DataFrame()

    # 计算盘口结果
    has_s = merged["spread_point"].notna()
    has_t = merged["total_point"].notna()

    merged["spread_result"] = 0
    merged.loc[has_s, "spread_result"] = (
            (merged.loc[has_s, "home_score"] + merged.loc[has_s, "spread_point"])
            > merged.loc[has_s, "away_score"]
    ).astype(int)

    merged["total_result"] = 0
    merged.loc[has_t, "total_result"] = (
            (merged.loc[has_t, "home_score"] + merged.loc[has_t, "away_score"])
            > merged.loc[has_t, "total_point"]
    ).astype(int)

    # 只保留需要列
    result = merged[["game_only_date", "home_n", "away_n",
                     "home_score", "away_score",
                     "spread_point", "total_point",
                     "spread_result", "total_result"]].copy()
    result.columns = ["date", "home", "away", "home_score", "away_score",
                      "spread_point", "total_point",
                      "spread_result", "total_result"]
    result["date"] = pd.to_datetime(result["date"])
    result = result.sort_values("date").reset_index(drop=True)

    out_path = Path(output_csv or OUTPUT_CSV)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out_path, index=False)
    logger.info("NBA 盘口结果已保存: %d 场 → %s", len(result), out_path)

    has_spread = result["spread_point"].notna().sum()
    has_total = result["total_point"].notna().sum()
    logger.info("  spread_result: %d 场, total_result: %d 场", has_spread, has_total)

    return result


if __name__ == "__main__":
    build_spread_results()
