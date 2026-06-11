"""投注预测跟踪系统 — 记录、结算、绩效分析。

职业博彩团队的第一原则：不测量就不存在。
本模块记录每条推荐的全生命周期：
  推荐 → 已投注 → 已结算（赢/输） → 绩效统计
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict

from config.logging_config import get_logger
logger = get_logger(__name__)

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "data" / "storage"
LOG_FILE = LOG_DIR / "prediction_log.csv"
PERF_FILE = LOG_DIR / "performance_summary.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# CSV 列定义（全系统统一 schema — ev_verification.py 引用此列表）
COLUMNS = [
    "id",               # 唯一 ID: {date}_{sport}_{market}_{seq}
    "timestamp",        # 推荐生成时间
    "date",             # 推荐日期
    "sport",            # nba / football / world_cup
    "league",           # NBA / 英超 / 西甲 ...
    "home_team",        # 英文队名（用于匹配结算）
    "away_team",        # 英文队名
    "home_team_cn",     # 中文队名（用于推送显示）
    "away_team_cn",     # 中文队名
    "home_team_en",     # 英文队名（用于 CLV 追踪回查）
    "away_team_en",     # 英文队名
    "market_type",      # WIN / H2H / SPREAD / TOTAL
    "market_detail",    # 主胜 / 主队 -5.5分 / 大 217.5
    "odds",             # 投注赔率
    "model_prob",       # 模型概率
    "market_prob",      # 市场隐含概率
    "ev",               # 期望值
    "sharp_prob",       # sharp consensus 市场概率（用于 edge 归因）
    "stake",            # 建议注额
    "match_time",       # 比赛时间
    "source",           # 推荐来源 (global_top5 / daily_bb / daily_fb / daily_wc)
    "quality_score",    # 推荐质量评分
    "quality_tier",     # 质量等级 (A/B/C/D)
    "model_version",    # 模型版本标识
    "n_bookmakers",     # 采样的博彩公司数量
    "scorer_breakdown", # 得分分解 (JSON)
    "status",           # pending / won / lost / void
    "settled_at",       # 结算时间
    "result_odds",      # 结算时的最终赔率（用于 CLV 分析）
]


def _next_id() -> str:
    now = datetime.now()
    seq = 0
    if LOG_FILE.exists():
        try:
            df = pd.read_csv(LOG_FILE)
            if not df.empty and "id" in df.columns:
                last_id = df["id"].iloc[-1]
                seq = int(last_id.split("_")[-1]) + 1
        except Exception:
            seq = 0
    return f"{now.strftime('%Y%m%d')}_{seq}"


def log_prediction(
    sport: str,
    league: str,
    home_team_cn: str,
    away_team_cn: str,
    market_type: str,
    market_detail: str,
    odds: float,
    model_prob: float,
    market_prob: float,
    ev: float,
    stake: float,
    match_time: Optional[datetime] = None,
    source: str = "global_top5",
    home_team_en: str = "",
    away_team_en: str = "",
    sharp_prob: Optional[float] = None,
    home_team: str = "",
    away_team: str = "",
    quality_score: Optional[float] = None,
    quality_tier: Optional[str] = None,
    model_version: Optional[str] = None,
    n_bookmakers: int = 0,
    scorer_breakdown: Optional[str] = None,
) -> str:
    """记录一条推荐。

    Args:
        home_team_cn / away_team_cn: 中文队名（推送显示用）
        home_team_en / away_team_en: 英文队名（CLV 追踪回查用）
        home_team / away_team: 英文队名（用于比赛匹配，默认取 home_team_en）
        quality_score / quality_tier: 推荐质量评分/等级
        model_version: 模型版本
        n_bookmakers: 采样的博彩公司数量
        scorer_breakdown: 得分分解 JSON 字符串

    Returns:
        预测 ID
    """
    prediction_id = _next_id()
    now = datetime.now(timezone.utc)
    row = {
        "id": prediction_id,
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "sport": sport,
        "league": league,
        "home_team": home_team or home_team_en or home_team_cn,
        "away_team": away_team or away_team_en or away_team_cn,
        "home_team_cn": home_team_cn,
        "away_team_cn": away_team_cn,
        "home_team_en": home_team_en,
        "away_team_en": away_team_en,
        "market_type": market_type,
        "market_detail": market_detail,
        "odds": round(odds, 4),
        "model_prob": round(model_prob, 4),
        "market_prob": round(market_prob, 4),
        "ev": round(ev, 4),
        "sharp_prob": round(sharp_prob, 4) if sharp_prob is not None and sharp_prob > 0 else "",
        "stake": round(stake, 2),
        "match_time": match_time.isoformat() if match_time else "",
        "source": source,
        "quality_score": round(quality_score, 1) if quality_score is not None else "",
        "quality_tier": quality_tier or "",
        "model_version": model_version or "",
        "n_bookmakers": str(n_bookmakers),
        "scorer_breakdown": scorer_breakdown or "",
        "status": "pending",
        "settled_at": "",
        "result_odds": "",
    }

    df = pd.DataFrame([row])
    if LOG_FILE.exists():
        existing = pd.read_csv(LOG_FILE)
        df = pd.concat([existing, df], ignore_index=True)
    df.to_csv(LOG_FILE, index=False)

    # 双写: predictions 表
    try:
        from src.storage.database import db
        home_prob = away_prob = draw_prob = over_prob = under_prob = None
        mt = market_type.lower().strip()
        md = market_detail.lower().strip()
        if mt in ("胜负", "h2h", "win"):
            if "主" in md or "home" in md:
                home_prob = model_prob
            elif "客" in md or "away" in md:
                away_prob = model_prob
            elif "平" in md or "draw" in md:
                draw_prob = model_prob
            else:
                home_prob = model_prob
        elif mt in ("大小球", "total", "totals"):
            if "大" in md or "over" in md:
                over_prob = model_prob
            elif "小" in md or "under" in md:
                under_prob = model_prob
        db.record_prediction(
            match_key=prediction_id, sport=sport,
            home_team=home_team or home_team_en or home_team_cn,
            away_team=away_team or away_team_en or away_team_cn,
            model_name=source,
            home_prob=home_prob, away_prob=away_prob, draw_prob=draw_prob,
            over_prob=over_prob, under_prob=under_prob,
            commence_time=match_time.isoformat() if match_time else "",
        )
    except Exception:
        pass

    return prediction_id


def settle_prediction(prediction_id: str, won: bool, result_odds: Optional[float] = None):
    """结算一条推荐。

    Args:
        prediction_id: 预测 ID
        won: 是否赢
        result_odds: 结算时的赔率（用于 CLV 分析）
    """
    if not LOG_FILE.exists():
        return
    df = pd.read_csv(LOG_FILE)
    mask = df["id"] == prediction_id
    if not mask.any():
        logger.warning("⚠️ 未找到预测: %s", prediction_id)
        return
    df.loc[mask, "status"] = "won" if won else "lost"
    df.loc[mask, "settled_at"] = datetime.now(timezone.utc).isoformat()
    if result_odds is not None:
        df.loc[mask, "result_odds"] = round(result_odds, 4)
    df.to_csv(LOG_FILE, index=False)

    # 双写: 更新 predictions 表结算状态
    try:
        from src.storage.database import db
        from src.storage.models import Prediction
        with db.Session() as session:
            pred = session.query(Prediction).filter_by(match_key=prediction_id).first()
            if pred:
                pred.was_correct = 1 if won else 0
                session.commit()
    except Exception:
        pass


def batch_settle(sport: str = None):
    """批量结算已结束比赛的预测（自动匹配结果）。

    通过 ESPN 免费 API 获取已结束比赛的比分，
    自动匹配 prediction_log 中的 pending 记录并结算。

    Args:
        sport: 指定运动类型（nba/football），None 表示全部
    """
    if not LOG_FILE.exists():
        return
    df = pd.read_csv(LOG_FILE)
    pending = df[df["status"] == "pending"]
    if sport:
        pending = pending[pending["sport"] == sport]
    if pending.empty:
        logger.info("📋 批量结算: 无待结算记录")
        return

    from fetchers.espn_scores import fetch_espn_scores, LEAGUE_ESPN_PATH
    from src.core.team_names import cn_to_odds_name

    settled_count = 0
    errors = 0

    # 按联赛分组，每组只拉一次 ESPN
    for (sport_name, league), grp in pending.groupby(["sport", "league"]):
        # 映射到 ESPN 联赛名
        espn_league = None
        if sport_name == "nba":
            espn_league = "NBA"
        elif sport_name in ("football", "soccer"):
            espn_league = league
        elif sport_name == "world_cup":
            espn_league = "世界杯"
        elif sport_name == "nfl":
            espn_league = "NFL"

        if espn_league not in LEAGUE_ESPN_PATH:
            logger.debug("  ⏭️ %s/%s: ESPN 不支持此联赛", sport_name, league)
            continue

        # 确定需拉取的天数范围
        match_dates = set()
        for _, row in grp.iterrows():
            try:
                dt = datetime.fromisoformat(row["match_time"])
                match_dates.add(dt.strftime("%Y%m%d"))
            except Exception:
                continue
        if not match_dates:
            continue

        days_back = max(
            (datetime.now(timezone.utc) - datetime.strptime(max(match_dates), "%Y%m%d").replace(tzinfo=timezone.utc)).days + 2,
            3
        )

        logger.info("  📡 拉取 %s ESPN 数据（回溯 %d 天）...", espn_league, days_back)
        try:
            espn_games = fetch_espn_scores(espn_league, days_back=min(days_back, 7))
        except Exception as e:
            logger.warning("  ⚠️ ESPN 拉取失败 %s: %s", espn_league, e)
            errors += 1
            continue

        if not espn_games:
            logger.info("  📭 %s: ESPN 无已结束比赛", espn_league)
            continue

        # 构建 ESPN 球队名索引
        espn_scores = {}
        for g in espn_games:
            h, a = g["home_team"].lower(), g["away_team"].lower()
            key_h = (h, a)
            key_a = (a, h)
            espn_scores[key_h] = (g["home_score"], g["away_score"])
            espn_scores[key_a] = (g["away_score"], g["home_score"])

        for _, row in grp.iterrows():
            pred_id = row["id"]
            home_cn = str(row.get("home_team_cn", "") or row.get("home_team", ""))
            away_cn = str(row.get("away_team_cn", "") or row.get("away_team", ""))
            home_en = str(row.get("home_team_en", "") or "")
            away_en = str(row.get("away_team_en", "") or "")

            # 尝试匹配英文队名
            h_name = home_en.lower().strip() if home_en else cn_to_odds_name(home_cn).lower().strip()
            a_name = away_en.lower().strip() if away_en else cn_to_odds_name(away_cn).lower().strip()

            # 模糊匹配：ESPN 名包含 odds 名 或 odds 名包含 ESPN 名
            match_key = None
            for (eh, ea), (hs, as_) in espn_scores.items():
                if (eh in h_name or h_name in eh) and (ea in a_name or a_name in ea):
                    match_key = (eh, ea)
                    break

            if match_key is None:
                continue

            home_score, away_score = espn_scores[match_key]
            mtype = str(row.get("market_type", ""))
            detail = str(row.get("market_detail", ""))

            won = _determine_result(mtype, detail, home_score, away_score)
            settle_prediction(pred_id, won, result_odds=row.get("odds"))
            settled_count += 1
            logger.info("  ✅ %s %s vs %s → %s (%d-%d)",
                       pred_id, home_cn, away_cn, "W" if won else "L",
                       home_score, away_score)

    logger.info("📋 批量结算: %s 条（错误 %d）", settled_count, errors)
    return settled_count


def _determine_result(market_type: str, market_detail: str,
                       home_score: int, away_score: int) -> bool:
    """根据盘口类型和比分判定输赢。"""
    mt = market_type.lower().strip()
    detail = market_detail.lower().strip()

    if mt in ("胜负", "win", "h2h"):
        if "主胜" in detail or "home" in detail:
            return home_score > away_score
        elif "客胜" in detail or "away" in detail:
            return away_score > home_score
        elif "平局" in detail or "draw" in detail:
            return home_score == away_score
        return home_score > away_score  # default: home win

    elif mt in ("大小球", "total", "totals"):
        # 提取盘口线: "大 217.5" → 217.5
        import re
        nums = re.findall(r"[\d.]+", detail)
        if not nums:
            return False
        line = float(nums[-1])
        total = home_score + away_score
        if "大" in detail or "over" in detail:
            return total > line
        elif "小" in detail or "under" in detail:
            return total < line
        return total > line

    elif mt in ("让分", "spread"):
        import re
        nums = re.findall(r"[-+]?[\d.]+", detail)
        if not nums:
            return False
        spread = float(nums[0])
        if "主" in detail or "home" in detail:
            return home_score + spread > away_score
        else:
            return away_score + spread > home_score

    elif mt in ("asian_handicap", "亚洲"):
        import re
        # Asian handicap is complex; simplified: compare actual score diff to line
        nums = re.findall(r"[-+]?[\d.]+", detail)
        if not nums:
            return False
        line = float(nums[0])
        diff = home_score - away_score
        if "主" in detail:
            return diff + line > 0
        else:
            return diff - line < 0

    # Unknown market type → not settled
    return False


def get_performance(date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
    """计算绩效统计。

    Returns:
        {
            'total_predictions': N,
            'settled': N,
            'won': N,
            'lost': N,
            'win_rate': 0.0,
            'total_stake': 0.0,
            'total_profit': 0.0,
            'roi': 0.0,
            'avg_odds': 0.0,
            'avg_ev': 0.0,
        }
    """
    result = {
        "total_predictions": 0,
        "settled": 0,
        "won": 0,
        "lost": 0,
        "win_rate": 0.0,
        "total_stake": 0.0,
        "total_profit": 0.0,
        "roi": 0.0,
        "avg_odds": 0.0,
        "avg_ev": 0.0,
        "by_sport": {},
        "by_league": {},
        "by_market": {},
    }

    if not LOG_FILE.exists():
        return result

    df = pd.read_csv(LOG_FILE)
    if date_from:
        df = df[df["date"] >= date_from]
    if date_to:
        df = df[df["date"] <= date_to]

    result["total_predictions"] = len(df)
    settled = df[df["status"].isin(["won", "lost"])]
    result["settled"] = len(settled)

    if not settled.empty:
        won = settled[settled["status"] == "won"]
        lost = settled[settled["status"] == "lost"]
        result["won"] = len(won)
        result["lost"] = len(lost)
        result["win_rate"] = round(len(won) / len(settled), 4)

        total_stake = settled["stake"].sum()
        result["total_stake"] = round(total_stake, 2)

        # 利润 = 赢的净盈利 - 输的注额
        won_profit = (won["odds"] - 1) * won["stake"]
        lost_loss = lost["stake"]
        result["total_profit"] = round(won_profit.sum() - lost_loss.sum(), 2)

        if total_stake > 0:
            result["roi"] = round(result["total_profit"] / total_stake, 4)

        result["avg_odds"] = round(settled["odds"].mean(), 4)
        result["avg_ev"] = round(settled["ev"].mean(), 4)

        # 分类统计
        for group_col, group_key in [
            ("sport", "by_sport"),
            ("league", "by_league"),
            ("market_type", "by_market"),
        ]:
            for name, grp in settled.groupby(group_col):
                grp_won = grp[grp["status"] == "won"]
                grp_stake = grp["stake"].sum()
                grp_profit = ((grp_won["odds"] - 1) * grp_won["stake"]).sum() - (grp[grp["status"] == "lost"]["stake"].sum())
                result[group_key][name] = {
                    "total": len(grp),
                    "won": len(grp_won),
                    "win_rate": round(len(grp_won) / len(grp), 4),
                    "stake": round(grp_stake, 2),
                    "profit": round(grp_profit, 2),
                    "roi": round(grp_profit / grp_stake, 4) if grp_stake > 0 else 0.0,
                }

    # 保存
    _save_perf(result)
    return result


def _save_perf(data: Dict):
    with open(PERF_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def print_performance(perf: Dict = None):
    """打印绩效仪表盘。"""
    if perf is None:
        perf = get_performance()

    logger.info("\n%s", "=" * 60)
    logger.info("  📊 预测绩效仪表盘")
    logger.info("%s", "=" * 60)
    logger.info("  总推荐: %s", perf['total_predictions'])
    logger.info("  已结算: %s", perf['settled'])
    logger.info("  胜: %s  负: %s", perf['won'], perf['lost'])
    logger.info("  胜率: %.2f%%", perf['win_rate'] * 100)
    logger.info("  总投注额: ¥%.0f", perf['total_stake'])
    logger.info("  总利润: ¥%.0f", perf['total_profit'])
    logger.info("  ROI: %+.2f%%", perf['roi'] * 100)
    logger.info("  平均赔率: %.2f", perf['avg_odds'])
    logger.info("  平均 EV: %+.2f%%", perf['avg_ev'] * 100)

    if perf.get("by_sport"):
        logger.info("\n  --- 按运动 ---")
        for sport, sp in perf["by_sport"].items():
            logger.info("  %s: %s/%s (%.1f%%) | ROI %+.1f%% | ¥%+.0f",
                        sport, sp['won'], sp['total'], sp['win_rate'] * 100, sp['roi'] * 100, sp['profit'])

    if perf.get("by_market"):
        logger.info("\n  --- 按盘口 ---")
        for mkt, mp in perf["by_market"].items():
            logger.info("  %s: %s/%s (%.1f%%) | ROI %+.1f%%",
                        mkt, mp['won'], mp['total'], mp['win_rate'] * 100, mp['roi'] * 100)

    logger.info("%s", "=" * 60)


if __name__ == "__main__":
    print_performance()
