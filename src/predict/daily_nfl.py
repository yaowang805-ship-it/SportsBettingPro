#!/usr/bin/env python3
"""NFL 每日预测 — 加载模型 + 赔率 → 找正 EV 推荐。

用法:
    python src/predict/daily_nfl.py

流程:
    1. 从 Odds API 获取 NFL 实时赔率 (americanfootball_nfl)
    2. EnsemblePredictor 计算概率
    3. 对每场比赛计算 EV → Kelly → 风控
    4. 保存推荐至 data/storage/daily_nfl_recommendations.json
"""
import json
import sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import DATA_DIR
from src.risk.manager import RiskManager

NFL_RECS_FILE = DATA_DIR / "daily_nfl_recommendations.json"
NFL_ODDS_CACHE = DATA_DIR / "odds" / "live_nfl_odds.json"

TOTAL_RELIABLE_ACC = 0.52  # 总得分模型可靠阈值


def _fetch_nfl_odds(force: bool = False):
    """获取 NFL 赔率（支持缓存）。"""
    if not force and NFL_ODDS_CACHE.exists():
        age = (datetime.now() - datetime.fromtimestamp(NFL_ODDS_CACHE.stat().st_mtime)).total_seconds()
        if age < 1800:  # 30 分钟缓存
            with open(NFL_ODDS_CACHE) as f:
                return json.load(f)

    try:
        from fetchers.odds_api import fetch_odds_api
        data = fetch_odds_api("americanfootball_nfl", force=True)
        if data:
            NFL_ODDS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            with open(NFL_ODDS_CACHE, "w") as f:
                json.dump(data, f)
        return data
    except Exception as e:
        logger.warning("  ⚠️ NFL 赔率获取失败: %s", e)
        return []


def _calculate_kelly(prob: float, odds: float) -> float:
    """全凯利比例。"""
    if odds <= 1.0 or prob <= 0:
        return 0.0
    b = odds - 1.0
    return max(0.0, (prob * b - (1.0 - prob)) / b)


def _model_is_reliable(target: str) -> bool:
    """检查模型准确率是否足够生成推荐。"""
    meta_path = ROOT / "models" / f"model_nfl_{target}_ensemble_meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
        acc = meta.get("optimal_threshold", {}).get("test_accuracy", 0)
        return acc >= TOTAL_RELIABLE_ACC if target == "total_result" else acc >= 0.50
    except Exception:
        return False


_NFL_TEAM_ABBR = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
_ABBR_TO_FULL = {v: k for k, v in _NFL_TEAM_ABBR.items()}


def _to_abbr(name: str) -> str:
    """将球队全名转为缩写。"""
    name = name.strip()
    # 直接匹配
    if name in _ABBR_TO_FULL:
        return _ABBR_TO_FULL[name]
    # 尝试从缩写反向匹配（全名可能是 "Kansas City Chiefs" → "KC"）
    for abbr, full in _NFL_TEAM_ABBR.items():
        if full == name or abbr == name:
            return abbr
    return name


def _extract_best_odds(match_data: dict) -> dict:
    """从 Odds API 数据中提取最佳赔率。"""
    result = {"home_odds": None, "away_odds": None,
              "spread_home_odds": None, "spread_away_odds": None,
              "spread_home_point": None, "spread_away_point": None,
              "over_odds": None, "under_odds": None, "total_point": None,
              "n_bookmakers": 0}

    bookmakers = match_data.get("bookmakers", [])
    result["n_bookmakers"] = len(bookmakers)

    for bm in bookmakers:
        for market in bm.get("markets", []):
            outcomes = market.get("outcomes", [])
            if market.get("key") == "h2h":
                for out in outcomes:
                    name = out.get("name", "").strip()
                    price = out.get("price", 0)
                    if price > 0:
                        if name == match_data.get("home_team", "").strip():
                            result["home_odds"] = max(result["home_odds"] or 0, price)
                        elif name == match_data.get("away_team", "").strip():
                            result["away_odds"] = max(result["away_odds"] or 0, price)

            elif market.get("key") == "spreads":
                for out in outcomes:
                    name = out.get("name", "").strip()
                    price = out.get("price", 0)
                    point = out.get("point", 0)
                    if price > 0:
                        if name == match_data.get("home_team", "").strip():
                            if result["spread_home_odds"] is None or price > result["spread_home_odds"]:
                                result["spread_home_odds"] = price
                                result["spread_home_point"] = point
                        elif name == match_data.get("away_team", "").strip():
                            if result["spread_away_odds"] is None or price > result["spread_away_odds"]:
                                result["spread_away_odds"] = price
                                result["spread_away_point"] = point

            elif market.get("key") == "totals":
                for out in outcomes:
                    name = out.get("name", "").strip()
                    price = out.get("price", 0)
                    point = out.get("point", 0)
                    if price > 0:
                        if name.lower() == "over":
                            if result["over_odds"] is None or price > result["over_odds"]:
                                result["over_odds"] = price
                                result["total_point"] = point
                        elif name.lower() == "under":
                            if result["under_odds"] is None or price > result["under_odds"]:
                                result["under_odds"] = price
                                result["total_point"] = point

    return result


def main():
    logger.info("=" * 60)
    logger.info("🏈 NFL 每日预测 - %s", datetime.now().strftime("%Y-%m-%d %H:%M"))

    # ── 休赛期检测 ──
    try:
        from src.core.season_check import has_upcoming_games
        if not has_upcoming_games("nfl", days_back=2):
            logger.info("🏈 NFL 休赛期，今日跳过")
            NFL_RECS_FILE.write_text(json.dumps({
                "date": datetime.now().isoformat(), "sport": "nfl",
                "total_games": 0, "recommendations": [],
            }, ensure_ascii=False, indent=2))
            return
    except Exception:
        pass

    # 获取赔率
    odds_data = _fetch_nfl_odds()
    if not odds_data:
        logger.warning("  ❌ 无 NFL 赔率数据")
        NFL_RECS_FILE.write_text(json.dumps({
            "date": datetime.now().isoformat(), "sport": "nfl",
            "total_games": 0, "recommendations": [],
        }, ensure_ascii=False, indent=2))
        return

    logger.info("  ✅ 实时赔率: %d 场", len(odds_data))

    # 筛选即将开始的比赛（24 小时内）
    now = pd.Timestamp.now(tz="UTC")
    upcoming = []
    for g in odds_data:
        ct = g.get("commence_time", "")
        if ct:
            t = pd.Timestamp(ct, tz="UTC")
            if t > now and (t - now).total_seconds() < 86400:
                upcoming.append(g)
    logger.info("  📅 今日即将开始: %d 场", len(upcoming))

    if not upcoming:
        logger.info("  ℹ️ 今日无即将开始的 NFL 比赛")
        NFL_RECS_FILE.write_text(json.dumps({
            "date": datetime.now().isoformat(), "sport": "nfl",
            "total_games": len(odds_data), "recommendations": [],
        }, ensure_ascii=False, indent=2))
        return

    # 加载预测模型
    try:
        from src.predict.ensemble_predictor import EnsemblePredictor
        predictor = EnsemblePredictor("nfl")
    except Exception as e:
        logger.error("  ❌ 模型加载失败: %s", e)
        return

    # 预测
    try:
        predictions = predictor.predict(upcoming)
    except Exception as e:
        logger.warning("  ⚠️ 预测失败: %s", e)
        predictions = []

    if not predictions:
        logger.warning("  ⚠️ 预测结果为空")
        return

    logger.info("  ✅ 预测完成: %d 场", len(predictions))

    # 评估各市场，生成推荐
    rm = RiskManager()
    cb = rm.circuit_breaker_status()
    if cb["tripped"]:
        logger.warning("🛑 止损断路器已触发: %s", cb["message"])
        logger.warning("⚠️ 冷却中，跳过所有推荐")
        NFL_RECS_FILE.write_text(json.dumps({
            "date": datetime.now().isoformat(),
            "sport": "nfl",
            "total_games": len(upcoming),
            "recommendations": [],
            "circuit_breaker": cb,
        }, ensure_ascii=False, indent=2))
        return
    logger.info("✅ 止损断路器状态正常: %s", cb["message"])

    recommendations = []

    # 检查各模型是否可靠
    win_reliable = _model_is_reliable("win")
    spread_reliable = _model_is_reliable("spread_result")
    total_reliable = _model_is_reliable("total_result")

    logger.info("  模型可靠性: win=%s spread=%s total=%s",
                "✅" if win_reliable else "❌",
                "✅" if spread_reliable else "❌",
                "✅" if total_reliable else "❌")

    for pred in predictions:
        odds = _extract_best_odds(pred)

        # 主胜（ML）
        if win_reliable and odds["home_odds"] and odds["n_bookmakers"] >= 3:
            prob = pred.get("win_prob", 0)
            ev = pred.get("win_ev", 0)
            if ev > 0.02 and _calculate_kelly(prob, odds["home_odds"]) > 0:
                stake = rm.get_max_stake(
                    prob, odds["home_odds"], current_exposure_pct=0.0,
                    input_is_prob=True, sport="nfl",
                    home_team=pred.get("home_team", ""),
                    away_team=pred.get("away_team", ""), market="win")
                if stake > 0:
                    recommendations.append({
                        "type": "win", "side": "home",
                        "home_team": pred.get("home_team", ""),
                        "away_team": pred.get("away_team", ""),
                        "odds": odds["home_odds"],
                        "model_prob": round(prob, 4),
                        "ev": round(ev, 4),
                        "kelly_frac": round(_calculate_kelly(prob, odds["home_odds"]), 4),
                        "stake": round(stake, 2),
                        "commence_time": pred.get("commence_time", ""),
                        "league": "NFL",
                    })

        # 客胜（ML）
        if win_reliable and odds["away_odds"] and odds["n_bookmakers"] >= 3:
            prob = 1 - pred.get("win_prob", 0)
            away_ev = prob - 1.0 / odds["away_odds"]
            if away_ev > 0.02 and _calculate_kelly(prob, odds["away_odds"]) > 0:
                stake = rm.get_max_stake(
                    prob, odds["away_odds"], current_exposure_pct=0.0,
                    input_is_prob=True, sport="nfl",
                    home_team=pred.get("home_team", ""),
                    away_team=pred.get("away_team", ""), market="win")
                if stake > 0:
                    recommendations.append({
                        "type": "win", "side": "away",
                        "home_team": pred.get("home_team", ""),
                        "away_team": pred.get("away_team", ""),
                        "odds": odds["away_odds"],
                        "model_prob": round(prob, 4),
                        "ev": round(away_ev, 4),
                        "kelly_frac": round(_calculate_kelly(prob, odds["away_odds"]), 4),
                        "stake": round(stake, 2),
                        "commence_time": pred.get("commence_time", ""),
                        "league": "NFL",
                    })

        # 让分
        if spread_reliable and odds["spread_home_odds"] and odds["n_bookmakers"] >= 3:
            prob = pred.get("spread_prob", 0)
            spread_ev = prob - 1.0 / odds["spread_home_odds"]
            if spread_ev > 0.02:
                kelly = _calculate_kelly(prob, odds["spread_home_odds"])
                if kelly > 0:
                    stake = rm.get_max_stake(
                        prob, odds["spread_home_odds"], current_exposure_pct=0.0,
                        input_is_prob=True, sport="nfl",
                        home_team=pred.get("home_team", ""),
                        away_team=pred.get("away_team", ""), market="spread")
                    if stake > 0:
                        pt = odds["spread_home_point"]
                        pt_str = f"{pt:+g}" if pt else ""
                        recommendations.append({
                            "type": "spread", "side": "home",
                            "home_team": pred.get("home_team", ""),
                            "away_team": pred.get("away_team", ""),
                            "point": pt,
                            "odds": odds["spread_home_odds"],
                            "model_prob": round(prob, 4),
                            "ev": round(spread_ev, 4),
                            "kelly_frac": round(kelly, 4),
                            "stake": round(stake, 2),
                            "line": f"{pred.get('home_team', '')} {pt_str}",
                            "commence_time": pred.get("commence_time", ""),
                            "league": "NFL",
                        })

                # 客场让分
                prob_away = 1 - prob
                spread_ev_away = prob_away - 1.0 / odds["spread_away_odds"]
                if spread_ev_away > 0.02:
                    kelly = _calculate_kelly(prob_away, odds["spread_away_odds"])
                    if kelly > 0:
                        stake = rm.get_max_stake(
                            prob_away, odds["spread_away_odds"], current_exposure_pct=0.0,
                            input_is_prob=True, sport="nfl",
                            home_team=pred.get("home_team", ""),
                            away_team=pred.get("away_team", ""), market="spread")
                        if stake > 0:
                            pt = odds["spread_away_point"]
                            pt_str = f"{pt:+g}" if pt else ""
                            recommendations.append({
                                "type": "spread", "side": "away",
                                "home_team": pred.get("home_team", ""),
                                "away_team": pred.get("away_team", ""),
                                "point": pt,
                                "odds": odds["spread_away_odds"],
                                "model_prob": round(prob_away, 4),
                                "ev": round(spread_ev_away, 4),
                                "kelly_frac": round(kelly, 4),
                                "stake": round(stake, 2),
                                "line": f"{pred.get('away_team', '')} {pt_str}",
                                "commence_time": pred.get("commence_time", ""),
                                "league": "NFL",
                            })

        # 大小分
        if total_reliable and odds["over_odds"] and odds["n_bookmakers"] >= 3:
            over_prob = pred.get("total_prob", 0)
            over_ev = over_prob - 1.0 / odds["over_odds"]
            if over_ev > 0.02:
                kelly = _calculate_kelly(over_prob, odds["over_odds"])
                if kelly > 0:
                    stake = rm.get_max_stake(
                        over_prob, odds["over_odds"], current_exposure_pct=0.0,
                        input_is_prob=True, sport="nfl",
                        home_team=pred.get("home_team", ""),
                        away_team=pred.get("away_team", ""), market="total")
                    if stake > 0:
                        recommendations.append({
                            "type": "over",
                            "home_team": pred.get("home_team", ""),
                            "away_team": pred.get("away_team", ""),
                            "total_line": odds["total_point"],
                            "odds": odds["over_odds"],
                            "model_prob": round(over_prob, 4),
                            "ev": round(over_ev, 4),
                            "kelly_frac": round(kelly, 4),
                            "stake": round(stake, 2),
                            "commence_time": pred.get("commence_time", ""),
                            "league": "NFL",
                        })

        # 小分
        if total_reliable and odds["under_odds"] and odds["n_bookmakers"] >= 3:
            under_prob = 1 - pred.get("total_prob", 0)
            under_ev = under_prob - 1.0 / odds["under_odds"]
            if under_ev > 0.02:
                kelly = _calculate_kelly(under_prob, odds["under_odds"])
                if kelly > 0:
                    stake = rm.get_max_stake(
                        under_prob, odds["under_odds"], current_exposure_pct=0.0,
                        input_is_prob=True, sport="nfl",
                        home_team=pred.get("home_team", ""),
                        away_team=pred.get("away_team", ""), market="total")
                    if stake > 0:
                        recommendations.append({
                            "type": "under",
                            "home_team": pred.get("home_team", ""),
                            "away_team": pred.get("away_team", ""),
                            "total_line": odds["total_point"],
                            "odds": odds["under_odds"],
                            "model_prob": round(under_prob, 4),
                            "ev": round(under_ev, 4),
                            "kelly_frac": round(kelly, 4),
                            "stake": round(stake, 2),
                            "commence_time": pred.get("commence_time", ""),
                            "league": "NFL",
                        })

    # 补充 sport 字段（虚拟投注需要）
    for r in recommendations:
        r["sport"] = "nfl"

    # 按 EV 排序
    recommendations.sort(key=lambda x: x["ev"], reverse=True)

    # 输出
    logger.info("  ✅ 推荐: %d 条", len(recommendations))
    for r in recommendations:
        h, a = r["home_team"], r["away_team"]
        logger.info("    %s %s vs %s | EV=%+.1f%% Kelly=%.1f%% Stake=¥%.0f Odds=%.2f",
                    r["type"].upper(), h, a, r["ev"] * 100, r["kelly_frac"] * 100, r["stake"], r["odds"])

    # 联合凯利组合优化
    if recommendations:
        try:
            for r in recommendations:
                r["sport"] = "nfl"
            recommendations = rm.batch_optimize(recommendations, bankroll=rm.current_balance)
        except Exception as e:
            logger.warning("  ⚠️ 组合优化跳过: %s", e)

    # 保存
    NFL_RECS_FILE.write_text(json.dumps({
        "date": datetime.now().isoformat(),
        "sport": "nfl",
        "total_games": len(upcoming),
        "recommendations": recommendations,
    }, ensure_ascii=False, indent=2))

    logger.info("  ✅ 推荐已保存至 %s", NFL_RECS_FILE.name)

    # 同步到虚拟投资组合
    if recommendations:
        try:
            from src.dashboard.components.virtual_portfolio import auto_place_bets
            auto_place_bets(recommendations)
            logger.info("  ✅ 已同步 %d 条推荐到虚拟投资组合", len(recommendations))
        except Exception as e:
            logger.warning("  ⚠️ 虚拟投资组合同步失败: %s", e)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
