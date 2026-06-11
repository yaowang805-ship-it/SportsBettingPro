"""ELO 评级系统 — 为比赛预测提供球队实力特征。"""
import pandas as pd


def compute_elo(df: pd.DataFrame, K: float = 30,
                home_col: str = "home", away_col: str = "away",
                score_home_col: str = "home_goals", score_away_col: str = "away_goals",
                date_col: str = "date") -> pd.DataFrame:
    """计算球队 ELO 评级作为比赛特征。

    对按日期排序的比赛逐场计算，每场比赛使用赛前 ELO。
    ELO 反映了球队的长期实力，比滚动窗口均值更稳定。

    Args:
        df: 原始比赛 DataFrame，必须含日期、主客队、比分列
        K: ELO 更新系数 (篮球=20, 足球=30)
        home_col: 主队列名
        away_col: 客队列名
        score_home_col: 主队比分列名
        score_away_col: 客队比分列名
        date_col: 日期列名

    Returns:
        原 DataFrame 新增三列: home_elo, away_elo, elo_diff
    """
    df = df.copy().sort_values(date_col).reset_index(drop=True)

    # 初始化 ELO 字典（所有球队 1500 分）
    elo_ratings = {}
    home_elo_list, away_elo_list = [], []

    for idx in range(len(df)):
        home = df.at[idx, home_col]
        away = df.at[idx, away_col]

        # 新球队初始 1500
        if home not in elo_ratings:
            elo_ratings[home] = 1500.0
        if away not in elo_ratings:
            elo_ratings[away] = 1500.0

        home_elo = elo_ratings[home]
        away_elo = elo_ratings[away]
        home_elo_list.append(home_elo)
        away_elo_list.append(away_elo)

        # 赛后更新 ELO（用于后续比赛）
        home_goals = df.at[idx, score_home_col]
        away_goals = df.at[idx, score_away_col]

        if pd.notna(home_goals) and pd.notna(away_goals):
            # 期望胜率
            expected_home = 1.0 / (1.0 + 10.0 ** ((away_elo - home_elo) / 400.0))
            expected_away = 1.0 - expected_home

            # 实际结果（1=胜, 0.5=平, 0=负）
            if home_goals > away_goals:
                actual_home, actual_away = 1.0, 0.0
            elif home_goals < away_goals:
                actual_home, actual_away = 0.0, 1.0
            else:
                actual_home, actual_away = 0.5, 0.5

            # 更新评级
            elo_ratings[home] += K * (actual_home - expected_home)
            elo_ratings[away] += K * (actual_away - expected_away)

    df["home_elo"] = home_elo_list
    df["away_elo"] = away_elo_list
    df["elo_diff"] = df["home_elo"] - df["away_elo"]
    return df
