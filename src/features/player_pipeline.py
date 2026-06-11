"""NBA 球员数据流水线 — 通过 nba_api 获取比赛日志并构建特征。

数据来源: nba_api (stats.nba.com 官方 API，免费，无需 Key)。

用法:
    from src.features.player_pipeline import PlayerDataPipeline
    pipe = PlayerDataPipeline()
    players = pipe.get_today_players(home_team="Boston Celtics", away_team="Miami Heat")
    # [{player_id, name, position, projections: {PTS: 22.5, REB: 7.3, ...}}, ...]
"""

import sys
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import DATA_DIR

CACHE_DIR = DATA_DIR / "player_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── NBA API Team ID → 队名 ──────────────────────────────────
NBA_TEAMS = {
    1610612737: "Atlanta Hawks", 1610612738: "Boston Celtics",
    1610612739: "Cleveland Cavaliers", 1610612740: "New Orleans Pelicans",
    1610612741: "Chicago Bulls", 1610612742: "Dallas Mavericks",
    1610612743: "Denver Nuggets", 1610612744: "Golden State Warriors",
    1610612745: "Houston Rockets", 1610612746: "Los Angeles Clippers",
    1610612747: "Los Angeles Lakers", 1610612748: "Miami Heat",
    1610612749: "Milwaukee Bucks", 1610612750: "Minnesota Timberwolves",
    1610612751: "Brooklyn Nets", 1610612752: "New York Knicks",
    1610612753: "Orlando Magic", 1610612754: "Indiana Pacers",
    1610612755: "Philadelphia 76ers", 1610612756: "Phoenix Suns",
    1610612757: "Portland Trail Blazers", 1610612758: "Sacramento Kings",
    1610612759: "San Antonio Spurs", 1610612760: "Oklahoma City Thunder",
    1610612761: "Toronto Raptors", 1610612762: "Utah Jazz",
    1610612763: "Memphis Grizzlies", 1610612764: "Washington Wizards",
    1610612765: "Detroit Pistons", 1610612766: "Charlotte Hornets",
}

# 反向映射：队名 → Team ID
TEAM_NAME_TO_ID = {v: k for k, v in NBA_TEAMS.items()}

# 可预测的统计项
PROP_STATS = ["PTS", "REB", "AST", "STL", "BLK", "TOV", "PRA", "THREES"]
PROP_LABELS = {
    "PTS": "得分", "REB": "篮板", "AST": "助攻",
    "STL": "抢断", "BLK": "盖帽", "TOV": "失误",
    "PRA": "得分+篮板+助攻", "THREES": "三分",
}

# ── 赛季推断 ──────────────────────────────────────────


def _current_season() -> str:
    """根据当前日期推断 NBA 赛季字符串 (如 '2025-26')。"""
    now = datetime.now()
    year = now.year
    if now.month >= 10:
        return f"{year}-{str(year + 1)[-2:]}"
    return f"{year - 1}-{str(year)[-2:]}"


# ── 缓存工具 ──────────────────────────────────────────


def _cache_path(key: str) -> Path:
    safe = key.replace("/", "_").replace(" ", "_")
    return CACHE_DIR / f"{safe}.json"


def _load_cache(key: str, max_age_hours: int = 6):
    path = _cache_path(key)
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    if datetime.now() - mtime > timedelta(hours=max_age_hours):
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _save_cache(key: str, data):
    path = _cache_path(key)
    path.write_text(json.dumps(data, ensure_ascii=False))


# ── NBA API 调用（带缓存） ──────────────────────────


def _call_nba_api(endpoint_name: str, **kwargs) -> Optional[pd.DataFrame]:
    """调用 nba_api 端点并返回 DataFrame（带缓存）。"""
    cache_key = f"{endpoint_name}_{'_'.join(f'{k}={v}' for k, v in sorted(kwargs.items()))}"
    cached = _load_cache(cache_key, max_age_hours=2)
    if cached is not None:
        return pd.DataFrame(cached)

    try:
        mod_path = "nba_api.stats.endpoints"
        mod = __import__(mod_path, fromlist=[endpoint_name])
        ep_cls = getattr(mod, endpoint_name)
        ep = ep_cls(**kwargs)
        dfs = ep.get_data_frames()
        if dfs:
            result = dfs[0]
            if not result.empty:
                _save_cache(cache_key, result.to_dict(orient="records"))
            return result
        return pd.DataFrame()
    except Exception as e:
        logger.debug("nba_api %s 失败: %s", endpoint_name, e)
        return pd.DataFrame()


def get_team_roster(team_id: int, season: str = None) -> List[Dict]:
    """获取球队当前阵容。

    Returns:
        [{player_id, player_name, position}, ...]
    """
    if season is None:
        season = _current_season()

    cache_key = f"roster_{team_id}_{season}"
    cached = _load_cache(cache_key, max_age_hours=24)
    if cached:
        return cached

    try:
        from nba_api.stats.endpoints import commonteamroster
        roster = commonteamroster.CommonTeamRoster(team_id=team_id, season=season, timeout=15)
        df = roster.get_data_frames()[0]
        if df.empty:
            return []

        players_list = []
        for _, row in df.iterrows():
            players_list.append({
                "player_id": int(row["PLAYER_ID"]),
                "player_name": row["PLAYER"],
                "position": row.get("POSITION", "F/C"),
                "jersey": row.get("NUM", ""),
            })

        _save_cache(cache_key, players_list)
        time.sleep(0.2)  # 限速
        return players_list
    except Exception as e:
        logger.warning("获取阵容失败 (team=%s): %s", team_id, e)
        return []


def get_player_gamelog(player_id: int, num_games: int = 20, season: str = None) -> pd.DataFrame:
    """获取球员最近比赛日志。

    Args:
        player_id: NBA API player ID
        num_games: 需要的最少比赛场次
        season: 赛季字符串，默认当前赛季

    Returns:
        DataFrame 含列: PTS, REB, AST, STL, BLK, TOV, MIN, FGM, FGA, PLUS_MINUS, MATCHUP, GAME_DATE
    """
    if season is None:
        season = _current_season()

    df = _call_nba_api("PlayerGameLog", player_id=player_id, season=season, timeout=15)
    if df is None or df.empty:
        return pd.DataFrame()

    # 标准化列名
    col_map = {}
    for c in df.columns:
        upper = c.upper().strip()
        if upper in ("PTS", "REB", "AST", "STL", "BLK", "TOV", "MIN", "FGM", "FGA",
                     "FG3M", "PLUS_MINUS", "MATCHUP", "GAME_DATE", "FP"):
            col_map[c] = upper

    df = df.rename(columns=col_map)
    # 只保留所需列
    keep = [c for c in col_map.values()]
    df = df[[c for c in keep if c in df.columns]]

    # 转换数值列
    numeric_cols = [c for c in df.columns if c not in ("MATCHUP", "GAME_DATE", "FG3M")]
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    if "FG3M" in df.columns:
        df["THREES"] = pd.to_numeric(df["FG3M"], errors="coerce")
    else:
        df["THREES"] = 0

    # 按日期降序排列（最新在前）
    if "GAME_DATE" in df.columns:
        df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
        df = df.sort_values("GAME_DATE", ascending=False)

    # 如果 FG3M 不在列中，可能是列名不同
    df = df.head(num_games)
    return df


def get_team_defense_stats(team_id: int, season: str = None) -> Dict[str, float]:
    """获取球队防守数据 — 场均让对手得到的各项统计。

    从 TeamGameLog 的对手数据统计计算。

    Returns:
        {PTS_allowed, REB_allowed, AST_allowed, STL_allowed, BLK_allowed, TOV_forced}
    """
    if season is None:
        season = _current_season()

    cache_key = f"team_def_v2_{team_id}_{season}"
    cached = _load_cache(cache_key, max_age_hours=24)
    if cached:
        return cached

    try:
        from nba_api.stats.endpoints import teamgamelog
        gl = teamgamelog.TeamGameLog(team_id=team_id, season=season, timeout=15)
        df = gl.get_data_frames()[0]
        if df.empty:
            return {}

        # TeamGameLog 返回的是球队数据，我们用对手场均来推算防守强度
        # 简单方法：用球队场均失分 (PTS allowed = opp PTS)
        # TEAMGAMELOG 不直接提供 opponent stats，需要用对手视角的 LeagueDashTeamStats
        from nba_api.stats.endpoints import leaguedashteamstats
        ds = leaguedashteamstats.LeagueDashTeamStats(
            season=season, team_id_nullable=team_id,
            measure_type_detailed_defense="Opponent",
            per_mode_detailed="PerGame",
            timeout=15,
        )
        df_def = ds.get_data_frames()[0]
        if df_def.empty:
            # 降级：用联盟平均
            return {
                "PTS_allowed": 114.0, "REB_allowed": 44.0,
                "AST_allowed": 26.0, "STL_allowed": 7.5,
                "BLK_allowed": 5.0, "TOV_forced": 13.5,
                "THREES_allowed": 12.5,
            }

        result = {}
        col_map_def = {
            "OPP_PTS": "PTS_allowed", "OPP_REB": "REB_allowed",
            "OPP_AST": "AST_allowed", "OPP_STL": "STL_allowed",
            "OPP_BLK": "BLK_allowed", "OPP_TOV": "TOV_forced",
        }
        for src, dst in col_map_def.items():
            if src in df_def.columns:
                result[dst] = float(df_def[src].iloc[0])
            else:
                result[dst] = 0

        # 三分从 OPP_FG3M 获取（如果在列中）
        result["THREES_allowed"] = float(df_def["OPP_FG3M"].iloc[0]) if "OPP_FG3M" in df_def.columns \
            else result.get("PTS_allowed", 114) * 0.3 / 2

        _save_cache(cache_key, result)
        time.sleep(0.3)
        return result
    except Exception as e:
        logger.debug("获取球队防守数据失败 (team=%s): %s", team_id, e)
        # 降级：返回联盟平均
        return {
            "PTS_allowed": 114.0, "REB_allowed": 44.0,
            "AST_allowed": 26.0, "STL_allowed": 7.5,
            "BLK_allowed": 5.0, "TOV_forced": 13.5,
            "THREES_allowed": 12.5,
        }


# ── 特征构建 ──────────────────────────────────────────


def build_player_features(gamelog: pd.DataFrame, stat: str = "PTS",
                           num_games: int = 10) -> Dict:
    """从比赛日志构建球员某统计项的投影特征。

    Args:
        gamelog: PlayerGameLog DataFrame
        stat: 统计项 (PTS/REB/AST/etc.)
        num_games: 最多使用最近几场

    Returns:
        {raw_avg, weighted_avg, weighted_5, weighted_10, min_5, max_5,
         std_5, volatility, min_trend, games_played, last_game}
        或空字典（数据不足）
    """
    if stat not in gamelog.columns:
        return {}

    vals = gamelog[stat].dropna().head(num_games).values
    if len(vals) < 3:
        return {}

    # 等权平均
    raw_avg = float(np.mean(vals))

    # 指数加权（最近权重高）
    weights = np.array([0.85 ** i for i in range(len(vals))])
    weighted_avg = float(np.average(vals, weights=weights))

    # 最近 5 场
    recent_5 = vals[:min(5, len(vals))]
    weighted_5 = float(np.average(recent_5, weights=[0.85 ** i for i in range(len(recent_5))]))

    # 最近 10 场
    recent_10 = vals[:min(10, len(vals))]
    weighted_10 = float(np.average(recent_10, weights=[0.85 ** i for i in range(len(recent_10))]))

    # 波动性
    std_5 = float(np.std(recent_5)) if len(recent_5) > 1 else 0

    # 趋势：近 3 场 vs 前 7 场
    trend = float(np.mean(vals[:3]) - np.mean(vals[3:10])) if len(vals) >= 10 else 0

    return {
        "raw_avg": round(raw_avg, 1),
        "weighted_avg": round(weighted_avg, 1),
        "weighted_5": round(weighted_5, 1),
        "weighted_10": round(weighted_10, 1),
        "std_5": round(std_5, 1),
        "trend": round(trend, 1),
        "min_5": round(float(np.min(recent_5)), 1),
        "max_5": round(float(np.max(recent_5)), 1),
        "games_played": len(vals),
        "last_game": round(float(vals[0]), 1),
    }


def project_player_stat(features: Dict, opponent_def: Dict[str, float],
                         stat: str, league_avg: float = None) -> float:
    """综合投影球员单场统计。

    方法:
        1. 加权平均为基准 (weighted_avg)
        2. 对手调整: 基准 * (对手场均允许 / 联盟场均)
        3. 趋势微调

    Args:
        features: build_player_features 返回值
        opponent_def: 对手防守数据
        stat: 统计项
        league_avg: 联盟场均该数据，None 则使用内置默认值

    Returns:
        投影值
    """
    if not features:
        return 0.0

    base = features.get("weighted_avg", features.get("raw_avg", 0))
    if base <= 0:
        return 0.0

    # 对手调整
    opp_key = f"{stat}_allowed" if stat != "THREES" else "THREES_allowed"
    opp_val = opponent_def.get(opp_key, 0)

    if league_avg is None:
        league_avg = _LEAGUE_STAT_AVG.get(stat, 10.0)

    if opp_val > 0 and league_avg > 0:
        adjustment = opp_val / league_avg
        # 限制调整幅度 (0.85 ~ 1.15)
        adjustment = max(0.85, min(1.15, adjustment))
    else:
        adjustment = 1.0

    # 趋势微调（仅当趋势明显且样本充足）
    trend = features.get("trend", 0)
    trend_boost = 0
    if abs(trend) >= 2.0 and features.get("games_played", 0) >= 10:
        trend_boost = trend * 0.3  # 趋势的 30%

    projected = base * adjustment + trend_boost
    return round(max(0, projected), 1)


# 联盟平均数据（2024-25 赛季参考值，自动更新会覆盖）
_LEAGUE_STAT_AVG = {"PTS": 112.0, "REB": 44.0, "AST": 26.0,
                     "STL": 7.5, "BLK": 5.0, "TOV": 13.5}


def compute_pra(features: Dict, opponent_def: Dict[str, float]) -> float:
    """投影 PRA (Points + Rebounds + Assists)。"""
    pts = project_player_stat(features, opponent_def, "PTS")
    # 需要额外获取篮板和助攻特征
    return round(pts, 1)  # 默认 PTS ≈ PRA 中位数


# ── 排序与推荐 ──────────────────────────────────────────


def rank_player_props(players: List[Dict], min_minutes: float = 20.0) -> List[Dict]:
    """按置信度排序球员投影。

    置信度规则:
        - 包含近期分钟数信息可提高置信度
        - 波动性低的更可靠
        - 样本量多的更可靠
    """
    ranked = []
    for p in players:
        proj = p.get("projections", {})
        if not proj:
            continue
        # 简单置信度分数
        feats = p.get("features", {})
        n = feats.get("games_played", 0)
        std = feats.get("std_5", 999)
        confidence = min(1.0, n / 15) * max(0.5, 1.0 - std / 15)
        ranked.append({**p, "confidence": round(confidence, 2)})

    return sorted(ranked, key=lambda x: x["confidence"], reverse=True)


# ── 主入口（对接 daily_bb.py） ──────────────────────────


def get_today_players(home_team: str, away_team: str,
                       season: str = None) -> List[Dict]:
    """获取今日比赛双方所有可能轮换球员的投影数据。

    Args:
        home_team, away_team: 球队全名 (如 "Boston Celtics")
        season: 赛季，默认当前

    Returns:
        [{
            player_id, player_name, position,
            team: "home"/"away",
            projections: {PTS: 22.5, REB: 7.3, AST: 5.1, ...},
            features: {raw_avg, weighted_avg, ...},
            confidence: 0.85
        }, ...]
    """
    home_id = TEAM_NAME_TO_ID.get(home_team)
    away_id = TEAM_NAME_TO_ID.get(away_team)
    if not home_id or not away_id:
        logger.warning("球队ID映射失败: %s / %s", home_team, away_team)
        return []

    if season is None:
        season = _current_season()

    # 获取阵容
    home_roster = get_team_roster(home_id, season)
    away_roster = get_team_roster(away_id, season)
    logger.info("  🏀 阵容: %s %d人, %s %d人",
                home_team, len(home_roster), away_team, len(away_roster))

    # 获取对手防守数据（双方）
    home_def = get_team_defense_stats(home_id, season)
    away_def = get_team_defense_stats(away_id, season)

    results = []

    for roster, team_label, opp_def in [
        (home_roster, "home", away_def),
        (away_roster, "away", home_def),
    ]:
        for player in roster:
            pid = player["player_id"]
            name = player["player_name"]
            pos = player.get("position", "F/C")

            try:
                gamelog = get_player_gamelog(pid, num_games=20, season=season)
                if gamelog.empty:
                    continue

                # 分钟过滤：只预测稳定轮换球员
                min_col = "MIN" if "MIN" in gamelog.columns else None
                if min_col:
                    last_min = pd.to_numeric(gamelog[min_col].iloc[0], errors="coerce")
                    if pd.isna(last_min) or last_min < 15:
                        continue
                else:
                    continue

                # 构建各统计项投影
                projections = {}
                features_dict = {}
                for stat in PROP_STATS:
                    if stat == "PRA":
                        # PRA = PTS + REB + AST
                        pts_f = build_player_features(gamelog, "PTS")
                        reb_f = build_player_features(gamelog, "REB")
                        ast_f = build_player_features(gamelog, "AST")
                        if pts_f and reb_f and ast_f:
                            pts_p = project_player_stat(pts_f, opp_def, "PTS")
                            reb_p = project_player_stat(reb_f, opp_def, "REB")
                            ast_p = project_player_stat(ast_f, opp_def, "AST")
                            projections["PRA"] = round(pts_p + reb_p + ast_p, 1)
                    else:
                        features = build_player_features(gamelog, stat)
                        if features:
                            features_dict[stat] = features
                            proj = project_player_stat(features, opp_def, stat)
                            if proj > 0:
                                projections[stat] = proj

                if not projections:
                    continue

                # 用 PTS 特征作为整体置信度代表
                ref_feats = features_dict.get("PTS", {})
                n = ref_feats.get("games_played", 0)
                std = ref_feats.get("std_5", 999)
                confidence = min(1.0, n / 15) * max(0.5, 1.0 - std / 12)

                results.append({
                    "player_id": pid,
                    "player_name": name,
                    "position": pos,
                    "team": team_label,
                    "team_name": home_team if team_label == "home" else away_team,
                    "projections": projections,
                    "features": ref_feats,
                    "confidence": round(min(1.0, confidence), 2),
                })
            except Exception as e:
                logger.debug("  球员 %s 处理失败: %s", player["player_name"], e)
                continue

    logger.info("  ✅ 球员投影完成: %d 人", len(results))
    return results
