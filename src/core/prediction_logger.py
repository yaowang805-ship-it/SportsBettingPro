"""投注预测跟踪系统 — 记录、结算、绩效分析。

职业博彩团队的第一原则：不测量就不存在。
本模块记录每条推荐的全生命周期：
  推荐 → 已投注 → 已结算（赢/输） → 绩效统计
"""
import json
import os
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass, asdict

from config.logging_config import get_logger
logger = get_logger(__name__)

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "data" / "storage"
LOG_FILE = LOG_DIR / "prediction_log.csv"
PERF_FILE = LOG_DIR / "performance_summary.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# CSV 列定义
COLUMNS = [
    "id",               # 唯一 ID: {date}_{sport}_{market}_{seq}
    "timestamp",        # 推荐生成时间
    "date",             # 推荐日期
    "sport",            # nba / football
    "league",           # NBA / 英超 / 西甲 ...
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
    "source",           # 推荐来源 (global_top5 / daily_bb / daily_fb)
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
) -> str:
    """记录一条推荐。

    Args:
        home_team_cn / away_team_cn: 中文队名（推送显示用）
        home_team_en / away_team_en: 英文队名（CLV 追踪回查用）

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
        "home_team": home_team_cn,  # 兼容旧列名：存中文名
        "away_team": away_team_cn,
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
        "status": "pending",
        "settled_at": "",
        "result_odds": "",
    }

    df = pd.DataFrame([row])
    if LOG_FILE.exists():
        existing = pd.read_csv(LOG_FILE)
        df = pd.concat([existing, df], ignore_index=True)
    df.to_csv(LOG_FILE, index=False)
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


def batch_settle(sport: str = None):
    """批量结算已结束比赛的预测（自动匹配结果）。

    Args:
        sport: 指定运动类型，None 表示全部
    """
    if not LOG_FILE.exists():
        return
    df = pd.read_csv(LOG_FILE)
    pending = df[df["status"] == "pending"]
    if sport:
        pending = pending[pending["sport"] == sport]
    if pending.empty:
        return

    settled_count = 0
    for _, row in pending.iterrows():
        match_time_str = row.get("match_time", "")
        if not match_time_str:
            continue
        try:
            match_dt = datetime.fromisoformat(match_time_str)
        except Exception:
            continue
        # 比赛已结束超过 2 小时才结算
        if datetime.now(timezone.utc) <= match_dt.replace(tzinfo=timezone.utc):
            continue

        # TODO: 接入实际赛果 API（balldontlie / football-data.org）自动结算
        # 目前仅标记为待手动结算
        pass
    logger.info("📋 批量结算: %s 条", settled_count)


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
