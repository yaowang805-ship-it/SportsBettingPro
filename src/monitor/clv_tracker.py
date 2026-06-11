"""CLV（Closing Line Value）追踪系统。

CLV 是职业博彩的第一指标：
  - 正 CLV = 你拿到的赔率比市场收盘价更好 = 你有真实 edge
  - 负 CLV = 你长期会输钱，不管短期胜率如何
  - 345,000 笔投注研究：正 CLV → +7.7% ROI，负 CLV → -8.38% ROI

用法:
  from src.monitor.clv_tracker import update_clv_for_pending, report_clv
  update_clv_for_pending()    # 对所有待结算预测计算 CLV
  report_clv()                # 打印 CLV 报告
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from fetchers.odds_api import fetch_odds_api
from config.logging_config import get_logger
logger = get_logger(__name__)

LOG_FILE = ROOT / "data" / "storage" / "prediction_log.csv"
CLV_REPORT_FILE = ROOT / "data" / "storage" / "clv_report.json"

# 我们认为"临场赔率"是在赛前 X 分钟内获取的赔率
CLOSE_WINDOW_MINUTES = 30


def _fetch_closing_odds(sport_key: str, match_time: datetime,
                        market_type: str = "h2h",
                        market_detail: str = "",
                        home_team_en: str = "",
                        away_team_en: str = "") -> Optional[float]:
    """获取指定比赛的收盘价（按盘口类型匹配）。

    只有 H2H 有可靠的 CLV 计算（跨博彩公司比价），
    spread/total 因涉及盘口点数的精确匹配，暂不计算 CLV。

    Args:
        sport_key: odds API sport key
        match_time: 比赛时间
        market_type: 盘口类型 (h2h/spread/total)
        market_detail: 盘口详情（如"主队 -5.5"），用于 spread/total 匹配
        home_team_en: 主队英文名
        away_team_en: 客队英文名

    Returns:
        收盘赔率，或 None
    """
    # 非 H2H 暂不计算 CLV（需要精确匹配盘口点数+方向）
    if market_type not in ("h2h", "H2H"):
        return None

    try:
        data = fetch_odds_api(sport_key, force=True)
    except Exception:
        return None

    if not data:
        return None

    for match in data:
        try:
            api_time_str = match.get("commence_time", "")
            api_time = datetime.fromisoformat(api_time_str.replace("Z", "+00:00"))
            time_diff = abs((api_time - match_time).total_seconds() / 60)
            if time_diff <= 5:
                home = match.get("home_team", "")
                best_home_odds = None
                for bm in match.get("bookmakers", []):
                    for market in bm.get("markets", []):
                        if market.get("key") != "h2h":
                            continue
                        for out in market.get("outcomes", []):
                            if out.get("name", "").strip().lower() == home.strip().lower():
                                price = out.get("price")
                                if price and (best_home_odds is None or price > best_home_odds):
                                    best_home_odds = price
                return best_home_odds
        except Exception:
            continue
    return None


def _sport_key_for_league(league: str) -> Optional[str]:
    """联赛名 → odds API sport key。"""
    mapping = {
        "NBA": "basketball_nba",
        "英超": "soccer_epl",
        "西甲": "soccer_spain_la_liga",
        "德甲": "soccer_germany_bundesliga",
        "意甲": "soccer_italy_serie_a",
        "法甲": "soccer_france_ligue_one",
    }
    return mapping.get(league)


def update_clv_for_pending() -> Dict:
    """对所有待结算预测计算 CLV。

    从 prediction_log.csv 读取 status=pending 且 match_time 接近的预测，
    获取当前赔率，计算 CLV = (推荐赔率 - 当前赔率) / 当前赔率。

    Returns:
        { updated: N, skipped: N, errors: N }
    """
    if not LOG_FILE.exists():
        return {"updated": 0, "skipped": 0, "errors": 0}

    df = pd.read_csv(LOG_FILE)
    pending = df[df["status"] == "pending"]
    if pending.empty:
        return {"updated": 0, "skipped": 0, "errors": 0}

    now = datetime.now(timezone.utc)
    result = {"updated": 0, "skipped": 0, "errors": 0}

    for idx, row in pending.iterrows():
        match_time_str = row.get("match_time", "")
        if not match_time_str:
            result["skipped"] += 1
            continue

        try:
            match_dt = datetime.fromisoformat(match_time_str)
            if match_dt.tzinfo is None:
                match_dt = match_dt.replace(tzinfo=timezone.utc)
        except Exception:
            result["skipped"] += 1
            continue

        mins_to_match = (match_dt - now).total_seconds() / 60

        # 跳过比赛还未接近开赛的（太早获取的赔率不反映收盘价）
        if mins_to_match > CLOSE_WINDOW_MINUTES + 60:
            result["skipped"] += 1
            continue

        # 比赛已结束超过 2 小时，标记为待手动结算（没有赛果无法自动判赢输）
        if mins_to_match < -120:
            df.loc[idx, "status"] = "void"
            df.loc[idx, "settled_at"] = now.isoformat()
            result["updated"] += 1
            continue

        league = row.get("league", "")
        sport_key = _sport_key_for_league(league)
        if not sport_key:
            result["skipped"] += 1
            continue

        recorded_odds = row.get("odds", 0)
        if not recorded_odds or float(recorded_odds) <= 0:
            result["skipped"] += 1
            continue

        market_type = str(row.get("market_type", "h2h")).lower()
        market_detail = str(row.get("market_detail", ""))
        home_en = str(row.get("home_team_en", ""))
        away_en = str(row.get("away_team_en", ""))

        # 非 H2H 盘口不计算 CLV（避免用 H2H 赔率去比较 spread/total 的数学错误）
        if market_type not in ("h2h",):
            result["skipped"] += 1
            continue

        current_odds = _fetch_closing_odds(sport_key, match_dt, market_type, market_detail, home_en, away_en)
        if current_odds is None:
            result["errors"] += 1
            continue

        rec_odds = float(recorded_odds)
        clv = (rec_odds - current_odds) / current_odds

        df.loc[idx, "result_odds"] = round(current_odds, 4)

        result["updated"] += 1
        home_cn = row.get("home_team_cn", row.get("home_team", ""))
        away_cn = row.get("away_team_cn", row.get("away_team", ""))
        logger.info("  📊 CLV: %s vs %s | 推荐 %.2f → 临场 %.2f | CLV %+.2f%%",
                     home_cn, away_cn, rec_odds, current_odds, clv * 100)

    df.to_csv(LOG_FILE, index=False)
    return result


def get_clv_summary() -> Dict:
    """计算所有已结算预测的 CLV 汇总。"""
    summary = {
        "total_settled": 0,
        "with_clv": 0,
        "positive_clv": 0,
        "negative_clv": 0,
        "avg_clv": 0.0,
        "avg_clv_by_league": {},
        "avg_clv_by_market": {},
        "best_clv": 0.0,
        "worst_clv": 0.0,
    }

    if not LOG_FILE.exists():
        return summary

    df = pd.read_csv(LOG_FILE)

    # 找到有 result_odds 的记录
    has_clv = df[df["result_odds"].notna() & (df["result_odds"] != "")]
    if has_clv.empty:
        return summary

    # 计算 CLV
    clv_values = []
    for _, row in has_clv.iterrows():
        try:
            rec_odds = float(row["odds"])
            close_odds = float(row["result_odds"])
            if close_odds > 0 and rec_odds > 0:
                clv = (rec_odds - close_odds) / close_odds
                clv_values.append((row, clv))
        except (ValueError, TypeError):
            continue

    if not clv_values:
        return summary

    clv_series = pd.Series([c[1] for c in clv_values])
    summary["total_settled"] = len(has_clv)
    summary["with_clv"] = len(clv_values)
    summary["positive_clv"] = int((clv_series > 0).sum())
    summary["negative_clv"] = int((clv_series <= 0).sum())
    summary["avg_clv"] = round(float(clv_series.mean()), 4)
    summary["best_clv"] = round(float(clv_series.max()), 4)
    summary["worst_clv"] = round(float(clv_series.min()), 4)

    # 按联赛汇总
    by_league = {}
    for (row, clv) in clv_values:
        league = row.get("league", "未知")
        if league not in by_league:
            by_league[league] = []
        by_league[league].append(clv)
    summary["avg_clv_by_league"] = {
        league: round(float(pd.Series(vals).mean()), 4)
        for league, vals in by_league.items()
    }

    # 按市场类型汇总
    by_market = {}
    for (row, clv) in clv_values:
        market = row.get("market_type", "未知")
        if market not in by_market:
            by_market[market] = []
        by_market[market].append(clv)
    summary["avg_clv_by_market"] = {
        market: round(float(pd.Series(vals).mean()), 4)
        for market, vals in by_market.items()
    }

    return summary


def report_clv():
    """打印 CLV 报告。"""
    logger.info("\n" + "=" * 60)
    logger.info("  📊 CLV（收盘价价值）追踪报告")
    logger.info("=" * 60)

    summary = get_clv_summary()

    if summary["with_clv"] == 0:
        # 尝试先更新
        logger.info("  尚无 CLV 数据，正在尝试更新...")
        res = update_clv_for_pending()
        logger.info("  更新: %s, 跳过: %s, 错误: %s", res['updated'], res['skipped'], res['errors'])
        summary = get_clv_summary()
        if summary["with_clv"] == 0:
            logger.warning("  ⚠️ 仍无 CLV 数据，系统尚未积累足够投注记录")
            return

    logger.info("  已结算: %s", summary['total_settled'])
    logger.info("  有 CLV 记录: %s", summary['with_clv'])
    logger.info("  正 CLV: %s  负 CLV: %s", summary['positive_clv'], summary['negative_clv'])
    logger.info("  平均 CLV: %+.4f", summary['avg_clv'])
    logger.info("  最佳 CLV: %+.4f", summary['best_clv'])
    logger.info("  最差 CLV: %+.4f", summary['worst_clv'])

    if summary["avg_clv_by_league"]:
        logger.info("\n  --- 按联赛 ---")
        for league, clv in sorted(summary["avg_clv_by_league"].items()):
            logger.info("  %s: %+.4f", league, clv)

    if summary["avg_clv_by_market"]:
        logger.info("\n  --- 按盘口 ---")
        for market, clv in sorted(summary["avg_clv_by_market"].items()):
            logger.info("  %s: %+.4f", market, clv)

    # CLV 评判
    avg = summary["avg_clv"]
    if avg > 0.02:
        logger.info("\n  ✅ 正 CLV (%+.2f%%) — 有真实 edge，系统具备盈利能力", avg * 100)
    elif avg > 0:
        logger.info("\n  🟡 微正 CLV (%+.2f%%) — 方向正确但 edge 不足", avg * 100)
    else:
        logger.error("\n  ❌ 负 CLV (%+.2f%%) — 没有真实 edge，需要改进模型", avg * 100)

    # 保存报告
    with open(CLV_REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)


# ── 开盘价捕获 ─────────────────────────────────────────────────

OPENING_ODDS_FILE = ROOT / "data" / "storage" / "opening_odds.json"


def _load_opening_odds() -> dict:
    """加载开盘价记录。"""
    if OPENING_ODDS_FILE.exists():
        try:
            return json.loads(OPENING_ODDS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_opening_odds(data: dict):
    """保存开盘价记录。"""
    OPENING_ODDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPENING_ODDS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def capture_opening_odds(match_key: str, market: str, odds: float,
                          bookmaker: str = "", league: str = ""):
    """捕获开盘赔率 — 在首次生成推荐时保存。

    Args:
        match_key: 比赛唯一标识（如 "Lakers @ Celtics 2026-05-25"）
        market: 市场类型（h2h/spread/total）
        odds: 赔率
        bookmaker: 推荐博彩公司
        league: 联赛名称
    """
    data = _load_opening_odds()
    record_key = f"{match_key}|{market}"

    # 只保留首次看到的赔率（开盘价）
    if record_key not in data:
        data[record_key] = {
            "match_key": match_key,
            "market": market,
            "opening_odds": odds,
            "opening_bookmaker": bookmaker,
            "league": league,
            "captured_at": datetime.now().isoformat(),
            "closing_odds": None,
            "closing_bookmaker": None,
            "clv": None,
        }

    _save_opening_odds(data)


def update_opening_with_closing(match_key: str, market: str,
                                 closing_odds: float, closing_bm: str = ""):
    """用收盘价更新开盘价记录，计算 CLV。

    CLV = (closing_odds - opening_odds) / opening_odds
    正 CLV = 开盘价优于收盘价（真实 edge）
    """
    data = _load_opening_odds()
    record_key = f"{match_key}|{market}"

    if record_key not in data:
        return

    rec = data[record_key]
    if rec["closing_odds"] is not None:
        return  # 已更新过

    rec["closing_odds"] = closing_odds
    rec["closing_bookmaker"] = closing_bm
    if rec["opening_odds"] and closing_odds:
        rec["clv"] = round((rec["opening_odds"] - closing_odds) / closing_odds, 6)

    _save_opening_odds(data)


def get_clv_by_bookmaker() -> dict:
    """按博彩公司统计 CLV。"""
    data = _load_opening_odds()
    bm_stats = {}

    for record_key, rec in data.items():
        bm = rec.get("opening_bookmaker", "未知") or "未知"
        clv = rec.get("clv")
        if clv is None:
            continue

        if bm not in bm_stats:
            bm_stats[bm] = {"count": 0, "total_clv": 0.0, "positive": 0, "negative": 0}
        bm_stats[bm]["count"] += 1
        bm_stats[bm]["total_clv"] += clv
        if clv > 0:
            bm_stats[bm]["positive"] += 1
        else:
            bm_stats[bm]["negative"] += 1

    for bm in bm_stats:
        s = bm_stats[bm]
        s["avg_clv"] = round(s["total_clv"] / s["count"], 6) if s["count"] > 0 else 0.0
        s["win_rate"] = round(s["positive"] / s["count"], 4) if s["count"] > 0 else 0.0

    return dict(sorted(bm_stats.items(), key=lambda x: x[1]["avg_clv"], reverse=True))


def report_clv_by_bookmaker():
    """打印分博彩公司 CLV 报告。"""
    stats = get_clv_by_bookmaker()
    if not stats:
        logger.info("  暂无分公司 CLV 数据")
        return

    logger.info("\n  --- 按博彩公司 CLV ---")
    for bm, s in stats.items():
        logger.info("  %-20s: %s 笔, 平均 CLV %+.4f, 胜率 %.0f%%",
                   bm, s["count"], s["avg_clv"], s["win_rate"] * 100)


def get_clv_trend(match_key: str = None) -> pd.DataFrame:
    """获取 CLV 趋势数据。

    Returns:
        DataFrame with [match_key, market, opening_odds, closing_odds, clv, captured_at]
    """
    data = _load_opening_odds()
    rows = []
    for record_key, rec in data.items():
        if match_key and rec.get("match_key") != match_key:
            continue
        rows.append({
            "match_key": rec.get("match_key", ""),
            "market": rec.get("market", ""),
            "opening_odds": rec.get("opening_odds"),
            "closing_odds": rec.get("closing_odds"),
            "clv": rec.get("clv"),
            "bookmaker": rec.get("opening_bookmaker", ""),
            "captured_at": rec.get("captured_at", ""),
        })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[df["clv"].notna()]
    return df.sort_values("captured_at") if not df.empty else df


# ── 基于预测日志的 Edge 特征（CLV 代理） ─────────────────────

def compute_team_edge_features(sport: str = None, window: int = 10) -> Dict[str, float]:
    """从预测日志计算每支球队的平均 edge（model_prob - market_prob）。

    CLV 代理特征：如果模型在某队上持续有正 edge，说明模型对该队的判断
    优于市场，可作为训练特征使用。

    Args:
        sport: 过滤运动类型 ("nba" / "football"), None=全部
        window: 只考虑最近 N 条预测

    Returns:
        {team_name_lower: avg_edge}
    """
    if not LOG_FILE.exists():
        return {}

    df = pd.read_csv(LOG_FILE)
    if df.empty:
        return {}

    if sport:
        df = df[df["sport"] == sport]
    if df.empty:
        return {}

    # 只保留有 edge 数据的记录
    df = df[df["ev"].notna() & df["model_prob"].notna() & df["market_prob"].notna()]
    if df.empty:
        return {}

    # 收集主客场 edge
    records = []
    for _, row in df.iterrows():
        edge = float(row["ev"])
        home = str(row.get("home_team_cn", row.get("home_team", ""))).strip().lower()
        away = str(row.get("away_team_cn", row.get("away_team", ""))).strip().lower()
        if home:
            records.append({"team": home, "edge": edge, "date": row.get("timestamp", "")})
        if away:
            records.append({"team": away, "edge": -edge, "date": row.get("timestamp", "")})
        # 客队用负 edge（模型看好的主队对客队不利）

    if not records:
        return {}

    edge_df = pd.DataFrame(records)
    if "date" in edge_df.columns:
        edge_df = edge_df.sort_values("date")

    # 滚动平均
    result = {}
    for team in edge_df["team"].unique():
        team_edges = edge_df[edge_df["team"] == team]["edge"].tail(window)
        if len(team_edges) >= 3:
            result[team] = round(float(team_edges.mean()), 4)

    return result


# ── 收盘价自动填充 ───────────────────────────────────────────────

_LEAGUE_TO_SPORTKEY = {
    "NBA": "basketball_nba",
    "英超": "soccer_epl", "西甲": "soccer_spain_la_liga",
    "德甲": "soccer_germany_bundesliga", "意甲": "soccer_italy_serie_a",
    "法甲": "soccer_france_ligue_one",
    "世界杯": "soccer_fifa_world_cup",
    "欧冠": "soccer_uefa_champions_league",
    "欧联": "soccer_uefa_europa_league",
    "国际足球": "soccer_epl",
}

# 泛化联赛名 → 候选 sport_key 列表（逐个尝试直到找到匹配）
_FALLBACK_SPORT_KEYS = {
    "足球": [
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_italy_serie_a", "soccer_france_ligue_one",
        "soccer_fifa_world_cup", "soccer_uefa_champions_league",
        "soccer_uefa_europa_league", "soccer_brazil_campeonato",
        "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
        "soccer_usa_mls",
    ],
}


def _parse_match_key(match_key: str):
    """从 match_key 'HomeTeam @ AwayTeam 2026-05-25' 解析球队名。"""
    try:
        team_part = match_key.rsplit(" ", 1)[0]
        parts = team_part.split(" @ ", 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    except Exception:
        pass
    return None, None


def _build_h2h_index(odds_data: list) -> dict:
    """从 odds API 响应构建 (home_lower, away_lower) → (best_h2h, bookmaker) 索引。"""
    idx = {}
    for match in odds_data:
        home = match.get("home_team", "").strip().lower()
        away = match.get("away_team", "").strip().lower()
        best_odds = None
        best_bm = ""
        for bm in match.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for out in mkt.get("outcomes", []):
                    if out.get("name", "").strip().lower() == home:
                        price = out.get("price")
                        if price and (best_odds is None or price > best_odds):
                            best_odds = price
                            best_bm = bm.get("title", "unknown")
        idx[(home, away)] = (best_odds, best_bm)
    return idx


def _sync_prediction_log(match_key: str, closing_odds: float):
    """同步更新 prediction_log.csv 的 result_odds。"""
    if not LOG_FILE.exists():
        return
    df = pd.read_csv(LOG_FILE)
    if df.empty:
        return

    home_name, away_name = _parse_match_key(match_key)
    if not home_name:
        return

    h_lower, a_lower = home_name.lower(), away_name.lower()
    mask = pd.Series([False] * len(df))

    # 尝试英文队名匹配
    for en_col in ("home_team_en",):
        if en_col in df.columns:
            col_lower = df[en_col].astype(str).str.strip().str.lower()
            ac = df.get("away_team_en", df.get(en_col, pd.Series([""] * len(df))))
            a_col_lower = ac.astype(str).str.strip().str.lower()
            mask = mask | (
                df["status"].isin(["pending", "won", "lost"])
                & (col_lower == h_lower)
                & (a_col_lower == a_lower)
            )

    # 兜底中文名
    if not mask.any():
        for col in ("home_team_cn", "home_team"):
            if col in df.columns:
                col_lower = df[col].astype(str).str.strip().str.lower()
                mask = mask | (
                    df["status"].isin(["pending", "won", "lost"])
                    & (col_lower == h_lower)
                )
                break

    if mask.any():
        df.loc[mask, "result_odds"] = round(closing_odds, 4)
        df.to_csv(LOG_FILE, index=False)


def auto_close_expired(rec: dict):
    """标记已过期的开盘记录（比赛已结束且无法获取收盘价）。"""
    data = _load_opening_odds()
    updated = False
    rmk = rec.get("match_key", "")
    rmkt = rec.get("market", "")
    for record_key, existing in list(data.items()):
        if existing.get("match_key") == rmk and existing.get("market") == rmkt:
            if existing.get("closing_odds") is not None:
                continue
            existing["closing_odds"] = 0
            existing["closing_bookmaker"] = "expired"
            existing["clv"] = 0
            updated = True
            break
    if updated:
        _save_opening_odds(data)


def refresh_closing_odds() -> dict:
    """扫描开盘价中未收盘的记录，在赛前窗口捕获收盘价。

    流程：读取 opening_odds.json → 按联赛分组拉取当前赔率 →
    匹配未关闭记录 → 调用 update_opening_with_closing() + 同步 prediction_log。

    Returns:
        {"updated": N, "skipped": N, "errors": N, "expired": N}
    """
    data = _load_opening_odds()
    if not data:
        return {"updated": 0, "skipped": 0, "errors": 0, "expired": 0}

    result = {"updated": 0, "skipped": 0, "errors": 0, "expired": 0}

    # 按联赛分组未收盘记录
    leagues = {}
    for record_key, rec in data.items():
        if rec.get("closing_odds") is not None:
            continue
        league = rec.get("league", "")
        leagues.setdefault(league, []).append((record_key, rec))

    if not leagues:
        return result

    for league, records in leagues.items():
        sport_keys = _FALLBACK_SPORT_KEYS.get(league, [])
        if not sport_keys:
            sk = _LEAGUE_TO_SPORTKEY.get(league)
            if sk:
                sport_keys = [sk]

        if not sport_keys:
            result["skipped"] += len(records)
            continue

        unmatched = list(records)
        for sk in sport_keys:
            if not unmatched:
                break
            try:
                odds_data = fetch_odds_api(sk, force=True)
            except Exception as e:
                logger.warning("  ⚠️ CLV收盘: %s 赔率拉取失败: %s", league, e)
                result["errors"] += 1
                continue

            if not odds_data or not isinstance(odds_data, (list, tuple)):
                continue

            current_index = _build_h2h_index(odds_data)
            still_unmatched = []
            for record_key, rec in unmatched:
                match_key = rec.get("match_key", "")
                home_name, away_name = _parse_match_key(match_key)
                if not home_name:
                    result["skipped"] += 1
                    continue

                entry = current_index.get((home_name.lower(), away_name.lower()))
                if not entry or entry[0] is None:
                    still_unmatched.append((record_key, rec))
                    continue

                current_odds, bookmaker = entry
                market = rec.get("market", "h2h")
                update_opening_with_closing(match_key, market, current_odds, bookmaker)
                _sync_prediction_log(match_key, current_odds)
                result["updated"] += 1
            unmatched = still_unmatched

        # 多轮尝试后仍未匹配的标记过期
        for record_key, rec in unmatched:
            auto_close_expired(rec)
            result["expired"] += 1

    if result["updated"]:
        logger.info("  📊 CLV收盘价已更新 %d 条", result["updated"])
    if result["expired"]:
        logger.info("  ⌛ CLV过期 %d 条（比赛已结束无法获取收盘价）", result["expired"])
    return result


def main():
    """CLV 更新入口：更新待结算预测的 CLV 并打印报告。"""
    logger.info("\n🔍 CLV 追踪更新 - %s", datetime.now().strftime('%Y-%m-%d %H:%M'))
    # 先尝试填充收盘价
    refresh_closing_odds()
    # 再更新待结算预测
    res = update_clv_for_pending()
    logger.info("  更新: %s, 跳过: %s, 错误: %s", res['updated'], res['skipped'], res['errors'])
    report_clv()
    report_clv_by_bookmaker()


if __name__ == "__main__":
    main()
