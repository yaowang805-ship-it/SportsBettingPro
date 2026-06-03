"""球队 Power Rating（战力评级）系统。

职业 bettor 的核心工具：
  1. 给每支球队一个数字评分（代表净胜分能力）
  2. 主场优势加成（通常 NBA 3 分，足球 0.5 球）
  3. 预期盘口 = 主队评分 - 客队评分 + 主场优势
  4. 与市场盘口对比 → 发现价值

使用时间加权移动平均和对手强度调整。
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

RATING_DIR = ROOT / "data" / "storage"

# NBA 队名归一化：短名/别名 → 标准全名（基于 team_names.NBA_CN 的 key）
_NBA_CANONICAL = {
    "Atlanta": "Atlanta Hawks",
    "Boston": "Boston Celtics",
    "Brooklyn": "Brooklyn Nets",
    "Charlotte": "Charlotte Hornets",
    "Chicago": "Chicago Bulls",
    "Cleveland": "Cleveland Cavaliers",
    "Dallas": "Dallas Mavericks",
    "Denver": "Denver Nuggets",
    "Detroit": "Detroit Pistons",
    "Golden State": "Golden State Warriors",
    "Houston": "Houston Rockets",
    "Indiana": "Indiana Pacers",
    "L.A. Clippers": "LA Clippers",
    "L.A. Lakers": "Los Angeles Lakers",
    "LA Clippers": "LA Clippers",
    "Memphis": "Memphis Grizzlies",
    "Miami": "Miami Heat",
    "Milwaukee": "Milwaukee Bucks",
    "Minnesota": "Minnesota Timberwolves",
    "New Orleans": "New Orleans Pelicans",
    "New York": "New York Knicks",
    "Oklahoma City": "Oklahoma City Thunder",
    "Orlando": "Orlando Magic",
    "Philadelphia": "Philadelphia 76ers",
    "Phoenix": "Phoenix Suns",
    "Portland": "Portland Trail Blazers",
    "Sacramento": "Sacramento Kings",
    "San Antonio": "San Antonio Spurs",
    "Toronto": "Toronto Raptors",
    "Utah": "Utah Jazz",
    "Washington": "Washington Wizards",
    # 已全名的保持不变
    "Los Angeles Lakers": "Los Angeles Lakers",
    "LA Clippers": "LA Clippers",
}


def _normalize_nba(name: str) -> str:
    """NBA 队名归一化：短名 → 标准全名。"""
    return _NBA_CANONICAL.get(name.strip(), name.strip())
RATING_DIR.mkdir(parents=True, exist_ok=True)
NBA_RATING_FILE = RATING_DIR / "nba_power_ratings.json"
FB_RATING_FILE = RATING_DIR / "fb_power_ratings.json"

# 时间衰减参数
# 越近的比赛权重越高
HALF_LIFE_GAMES = 10  # 10 场比赛后半衰期


def _time_weight(game_idx: int, total_games: int, half_life: float = HALF_LIFE_GAMES) -> float:
    """计算比赛的时间权重：越近权重越高。"""
    distance = total_games - 1 - game_idx
    return 2 ** (-distance / half_life)


def _compute_power_ratings(
    df: pd.DataFrame,
    score_col: str = "score",
    opponent_col: str = "opponent",
    is_home_col: str = "is_home",
    date_col: str = "date",
    min_games: int = 5,
) -> Dict[str, float]:
    """计算球队 Power Rating。

    使用对抗强度调整的净胜分：
      rating_i = avg(净胜分_i + 对手_rating_j)
    迭代计算直到收敛（类似于 PageRank / Elo 的思路）。

    Args:
        df: 包含每场比赛每支球队视角的 DataFrame
        score_col: 己方得分列名
        opponent_col: 对手列名
        is_home_col: 是否主场列名
        date_col: 比赛日期列名

    Returns:
        {球队名: 评分}
    """
    teams = set(df["team"].unique())
    # 初始化：每支球队 0 分
    ratings = {t: 0.0 for t in teams}
    games_per_team = df.groupby("team").size().to_dict()

    # 过滤比赛数太少的球队
    active_teams = {t for t in teams if games_per_team.get(t, 0) >= min_games}

    if not active_teams:
        return {}

    # 迭代计算（最多 20 轮）
    for iteration in range(20):
        new_ratings = {}
        max_change = 0.0

        for team in active_teams:
            team_games = df[df["team"] == team].sort_values(date_col)
            total_weight = 0.0
            weighted_sum = 0.0
            n_games = len(team_games)

            for idx, (_, game) in enumerate(team_games.iterrows()):
                w = _time_weight(idx, n_games)
                margin = game[score_col]
                opp = game[opponent_col]
                opp_rating = ratings.get(opp, 0.0)
                # 对抗强度调整：净胜分 + 对手评分
                adj_margin = margin + opp_rating * 0.5
                weighted_sum += w * adj_margin
                total_weight += w

            new_ratings[team] = weighted_sum / total_weight if total_weight > 0 else 0.0
            change = abs(new_ratings[team] - ratings.get(team, 0.0))
            max_change = max(max_change, change)

        ratings = new_ratings
        if max_change < 0.01:
            break

    # 归一化到平均值 = 0
    mean_rating = np.mean(list(ratings.values()))
    ratings = {t: round(r - mean_rating, 4) for t, r in ratings.items()}

    return ratings


def build_nba_ratings() -> Dict[str, float]:
    """构建 NBA 球队 Power Rating。"""
    csv_path = ROOT / "data" / "processed" / "bb_features.csv"
    if not csv_path.exists():
        logger.warning("⚠️ bb_features.csv 不存在")
        return {}

    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"], format="mixed", utc=True)
    # bb_features.csv 用 home_goals/away_goals 表示得分
    score_col = "home_goals" if "home_goals" in df.columns else "home_score"
    opp_score_col = "away_goals" if "away_goals" in df.columns else "away_score"

    # 队名归一化（统一短名/全名）
    df["home"] = df["home"].apply(_normalize_nba)
    df["away"] = df["away"].apply(_normalize_nba)

    # 转换为每队视角
    home = df[["date", "home", "away", score_col, opp_score_col]].copy()
    home.columns = ["date", "team", "opponent", "score", "opp_score"]
    home["margin"] = home["score"] - home["opp_score"]
    home["is_home"] = 1

    away = df[["date", "away", "home", opp_score_col, score_col]].copy()
    away.columns = ["date", "team", "opponent", "score", "opp_score"]
    away["margin"] = away["score"] - away["opp_score"]
    away["is_home"] = 0

    all_games = pd.concat([home, away], ignore_index=True)
    ratings = _compute_power_ratings(all_games, score_col="margin", min_games=3)

    # 保存
    with open(NBA_RATING_FILE, "w", encoding="utf-8") as f:
        json.dump(ratings, f, ensure_ascii=False, indent=2)
    logger.info("🏀 NBA Power Ratings: %s 支球队", len(ratings))
    for team, rating in sorted(ratings.items(), key=lambda x: -x[1])[:10]:
        logger.info("  %s: %+.2f", team, rating)
    return ratings


def build_football_ratings() -> Dict[str, float]:
    """构建足球球队 Power Rating（用净胜球）。"""
    csv_path = ROOT / "data" / "processed" / "fb_features.csv"
    if not csv_path.exists():
        logger.warning("⚠️ fb_features.csv 不存在")
        return {}

    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"], format="mixed", utc=True)
    score_col = [c for c in df.columns if "home_goal" in c or "home_score" in c]
    opp_score_col = [c for c in df.columns if "away_goal" in c or "away_score" in c]
    fb_score = score_col[0] if score_col else "home_goals"
    fb_opp = opp_score_col[0] if opp_score_col else "away_goals"
    df = df.dropna(subset=[fb_score, fb_opp])

    # 转换为每队视角
    home = df[["date", "home", "away", fb_score, fb_opp]].copy()
    home.columns = ["date", "team", "opponent", "score", "opp_score"]
    home["margin"] = home["score"] - home["opp_score"]
    home["is_home"] = 1

    away = df[["date", "away", "home", "away_goals", "home_goals"]].copy()
    away.columns = ["date", "team", "opponent", "score", "opp_score"]
    away["margin"] = away["score"] - away["opp_score"]
    away["is_home"] = 0

    all_games = pd.concat([home, away], ignore_index=True)
    ratings = _compute_power_ratings(all_games, score_col="margin", min_games=3)

    with open(FB_RATING_FILE, "w", encoding="utf-8") as f:
        json.dump(ratings, f, ensure_ascii=False, indent=2)
    logger.info("⚽ Football Power Ratings: %s 支球队", len(ratings))
    for team, rating in sorted(ratings.items(), key=lambda x: -x[1])[:10]:
        logger.info("  %s: %+.2f", team, rating)
    return ratings


def load_nba_ratings() -> Dict[str, float]:
    """加载缓存的 NBA Power Ratings。"""
    if NBA_RATING_FILE.exists():
        return json.loads(NBA_RATING_FILE.read_text())
    return build_nba_ratings()


def load_football_ratings() -> Dict[str, float]:
    """加载缓存的足球 Power Ratings。"""
    if FB_RATING_FILE.exists():
        return json.loads(FB_RATING_FILE.read_text())
    return build_football_ratings()


def predict_spread(
    home_team: str,
    away_team: str,
    ratings: Dict[str, float],
    home_advantage: float = 3.0,
) -> Tuple[float, float]:
    """用 Power Rating 预测让分盘。

    Args:
        home_team: 主队
        away_team: 客队
        ratings: Power Ratings dict
        home_advantage: 主场优势（NBA ~3 分，足球 ~0.5 球）

    Returns:
        (预期让分, 置信度)
        正数表示主队让分，负数表示主队受让
    """
    home_r = ratings.get(home_team, 0.0)
    away_r = ratings.get(away_team, 0.0)
    spread = home_r - away_r + home_advantage
    confidence = min(abs(home_r - away_r) / 5.0, 1.0)
    return round(spread, 1), round(confidence, 2)


def find_value_bets(
    market_spreads: List[Dict],
    ratings: Dict[str, float],
    home_advantage: float = 3.0,
    min_edge: float = 1.5,
) -> List[Dict]:
    """将 Power Rating 预期盘口与市场盘口对比，发现价值投注。

    Args:
        market_spreads: [{home_team, away_team, market_spread, market_odds}, ...]
        ratings: Power Ratings dict
        home_advantage: 主场优势
        min_edge: 最小偏差阈值（分）

    Returns:
        [{home_team, away_team, pred_spread, market_spread, diff, confidence}, ...]
    """
    value_bets = []
    for game in market_spreads:
        home = game["home_team"]
        away = game["away_team"]
        mkt_spread = game["market_spread"]

        pred_spread, confidence = predict_spread(home, away, ratings, home_advantage)
        diff = pred_spread - mkt_spread

        if abs(diff) >= min_edge and confidence > 0.3:
            value_bets.append({
                "home_team": home,
                "away_team": away,
                "pred_spread": pred_spread,
                "market_spread": mkt_spread,
                "diff": round(diff, 1),
                "confidence": confidence,
                "direction": "主队" if diff > 0 else "客队",
            })

    value_bets.sort(key=lambda x: -abs(x["diff"]))
    return value_bets


def print_ratings_report():
    """打印 Power Rating 报告。"""
    logger.info("\n%s", "=" * 60)
    logger.info("  📊 Power Rating 战力评级报告")
    logger.info("%s", "=" * 60)

    try:
        nba = load_nba_ratings()
        logger.info("\n🏀 NBA Top 10:")
        for i, (team, rating) in enumerate(sorted(nba.items(), key=lambda x: -x[1])[:10], 1):
            logger.info("  %2d. %-25s %+.2f", i, team, rating)
    except Exception as e:
        logger.warning("  ⚠️ NBA: %s", e)

    try:
        fb = load_football_ratings()
        logger.info("\n⚽ 足球 Top 10:")
        for i, (team, rating) in enumerate(sorted(fb.items(), key=lambda x: -x[1])[:10], 1):
            logger.info("  %2d. %-25s %+.2f", i, team, rating)
    except Exception as e:
        logger.warning("  ⚠️ 足球: %s", e)

    logger.info("%s", "=" * 60)


def main():
    print_ratings_report()


if __name__ == "__main__":
    main()
