"""NBA 数据抓取器 — 使用 sports-skills (免费, 无需 API Key)。

数据源:
  - sports-skills: 封装 ESPN API, 零配置, 覆盖 NBA 实时比分/赛程/伤病
  - 历史数据 (2007-2026) 继续使用 data/raw/nba_betting_2007_2026.csv (24427 行)

用法:
    from fetchers.nba_api import fetch_scoreboard, fetch_injuries, run_sync
    games = fetch_scoreboard(date="2026-06-01")
    injuries = fetch_injuries()
"""
import sys, time, json, logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import DATA_DIR

# NBA 球队 ESPN ID → 全名映射（用于 sports-skills 输出转换）
ESPN_TEAM_NAMES = {
    "1": "Atlanta Hawks", "2": "Boston Celtics", "17": "Brooklyn Nets",
    "30": "Charlotte Hornets", "4": "Chicago Bulls", "5": "Cleveland Cavaliers",
    "6": "Dallas Mavericks", "7": "Denver Nuggets", "8": "Detroit Pistons",
    "9": "Golden State Warriors", "10": "Houston Rockets", "11": "Indiana Pacers",
    "12": "LA Clippers", "13": "Los Angeles Lakers", "14": "Memphis Grizzlies",
    "15": "Miami Heat", "16": "Milwaukee Bucks", "18": "Minnesota Timberwolves",
    "19": "New Orleans Pelicans", "20": "New York Knicks",
    "21": "Oklahoma City Thunder", "22": "Orlando Magic",
    "23": "Philadelphia 76ers", "24": "Phoenix Suns",
    "25": "Portland Trail Blazers", "26": "Sacramento Kings",
    "27": "San Antonio Spurs", "28": "Toronto Raptors",
    "29": "Utah Jazz", "31": "Washington Wizards",
}

SEASONS_AVAILABLE = [f'{y}-{str(y+1)[-2:]}' for y in range(2015, 2026)]


def _team_name(team_data: dict) -> str:
    """从 sports-skills 球队数据中提取全名。"""
    name = team_data.get("name", "")
    if name and name != "?" and not name.startswith("http"):
        return name
    abbreviation = team_data.get("abbreviation", "")
    espn_id = str(team_data.get("id", ""))
    return ESPN_TEAM_NAMES.get(espn_id, abbreviation or "Unknown")


def _event_to_row(event: dict) -> Optional[dict]:
    """将 sports-skills 比赛事件转换为标准格式行。"""
    try:
        competitors = event.get("competitors", [])
        if not competitors:
            return None

        home = next((c for c in competitors if c.get("home_away") == "home"), None)
        away = next((c for c in competitors if c.get("home_away") == "away"), None)
        if not home or not away:
            return None

        home_team = _team_name(home.get("team", {}))
        away_team = _team_name(away.get("team", {}))

        home_score = home.get("score")
        away_score = away.get("score")

        # 只返回有比分的比赛（已结束或进行中）
        if not home_score or not away_score:
            return None

        # 比赛时间
        date_str = event.get("date", "")
        if not date_str:
            date_str = event.get("start_time", event.get("game_time_utc", ""))

        return {
            "date": pd.to_datetime(date_str) if date_str else pd.NaT,
            "home": home_team,
            "away": away_team,
            "home_score": int(home_score),
            "away_score": int(away_score),
        }
    except Exception:
        return None


def fetch_scoreboard(date: str = None) -> List[Dict]:
    """从 sports-skills 获取指定日期的 NBA 赛果。

    Args:
        date: YYYY-MM-DD 格式, 默认今天

    Returns:
        标准格式比赛列表 [{date, home, away, home_score, away_score}]
    """
    try:
        from sports_skills import nba as nba_ss

        kwargs = {}
        if date:
            kwargs["date"] = date

        result = nba_ss.get_scoreboard(**kwargs)
        if not result.get("status"):
            logger.debug("  sports-skills scoreboard 无数据: %s", result.get("message", ""))
            return []

        data = result.get("data", {})
        if isinstance(data, str):
            return []

        events = data.get("events", []) if isinstance(data, dict) else []
        rows = []
        for e in events:
            row = _event_to_row(e)
            if row is not None:
                rows.append(row)

        return rows
    except Exception as e:
        logger.warning("  sports-skills scoreboard 失败: %s", e)
        return []


def fetch_live_scoreboard() -> Optional[List[Dict]]:
    """获取今日 NBA 实时记分牌。"""
    return fetch_scoreboard(date=None)


def fetch_injuries() -> List[Dict]:
    """获取 NBA 球员伤病数据。

    Returns:
        [{player, team, injury, status, date}] 或空列表
    """
    try:
        from sports_skills import nba as nba_ss

        result = nba_ss.get_injuries()
        if not result.get("status"):
            logger.debug("  sports-skills injuries 不可用")
            return []

        data = result.get("data", {})
        if isinstance(data, str):
            return []

        if isinstance(data, dict):
            injuries = data.get("injuries", data.get("events", []))
        elif isinstance(data, list):
            injuries = data
        else:
            injuries = []

        parsed = []
        for inj in injuries:
            try:
                if isinstance(inj, dict):
                    parsed.append({
                        "player": inj.get("name", inj.get("player", "")),
                        "team": inj.get("team", {}).get("name", inj.get("team_name", "")),
                        "injury": inj.get("injury", inj.get("details", "")),
                        "status": inj.get("status", inj.get("type", "")),
                        "date": inj.get("date", ""),
                    })
            except Exception:
                continue

        return parsed
    except Exception as e:
        logger.warning("  sports-skills injuries 失败: %s", e)
        return []


def fetch_standings() -> List[Dict]:
    """获取 NBA 排名数据。"""
    try:
        from sports_skills import nba as nba_ss

        result = nba_ss.get_standings()
        if result.get("status"):
            return result.get("data", {})
        return []
    except Exception:
        return []


def fetch_multiple_seasons(seasons: List[str] = None, include_playoffs: bool = False) -> pd.DataFrame:
    """通过 sports-skills 获取多赛季数据。

    注意: sports-skills 的 team_schedule 需要 ESPN 内部 ID，
    当前通过逐日 scoreboard 查询构建数据集（覆盖最近 N 天）。

    对于完整历史数据 (2007-2026), 使用 data/raw/nba_betting_2007_2026.csv。

    Args:
        seasons: 赛季列表（当前通过日期范围查询）
        include_playoffs: 是否包含季后赛

    Returns:
        DataFrame 列: date, home, away, home_score, away_score
    """
    # sports-skills 通过比分板获取历史数据有限
    # 真正的历史数据来自 nba_betting_2007_2026.csv
    # 这里用于获取近期完整赛季数据
    all_games = []
    today = datetime.utcnow().date()

    # 查询最近 14 天的比赛（比分板覆盖范围）
    for i in range(14):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        games = fetch_scoreboard(date=d)
        if games:
            all_games.extend(games)
            logger.info("  ✅ %s: %d 场", d, len(games))
        time.sleep(0.3)

    if not all_games:
        return pd.DataFrame()

    df = pd.DataFrame(all_games)
    df = df.dropna(subset=["date", "home", "away"])
    df = df.sort_values("date").drop_duplicates(subset=["date", "home", "away"]).reset_index(drop=True)
    return df


def convert_nba_to_standard_format(df: pd.DataFrame) -> pd.DataFrame:
    """确保 DataFrame 为标准格式。"""
    required = {"date", "home", "away", "home_score", "away_score"}
    if df.empty:
        return df
    missing = required - set(df.columns)
    if missing:
        logger.warning("convert_nba_to_standard_format: 缺少列 %s", missing)
        return pd.DataFrame()
    return df


def fetch_and_save_all(output_path: str = None, include_playoffs: bool = False) -> pd.DataFrame:
    """获取 NBA 数据并保存到 CSV。

    策略:
      1. sports-skills: 获取最近 14 天的实时比赛数据（免费，可靠）
      2. 与已有 basketball_history.csv 合并
      3. 历史全量数据由 bb_pipeline 从 nba_betting_2007_2026.csv 加载
    """
    output_path = output_path or str(DATA_DIR / "basketball_history.csv")

    logger.info("开始同步 NBA 数据（sports-skills + ESPN）...")

    # 通过 sports-skills 获取近期数据
    df = fetch_multiple_seasons(include_playoffs=include_playoffs)

    # 合并已有数据
    existing = Path(output_path)
    if existing.exists():
        old_df = pd.read_csv(existing)
        old_df["date"] = pd.to_datetime(old_df["date"], utc=True, errors="coerce")
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
            combined = pd.concat([old_df, df], ignore_index=True)
        else:
            combined = old_df
        combined = combined.drop_duplicates(subset=["date", "home", "away"])
        combined = combined.sort_values("date").reset_index(drop=True)
        logger.info("NBA 数据: %d → %d 行", len(old_df), len(combined))
    else:
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
        combined = df
        logger.info("NBA 数据: %d 行（新建）", len(combined))

    combined.to_csv(output_path, index=False)
    logger.info("✅ 已保存至 %s", output_path)
    return combined


def run_sync(days_back: int = 365) -> pd.DataFrame:
    """增量同步 — 使用 sports-skills 获取最近 days_back 天的比赛。"""
    end = datetime.utcnow().date()
    start = end - timedelta(days=min(days_back, 14))  # sports-skills 约覆盖 14 天
    logger.info("增量同步: %s 到 %s", start, end)

    all_games = []
    d = start
    while d <= end:
        ds = d.strftime("%Y-%m-%d")
        games = fetch_scoreboard(date=ds)
        if games:
            all_games.extend(games)
        d += timedelta(days=1)
        time.sleep(0.3)

    if not all_games:
        logger.info("  sports-skills 未返回任何比赛数据（可能是休赛期）")
        return pd.DataFrame()

    df = pd.DataFrame(all_games)
    df = df.dropna(subset=["date", "home", "away"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


if __name__ == "__main__":
    fetch_and_save_all()

    print("\n--- 伤病数据 ---")
    injuries = fetch_injuries()
    if injuries:
        for inj in injuries[:5]:
            print(f"  {inj['player']} ({inj['team']}): {inj['injury']} [{inj['status']}]")
    else:
        print("  无伤病数据（休赛期或无可用数据）")
