"""NBA 球员表现预测 — 基于历史数据 + 对手调整。

用法:
    from src.predict.player_projection import predict_game_player_props
    players = predict_game_player_props("Boston Celtics", "Los Angeles Lakers")
    for p in players:
        print(f"{p['name']}: PTS={p['PTS']}, REB={p['REB']}, AST={p['AST']}")
"""

from typing import Dict, List, Optional
from datetime import datetime

from config.logging_config import get_logger
logger = get_logger(__name__)

from src.features.player_pipeline import (
    get_today_players, PROP_STATS, PROP_LABELS, rank_player_props
)

# ── 置信度阈值 ──
_MIN_CONFIDENCE = 0.45  # 低于此值的球员不输出推荐
_MIN_PTS_PROJECTION = 8.0  # 得分低于此值不推荐
_MIN_REB_PROJECTION = 4.0
_MIN_AST_PROJECTION = 3.0


def predict_game_player_props(home_team: str, away_team: str,
                                min_confidence: float = _MIN_CONFIDENCE,
                                season: str = None) -> List[Dict]:
    """对一场比赛的所有球员输出投影数据。

    Returns:
        [{
            "name": "Jayson Tatum",
            "team": "home",
            "team_name": "Boston Celtics",
            "position": "F-G",
            "PTS": 23.3, "REB": 10.9, "AST": 6.7,
            "PRA": 40.9, "THREES": 2.5,
            "confidence": 0.85,
            "recommendations": ["PTS 大球: 投影 23.3"],
        }, ...]
    """
    players = get_today_players(home_team, away_team, season=season)
    if not players:
        return []

    # 按置信度排序
    players = rank_player_props(players)

    results = []
    for p in players:
        if p["confidence"] < min_confidence:
            continue

        proj = p.get("projections", {})
        if not proj:
            continue

        # 过滤低量级球员
        pts = proj.get("PTS", 0)
        reb = proj.get("REB", 0)
        ast = proj.get("AST", 0)
        if pts < _MIN_PTS_PROJECTION and reb < _MIN_REB_PROJECTION and ast < _MIN_AST_PROJECTION:
            continue

        # 格式化输出
        result = {
            "name": p["player_name"],
            "team": p["team"],
            "team_name": p["team_name"],
            "position": p["position"],
            "confidence": p["confidence"],
            "PTS": pts,
            "REB": reb,
            "AST": ast,
            "STL": proj.get("STL", 0),
            "BLK": proj.get("BLK", 0),
            "TOV": proj.get("TOV", 0),
            "PRA": proj.get("PRA", pts + reb + ast),
            "THREES": proj.get("THREES", 0),
            "recommendations": [],
        }

        # 生成简短建议（未来对接赔率后改为 EV 建议）
        notes = []
        if pts >= 15:
            notes.append(f"得分{pts:.0f}")
        if reb >= 8:
            notes.append(f"篮板{reb:.0f}")
        if ast >= 6:
            notes.append(f"助攻{ast:.0f}")
        if proj.get("THREES", 0) >= 2.5:
            notes.append(f"三分{proj['THREES']:.1f}")
        if notes:
            result["recommendations"] = [f"关注: {'/'.join(notes)}"]

        results.append(result)

    return results


def format_player_report(players: List[Dict]) -> str:
    """格式化球员投影输出为可读字符串。"""
    if not players:
        return "🏀 今日无球员数据"

    lines = []
    lines.append("🏀 球员表现预测")
    lines.append("-" * 50)

    for team_label, team_cn in [("home", "主队"), ("away", "客队")]:
        team_players = [p for p in players if p["team"] == team_label]
        if not team_players:
            continue
        team_name = team_players[0]["team_name"]
        lines.append(f"\n{team_cn} - {team_name}:")
        for p in team_players[:8]:  # 每队最多 8 人
            proj_str = (f"PTS {p['PTS']:.1f} | REB {p['REB']:.1f} | AST {p['AST']:.1f}"
                        f" | 三分 {p['THREES']:.1f}" if p['THREES'] > 0 else
                        f"PTS {p['PTS']:.1f} | REB {p['REB']:.1f} | AST {p['AST']:.1f}")
            if p["confidence"] >= 0.7:
                tag = "✨"
            elif p["confidence"] >= 0.5:
                tag = "👍"
            else:
                tag = "  "
            lines.append(f"  {tag} {p['name']} ({p['position']}): {proj_str}")
            if p.get("recommendations"):
                for rec in p["recommendations"]:
                    lines.append(f"     {rec}")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    # 测试
    import sys
    print("NBA 球员投影测试")
    print("=" * 50)
    players = predict_game_player_props("Boston Celtics", "Los Angeles Lakers")
    print(format_player_report(players))
