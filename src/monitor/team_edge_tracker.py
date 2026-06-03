"""球队级 Edge 跟踪 — 每支球队的历史投注表现。

跟踪每支球队的累计 edge、胜率、ROI，帮助识别哪些球队
持续提供正/负期望值。

用法:
    from src.monitor.team_edge_tracker import print_team_edge_report
    print_team_edge_report()
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

LOG_FILE = ROOT / "data" / "storage" / "prediction_log.csv"
TRACKING_FILE = ROOT / "data" / "storage" / "team_edge_tracking.json"

MIN_BETS_FOR_TRACKING = 3


def build_team_performance_report() -> Dict:
    """构建每支球队的投注表现报告。

    Returns:
        {
            "generated_at": "...",
            "total_teams": N,
            "teams": {team_name: {total_bets, won, lost, win_rate, avg_edge, ...}},
            "top_teams_by_edge": [...],
            "bottom_teams_by_edge": [...],
        }
    """
    if not LOG_FILE.exists():
        return {"error": "prediction_log.csv 不存在"}

    df = pd.read_csv(LOG_FILE)
    if df.empty:
        return {"error": "空的预测日志"}

    # 每条预测同时贡献给主队和客队
    team_records = {}  # {team_name: [{result, edge, odds, stake, opponent}]}

    for _, row in df.iterrows():
        home = str(row.get("home_team_cn", row.get("home_team", ""))).strip().lower()
        away = str(row.get("away_team_cn", row.get("away_team", ""))).strip().lower()
        if not home or not away:
            continue

        ev = float(row.get("ev", 0)) if row.get("ev") else 0.0
        status = row.get("status", "pending")
        odds_val = float(row.get("odds", 0))
        stake_val = float(row.get("stake", 0))

        # 主场 edge 直接使用 ev，客场 edge 为 -ev
        for team, edge_sign, opp in [(home, 1.0, away), (away, -1.0, home)]:
            if team not in team_records:
                team_records[team] = []
            team_records[team].append({
                "result": status,
                "edge": ev * edge_sign,
                "odds": odds_val,
                "stake": stake_val,
                "opponent": opp,
                "market": str(row.get("market_type", "")),
            })

    if not team_records:
        return {"error": "无球队记录"}

    # 聚合
    teams = {}
    for team, bets in team_records.items():
        settled = [b for b in bets if b["result"] in ("won", "lost")]
        if len(settled) < MIN_BETS_FOR_TRACKING:
            continue

        won = [b for b in settled if b["result"] == "won"]
        lost = [b for b in settled if b["result"] == "lost"]
        total_stake = sum(b["stake"] for b in settled)
        profit = sum(b["stake"] * (b["odds"] - 1) for b in won) - sum(b["stake"] for b in lost)

        teams[team] = {
            "total_bets": len(settled),
            "won": len(won),
            "lost": len(lost),
            "win_rate": round(len(won) / len(settled), 4) if settled else 0.0,
            "avg_edge": round(float(np.mean([b["edge"] for b in settled])), 4),
            "avg_odds": round(float(np.mean([b["odds"] for b in settled])), 4),
            "total_stake": round(total_stake, 2),
            "profit": round(profit, 2),
            "roi": round(profit / total_stake, 4) if total_stake > 0 else 0.0,
        }

    sorted_teams = sorted(teams.items(), key=lambda x: x[1]["avg_edge"], reverse=True)
    top_teams = [{"team": t, **s} for t, s in sorted_teams[:15]]
    bottom_teams = [{"team": t, **s} for t, s in sorted_teams[-15:]][::-1]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_teams": len(teams),
        "teams": teams,
        "top_teams_by_edge": top_teams,
        "bottom_teams_by_edge": bottom_teams,
    }

    TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRACKING_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("Team edge tracking report saved to %s", TRACKING_FILE)

    return report


def print_team_edge_report():
    """打印球队 edge 跟踪报告。"""
    report = build_team_performance_report()
    if "error" in report:
        logger.warning("Team edge report not available: %s", report["error"])
        return

    logger.info("\n" + "=" * 60)
    logger.info("  Team Edge Tracking Report")
    logger.info("=" * 60)
    logger.info("  Teams tracked: %d", report["total_teams"])

    logger.info("\n  Top 5 teams by edge:")
    for t in report["top_teams_by_edge"][:5]:
        logger.info("    %-25s edge=%+.4f  win=%d/%d  ROI=%+.1f%%",
                   t["team"][:25], t["avg_edge"], t["won"], t["total_bets"], t["roi"] * 100)

    logger.info("\n  Bottom 5 teams by edge:")
    for t in report["bottom_teams_by_edge"][-5:]:
        logger.info("    %-25s edge=%+.4f  win=%d/%d  ROI=%+.1f%%",
                   t["team"][:25], t["avg_edge"], t["won"], t["total_bets"], t["roi"] * 100)

    logger.info("=" * 60)


if __name__ == "__main__":
    print_team_edge_report()
