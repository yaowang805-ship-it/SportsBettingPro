"""数据质量自动检测管线 — 在每日流程中前置检查数据健康度。

检测项:
  1. 特征CSV: 空值率、日期新鲜度、分布漂移、重复行
  2. 预测日志: 记录量、结算率、异常状态分布
  3. 模型文件: 时效性、特征一致性
  4. Odds API: 响应体完整性、博彩公司数量

用法:
  from src.monitor.data_quality import run_data_quality_check
  report = run_data_quality_check()
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from config.logging_config import get_logger
from fetchers.odds_api import fetch_odds_api

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = ROOT / "models"
DATA_PROCESSED = ROOT / "data" / "processed"
PREDICTION_LOG = ROOT / "data" / "storage" / "prediction_log.csv"

# 阈值配置
MAX_NULL_RATE = 0.05          # 最大允许空值率
MAX_STALE_DAYS = 7             # 特征数据最大允许天数
MAX_DUPLICATE_RATE = 0.01     # 最大允许重复行率
MIN_BOOKMAKERS = 3            # 最少应有博彩公司数
MAX_VOID_RATE = 0.30          # 最大允许 void 率
MIN_DAILY_PREDICTIONS = 5     # 每日最少预测数
MODEL_MAX_AGE_DAYS = 30       # 模型文件最大允许天数


def _load_feature_csv(path: Path) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True)
        return df
    except Exception as e:
        logger.warning("  无法读取 %s: %s", path.name, e)
        return None


def check_feature_quality(name: str, csv_path: Path) -> Dict:
    """检查特征CSV的数据质量。"""
    result = {"file": csv_path.name, "status": "ok", "issues": []}

    df = _load_feature_csv(csv_path)
    if df is None:
        result["status"] = "error"
        result["issues"].append("无法读取文件")
        return result

    result["rows"] = len(df)
    result["cols"] = len(df.columns)

    # 空值检查
    null_rate = df.isnull().mean()
    bad_cols = null_rate[null_rate > MAX_NULL_RATE]
    if not bad_cols.empty:
        result["issues"].append(
            f"空值率过高: {bad_cols.to_dict()}"
        )
        result["status"] = "warning"

    # 整体空值率
    overall_null = df.isnull().mean().mean()
    result["null_rate"] = round(overall_null, 4)
    if overall_null > MAX_NULL_RATE:
        result["status"] = "warning"

    # 日期新鲜度
    if "date" in df.columns and not df["date"].isna().all():
        latest_date = df["date"].max()
        if latest_date.tzinfo is None:
            latest_date = latest_date.tz_localize("UTC")
        days_old = (datetime.now(timezone.utc) - latest_date).days
        result["last_data_date"] = latest_date.strftime("%Y-%m-%d")
        result["days_since_last_data"] = days_old
        if days_old > MAX_STALE_DAYS:
            result["issues"].append(
                f"数据已 {days_old} 天未更新 (阈值: {MAX_STALE_DAYS})"
            )
            result["status"] = "error"

    # 重复行检查
    dup_rate = df.duplicated().mean()
    result["dup_rate"] = round(dup_rate, 4)
    if dup_rate > MAX_DUPLICATE_RATE:
        result["issues"].append(f"重复行率 {dup_rate:.1%}")
        result["status"] = "warning"

    # 最近30天数据量
    recent = df[df["date"] >= pd.Timestamp.now(timezone.utc) - timedelta(days=30)]
    result["recent_30d_rows"] = len(recent)

    # 目标列分布检查
    for col in ["win", "spread_result", "total_result"]:
        if col in df.columns:
            vc = df[col].value_counts(normalize=True)
            result[f"{col}_dist"] = vc.to_dict()

    return result


def check_prediction_log_quality(log_path: Path) -> Dict:
    """检查预测日志的数据质量。"""
    result = {"file": log_path.name, "status": "ok", "issues": []}

    if not log_path.exists():
        result["status"] = "error"
        result["issues"].append("预测日志文件不存在")
        return result

    df = pd.read_csv(log_path)
    result["total_records"] = len(df)
    if df.empty:
        result["status"] = "warning"
        result["issues"].append("预测日志为空")
        return result

    # 状态分布
    status_dist = df["status"].value_counts().to_dict()
    result["status_distribution"] = status_dist

    # Void 率检查
    void_rate = status_dist.get("void", 0) / max(len(df), 1)
    result["void_rate"] = round(void_rate, 4)
    if void_rate > MAX_VOID_RATE:
        result["issues"].append(
            f"Void 率 {void_rate:.1%} 超过阈值 {MAX_VOID_RATE:.0%}"
        )
        result["status"] = "error"

    # 结算率
    settled = status_dist.get("won", 0) + status_dist.get("lost", 0)
    result["settled_rate"] = round(settled / max(len(df), 1), 4)

    # 按运动统计
    sport_dist = df["sport"].value_counts().to_dict()
    result["by_sport"] = sport_dist

    # 按来源统计
    if "source" in df.columns:
        result["by_source"] = df["source"].value_counts().to_dict()

    # 日期范围
    if "timestamp" in df.columns:
        df["timestamp_dt"] = pd.to_datetime(df["timestamp"], errors="coerce")
        valid_ts = df["timestamp_dt"].dropna()
        if not valid_ts.empty:
            result["date_range"] = {
                "from": valid_ts.min().strftime("%Y-%m-%d"),
                "to": valid_ts.max().strftime("%Y-%m-%d"),
            }

    # 缺失字段检查
    required_cols = ["sport", "league", "market_type", "odds", "status"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        result["issues"].append(f"缺少必需字段: {missing}")
        result["status"] = "error"

    return result


def check_model_quality() -> List[Dict]:
    """检查模型文件的时效性和一致性。"""
    results = []

    # 检查模型文件
    for pkl_file in sorted(MODEL_DIR.glob("*.pkl")):
        mtime = datetime.fromtimestamp(pkl_file.stat().st_mtime, tz=timezone.utc)
        age_days = (datetime.now(timezone.utc) - mtime).days
        entry = {
            "file": pkl_file.name,
            "size_kb": round(pkl_file.stat().st_size / 1024, 1),
            "age_days": age_days,
            "status": "ok",
        }
        if age_days > MODEL_MAX_AGE_DAYS:
            entry["status"] = "warning"
            entry["issue"] = f"模型已 {age_days} 天未更新"
        results.append(entry)

    # 检查特征JSON与模型文件版本一致性
    feat_files = list(MODEL_DIR.glob("*features.json"))
    for fjson in feat_files:
        if fjson.exists():
            mtime = datetime.fromtimestamp(fjson.stat().st_mtime, tz=timezone.utc)
            age = (datetime.now(timezone.utc) - mtime).days
            results.append({
                "file": fjson.name,
                "size_kb": round(fjson.stat().st_size / 1024, 1),
                "age_days": age,
                "status": "ok",
            })

    return results


def check_odds_api_quality() -> Dict:
    """实时检查 Odds API 数据质量。"""
    result = {"leagues_checked": 0, "bookmaker_counts": {}, "issues": []}

    test_leagues = [
        ("basketball_nba", "NBA"),
        ("soccer_epl", "英超"),
    ]

    for sport_key, label in test_leagues:
        try:
            data = fetch_odds_api(sport_key, force=True)
            result["leagues_checked"] += 1

            if not data:
                result["issues"].append(f"{label}: API 返回空数据")
                continue

            # 防御：确保 data 是列表
            if not isinstance(data, list):
                result["issues"].append(f"{label}: API 返回非列表类型 ({type(data).__name__})")
                continue

            # 平均博彩公司数
            bm_counts = []
            for match in data:
                bm_count = len(match.get("bookmakers", []))
                bm_counts.append(bm_count)

            avg_bm = np.mean(bm_counts) if bm_counts else 0
            result["bookmaker_counts"][label] = {
                "avg": round(avg_bm, 1),
                "min": min(bm_counts) if bm_counts else 0,
                "max": max(bm_counts) if bm_counts else 0,
            }

            if avg_bm < MIN_BOOKMAKERS:
                result["issues"].append(
                    f"{label}: 平均博彩公司数 {avg_bm:.1f} < {MIN_BOOKMAKERS}"
                )

            # 检查比赛是否有赔率
            matches_with_odds = sum(
                1 for m in data
                if any(m.get("bookmakers", []))
            )
            odds_coverage = matches_with_odds / max(len(data), 1)
            if odds_coverage < 0.5:
                result["issues"].append(
                    f"{label}: 仅有 {odds_coverage:.0%} 比赛有赔率"
                )

        except Exception as e:
            result["issues"].append(f"{label}: API 请求失败 ({e})")

    return result


def run_data_quality_check() -> Dict:
    """运行所有数据质量检查，返回全面报告。

    Returns:
        {
            "timestamp": str,
            "overall_status": "ok" | "warning" | "error",
            "features": [...],
            "prediction_log": {...},
            "models": [...],
            "odds_api": {...},
            "summary": str,
        }
    """
    logger.info("\n" + "=" * 55)
    logger.info("  数据质量自动检测 - %s", datetime.now().strftime('%Y-%m-%d'))
    logger.info("=" * 55)

    report = {
        "timestamp": datetime.now().isoformat(),
        "overall_status": "ok",
        "features": [],
        "prediction_log": {},
        "models": [],
        "odds_api": {},
    }

    issues = []

    # 1. 特征CSV检查
    for name, path in [
        ("NBA特征", DATA_PROCESSED / "bb_features.csv"),
        ("足球特征", DATA_PROCESSED / "fb_features.csv"),
    ]:
        logger.info("\n  [特征] %s:", name)
        fq = check_feature_quality(name, path)
        report["features"].append(fq)
        logger.info("    行: %s, 列: %s, 空值率: %.2f%%",
                    fq.get("rows", "?"), fq.get("cols", "?"),
                    fq.get("null_rate", 0) * 100)
        if fq.get("status") != "ok":
            for iss in fq.get("issues", []):
                logger.warning("    ⚠️ %s", iss)
                issues.append(f"[{name}] {iss}")

    # 2. 预测日志检查
    logger.info("\n  [日志] 预测记录:")
    pl = check_prediction_log_quality(PREDICTION_LOG)
    report["prediction_log"] = pl
    logger.info("    总记录: %s, Void率: %.1f%%",
                pl.get("total_records", 0),
                pl.get("void_rate", 0) * 100)
    if pl.get("status") != "ok":
        for iss in pl.get("issues", []):
            logger.warning("    ⚠️ %s", iss)
            issues.append(f"[预测日志] {iss}")

    # 3. 模型文件检查
    logger.info("\n  [模型] 文件状态:")
    models = check_model_quality()
    report["models"] = models
    for m in models:
        status_sym = "⚠️" if m["status"] != "ok" else "✅"
        logger.info("    %s %s (%d天, %sKB)",
                    status_sym, m["file"], m["age_days"], m["size_kb"])
        if m.get("issue"):
            issues.append(f"[模型] {m['file']}: {m['issue']}")

    # 4. Odds API 检查
    logger.info("\n  [API] Odds API 数据质量:")
    oq = check_odds_api_quality()
    report["odds_api"] = oq
    for league, bc in oq.get("bookmaker_counts", {}).items():
        logger.info("    %s: 平均 %.1f 家博彩公司 (范围 %d-%d)",
                    league, bc["avg"], bc["min"], bc["max"])
    for iss in oq.get("issues", []):
        logger.warning("    ⚠️ %s", iss)
        issues.append(f"[API] {iss}")

    # 汇总
    if issues:
        report["issues"] = issues
        has_error = any(
            fq.get("status") == "error" for fq in report["features"]
        ) or pl.get("status") == "error"
        report["overall_status"] = "error" if has_error else "warning"
        report["summary"] = f"发现 {len(issues)} 个问题" + (" (含严重错误)" if has_error else "")
        if has_error:
            logger.error("\n  ❌ %s", report["summary"])
        else:
            logger.warning("\n  ⚠️ %s", report["summary"])
    else:
        report["summary"] = "所有检查通过"
        logger.info("\n  ✅ 所有数据质量检查通过")

    logger.info("=" * 55)

    return report


if __name__ == "__main__":
    run_data_quality_check()
