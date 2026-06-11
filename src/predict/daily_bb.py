#!/usr/bin/env python3
"""篮球每日推荐 — 使用集成模型 + 市场赔率特征。"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger(__name__)

import pandas as pd

from config.settings import DATA_DIR
from fetchers.odds_api import fetch_basketball_odds
from src.predict.ensemble_predictor import EnsemblePredictor
from src.risk.manager import RiskManager
from src.notify.dingtalk import get_notifier
from src.notify.formatter import Recommendation, MarketType, RecommendationFormatter
from src.monitor.clv_tracker import capture_opening_odds
from src.dashboard.components.virtual_portfolio import auto_place_bets
from src.predict.ev_verification import log_prediction
from src.core.recommendation_scorer import RecommendationScorer
from src.predict.alt_line_finder import AltLineFinder

_SCORER = RecommendationScorer()

logger.info("=" * 60)
logger.info("🏀 篮球每日预测 - %s", datetime.now().strftime('%Y-%m-%d %H:%M'))

# ── 模型准确率检查：低于阈值的市场不生成推荐 ──
_MODEL_ACC_MIN = 0.52


def _model_is_reliable(target: str) -> bool:
    """检查模型在测试集上的准确率是否达到推荐门槛。"""
    meta_path = ROOT / "models" / f"model_bb_{target}_ensemble_meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
        acc = meta.get("optimal_threshold", {}).get("test_accuracy", 0)
        if acc >= _MODEL_ACC_MIN:
            return True
        logger.info("  ⚠️ %s 模型准确率 %.1f%% < %.0f%%，跳过该市场的推荐", target, acc * 100, _MODEL_ACC_MIN * 100)
        return False
    except Exception:
        return False


_TOTAL_RELIABLE = _model_is_reliable("total_result")
_SPREAD_RELIABLE = _model_is_reliable("spread_result")
if not _TOTAL_RELIABLE:
    logger.info("  ℹ️ 大小分推荐已禁用")
if not _SPREAD_RELIABLE:
    logger.info("  ℹ️ 让分推荐已禁用")

from src.core.team_names import cn_team
def cn(name): return cn_team(name, sport="nba")

# ── 休赛期检测 ──
from src.core.season_check import has_upcoming_games
if not has_upcoming_games("nba", days_back=2):
    logger.info("🏀 NBA 休赛期，今日跳过")
    sys.exit(0)

# 拉取赔率
odds_data = fetch_basketball_odds(force=True)
logger.info("✅ 实时赔率: %s 场", len(odds_data))

# 备选盘口查找器（多区域赔率 + alternate lines）
_ALT_FINDER = AltLineFinder(odds_data)

# 时间过滤
now = pd.Timestamp.now(tz=timezone.utc)
tomorrow_end = now.replace(hour=23, minute=59, second=59) + pd.Timedelta(days=1)
valid = []
for g in odds_data:
    t = pd.to_datetime(g["commence_time"])
    if now < t <= tomorrow_end:
        valid.append(g)
logger.info("📅 今日即将开始: %s 场", len(valid))

if not valid:
    logger.warning("⚠️ 今日无即将开始的比赛")
    sys.exit(0)

# 加载预测器
predictor = EnsemblePredictor("bb")
predictions = predictor.predict(valid)
logger.info("✅ 预测完成: %s 场", len(predictions))

# ── 联赛校准器 ──
try:
    from src.core.league_calibration import LeagueCalibrator
    _CALIBRATOR = LeagueCalibrator()
    if _CALIBRATOR.get_league_stats():
        logger.info("✅ 联赛校准器: %d 个联赛", len(_CALIBRATOR.get_league_stats()))
except Exception:
    _CALIBRATOR = None

# 风险评估与推荐生成（多候选 → 选最优）
rm = RiskManager()
cb = rm.circuit_breaker_status()
if cb["tripped"]:
    logger.warning("🛑 止损断路器已触发: %s", cb["message"])
    logger.warning("⚠️ 冷却中，跳过所有推荐")
    # 仍发送空推荐通知
    notifier = get_notifier()
    msg = notifier.build_markdown_message(
        "【投注推荐】NBA 推荐已暂停",
        f"✅ NBA推荐分析已完成\n\n🛑 **止损断路器已触发**\n\n{cb['message']}\n\n今日不生成推荐。"
    )
    notifier.send(msg, "NBA推荐暂停通知")
    # 保存空推荐记录
    output = {
        "date": datetime.now().isoformat(),
        "sport": "nba",
        "total_games": len(valid),
        "recommendations": [],
        "circuit_breaker": cb,
    }
    Path(DATA_DIR / "daily_bb_recommendations.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
    sys.exit(0)
else:
    logger.info("✅ 止损断路器状态正常: %s", cb["message"])

recs = []
current_exposure = 0.0
for pred in predictions:
    home, away = pred["home_team"], pred["away_team"]
    n_bookies = pred["n_bookmakers"]
    if n_bookies < 3:
        continue

    has_sharp = pred.get("sharp_available", False)
    mkt_home = pred.get("sharp_home_prob") if has_sharp else pred["market_home_prob"]
    if mkt_home is None:
        mkt_home = pred["market_home_prob"]
    mkt_away = pred.get("sharp_away_prob") if has_sharp else pred["market_away_prob"]
    if mkt_away is None:
        mkt_away = pred["market_away_prob"]

    candidates = []

    # 1. 主胜
    home_ev = pred["win_ev"]
    home_odds = pred["home_odds"]
    if home_ev > 0 and home_odds > 0:
        kelly = (pred["win_prob"] * home_odds - 1) / (home_odds - 1) if home_odds > 1 else 0
        stake = rm.get_max_stake(pred["win_prob"], home_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
        if stake > 5:
            candidates.append({
                "type": "主胜", "side": "home", "odds": home_odds,
                "model_prob": pred["win_prob"], "mkt_prob": mkt_home,
                "ev": home_ev, "stake": stake, "recommended_bookmaker": pred.get("recommended_bookmaker", ""),
                "market_key": "h2h",
            })

    # 2. 客胜 (NEW)
    away_odds = pred["away_odds"]
    away_model_prob = 1.0 - pred["win_prob"]
    away_ev = away_model_prob - mkt_away
    if away_ev > 0 and away_odds > 0:
        kelly = (away_model_prob * away_odds - 1) / (away_odds - 1) if away_odds > 1 else 0
        stake = rm.get_max_stake(away_model_prob, away_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
        if stake > 5:
            candidates.append({
                "type": "客胜", "side": "away", "odds": away_odds,
                "model_prob": away_model_prob, "mkt_prob": mkt_away,
                "ev": away_ev, "stake": stake,
                "recommended_bookmaker": "", "market_key": "h2h",
            })

    # 3. 主队让分
    spread_prob = pred.get("spread_result_prob")
    spread_odds = pred.get("spread_odds", 0)
    spread_pt = pred.get("spread_point")
    if _SPREAD_RELIABLE and spread_prob is not None and spread_odds > 0 and spread_pt is not None:
        spread_mkt = 0.5  # 让分市场近似 50/50
        spread_ev = spread_prob - spread_mkt
        if spread_ev > 0:
            kelly = (spread_prob * spread_odds - 1) / (spread_odds - 1) if spread_odds > 1 else 0
            stake = rm.get_max_stake(spread_prob, spread_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
            if stake > 5:
                pt_str = f"{spread_pt:+.1f}"
                candidates.append({
                    "type": f"主队让分 {pt_str}", "side": "home_spread",
                    "odds": spread_odds, "model_prob": spread_prob, "mkt_prob": spread_mkt,
                    "ev": spread_ev, "stake": stake,
                    "recommended_bookmaker": pred.get("spread_bookmaker", ""),
                    "market_key": "spread",
                })

    # 4. 客队让分
    if _SPREAD_RELIABLE and spread_prob is not None and spread_odds > 0 and spread_pt is not None:
        away_spread_prob = 1.0 - spread_prob
        away_spread_ev = away_spread_prob - 0.5
        if away_spread_ev > 0:
            kelly = (away_spread_prob * spread_odds - 1) / (spread_odds - 1) if spread_odds > 1 else 0
            stake = rm.get_max_stake(away_spread_prob, spread_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
            if stake > 5:
                pt_str = f"{-spread_pt:+.1f}" if spread_pt else "0.0"
                candidates.append({
                    "type": f"客队让分 {pt_str}", "side": "away_spread",
                    "odds": spread_odds, "model_prob": away_spread_prob, "mkt_prob": 0.5,
                    "ev": away_spread_ev, "stake": stake,
                    "recommended_bookmaker": pred.get("spread_bookmaker", ""),
                    "market_key": "spread",
                })

    # 5. 大球
    total_prob = pred.get("total_result_prob")
    total_odds = pred.get("total_odds", 0)
    total_pt = pred.get("total_point")
    if _TOTAL_RELIABLE and total_prob is not None and (total_odds or 0) > 0 and total_pt is not None:
        total_mkt = 0.5
        over_ev = total_prob - total_mkt
        if over_ev > 0:
            kelly = (total_prob * total_odds - 1) / (total_odds - 1) if total_odds > 1 else 0
            stake = rm.get_max_stake(total_prob, total_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
            if stake > 5:
                candidates.append({
                    "type": f"大 {total_pt}", "side": "over",
                    "odds": total_odds, "model_prob": total_prob, "mkt_prob": total_mkt,
                    "ev": over_ev, "stake": stake,
                    "recommended_bookmaker": pred.get("total_bookmaker", ""),
                    "market_key": "total",
                })

    # 6. 小球
    if _TOTAL_RELIABLE and total_prob is not None and (total_odds or 0) > 0 and total_pt is not None:
        under_prob = 1.0 - total_prob
        under_ev = under_prob - 0.5
        if under_ev > 0:
            kelly = (under_prob * total_odds - 1) / (total_odds - 1) if total_odds > 1 else 0
            stake = rm.get_max_stake(under_prob, total_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
            if stake > 5:
                candidates.append({
                    "type": f"小 {total_pt}", "side": "under",
                    "odds": total_odds, "model_prob": under_prob, "mkt_prob": 0.5,
                    "ev": under_ev, "stake": stake,
                    "recommended_bookmaker": pred.get("total_bookmaker", ""),
                    "market_key": "total",
                })

    # ── 备选盘口检查：寻找比标准线更高 EV 的 alternate lines ──
    try:
        # 让分备选（find_best_spread 的 model_prob 始终用主队概率）
        if _SPREAD_RELIABLE and spread_prob is not None and spread_pt is not None:
            for side_key, side_label in [
                ("home", "主队让分(备选)"),
                ("away", "客队让分(备选)"),
            ]:
                alt = _ALT_FINDER.find_best_spread(home, away, spread_prob, spread_pt, side=side_key)
                if alt and alt["ev"] > 0:
                    stake = rm.get_max_stake(alt["adj_prob"], alt["odds"], current_exposure, input_is_prob=True)
                    if stake > 5:
                        candidates.append({
                            "type": f"{side_label} {alt['line']:+.1f}",
                            "side": f"{side_key}_spread_alt",
                            "odds": alt["odds"],
                            "model_prob": alt["adj_prob"],
                            "mkt_prob": 0.5,
                            "ev": alt["ev"],
                            "stake": stake,
                            "recommended_bookmaker": "",
                            "market_key": "spread_alt",
                        })

        # 总分备选（find_best_total 的 model_prob 始终用大球概率）
        if _TOTAL_RELIABLE and total_prob is not None and total_pt is not None:
            for side_key, side_label in [
                ("over", f"大(备选)"),
                ("under", f"小(备选)"),
            ]:
                alt = _ALT_FINDER.find_best_total(home, away, total_prob, total_pt, side=side_key)
                if alt and alt["ev"] > 0:
                    stake = rm.get_max_stake(alt["adj_prob"], alt["odds"], current_exposure, input_is_prob=True)
                    if stake > 5:
                        candidates.append({
                            "type": f"{side_label} {alt['line']:.1f}",
                            "side": f"{side_key}_total_alt",
                            "odds": alt["odds"],
                            "model_prob": alt["adj_prob"],
                            "mkt_prob": 0.5,
                            "ev": alt["ev"],
                            "stake": stake,
                            "recommended_bookmaker": "",
                            "market_key": "total_alt",
                        })
    except Exception as exc:
        logger.debug("备选盘口检查失败: %s", exc)

    if not candidates:
        continue

    # 选 EV 最高的候选（备选盘口如 EV 更高可替代标准盘口）
    best = max(candidates, key=lambda x: x["ev"])

    # 联赛校准（如果可用）
    if _CALIBRATOR is not None:
        league = "NBA"
        cal_prob = _CALIBRATOR.calibrate(league, best["model_prob"])
        if cal_prob != best["model_prob"]:
            logger.debug("  📐 NBA 校准: %.1f%% → %.1f%%", best["model_prob"] * 100, cal_prob * 100)
            best["model_prob"] = cal_prob

    match_time = pd.to_datetime(pred["commence_time"]).tz_convert("Asia/Shanghai")
    time_str = match_time.strftime("%m/%d %H:%M")
    logger.info("✅ %s vs %s | %s | 模型:%s 市场:%s 赔率:%.2f EV:%s 注额:%.0f ⏰%s",
                cn(home), cn(away), best['type'],
                "{:.1%}".format(best['model_prob']), "{:.1%}".format(best['mkt_prob']),
                best['odds'], "{:+.1%}".format(best['ev']), best['stake'], time_str)

    # ── 推荐质量评分过滤 ──
    merged = {**pred, **best, "sport": "nba", "league": "NBA"}
    sq = _SCORER.score(merged, market_type="胜负")
    score, tier = sq["score"], sq["tier"]
    logger.info("  📊 质量评分: %.1f (%s) SM指数: %.1f",
                score, tier, sq.get("smart_money_index", 0))

    if tier == "low":
        logger.info("  ⏭️ 质量分 %.1f < 60, 跳过推荐", score)
        continue
    elif tier == "medium":
        best["stake"] = round(best["stake"] * 0.5, 2)
        logger.info("  ⚖️ 中等质量, 半仓: ¥%.0f", best["stake"])

    recs.append({**pred, **best, "time_str": time_str,
                 "home_cn": cn(home), "away_cn": cn(away),
                 "sport": "nba", "league": "NBA", "market": best["type"],
                 "quality_score": score, "quality_tier": tier})
    log_prediction(
        sport="nba", league="NBA",
        home_team=home, away_team=away,
        market_type="胜负", market_detail=best["type"],
        odds=best["odds"], model_prob=best["model_prob"],
        market_prob=best["mkt_prob"], ev=best["ev"],
        stake=best["stake"], match_time=pred["commence_time"],
        source="daily", home_team_cn=cn(home), away_team_cn=cn(away),
        sharp_prob=pred.get("sharp_home_prob"),
        quality_score=score, quality_tier=tier,
        model_version=pred.get("model_version", ""),
        n_bookmakers=pred.get("n_bookmakers", 0),
        scorer_breakdown=json.dumps(sq["breakdown"], ensure_ascii=False),
    )
    current_exposure += best["stake"] / max(rm.current_balance, 1.0)

    # 每日刷新一次市场效率数据
    _SCORER.reload_efficiency()

# ── NBA 球员表现预测 ──
_player_projections = []
try:
    from src.predict.player_projection import predict_game_player_props, format_player_report
    from src.features.player_pipeline import TEAM_NAME_TO_ID
    seen_matchups = set()
    for pred in predictions:
        home, away = pred["home_team"], pred["away_team"]
        matchup = f"{home} @ {away}"
        if matchup in seen_matchups:
            continue
        seen_matchups.add(matchup)
        if home in TEAM_NAME_TO_ID and away in TEAM_NAME_TO_ID:
            players = predict_game_player_props(home, away)
            if players:
                _player_projections.extend(players)
                logger.info("🏀 球员投影: %s vs %s — %d 人", cn(home), cn(away), len(players))
                for p in players[:5]:
                    logger.info("  %s: PTS %.1f REB %.1f AST %.1f (conf=%.2f)",
                                p["name"], p["PTS"], p["REB"], p["AST"], p["confidence"])
    if _player_projections:
        proj_path = DATA_DIR / "daily_bb_player_props.json"
        proj_path.write_text(
            json.dumps(_player_projections, ensure_ascii=False, indent=2)
        )
        logger.info("✅ 球员投影已保存: %s (%d 人)", proj_path, len(_player_projections))
except Exception as e:
    logger.debug("球员投影失败: %s", e)

# 联合凯利组合优化（替代逐个调 get_max_stake 的启发式分散调整）
if recs:
    recs = rm.batch_optimize(recs)

# 钉钉通知
notifier = get_notifier()
if not recs:
    logger.warning("⚠️ 今日无符合策略的推荐")
    msg = notifier.build_markdown_message(
        "【投注推荐】无NBA推荐",
        "✅ 已完成NBA推荐分析\n\n⚠️ 今日未检测到符合策略的正期望值投注。"
    )
    notifier.send(msg, "无NBA推荐通知")
else:
    rec_objs = []
    for r in recs:
        rec_objs.append(Recommendation(
            sport="NBA", league="NBA",
            home_team=cn(r["home_team"]), away_team=cn(r["away_team"]),
            market_type=MarketType.WIN, market_detail=r["type"],
            odds=r["odds"], model_prob=r["model_prob"],
            market_prob=r["mkt_prob"], ev=r["ev"],
            stake=r["stake"],
            match_time=pd.to_datetime(r["commence_time"]),
            bookmaker=r.get("recommended_bookmaker", ""),
        ))
    formatter = RecommendationFormatter()
    msg_text = formatter.format_recommendations_for_dingtalk(rec_objs, title="NBA 每日推荐", sport_name="NBA")
    msg = notifier.build_markdown_message("【投注推荐】NBA 推荐", msg_text)
    notifier.send(msg, f"{len(rec_objs)}条NBA推荐")

# 记录开盘价（用于 CLV 追踪）
for r in recs:
    match_key = f"{r['home_team']} @ {r['away_team']} {pd.to_datetime(r['commence_time']).strftime('%Y-%m-%d')}"
    capture_opening_odds(match_key, r.get("market_key", "h2h"), r["odds"],
                         r.get("recommended_bookmaker", ""), "NBA")

# 保存推荐记录
output = {
    "date": datetime.now().isoformat(),
    "sport": "nba",
    "total_games": len(valid),
    "recommendations": [
        {k: r[k] for k in ("home_team", "away_team", "type", "model_prob", "mkt_prob", "odds", "ev", "stake", "commence_time", "recommended_bookmaker")}
        for r in recs
    ],
}
Path(DATA_DIR / "daily_bb_recommendations.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
logger.info("✅ 推荐已保存至 data/storage/daily_bb_recommendations.json")

# 同步到虚拟投注组合
auto_place_bets(recs)
logger.info("✅ 已同步 %d 条推荐到虚拟投注组合", len(recs))
