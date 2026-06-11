"""xG（预期进球）特征流水线 — 从 Understat 拉取数据并构建特征。

用法:
    from src.features.xg_pipeline import build_xg_features
    df_with_xg = build_xg_features(df, match_identifiers)
"""
import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
import numpy as np
import pandas as pd
from understat import Understat

# Understat 联赛名称到我们系统的映射
LEAGUE_MAP = {
    "EPL": "epl",
    "La_liga": "la_liga",
    "Serie_A": "serie_a",
    "Bundesliga": "bundesliga",
    "Ligue_1": "ligue_1",
}

# 反向映射：我们系统到 Understat
LEAGUE_TO_UNDERSTAT = {
    "epl": "EPL", "english_premier_league": "EPL",
    "la_liga": "La_liga", "spain_la_liga": "La_liga",
    "serie_a": "Serie_A", "italy_serie_a": "Serie_A",
    "bundesliga": "Bundesliga", "germany_bundesliga": "Bundesliga",
    "ligue_1": "Ligue_1", "france_ligue_one": "Ligue_1",
}

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "storage"
XG_CACHE = CACHE_DIR / "xg_cache.json"


def _load_cache() -> Dict:
    if XG_CACHE.exists():
        return json.loads(XG_CACHE.read_text())
    return {}


def _save_cache(cache: Dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    XG_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def _extract_league(sport_key: str) -> str:
    """从 odds API 的 sport_key 提取 Understat 联赛名。"""
    for our_key, us_key in LEAGUE_TO_UNDERSTAT.items():
        if our_key in sport_key.lower():
            return us_key
    return "EPL"


# ── 异步拉取 ──

async def _fetch_league_xg(league_name: str, season: int) -> List[Dict]:
    """拉取单个联赛整个赛季的 xG 数据。"""
    async with aiohttp.ClientSession() as session:
        us = Understat(session)
        data = await us.get_league_results(league_name, season)
    return data


def fetch_all_leagues_xg(seasons: Optional[List[int]] = None) -> Dict[str, List[Dict]]:
    """拉取所有五大联赛的 xG 数据。"""
    if seasons is None:
        seasons = [2024, 2023, 2022]  # 近3个赛季

    cache = _load_cache()
    results = {}

    for league in LEAGUE_MAP:
        for season in seasons:
            key = f"{league}_{season}"
            if key in cache:
                results[key] = cache[key]
                continue

            print(f"  📥 拉取 {league} {season}/{season+1} xG...")
            try:
                data = asyncio.run(_fetch_league_xg(league, season))
                # 保留关键字段
                cleaned = []
                for d in data:
                    if d.get("xG") and d["xG"].get("h"):
                        cleaned.append({
                            "id": d["id"],
                            "datetime": d["datetime"],
                            "home": d["h"]["title"],
                            "away": d["a"]["title"],
                            "home_goals": d["goals"]["h"],
                            "away_goals": d["goals"]["a"],
                            "home_xg": d["xG"]["h"],
                            "away_xg": d["xG"]["a"],
                        })
                results[key] = cleaned
                cache[key] = cleaned
                _save_cache(cache)
                print(f"    ✅ {len(cleaned)} 场")
            except Exception as e:
                print(f"    ⚠️ 失败: {e}")
                results[key] = cache.get(key, [])

    _save_cache(cache)
    return results


# ── 球队名称标准化 ──

_UNDERSTAT_TEAM_MAP = {
    "Nott'm Forest": "Nottingham Forest",
    "Manchester Utd": "Manchester United",
    "Newcastle Utd": "Newcastle United",
    "Sheffield Utd": "Sheffield United",
    "Wolverhampton": "Wolverhampton Wanderers",
    "Tottenham": "Tottenham Hotspur",
    "Brighton": "Brighton & Hove Albion FC",
    "West Ham": "West Ham United",
    "Lecce": "US Lecce",
    "Sassuolo": "US Sassuolo Calcio",
    "Cremonese": "US Cremonese",
    "AC Milan": "AC Milan",
    "Inter Milan": "FC Internazionale Milano",
    "Udinese": "Udinese Calcio",
    "Torino": "Torino FC",
    "Genoa": "Genoa CFC",
    "Parma": "Parma Calcio 1913",
    "Hellas Verona": "Hellas Verona FC",
    "Lazio": "SS Lazio",
    "Napoli": "SSC Napoli",
    "Bologna": "Bologna FC 1909",
    "Cagliari": "Cagliari Calcio",
    "Como": "Como 1907",
    "Pisa": "AC Pisa 1909",
    "Athletic Club": "Athletic Bilbao",
    "Alaves": "Deportivo Alavés",
    "Celta Vigo": "RC Celta de Vigo",
    "Espanyol": "RCD Espanyol de Barcelona",
    "Real Betis": "Real Betis Balompié",
    "Real Sociedad": "Real Sociedad de Fútbol",
    "Rayo Vallecano": "Rayo Vallecano de Madrid",
    "Mallorca": "RCD Mallorca",
    "Osasuna": "CA Osasuna",
    "Valencia": "Valencia CF",
    "Villarreal": "Villarreal CF",
    "Atletico Madrid": "Club Atlético de Madrid",
    "Getafe": "Getafe CF",
    "Sevilla": "Sevilla FC",
    "Leverkusen": "Bayer 04 Leverkusen",
    "M'gladbach": "Borussia Mönchengladbach",
    "FC Koln": "1. FC Köln",
    "Mainz 05": "1. FSV Mainz 05",
    "FC Heidenheim": "1. FC Heidenheim 1846",
    "Union Berlin": "1. FC Union Berlin",
    "St. Pauli": "FC St. Pauli 1910",
    "Bayern Munich": "FC Bayern München",
    "Stuttgart": "VfB Stuttgart",
    "Wolfsburg": "VfL Wolfsburg",
    "Bochum": "VfL Bochum",
    "Monaco": "AS Monaco FC",
    "Brest": "Stade Brestois 29",
    "Rennes": "Stade Rennais FC 1901",
    "Strasbourg": "RC Strasbourg Alsace",
    "PSG": "Paris Saint-Germain FC",
    "Lyon": "Olympique Lyonnais",
    "Marseille": "Olympique de Marseille",
    "Saint-Etienne": "AS Saint-Étienne",
    "Nantes": "FC Nantes",
    "Lorient": "FC Lorient",
    "Auxerre": "AJ Auxerre",
    "Le Havre": "Le Havre AC",
    "Toulouse": "Toulouse FC",
    "Angers": "Angers SCO",
}


def _normalize_team(name: str) -> str:
    """标准化球队名称。"""
    name = name.strip()
    return _UNDERSTAT_TEAM_MAP.get(name, name)


# ── 构建 xG 特征 ──

def build_xg_features(seasons: Optional[List[int]] = None) -> pd.DataFrame:
    """构建球队级 xG 滚动统计特征。

    Returns:
        DataFrame 包含: date, team, xg_avg_3/5/7/10, xg_conceded_avg_*, xg_diff_*, ...
    """
    raw = fetch_all_leagues_xg(seasons)

    rows = []
    for key, matches in raw.items():
        for m in matches:
            rows.append({
                "date": pd.to_datetime(m["datetime"]),
                "team": _normalize_team(m["home"]),
                "opponent": _normalize_team(m["away"]),
                "xg_for": float(m["home_xg"]),
                "xg_against": float(m["away_xg"]),
                "goals_for": int(m["home_goals"]) if m.get("home_goals") else np.nan,
                "goals_against": int(m["away_goals"]) if m.get("away_goals") else np.nan,
                "is_home": 1,
            })
            rows.append({
                "date": pd.to_datetime(m["datetime"]),
                "team": _normalize_team(m["away"]),
                "opponent": _normalize_team(m["home"]),
                "xg_for": float(m["away_xg"]),
                "xg_against": float(m["home_xg"]),
                "goals_for": int(m["away_goals"]) if m.get("away_goals") else np.nan,
                "goals_against": int(m["home_goals"]) if m.get("home_goals") else np.nan,
                "is_home": 0,
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values(["team", "date"])

    # 滚动 xG 平均（shift 1 防泄漏）
    for w in [3, 10]:
        df[f"xg_avg_{w}"] = df.groupby("team")["xg_for"].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        df[f"xga_avg_{w}"] = df.groupby("team")["xg_against"].transform(
            lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        df[f"xg_diff_{w}"] = df[f"xg_avg_{w}"] - df[f"xga_avg_{w}"]

    # 近期 xG 趋势（指数加权）
    df["xg_ewm5"] = df.groupby("team")["xg_for"].transform(
        lambda x: x.shift(1).ewm(span=5, adjust=False).mean())
    df["xga_ewm5"] = df.groupby("team")["xg_against"].transform(
        lambda x: x.shift(1).ewm(span=5, adjust=False).mean())

    # xG 转化率（xG → 实际进球）— 衡量球队终结能力
    df["xg_conversion"] = df.groupby("team")["goals_for"].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean()) / \
        df.groupby("team")["xg_for"].transform(
            lambda x: x.shift(1).rolling(10, min_periods=1).mean()).clip(lower=0.01)

    # xG 残差：实际进球 vs xG（捕捉运气/超常发挥）
    df["xg_residual"] = df["goals_for"] - df["xg_for"]
    df["xg_residual_5"] = df.groupby("team")["xg_residual"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=2).mean())
    df["xg_residual_10"] = df.groupby("team")["xg_residual"].transform(
        lambda x: x.shift(1).rolling(10, min_periods=3).mean())

    return df


def merge_xg_into_match(match_df: pd.DataFrame, xg_df: pd.DataFrame) -> pd.DataFrame:
    """将 xG 特征合并到比赛 DataFrame。"""
    match_df = match_df.copy()

    # 需要保留的滚动 xG 特征列（排除原始 xg_for/xg_against 防泄漏）
    XG_COLS_KEEP = [
        "xg_avg_3", "xg_avg_10",
        "xga_avg_3", "xga_avg_10",
        "xg_diff_3", "xg_diff_10",
        "xg_ewm5", "xga_ewm5", "xg_conversion",
        "xg_residual_5", "xg_residual_10",
    ]
    for side, team_col in [("home", "home"), ("away", "away")]:
        sf = xg_df[["date", "team"] + XG_COLS_KEEP].copy()
        sf = sf.add_prefix(f"{side}_")
        sf.rename(columns={f"{side}_date": "date", f"{side}_team": team_col}, inplace=True)
        match_df = pd.merge_asof(
            match_df.sort_values("date"), sf.sort_values("date"),
            by=team_col, on="date", direction="backward",
        )

    match_df["xg_off_vs_def"] = match_df["home_xg_ewm5"] - match_df["away_xga_ewm5"]
    match_df = match_df.ffill().fillna(0)
    return match_df


if __name__ == "__main__":
    df = build_xg_features()
    print(f"✅ xG 特征构建完成: {len(df)} 行, {df.shape[1]} 列")
    print(f"列: {[c for c in df.columns if c not in ('date','team','opponent')][:15]}")
