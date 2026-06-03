#!/usr/bin/env python3
"""足球每日推荐 — 使用集成模型 + 市场赔率特征。"""
import sys, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger(__name__)

import numpy as np
import pandas as pd

from config.settings import DATA_DIR
from fetchers.odds_api import fetch_football_odds
from src.predict.ensemble_predictor import EnsemblePredictor
from src.risk.manager import RiskManager
from src.notify.dingtalk import get_notifier
from src.notify.formatter import Recommendation, MarketType, RecommendationFormatter
from src.monitor.clv_tracker import capture_opening_odds
from src.core.team_names import LEAGUE_CN
from src.dashboard.components.virtual_portfolio import auto_place_bets

# ── Dixon-Coles 比分模型 + 亚洲盘口 ──
_DC_MODEL = None
_DC_MODEL_PATH = ROOT / "models" / "dixon_coles_model.json"
if _DC_MODEL_PATH.exists():
    try:
        from src.models.dixon_coles import DixonColesModel
        _DC_MODEL = DixonColesModel()
        _DC_MODEL.load(str(_DC_MODEL_PATH))
        logger.info("  ✅ Dixon-Coles 比分模型已加载 (%d 支球队)", _DC_MODEL.n_teams)
    except Exception as e:
        logger.warning("  ⚠️ Dixon-Coles 加载失败: %s", e)

from src.betting.asian_handicap import extract_ah_odds, compute_ah_ev

logger.info("=" * 60)
logger.info("⚽ 足球每日预测 - %s", datetime.now().strftime('%Y-%m-%d %H:%M'))

# ── 模型准确率检查 ──
_MODEL_ACC_MIN = 0.52


def _model_is_reliable(target: str) -> bool:
    meta_path = ROOT / "models" / f"model_fb_{target}_ensemble_meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
        acc = meta.get("optimal_threshold", {}).get("test_accuracy", 0)
        if acc >= _MODEL_ACC_MIN:
            return True
        logger.info("  ⚠️ FB %s 模型准确率 %.1f%% < %.0f%%，跳过该市场的推荐", target, acc * 100, _MODEL_ACC_MIN * 100)
        return False
    except Exception:
        return False


_TOTAL_RELIABLE = _model_is_reliable("total_result")
if not _TOTAL_RELIABLE:
    logger.info("  ℹ️ 足球大小球推荐已禁用（模型准确率不足）")

from src.core.team_names import cn_team
def cn(name): return cn_team(name, sport="football")


def _dc_knows_teams(home: str, away: str) -> bool:
    """检查 DC 模型是否拥有这两支球队的参数，避免使用全局先验。"""
    if _DC_MODEL is None or not _DC_MODEL.fitted:
        return False
    return home in _DC_MODEL.attack_params and away in _DC_MODEL.attack_params

# 拉取赔率
odds_data = fetch_football_odds(force=True)
logger.info("✅ 实时赔率: %s 场", len(odds_data))

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
predictor = EnsemblePredictor("fb")
predictions = predictor.predict(valid)
logger.info("✅ 预测完成: %s 场", len(predictions))

# 风险评估与推荐生成
rm = RiskManager()
recs = []
current_exposure = 0.0
for pred in predictions:
    home, away = pred["home_team"], pred["away_team"]
    home_odds, away_odds = pred["home_odds"], pred["away_odds"]
    # 使用 sharp consensus 作为市场参考概率
    has_sharp = pred.get("sharp_available", False)
    mkt_home = pred.get("sharp_home_prob") if has_sharp else pred["market_home_prob"]
    if mkt_home is None:
        mkt_home = pred["market_home_prob"]
    mkt_draw = pred.get("sharp_draw_prob") if has_sharp else pred["market_draw_prob"]
    if mkt_draw is None:
        mkt_draw = pred["market_draw_prob"]
    mkt_away = pred.get("sharp_away_prob") if has_sharp else pred["market_away_prob"]
    if mkt_away is None:
        mkt_away = pred["market_away_prob"]

    # ── 模型-市场一致性格挡 ──
    # 若模型原始概率与市场隐含概率偏差 > 30pp，模型对该对阵无训练数据
    raw_win = pred.get("win_raw", pred["win_prob"])
    deviation = abs(raw_win - pred["market_home_prob"])
    if deviation > 0.30:
        logger.info("  ⏭️ %s vs %s: 模型-市场偏差%.0fpp，跳过未知对阵",
                    home, away, deviation * 100)
        continue

    # 检查各目标
    candidates = []

    # 主胜
    home_ev = pred["win_ev"]
    if home_ev > 0 and home_odds > 0 and pred["n_bookmakers"] >= 3:
        kelly = (pred["win_prob"] * home_odds - 1) / (home_odds - 1) if home_odds > 1 else 0
        stake = rm.get_max_stake(pred["win_prob"], home_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
        can_bet, _ = rm.can_place_bet(stake, 0.0)
        if stake > 5 and can_bet:
            candidates.append({
                "type": "主胜", "odds": home_odds, "model_prob": pred["win_prob"],
                "mkt_prob": mkt_home, "ev": home_ev, "stake": stake,
            })

    # ── 客胜 ──
    # 优先使用 DC 模型（需包含两队）的 3-way 概率，避免二元模型推导偏差
    away_model = None
    if _dc_knows_teams(home, away):
        try:
            dc_pred = _DC_MODEL.predict(home, away)
            if "error" not in dc_pred and dc_pred.get("away", 0) > 0:
                away_model = dc_pred["away"]
        except Exception:
            pass
    if away_model is None:
        # 从收缩后的主胜概率推导（已包含市场信息）
        win_shrunk = pred["win_prob"]
        away_model = 1.0 - win_shrunk - mkt_draw
        away_model = np.clip(away_model, 0.001, 0.999)

    away_ev = away_model - mkt_away
    if away_ev > 0 and away_odds > 0 and pred["n_bookmakers"] >= 3:
        kelly = (away_model * away_odds - 1) / (away_odds - 1) if away_odds > 1 else 0
        stake = rm.get_max_stake(away_model, away_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
        can_bet, _ = rm.can_place_bet(stake, 0.0)
        if stake > 5 and can_bet:
            candidates.append({
                "type": "客胜", "odds": away_odds, "model_prob": away_model,
                "mkt_prob": mkt_away, "ev": away_ev, "stake": stake,
            })

    # ── 平局候选 ──
    # 只在 DC 模型拥有两队参数时才使用（否则返回全局先验，无参考价值）
    if mkt_draw > 0 and _dc_knows_teams(home, away):
        try:
            dc_pred = _DC_MODEL.predict(home, away)
            if "error" not in dc_pred:
                draw_model_prob = dc_pred.get("draw", 0)
                if draw_model_prob > 0:
                    draw_odds = 1.0 / mkt_draw
                    draw_ev = draw_model_prob - mkt_draw
                    if draw_ev > 0 and draw_odds > 0:
                        kelly = (draw_model_prob * draw_odds - 1) / (draw_odds - 1) if draw_odds > 1 else 0
                        stake_draw = rm.get_max_stake(draw_model_prob, draw_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
                        if stake_draw > 5:
                            candidates.append({
                                "type": "平局", "odds": round(draw_odds, 2),
                                "model_prob": draw_model_prob, "mkt_prob": mkt_draw,
                                "ev": draw_ev, "stake": stake_draw,
                            })
        except Exception:
            pass

    # ── 大小球候选 ──
    total_prob = pred.get("total_result_prob")
    total_odds = pred.get("total_odds", 0)
    total_pt = pred.get("total_point")
    if _TOTAL_RELIABLE and total_prob is not None and total_odds > 0 and total_pt is not None:
        under_prob = 1.0 - total_prob
        under_ev = under_prob - 0.5
        if under_ev > 0:
            kelly = (under_prob * total_odds - 1) / (total_odds - 1) if total_odds > 1 else 0
            stake_under = rm.get_max_stake(under_prob, total_odds, current_exposure, input_is_prob=True) if kelly > 0 else 0
            if stake_under > 5:
                candidates.append({
                    "type": f"小 {total_pt}", "odds": total_odds,
                    "model_prob": under_prob, "mkt_prob": 0.5,
                    "ev": under_ev, "stake": stake_under,
                })

    # ── 亚洲盘口候选 ──
    if _dc_knows_teams(home, away):
        try:
            ah_odds_list = extract_ah_odds([g for g in valid if g["home_team"] == home and g["away_team"] == away])
            for ah in ah_odds_list:
                dc_pred = _DC_MODEL.predict_asian_handicap(home, away, ah["handicap"])
                if "error" not in dc_pred:
                    model_cover = dc_pred["home_cover"]
                    ev_result = compute_ah_ev(ah, model_cover)
                    if ev_result.get("home_ev", 0) > 0.05:
                        ev_val = ev_result["home_ev"]
                        ah_odds_val = ah["home_odds"]
                        kelly_val = (model_cover * ah_odds_val - 1) / (ah_odds_val - 1) if ah_odds_val > 1 else 0
                        stake_val = rm.get_max_stake(model_cover, ah_odds_val, current_exposure, input_is_prob=True) if kelly_val > 0 else 0
                        can_bet, _ = rm.can_place_bet(stake_val, 0.0)
                        if stake_val > 5 and can_bet:
                            hcp_str = f"{ah['handicap']:+.1f}"
                            candidates.append({
                                "type": f"亚洲让球 {hcp_str}", "odds": ah_odds_val,
                                "model_prob": model_cover,
                                "mkt_prob": 1.0 / ah_odds_val,
                                "ev": ev_val, "stake": stake_val,
                            })
        except Exception:
            pass

    if not candidates:
        continue

    # 选 EV 最高的
    best = max(candidates, key=lambda x: x["ev"])
    match_time = pd.to_datetime(pred["commence_time"]).tz_convert("Asia/Shanghai")
    time_str = match_time.strftime("%m/%d %H:%M")
    logger.info("⚽ %s vs %s | %s | 模型:%s 市场:%s 赔率:%.2f EV:%s 注额:%.0f ⏰%s", cn(home), cn(away), best['type'], "{:.1%}".format(best['model_prob']), "{:.1%}".format(best['mkt_prob']), best['odds'], "{:+.1%}".format(best['ev']), best['stake'], time_str)
    recs.append({**pred, **best, "time_str": time_str,
                 "home_cn": cn(home), "away_cn": cn(away),
                 "sport": "football",
                 "league": LEAGUE_CN.get(pred.get("sport_key", ""), "国际足球"),
                 "market": best["type"]})
    current_exposure += best["stake"] / max(rm.current_balance, 1.0)

# 钉钉通知
notifier = get_notifier()
if not recs:
    logger.warning("⚠️ 今日无符合策略的推荐")
    msg = notifier.build_markdown_message(
        "【投注推荐】无足球推荐",
        "✅ 已完成足球推荐分析\n\n⚠️ 今日未检测到符合策略的正期望值投注。"
    )
    notifier.send(msg, "无足球推荐通知")
else:
    rec_objs = []
    for r in recs:
        rec_objs.append(Recommendation(
            sport="足球", league="国际足球",
            home_team=cn(r["home_team"]), away_team=cn(r["away_team"]),
            market_type=MarketType.H2H, market_detail=r["type"],
            odds=r["odds"], model_prob=r["model_prob"],
            market_prob=r["mkt_prob"], ev=r["ev"],
            stake=r["stake"],
            match_time=pd.to_datetime(r["commence_time"]),
            bookmaker=r.get("recommended_bookmaker", ""),
        ))
    formatter = RecommendationFormatter()
    msg_text = formatter.format_recommendations_for_dingtalk(rec_objs, title="足球 每日推荐", sport_name="足球")
    msg = notifier.build_markdown_message("【投注推荐】足球 推荐", msg_text)
    notifier.send(msg, f"{len(rec_objs)}条足球推荐")

# 记录开盘价（用于 CLV 追踪）
for r in recs:
    match_key = f"{r['home_team']} @ {r['away_team']} {pd.to_datetime(r['commence_time']).strftime('%Y-%m-%d')}"
    market_key = "h2h" if "亚洲" not in str(r.get("type", "")) else "asian_handicap"
    capture_opening_odds(match_key, market_key, r["odds"], r.get("recommended_bookmaker", ""), "足球")

# 保存推荐记录（含亚洲盘口）
output = {
    "date": datetime.now().isoformat(),
    "sport": "football",
    "total_games": len(valid),
    "dc_model_loaded": _DC_MODEL is not None and _DC_MODEL.fitted,
    "recommendations": [
        {k: r[k] for k in ("home_team", "away_team", "type", "model_prob", "mkt_prob", "odds", "ev", "stake", "commence_time")}
        for r in recs
    ],
    "asian_handicap_count": sum(1 for r in recs if "亚洲" in str(r.get("type", ""))),
}
Path(DATA_DIR / "daily_fb_recommendations.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
logger.info("✅ 推荐已保存至 data/storage/daily_fb_recommendations.json")

# 同步到虚拟投注组合
auto_place_bets(recs)
logger.info("✅ 已同步 %d 条推荐到虚拟投注组合", len(recs))
