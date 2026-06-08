#!/usr/bin/env python3
"""全体育统一推荐排名 — 跨运动按 EV/Kelly 排序 + 组合凯利仓位分配。

职业博彩 vs 业余博彩的关键区别：
  - 业余：每个运动独立生成推荐，各自分配仓位
  - 专业：所有运动统一排名，只打全局最优的前 N 个，组合级别分配 bankroll
"""
import sys, json
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
logger = get_logger(__name__)

import numpy as np
import pandas as pd

from config.settings import DATA_DIR, KELLY_FRACTION
from src.risk.manager import RiskManager
from src.core.team_names import cn_team

BB_RECS_FILE = DATA_DIR / "daily_bb_recommendations.json"
FB_RECS_FILE = DATA_DIR / "daily_fb_recommendations.json"
NFL_RECS_FILE = DATA_DIR / "daily_nfl_recommendations.json"
ARBITRAGE_FILE = DATA_DIR / "arbitrage_log.json"
RANKED_OUTPUT = DATA_DIR / "ranked_recommendations.json"

# 最大全局推荐数
MAX_GLOBAL_RECS = 8


def load_recommendations(path: Path, sport: str) -> List[Dict]:
    """加载单个运动的推荐列表。"""
    if not path.exists():
        logger.debug("  无 %s 推荐文件", sport)
        return []
    try:
        data = json.loads(path.read_text())
        recs = data.get("recommendations", [])
        for r in recs:
            r["sport"] = sport
        logger.info("  %s: %d 条候选", sport.upper(), len(recs))
        return recs
    except Exception as e:
        logger.warning("  ⚠️ 加载 %s 推荐失败: %s", sport, e)
        return []


def _load_arbitrage_opportunities() -> List[Dict]:
    """加载套利机会并转为推荐候选格式。"""
    if not ARBITRAGE_FILE.exists():
        return []
    try:
        data = json.loads(ARBITRAGE_FILE.read_text())
        opps = data.get("opportunities", [])
        candidates = []
        for opp in opps:
            if opp.get("type") == "h2h" or opp.get("type") == "spread_arb":
                profit = opp.get("profit_pct", 0)
                if profit > 0.5:
                    total_odds = sum(1.0 / p for p in opp.get("best_prices", {}).values())
                    avg_odds = 1.0 / (total_odds / max(len(opp.get("best_prices", {})), 1))
                    candidates.append({
                        "sport": opp.get("_sport", "arbitrage"),
                        "type": "arbitrage",
                        "home_team": opp.get("home_team", ""),
                        "away_team": opp.get("away_team", ""),
                        "odds": round(avg_odds, 4),
                        "model_prob": 1.0,
                        "mkt_prob": 1.0 - profit / 100.0,
                        "_ev": profit / 100.0,
                        "_kelly_frac": 0.0,
                        "_arbitrage": opp,
                        "league": opp.get("_sport", ""),
                        "commence_time": "",
                    })
            elif opp.get("type") == "line_shopping":
                gap = opp.get("price_gap", 0)
                if gap > 0.07:
                    candidates.append({
                        "sport": opp.get("_sport", "line_shopping"),
                        "type": "line_shopping",
                        "home_team": opp.get("home_team", ""),
                        "away_team": opp.get("away_team", ""),
                        "odds": opp.get("best_price", 0),
                        "model_prob": 0.55,
                        "mkt_prob": 1.0 / opp.get("best_price", 2.0),
                        "_ev": 0.55 - 1.0 / opp.get("best_price", 2.0),
                        "_kelly_frac": 0.0,
                        "_arbitrage": opp,
                        "league": opp.get("_sport", ""),
                        "commence_time": "",
                    })
        logger.info("  📌 套利/比价候选: %d 条", len(candidates))
        return candidates
    except Exception as e:
        logger.warning("  ⚠️ 加载套利数据失败: %s", e)
        return []


def _calculate_kelly(prob: float, odds: float) -> float:
    """全凯利比例。"""
    if odds <= 1.0 or prob <= 0:
        return 0.0
    b = odds - 1.0
    return max(0.0, (prob * b - (1.0 - prob)) / b)


def rank_recommendations() -> List[Dict]:
    """跨运动统一排名：按 EV 排序，组合凯利分配仓位。

    流程:
      1. 加载所有运动的推荐
      2. 统一计算 EV 和 Kelly 比例
      3. 按 EV 降序排列
      4. 用单一 RiskManager 做组合级仓位分配（含跨运动相关投注检测）
      5. 返回前 MAX_GLOBAL_RECS 条
    """
    # 1. 收集所有候选（含套利机会）
    bb_recs = load_recommendations(BB_RECS_FILE, "nba")
    fb_recs = load_recommendations(FB_RECS_FILE, "football")
    nfl_recs = load_recommendations(NFL_RECS_FILE, "nfl")
    arb_recs = _load_arbitrage_opportunities()
    all_recs = bb_recs + fb_recs + nfl_recs + arb_recs

    if not all_recs:
        logger.info("  无任何候选推荐")
        return []

    # 2. 计算 EV 和 Kelly
    for r in all_recs:
        prob = r.get("model_prob", 0)
        odds = r.get("odds", 0)
        mkt_prob = r.get("mkt_prob", 1.0 / odds if odds > 0 else 0)
        r["_ev"] = prob - mkt_prob
        r["_kelly_frac"] = _calculate_kelly(prob, odds)

    # 3. 按 EV 排序
    all_recs.sort(key=lambda x: x["_ev"], reverse=True)

    # 4. 组合凯利分配（两阶段）
    #   阶段1：筛选符合条件的候选
    #   阶段2：用 KellyPortfolioOptimizer 做联合最优分配
    rm = RiskManager()
    candidates = []
    sport_counts = {}

    for r in all_recs:
        if len(candidates) >= MAX_GLOBAL_RECS * 2:  # 多收一些候选供优化器选择
            break

        prob = r.get("model_prob", 0)
        odds = r.get("odds", 0)
        home = r.get("home_team", "")
        away = r.get("away_team", "")
        sport = r.get("sport", "")
        market = r.get("type", "")
        ev = r["_ev"]

        if ev < 0.02:
            continue

        # 分散度控制
        sport_key = sport
        if sport_counts.get(sport_key, 0) >= MAX_GLOBAL_RECS:
            continue
        sport_counts[sport_key] = sport_counts.get(sport_key, 0) + 1

        # 同场比赛互斥检测
        stake_test = rm.get_max_stake(
            prob, odds,
            current_exposure_pct=0.0,
            input_is_prob=True,
            sport=sport,
            home_team=home,
            away_team=away,
            market=market,
        )
        if stake_test <= 0:
            continue

        candidates.append(r)

    # 阶段2：联合优化
    ranked = []
    if candidates:
        # 将所有候选加入组合优化器
        for r in candidates:
            rm.portfolio_optimizer.add_bet({
                "sport": r.get("sport", ""),
                "home_team": r.get("home_team", ""),
                "away_team": r.get("away_team", ""),
                "market": r.get("type", ""),
                "model_prob": r.get("model_prob", 0),
                "odds": r.get("odds", 0),
            })

        # 求解最优分配
        result = rm.portfolio_optimizer.solve_kelly_portfolio(
            bankroll=rm.current_balance,
            max_single_pct=rm.max_single_pct,
            max_total_pct=rm.max_total_exposure,
        )

        allocations = {f"{a['sport']}_{a['home_team']}_{a['away_team']}_{a['market']}": a
                       for a in result.get("allocations", [])}

        for r in candidates:
            key = f"{r.get('sport', '')}_{r.get('home_team', '')}_{r.get('away_team', '')}_{r.get('type', '')}"
            alloc = allocations.get(key)
            if alloc is None:
                continue

            ranked.append({
                "rank": len(ranked) + 1,
                "sport": r.get("sport", ""),
                "home_team": r.get("home_team", ""),
                "away_team": r.get("away_team", ""),
                "type": r.get("type", ""),
                "league": r.get("league", ""),
                "odds": r.get("odds", 0),
                "model_prob": round(r.get("model_prob", 0), 4),
                "mkt_prob": round(r.get("mkt_prob", 0), 4),
                "ev": round(r["_ev"], 4),
                "kelly_frac": round(r["_kelly_frac"], 4),
                "stake": round(alloc["stake"], 2),
                "weight_pct": alloc["weight_pct"],
                "match_time": r.get("commence_time", ""),
            })

            if len(ranked) >= MAX_GLOBAL_RECS:
                break

    return ranked


def main():
    logger.info("─" * 60)
    logger.info("🏆 全体育统一排名")
    logger.info("─" * 60)

    ranked = rank_recommendations()

    if not ranked:
        logger.info("  今日无跨运动推荐")
        RANKED_OUTPUT.write_text(json.dumps({
            "date": pd.Timestamp.now().isoformat(),
            "total": 0,
            "recommendations": [],
        }, ensure_ascii=False, indent=2))
        return

    # 输出排名
    logger.info("  %s", "─" * 50)
    logger.info("  %3s | %-6s | %-30s | %5s | %5s | %5s | %6s" %
               ("#", "运动", "对阵", "赔率", "EV", "凯利", "注额"))
    logger.info("  %s", "─" * 50)
    for r in ranked:
        h = cn_team(r.get("home_team", ""), sport="football" if r["sport"] == "football" else "nba")
        a = cn_team(r.get("away_team", ""), sport="football" if r["sport"] == "football" else "nba")
        matchup = f"{h} vs {a}"
        logger.info("  %3d | %-6s | %-30s | %.2f | %+.1f%% | %.1f%% | ¥%.0f" %
                   (r["rank"], r["sport"].upper(), matchup[:30],
                    r["odds"], r["ev"] * 100, r["kelly_frac"] * 100, r["stake"]))
    logger.info("  %s", "─" * 50)
    logger.info("  总推荐: %d 条 | 总注额: ¥%.0f",
               len(ranked), sum(r["stake"] for r in ranked))

    # 保存
    RANKED_OUTPUT.write_text(json.dumps({
        "date": pd.Timestamp.now().isoformat(),
        "total": len(ranked),
        "recommendations": ranked,
    }, ensure_ascii=False, indent=2))

    logger.info("  ✅ 排名已保存至 %s", RANKED_OUTPUT.name)


if __name__ == "__main__":
    main()
