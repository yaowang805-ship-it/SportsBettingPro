"""模型性能衰减监控 — 跟踪预测准确率趋势，检测模型退化。

功能:
  1. 将待结算预测与实际赛果匹配，计算预测准确率
  2. 跟踪滚动窗口准确率（7/14/30天），检测衰退趋势
  3. 当性能下降超过阈值时触发智能重训信号
  4. 保存性能历史用于分析和看板展示

用法:
  from src.monitor.model_decay import run_decay_check
  report = run_decay_check()
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from config.logging_config import get_logger
from config.settings import ODDS_API_KEY, SPORTS_API_TIMEOUT
from src.notify.dingtalk import get_notifier

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_FILE = ROOT / "data" / "storage" / "prediction_log.csv"

from fetchers.espn_scores import fetch_espn_scores_by_sport_key
DECAY_REPORT_FILE = ROOT / "data" / "storage" / "model_decay_report.json"
PERFORMANCE_HISTORY_FILE = ROOT / "data" / "storage" / "model_accuracy_history.csv"
PERF_FILE = ROOT / "data" / "storage" / "performance_history.csv"

# 准确率退化阈值: 滚动14天准确率比基线低超过此值 → 触发重训
DECAY_THRESHOLD = 0.08  # 8个百分点
MIN_SAMPLES_FOR_DECAY = 20  # 最少样本数才开始判断


def _fetch_scores(sport_key: str, days_back: int = 10) -> List[Dict]:
    """获取已完成比赛结果：优先 ESPN（免费），降级到 Odds API。"""
    # 尝试 ESPN 免费 API
    try:
        espn_scores = fetch_espn_scores_by_sport_key(sport_key, days_back)
        if espn_scores:
            logger.debug("ESPN %s: %s 场已完成", sport_key, len(espn_scores))
            # 转换为 Odds API 兼容格式
            odds_format = []
            for g in espn_scores:
                odds_format.append({
                    "home_team": g["home_team"],
                    "away_team": g["away_team"],
                    "completed": g.get("completed", True),
                    "scores": [
                        {"name": g["home_team"], "score": g["home_score"]},
                        {"name": g["away_team"], "score": g["away_score"]},
                    ],
                })
            return odds_format
    except Exception as e:
        logger.debug("ESPN 比分获取失败 %s: %s", sport_key, e)

    # 降级：Odds API
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/"
    params = {"apiKey": ODDS_API_KEY, "days_from": days_back}
    try:
        resp = requests.get(url, params=params, timeout=SPORTS_API_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception as e:
        logger.warning("比分API请求失败 %s: %s", sport_key, e)
        return []


def _extract_winner(scores_data: List[Dict]) -> Dict[str, Tuple[str, int, int]]:
    """从 scores API 响应提取比赛结果。

    Returns:
        {(home_team, away_team): (winner_name, home_score, away_score)}
    """
    results = {}
    for game in scores_data:
        if not game.get("completed"):
            continue
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        scores = game.get("scores", [])
        if not scores:
            continue

        home_score, away_score = None, None
        for s in scores:
            if s.get("name", "").strip().lower() == home.strip().lower():
                home_score = s.get("score")
            elif s.get("name", "").strip().lower() == away.strip().lower():
                away_score = s.get("score")

        if home_score is not None and away_score is not None:
            winner = home if home_score > away_score else away
            results[(home.strip().lower(), away.strip().lower())] = (winner, home_score, away_score)
    return results


def _normalize(name) -> str:
    if not isinstance(name, str):
        return ""
    n = name.strip().lower().replace("fc", "").replace("cf", "").replace("afc", "").strip()
    # 队名别名归一化（Odds API vs ESPN 名称差异）
    ALIASES = {
        "usa": "united states",
        "turkey": "türkiye",
        "dr congo": "congo dr",
    }
    return ALIASES.get(n, n)


# 中文 → 英文队名映射（用于结算匹配）
def _build_cn_to_en_map():
    """从 team_names.py 构建中文→英文队名映射。"""
    from src.core.team_names import FOOTBALL_MAP, NBA_CN
    mapping = {}
    # NBA: 中文名 → 英文名（Odds API 格式）
    for en, cn in NBA_CN.items():
        mapping[cn.lower()] = en.lower()
    # 足球: 中文名 → Odds API 英文名
    for odds_name, (_, cn) in FOOTBALL_MAP.items():
        mapping[cn.lower()] = odds_name.lower()
    return mapping


_CN_TO_EN_CACHE = None


def _cn_to_en(cn_name: str) -> str:
    global _CN_TO_EN_CACHE
    if _CN_TO_EN_CACHE is None:
        _CN_TO_EN_CACHE = _build_cn_to_en_map()
    return _CN_TO_EN_CACHE.get(cn_name.strip().lower(), cn_name.strip().lower())


def _match_prediction_outcome(pred_row: Dict, all_results: Dict[str, Dict]) -> Optional[bool]:
    """判断一条预测是否命中。

    遍历所有候选队名组合（英文翻译 → CSV英文名 → 中文名），
    与 Odds API 返回的已完成比赛结果匹配。

    Args:
        pred_row: prediction_log 的一行 (dict)
        all_results: {sport_key: results_dict}

    Returns:
        True=命中, False=未命中, None=无法匹配
    """
    home_raw = pred_row.get("home_team_cn", pred_row.get("home_team", ""))
    away_raw = pred_row.get("away_team_cn", pred_row.get("away_team", ""))

    # 英文翻译优先（Odds API 返回英文名），中文名兜底
    candidates_home = [
        _normalize(_cn_to_en(home_raw)),
        _normalize(pred_row.get("home_team_en", "")),
        _normalize(home_raw),
    ]
    candidates_away = [
        _normalize(_cn_to_en(away_raw)),
        _normalize(pred_row.get("away_team_en", "")),
        _normalize(away_raw),
    ]

    # 去重 + 去空
    def _unique_nonempty(items):
        seen = set()
        result = []
        for c in items:
            if c and c not in seen:
                seen.add(c)
                result.append(c)
        return result

    home_cands = _unique_nonempty(candidates_home)
    away_cands = _unique_nonempty(candidates_away)

    if not home_cands or not away_cands:
        return None

    for sport_key, results in all_results.items():
        for (api_home, api_away), (winner, hs, aws) in results.items():
            api_h_norm = _normalize(api_home)
            api_a_norm = _normalize(api_away)

            for hc in home_cands:
                for ac in away_cands:
                    # 正向匹配
                    home_ok = (hc in api_h_norm or api_h_norm in hc)
                    away_ok = (ac in api_a_norm or api_a_norm in ac)
                    if home_ok and away_ok:
                        return _determine_outcome(pred_row, winner, hc, hs, aws)

                    # 主客互换（API 数据可能标反）
                    home_ok2 = (hc in api_a_norm or api_a_norm in hc)
                    away_ok2 = (ac in api_h_norm or api_h_norm in ac)
                    if home_ok2 and away_ok2:
                        market = pred_row.get("market_type", "")
                        if market in ("胜平负", "胜负"):
                            return _normalize(winner) == api_away
                        return None

    return None


def _determine_outcome(pred_row: Dict, api_winner: str, home_normalized: str,
                       home_score: int, away_score: int) -> Optional[bool]:
    """根据比赛结果判断预测是否命中。"""
    market = pred_row.get("market_type", "")
    winner_norm = _normalize(api_winner)

    if market in ("胜平负", "胜负"):
        return winner_norm == home_normalized

    if market == "让球":
        return winner_norm == home_normalized

    if market == "大小球":
        total_goals = home_score + away_score
        detail = str(pred_row.get("market_detail", ""))
        # 从 "大 3.75" / "小 2.5" 中提取盘口点数
        m = re.search(r'(\d+\.?\d*)', detail)
        if m:
            line = float(m.group(1))
            if "大" in detail:
                return total_goals > line
            else:
                return total_goals < line
        # 无法解析盘口时用实际总分近似
        if "大" in detail:
            return total_goals > home_score + away_score
        return total_goals <= home_score + away_score

    return None


def _sport_key_for_league(league: str) -> Optional[str]:
    mapping = {
        "NBA": "basketball_nba",
        "英超": "soccer_epl",
        "西甲": "soccer_spain_la_liga",
        "德甲": "soccer_germany_bundesliga",
        "意甲": "soccer_italy_serie_a",
        "法甲": "soccer_france_ligue_one",
        "soccer_fifa_world_cup": "soccer_fifa_world_cup",
    }
    return mapping.get(league)


def _load_prediction_log() -> pd.DataFrame:
    if LOG_FILE.exists():
        df = pd.read_csv(LOG_FILE)
        if "match_time" in df.columns:
            df["match_time_dt"] = pd.to_datetime(df["match_time"], errors="coerce")
        return df
    return pd.DataFrame()


def settle_pending_predictions(days_back: int = 10) -> int:
    """匹配待结算预测与实际赛果，更新 prediction_log.csv。

    Returns:
        已结算的预测数量
    """
    df = _load_prediction_log()
    if df.empty:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=2)  # 比赛结束至少2小时

    # 找到待处理且比赛已结束的预测
    pending = df[df["status"] == "pending"].copy()
    if pending.empty:
        return 0

    # 按联赛分组拉取赛果
    leagues_needed = pending["league"].unique()
    all_results = {}
    for league in leagues_needed:
        sport_key = _sport_key_for_league(league)
        if sport_key:
            scores = _fetch_scores(sport_key, days_back)
            all_results[sport_key] = _extract_winner(scores)

    settled_count = 0
    # 确保 settled_at 列类型为字符串
    if "settled_at" in df.columns:
        df["settled_at"] = df["settled_at"].astype(object)
    else:
        df["settled_at"] = ""

    for idx, row in pending.iterrows():
        match_time = row.get("match_time_dt")
        if pd.isna(match_time):
            # 从原始 match_time 字符串解析
            raw = row.get("match_time", "")
            if raw:
                try:
                    match_time = pd.to_datetime(raw, utc=True)
                except Exception:
                    continue
            else:
                continue
        elif match_time.tz is None:
            try:
                match_time = match_time.tz_localize("UTC")
            except Exception:
                continue

        if match_time > cutoff:
            continue  # 比赛可能还没结束

        outcome = _match_prediction_outcome(row.to_dict(), all_results)
        if outcome is True:
            df.loc[idx, "status"] = "won"
            settled_count += 1
        elif outcome is False:
            df.loc[idx, "status"] = "lost"
            settled_count += 1
        else:
            # 无法匹配结果 → 标记为 void
            df.loc[idx, "status"] = "void"

        df.loc[idx, "settled_at"] = now.isoformat()

    if settled_count > 0:
        # 只保存需要的列
        cols = [c for c in df.columns if c != "match_time_dt"]
        df[cols].to_csv(LOG_FILE, index=False)
        logger.info("已结算 %d 条预测", settled_count)

    return settled_count


def _compute_accuracy_from_perf_history() -> Dict:
    """从 performance_history.csv 计算准确率（fallback 路径）。

    performance_history.csv 字段: date, game, bet, prob, market_prob, stake, result, profit, odds, cumulative_balance
    其中 result = won/lost
    """
    if not PERF_FILE.exists():
        return {"n_total": 0, "message": "performance_history 不存在"}

    df = pd.read_csv(PERF_FILE)
    df = df[df["result"].isin(["won", "lost"])].copy()
    if df.empty:
        return {"n_total": 0, "message": "performance_history 无已结算记录"}

    df["is_win"] = (df["result"] == "won").astype(int)
    df["settle_date"] = pd.to_datetime(df["date"], errors="coerce")
    if df["settle_date"].dt.tz is None:
        df["settle_date"] = df["settle_date"].dt.tz_localize("UTC")

    now_ts = pd.Timestamp.now(tz=timezone.utc)
    baseline = df["is_win"].mean()

    result = {
        "baseline": round(float(baseline), 4),
        "n_total": len(df),
        "n_won": int(df["is_win"].sum()),
        "source": "performance_history",  # 标记数据来源
    }

    for window, label in [(7, "rolling_7d"), (14, "rolling_14d"), (30, "rolling_30d")]:
        cutoff = now_ts - timedelta(days=window)
        recent = df[df["settle_date"] >= cutoff]
        n = len(recent)
        result[label] = round(float(recent["is_win"].mean()), 4) if n > 0 else None
        result[f"n_{window}d"] = n

    # 退化检测（简化版，无 by_sport/by_market）
    is_decaying = False
    decay_signals = []
    rolling_14d = result.get("rolling_14d")
    if rolling_14d is not None and result["n_14d"] >= MIN_SAMPLES_FOR_DECAY:
        gap = baseline - rolling_14d
        if gap > DECAY_THRESHOLD:
            is_decaying = True
            decay_signals.append(
                f"14天准确率 ({rolling_14d:.1%}) 比基线 ({baseline:.1%}) 低 {gap:.1%}"
            )

    # 趋势斜率
    if len(df) >= 3:
        df_sorted = df.sort_values("settle_date")
        df_sorted["week"] = df_sorted["settle_date"].dt.isocalendar().week.astype(int)
        weekly_acc = df_sorted.groupby("week")["is_win"].mean()
        if len(weekly_acc) >= 3:
            recent_weeks = weekly_acc.tail(5)
            if len(recent_weeks) >= 3:
                slope = np.polyfit(range(len(recent_weeks)), recent_weeks.values, 1)[0]
                result["trend_slope"] = round(float(slope), 4)
                if slope < -0.02:
                    is_decaying = True
                    decay_signals.append(f"准确率趋势斜率为负 ({slope:.4f})")

    result["is_decaying"] = is_decaying
    result["decay_signal"] = "; ".join(decay_signals) if decay_signals else "无退化信号"
    result["decay_level"] = "critical" if (is_decaying and len(decay_signals) > 1) else ("warning" if is_decaying else "healthy")
    return result


def compute_accuracy_trend() -> Dict:
    """计算各时间窗口的预测准确率趋势。

    优先使用 prediction_log.csv，若已结算样本不足 10 条则回退到
    performance_history.csv（虚拟投注历史）。

    Returns:
        {
            "baseline": float,          # 整体准确率
            "rolling_7d": float,        # 近7天准确率
            "rolling_14d": float,       # 近14天
            "rolling_30d": float,       # 近30天
            "n_total": int,             # 总样本
            "n_7d": int,
            "n_14d": int,
            "by_sport": {sport: accuracy},
            "by_market": {market: accuracy},
            "is_decaying": bool,        # 是否正在退化
            "decay_signal": str,        # 退化信号描述
        }
    """
    df = _load_prediction_log()
    if df.empty:
        return _compute_accuracy_from_perf_history()

    # 筛选已结算的预测
    settled = df[df["status"].isin(["won", "lost"])].copy()
    if len(settled) < 10:
        logger.info("  prediction_log 已结算样本不足 (%d < 10)，回退到 performance_history", len(settled))
        return _compute_accuracy_from_perf_history()

    settled["is_win"] = (settled["status"] == "won").astype(int)
    if "settled_at" in settled.columns:
        settled["settle_date"] = pd.to_datetime(settled["settled_at"], errors="coerce")
    else:
        settled["settle_date"] = pd.Timestamp.now()

    now_ts = pd.Timestamp.now(tz=timezone.utc) if hasattr(pd.Timestamp, 'tz') else pd.Timestamp.now()
    if "settle_date" in settled.columns and settled["settle_date"].dt.tz is None:
        settled["settle_date"] = settled["settle_date"].dt.tz_localize("UTC")

    # 计算各窗口准确率
    baseline = settled["is_win"].mean()

    result = {
        "baseline": round(float(baseline), 4),
        "n_total": len(settled),
        "n_won": int(settled["is_win"].sum()),
    }

    for window, label in [(7, "rolling_7d"), (14, "rolling_14d"), (30, "rolling_30d")]:
        cutoff_ts = now_ts - timedelta(days=window)
        recent = settled[settled["settle_date"] >= cutoff_ts]
        n = len(recent)
        result[label] = round(float(recent["is_win"].mean()), 4) if n > 0 else None
        result[f"n_{window}d"] = n

    # 按运动
    by_sport = {}
    for sport in settled["sport"].unique():
        subset = settled[settled["sport"] == sport]
        by_sport[sport] = {
            "accuracy": round(float(subset["is_win"].mean()), 4),
            "n": len(subset),
        }
    result["by_sport"] = by_sport

    # 按市场
    by_market = {}
    for market in settled["market_type"].unique():
        subset = settled[settled["market_type"] == market]
        by_market[market] = {
            "accuracy": round(float(subset["is_win"].mean()), 4),
            "n": len(subset),
        }
    result["by_market"] = by_market

    # 退化检测
    is_decaying = False
    decay_signals = []

    rolling_14d = result.get("rolling_14d")
    if rolling_14d is not None and result["n_14d"] >= MIN_SAMPLES_FOR_DECAY:
        gap = baseline - rolling_14d
        if gap > DECAY_THRESHOLD:
            is_decaying = True
            decay_signals.append(
                f"14天准确率 ({rolling_14d:.1%}) 比基线 ({baseline:.1%}) 低 {gap:.1%}"
            )

    # 检测趋势斜率（最近5个有数据的窗口）
    if "settle_date" in settled.columns:
        settled_sorted = settled.sort_values("settle_date")
        settled_sorted["week"] = settled_sorted["settle_date"].dt.isocalendar().week.astype(int)
        weekly_acc = settled_sorted.groupby("week")["is_win"].mean()
        if len(weekly_acc) >= 3:
            recent_weeks = weekly_acc.tail(5)
            if len(recent_weeks) >= 3:
                slope = np.polyfit(range(len(recent_weeks)), recent_weeks.values, 1)[0]
                result["trend_slope"] = round(float(slope), 4)
                if slope < -0.02:
                    is_decaying = True
                    decay_signals.append(f"准确率趋势斜率为负 ({slope:.4f})")

    result["is_decaying"] = is_decaying
    result["decay_signal"] = "; ".join(decay_signals) if decay_signals else "无退化信号"

    # 退化级别
    if is_decaying:
        result["decay_level"] = "critical" if len(decay_signals) > 1 else "warning"
        # 钉钉告警
        try:
            notifier = get_notifier()
            details = "; ".join(decay_signals)
            summary = (
                f"### ⚠️ 模型性能退化检测\n\n"
                f"**级别**: {result['decay_level']}\n"
                f"**信号**: {details}\n\n"
                f"| 指标 | 值 |\n"
                f"|---|---|\n"
                f"| 14天准确率 | {result.get('rolling_14d', 'N/A'):.1%} |\n"
                f"| 7天准确率 | {result.get('rolling_7d', 'N/A'):.1%} |\n"
                f"| 基线准确率 | {baseline:.1%} |\n"
                f"| 14天样本量 | {result.get('n_14d', 0)} |\n"
                f"| 趋势斜率 | {result.get('trend_slope', 'N/A')} |\n\n"
                f"系统将在下次定时任务中自动触发重训。"
            )
            msg = notifier.build_markdown_message("⚠️ 模型退化告警", summary)
            notifier.send(msg, "模型退化告警")
            logger.warning("  模型退化告警已发送钉钉")
        except Exception as e:
            logger.warning("  模型退化钉钉通知失败: %s", e)
    else:
        result["decay_level"] = "healthy"

    return result


def calibration_report() -> Dict:
    """可靠性诊断报告 — 模型概率校准度检查。

    将预测概率分桶，对比每个桶的预测概率均值 vs 实际胜率。
    这是判断模型是否"知道自己不知道"的关键指标。

    Returns:
        {"buckets": [{bucket, n, avg_prob, win_rate, gap}, ...],
         "ece": 期望校准误差, "mce": 最大校准误差,
         "n_total": 总样本}
    """
    df = _load_prediction_log()
    if df.empty or len(df[df["status"].isin(["won", "lost"])]) < 5:
        # 兜底：从 performance_history.csv 读取 prob 和 result
        if PERF_FILE.exists():
            perf = pd.read_csv(PERF_FILE)
            perf = perf[perf["result"].isin(["won", "lost"])].copy()
            if len(perf) >= 5:
                perf["is_win"] = (perf["result"] == "won").astype(int)
                perf["model_prob"] = pd.to_numeric(perf["prob"], errors="coerce").fillna(0)
                perf["sport"] = perf["game"].apply(lambda x: "bb" if "NBA" in str(x) else "fb")
                settled = perf
    else:
        settled = df[df["status"].isin(["won", "lost"])].copy()
        settled["is_win"] = (settled["status"] == "won").astype(int)
        probs = pd.to_numeric(settled["model_prob"], errors="coerce").dropna()
        settled = settled.loc[probs.index].copy()
        settled["model_prob"] = probs

    if len(settled) < 5:
        return {"error": f"有效样本不足 ({len(settled)} < 5)", "n_total": len(settled)}

    # 10 个等宽桶: 0-0.1, 0.1-0.2, ..., 0.9-1.0
    settled["bucket"] = pd.cut(settled["model_prob"], bins=10, labels=False) / 10
    buckets = []
    weighted_sum = 0.0
    n_total = len(settled)

    print(f"\n{'='*60}")
    print(f"  校准度诊断 — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"  总样本: {n_total}")
    print(f"{'='*60}")
    print(f"  {'概率区间':<10} {'数量':>6} {'平均概率':>10} {'实际胜率':>10} {'偏差':>8}")
    print(f"  {'-'*10} {'-'*6} {'-'*10} {'-'*10} {'-'*8}")

    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        mask = (settled["model_prob"] >= lo) & (settled["model_prob"] < hi)
        # 让上边界包含 1.0
        if i == 9:
            mask |= settled["model_prob"] == 1.0
        subset = settled[mask]
        n = len(subset)
        if n == 0:
            print(f"  {lo:.0%}-{hi:.0%}:    {n:>6} {'—':>10} {'—':>10} {'—':>8}")
            continue
        avg_prob = subset["model_prob"].mean()
        win_rate = subset["is_win"].mean()
        gap = avg_prob - win_rate
        weighted_sum += abs(gap) * n / n_total
        buckets.append({
            "bucket": f"{lo:.0%}-{hi:.0%}",
            "n": n,
            "avg_prob": round(float(avg_prob), 4),
            "win_rate": round(float(win_rate), 4),
            "gap": round(float(gap), 4),
        })
        gap_str = f"{gap:+.1%}"
        print(f"  {lo:.0%}-{hi:.0%}:    {n:>6} {avg_prob:>10.1%} {win_rate:>10.1%} {gap_str:>8}")

    ece = round(weighted_sum, 4)
    mce = round(max(b["gap"] for b in buckets), 4) if buckets else 0
    print(f"  {'-'*44}")
    print(f"  ECE (期望校准误差): {ece:.2%}")
    print(f"  MCE (最大校准误差): {mce:.2%}")

    # 按运动拆分
    by_sport = {}
    for sport in settled["sport"].unique():
        ss = settled[settled["sport"] == sport]
        if len(ss) < 3:
            continue
        ece_sport = sum(abs(ss["model_prob"] - ss["is_win"])) / len(ss)
        by_sport[sport] = {
            "n": len(ss),
            "ece": round(float(ece_sport), 4),
            "win_rate": round(float(ss["is_win"].mean()), 4),
        }
        print(f"  {sport}: ECE={ece_sport:.2%} ({len(ss)}条)")

    result = {
        "buckets": buckets,
        "ece": ece,
        "mce": mce,
        "n_total": n_total,
        "by_sport": by_sport,
    }

    # 保存到报告文件
    report_path = ROOT / "data" / "storage" / "calibration_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    result["timestamp"] = datetime.now().isoformat()
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"{'='*60}\n")
    return result


def run_decay_check(settle_first: bool = True) -> Dict:
    """运行完整的衰减检测流程。

    1. 先结算待处理预测
    2. 计算准确率趋势
    3. 检测退化信号
    4. 保存报告

    Returns:
        衰减检测报告 dict
    """
    if settle_first:
        n_settled = settle_pending_predictions()
        if n_settled > 0:
            logger.info("  新结算 %d 条预测", n_settled)

    trend = compute_accuracy_trend()
    if "error" in trend:
        logger.warning("  衰减检测失败: %s", trend["error"])
        return trend

    logger.info("\n" + "=" * 55)
    logger.info("  模型性能衰减检测 - %s", datetime.now().strftime('%Y-%m-%d'))
    logger.info("=" * 55)
    if "n_won" not in trend:
        logger.info("  %s", trend.get("message", "暂无数据"))
        return trend
    logger.info("  总样本: %d | 命中: %d | 基线准确率: %.1f%%",
               trend["n_total"], trend["n_won"], trend["baseline"] * 100)

    for window, label in [(7, "滚动7天"), (14, "滚动14天"), (30, "滚动30天")]:
        key = {"7": "rolling_7d", "14": "rolling_14d", "30": "rolling_30d"}[str(window)]
        val = trend.get(key)
        n = trend.get(f"n_{window}d", 0)
        if val is not None:
            logger.info("  %s: %.1f%% (%d 样本)", label, val * 100, n)

    if trend.get("is_decaying"):
        logger.warning("  ⚠️ 检测到模型退化: %s", trend["decay_signal"])
        logger.warning("  ⚠️ 退化级别: %s", trend.get("decay_level", "unknown"))
        logger.warning("  💡 建议执行模型重训: python3 src/models/ensemble_trainer.py bb && python3 src/models/ensemble_trainer.py fb")
    else:
        logger.info("  ✅ 模型性能正常，无退化信号")

    if trend.get("by_sport"):
        logger.info("\n  按运动:")
        for sport, info in trend["by_sport"].items():
            logger.info("    %s: %.1f%% (%d 样本)", sport, info["accuracy"] * 100, info["n"])

    # 校准度诊断
    logger.info("")
    cal = calibration_report()
    trend["calibration"] = {
        "ece": cal.get("ece"),
        "mce": cal.get("mce"),
        "n_calibrated": cal.get("n_total", 0),
    }
    if cal.get("ece") is not None:
        ece_rating = "✅ 良好" if cal["ece"] < 0.05 else ("⚠️ 一般" if cal["ece"] < 0.10 else "❌ 差")
        logger.info("  校准度 ECE: %.2f%% %s", cal["ece"] * 100, ece_rating)

    logger.info("=" * 55)

    # 保存报告
    trend["timestamp"] = datetime.now().isoformat()
    DECAY_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DECAY_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(trend, f, ensure_ascii=False, indent=2)

    # 追加到历史CSV
    hist_row = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "baseline": trend.get("baseline"),
        "rolling_7d": trend.get("rolling_7d"),
        "rolling_14d": trend.get("rolling_14d"),
        "rolling_30d": trend.get("rolling_30d"),
        "n_total": trend.get("n_total", 0),
        "is_decaying": trend.get("is_decaying", False),
        "trend_slope": trend.get("trend_slope"),
    }
    try:
        PERFORMANCE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if PERFORMANCE_HISTORY_FILE.exists():
            hist = pd.read_csv(PERFORMANCE_HISTORY_FILE)
            hist = pd.concat([hist, pd.DataFrame([hist_row])], ignore_index=True)
        else:
            hist = pd.DataFrame([hist_row])
        hist.to_csv(PERFORMANCE_HISTORY_FILE, index=False)
    except Exception as e:
        logger.warning("保存性能历史失败: %s", e)

    # 双写: model_accuracy 表（按运动/来源写入准确率）
    try:
        from src.storage.database import db
        from src.storage.models import ModelAccuracy
        import math
        with db.Session() as session:
            for sport, info in trend.get("by_sport", {}).items():
                n = info.get("n", 0)
                acc = info.get("accuracy", 0)
                existing = session.query(ModelAccuracy).filter_by(
                    model_name="ensemble", target=sport).first()
                if existing:
                    existing.total_predictions = n
                    existing.correct = int(round(n * acc)) if not math.isnan(acc) else 0
                    existing.accuracy = acc if not math.isnan(acc) else 0.0
                    existing.updated_at = datetime.now(timezone.utc)
                else:
                    session.add(ModelAccuracy(
                        model_name="ensemble", target=sport,
                        total_predictions=n,
                        correct=int(round(n * acc)) if not math.isnan(acc) else 0,
                        accuracy=acc if not math.isnan(acc) else 0.0,
                        updated_at=datetime.now(timezone.utc),
                    ))
            session.commit()
    except Exception:
        pass

    return trend


if __name__ == "__main__":
    run_decay_check()
