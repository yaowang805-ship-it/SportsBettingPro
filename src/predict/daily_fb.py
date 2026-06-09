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
from src.predict.ev_verification import log_prediction

# ── Dixon-Coles 比分模型（贝叶斯优先，点估计备选） ──
_DC_MODEL = None
_BAYES_DC_PATH = ROOT / "models" / "bayesian_dc_model.json"
_LEGACY_DC_PATH = ROOT / "models" / "dixon_coles_model.json"

if _BAYES_DC_PATH.exists():
    try:
        from src.models.bayesian_dixon_coles import BayesianDixonColes
        _DC_MODEL = BayesianDixonColes()
        _DC_MODEL.load(str(_BAYES_DC_PATH))
        logger.info("  ✅ 贝叶斯 Dixon-Coles 已加载 (%d 支球队, σ_att=%.3f, σ_def=%.3f)",
                    _DC_MODEL.n_teams, _DC_MODEL.sigma_attack, _DC_MODEL.sigma_defense)
    except Exception as e:
        logger.warning("  ⚠️ 贝叶斯 DC 加载失败: %s", e)
        _DC_MODEL = None

if _DC_MODEL is None and _LEGACY_DC_PATH.exists():
    try:
        from src.models.dixon_coles import DixonColesModel
        _DC_MODEL = DixonColesModel()
        _DC_MODEL.load(str(_LEGACY_DC_PATH))
        logger.info("  ✅ 传统 Dixon-Coles 已加载 (%d 支球队)", _DC_MODEL.n_teams)
    except Exception as e:
        logger.warning("  ⚠️ 传统 DC 加载失败: %s", e)

# ── 泊松模型（用于混合+任意线大小球） ──
_POISSON_MODEL = None
if (ROOT / "models/poisson_model.pkl").exists():
    try:
        from src.models.poisson_model import PoissonGoalModel
        _POISSON_MODEL = PoissonGoalModel()
        _POISSON_MODEL.load(str(ROOT / "models/poisson_model.pkl"))
        logger.info("  ✅ 泊松模型已加载 (%d 支球队)", _POISSON_MODEL.n_teams)
    except Exception as e:
        logger.warning("  ⚠️ 泊松模型加载失败: %s", e)

from src.betting.asian_handicap import extract_ah_odds, compute_ah_ev

logger.info("=" * 60)
logger.info("⚽ 足球每日预测 - %s", datetime.now().strftime('%Y-%m-%d %H:%M'))

# ── 数据刷新（ESPN 免费源补充扩展联赛） ──
try:
    from fetchers.data_sync import supplement_football_espn
    supplement_football_espn()
except ImportError:
    logger.debug("  ESPN 补充模块不可用，跳过")
except Exception as e:
    logger.warning("  ⚠️ ESPN 数据补充失败: %s", e)

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


from src.core.team_names import cn_team
def cn(name): return cn_team(name, sport="football")


def _dc_knows_teams(home: str, away: str) -> bool:
    """检查 DC 模型是否拥有这两支球队的参数，避免使用全局先验。"""
    if _DC_MODEL is None or not _DC_MODEL.fitted:
        return False
    # 尝试精确匹配；若 odds API 名不含 "FC" 但模型名含 "FC"，自动补全
    for name in (home, away):
        if name in _DC_MODEL.attack_params:
            continue
        # 追加 " FC" 重试
        if f"{name} FC" in _DC_MODEL.attack_params:
            continue
        return False
    return True


def _dc_name(odds_name: str) -> str:
    """将 odds API 球队名转为 DC 模型使用的名称。"""
    if odds_name in _DC_MODEL.attack_params:
        return odds_name
    fc_name = f"{odds_name} FC"
    if fc_name in _DC_MODEL.attack_params:
        return fc_name
    return odds_name


_FB_KNOWN_TEAMS = None

def _teams_in_training_data(home: str, away: str) -> bool:
    """检查至少一支球队在训练数据中，避免先验噪音。"""
    global _FB_KNOWN_TEAMS
    if _FB_KNOWN_TEAMS is None:
        try:
            import pandas as pd
            csv_path = ROOT / "data" / "storage" / "football_history.csv"
            hist = pd.read_csv(csv_path)
            _FB_KNOWN_TEAMS = frozenset(
                str(c).strip() for c in pd.concat([hist['home'], hist['away']]).dropna().unique()
            )
        except Exception:
            _FB_KNOWN_TEAMS = frozenset()
    return home in _FB_KNOWN_TEAMS or away in _FB_KNOWN_TEAMS

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

# ── 泊松模型混合（如可用） ──
if _POISSON_MODEL is not None and _POISSON_MODEL.fitted:
    n_blended = 0
    for pred in predictions:
        poisson_pred = _POISSON_MODEL.predict_proba(pred["home_team"], pred["away_team"])
        if "error" not in poisson_pred:
            pred["win_prob"] = 0.5 * pred["win_prob"] + 0.5 * poisson_pred["home_win"]
            n_blended += 1
    if n_blended > 0:
        logger.info("✅ 泊松模型混合: %d 场", n_blended)

# ── 联赛校准器 ──
try:
    from src.core.league_calibration import LeagueCalibrator
    _CALIBRATOR = LeagueCalibrator()
    if _CALIBRATOR.get_league_stats():
        logger.info("✅ 联赛校准器: %d 个联赛", len(_CALIBRATOR.get_league_stats()))
except Exception:
    _CALIBRATOR = None

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
    # 若模型原始概率与市场隐含概率偏差 > 25pp，模型对该对阵无训练数据
    raw_win = pred.get("win_raw", pred["win_prob"])
    deviation = abs(raw_win - pred["market_home_prob"])
    if deviation > 0.25:
        logger.info("  ⏭️ %s vs %s: 模型-市场偏差%.0fpp，跳过未知对阵",
                    home, away, deviation * 100)
        continue

    # ── 训练数据覆盖检查 ──
    # 若双方均不在训练数据中，模型输出仅为先验概率，无分析价值
    if not _teams_in_training_data(home, away):
        logger.info("  ⏭️ %s vs %s: 双方均无历史数据，跳过",
                    home, away)
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
    # 仅当 DC 3-way 模型拥有两队参数时启用。二元模型无法推导客胜概率。
    if mkt_draw > 0 and _dc_knows_teams(home, away):
        try:
            dc_pred = _DC_MODEL.predict(_dc_name(home), _dc_name(away))
            if "error" not in dc_pred:
                away_model = dc_pred.get("away_win", dc_pred.get("away", 0))
                if away_model > 0:
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
        except Exception:
            pass

    # ── 平局候选 ──
    # 只在 DC 模型拥有两队参数时才使用（否则返回全局先验，无参考价值）
    if mkt_draw > 0 and _dc_knows_teams(home, away):
        try:
            dc_pred = _DC_MODEL.predict(_dc_name(home), _dc_name(away))
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

    # ── 大小球候选（泊松模型支持任意线，接近 2.5 时与 ensemble 混合）──
    total_odds = pred.get("total_odds", 0)
    total_pt = pred.get("total_point")
    if total_odds > 0 and total_pt is not None:
        total_prob = None
        total_source = "none"

        # 泊松模型：任意盘口线
        if _POISSON_MODEL is not None and _POISSON_MODEL.fitted:
            poisson_ou = _POISSON_MODEL.predict_over_under(home, away, total_pt)
            if "error" not in poisson_ou:
                total_prob = poisson_ou["over"]
                total_source = "poisson"

        # 接近 2.5 时与 ensemble 混合
        ensemble_total = pred.get("total_result_prob")
        if ensemble_total is not None and abs(total_pt - 2.5) <= 0.3:
            if total_source == "poisson":
                total_prob = 0.5 * total_prob + 0.5 * ensemble_total
                total_source = "blended"
            else:
                total_prob = ensemble_total
                total_source = "ensemble"

        if total_prob is not None:
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
                dc_pred = _DC_MODEL.predict_asian_handicap(_dc_name(home), _dc_name(away), ah["handicap"])
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

    # 联赛校准（如果可用）
    if _CALIBRATOR is not None:
        league = pred.get("sport_key", "")
        cal_prob = _CALIBRATOR.calibrate(league, best["model_prob"])
        if cal_prob != best["model_prob"]:
            logger.debug("  📐 %s 校准: %.1f%% → %.1f%%", league, best["model_prob"] * 100, cal_prob * 100)
            best["model_prob"] = cal_prob

    match_time = pd.to_datetime(pred["commence_time"]).tz_convert("Asia/Shanghai")
    time_str = match_time.strftime("%m/%d %H:%M")
    logger.info("⚽ %s vs %s | %s | 模型:%s 市场:%s 赔率:%.2f EV:%s 注额:%.0f ⏰%s", cn(home), cn(away), best['type'], "{:.1%}".format(best['model_prob']), "{:.1%}".format(best['mkt_prob']), best['odds'], "{:+.1%}".format(best['ev']), best['stake'], time_str)
    recs.append({**pred, **best, "time_str": time_str,
                 "home_cn": cn(home), "away_cn": cn(away),
                 "sport": "football",
                 "league": LEAGUE_CN.get(pred.get("sport_key", ""), "国际足球"),
                 "market": best["type"]})
    log_prediction(
        sport="football", league=pred.get("sport_key", ""),
        home_team=home, away_team=away,
        market_type="胜负", market_detail=best["type"],
        odds=best["odds"], model_prob=best["model_prob"],
        market_prob=best["mkt_prob"], ev=best["ev"],
        stake=best["stake"], match_time=pred["commence_time"],
        source="daily", home_team_cn=cn(home), away_team_cn=cn(away),
        sharp_prob=pred.get("sharp_home_prob"),
    )
    current_exposure += best["stake"] / max(rm.current_balance, 1.0)

# 联合凯利组合优化（替代逐个调 get_max_stake 的启发式分散调整）
if recs:
    recs = rm.batch_optimize(recs)

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
