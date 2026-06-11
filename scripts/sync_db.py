"""数据库回填脚本 — 将 CSV/JSON 数据同步到 SQLAlchemy 表。

用法:
    python scripts/sync_db.py              # 全量同步
    python scripts/sync_db.py --dry-run     # 仅预览，不写入
    python scripts/sync_db.py --table clv   # 仅同步指定表

支持的表: predictions, clv, performance, accuracy, odds_cache
"""
import argparse
import json
import csv
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.logging_config import get_logger, setup_logging
setup_logging(log_to_file=False)
from src.storage.database import db, _utcnow
from src.storage.models import Prediction, CLVData, PerformanceSnapshot, \
    ModelAccuracy, OddsCache
from src.dashboard.config import DATA_DIR

from sqlalchemy import text

logger = get_logger("sync_db")


# ─── helpers ───────────────────────────────────────────────────────────────

def _parse_ts(val: str) -> datetime:
    """解析 ISO 时间戳 → datetime(utc)"""
    if not val:
        return _utcnow()
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return _utcnow()


def _safe_float(val, default=None):
    if val is None or val == "" or val == "null":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ─── table syncs ──────────────────────────────────────────────────────────

def sync_predictions(dry_run: bool = False) -> int:
    """prediction_log.csv → predictions 表"""
    path = DATA_DIR / "prediction_log.csv"
    if not path.exists():
        logger.warning("  ⏭️  prediction_log.csv 不存在")
        return 0

    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            market = row.get("market_type", "")
            detail = row.get("market_detail", "")
            home_prob = away_prob = draw_prob = over_prob = under_prob = None

            if "胜" in market or "负" in market:
                if "主" in detail:
                    home_prob = _safe_float(row.get("model_prob"))
                elif "客" in detail:
                    away_prob = _safe_float(row.get("model_prob"))
            elif "大小" in market or "大" in detail or "小" in detail:
                if "大" in detail:
                    over_prob = _safe_float(row.get("model_prob"))
                elif "小" in detail:
                    under_prob = _safe_float(row.get("model_prob"))

            model_name = row.get("source", "unknown")
            status = row.get("status", "pending")
            was_correct = 1 if status == "won" else (0 if status == "lost" else None)

            # 队名优先级: 英文 > 中文 > 原始
            ht = (row.get("home_team_en") or row.get("home_team_cn")
                  or row.get("home_team", ""))
            at = (row.get("away_team_en") or row.get("away_team_cn")
                  or row.get("away_team", ""))
            rows.append(Prediction(
                match_key=row.get("id", ""),
                sport=row.get("sport", ""),
                home_team=ht,
                away_team=at,
                commence_time=row.get("match_time", ""),
                model_name=model_name,
                home_prob=home_prob,
                away_prob=away_prob,
                draw_prob=draw_prob,
                over_prob=over_prob,
                under_prob=under_prob,
                predicted_at=_parse_ts(row.get("timestamp", "")),
                was_correct=was_correct,
            ))

    if not rows:
        return 0
    if dry_run:
        logger.info("  📊 predictions: 将插入 %d 行", len(rows))
        return len(rows)

    with db.Session() as session:
        # 清空旧数据（幂等）
        session.execute(text("DELETE FROM predictions"))
        # 分批插入
        for i in range(0, len(rows), 100):
            session.add_all(rows[i:i+100])
        session.commit()
    logger.info("  ✅ predictions: 插入 %d 行", len(rows))
    return len(rows)


def sync_clv(dry_run: bool = False) -> int:
    """opening_odds.json → clv_data 表"""
    path = DATA_DIR / "opening_odds.json"
    if not path.exists():
        logger.warning("  ⏭️  opening_odds.json 不存在")
        return 0

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    for key, entry in data.items():
        rows.append(CLVData(
            match_key=entry.get("match_key", key.split("|")[0]),
            bookmaker=entry.get("opening_bookmaker", ""),
            market=entry.get("market", ""),
            opening_odds=_safe_float(entry.get("opening_odds")),
            closing_odds=_safe_float(entry.get("closing_odds")),
            clv=_safe_float(entry.get("clv")),
            captured_at=_parse_ts(entry.get("captured_at", "")),
        ))

    if not rows:
        return 0
    if dry_run:
        logger.info("  📊 clv_data: 将插入 %d 行", len(rows))
        return len(rows)

    with db.Session() as session:
        session.execute(text("DELETE FROM clv_data"))
        for i in range(0, len(rows), 100):
            session.add_all(rows[i:i+100])
        session.commit()
    logger.info("  ✅ clv_data: 插入 %d 行", len(rows))
    return len(rows)


def sync_performance(dry_run: bool = False) -> int:
    """performance_history.csv + virtual_portfolio.json → performance_snapshots 表"""
    rows = []

    # 来源1: performance_history.csv（每条记录是一个快照）
    perf_path = DATA_DIR / "performance_history.csv"
    if perf_path.exists():
        with open(perf_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                balance = _safe_float(row.get("cumulative_balance"))
                if balance is None:
                    continue
                profit = _safe_float(row.get("profit", 0))
                stake = _safe_float(row.get("stake", 0))
                total_bets = None
                roi = None
                if stake and stake > 0 and profit is not None:
                    roi = profit / stake

                rows.append(PerformanceSnapshot(
                    timestamp=_parse_ts(row.get("date", "")),
                    balance=balance,
                    equity=balance,
                    roi=roi,
                    total_bets=1,
                    settled_bets=1 if row.get("result") in ("won", "lost") else 0,
                    notes=f"from perf_history: {row.get('game', '')[:100]}",
                ))

    # 来源2: virtual_portfolio.json（当前余额作为最新快照）
    port_path = DATA_DIR / "virtual_portfolio.json"
    if port_path.exists():
        with open(port_path, encoding="utf-8") as f:
            port = json.load(f)
        balance = _safe_float(port.get("balance"))
        if balance is not None:
            history = port.get("history", [])
            settled = sum(1 for h in history if h.get("status") in ("won", "lost"))
            total_profit = sum(_safe_float(h.get("profit", 0)) or 0 for h in history)
            rows.append(PerformanceSnapshot(
                timestamp=_utcnow(),
                balance=balance,
                equity=balance,
                total_bets=len(history),
                settled_bets=settled,
                notes="from virtual_portfolio.json (latest)",
            ))

    if not rows:
        return 0
    if dry_run:
        logger.info("  📊 performance_snapshots: 将插入 %d 行", len(rows))
        return len(rows)

    with db.Session() as session:
        session.execute(text("DELETE FROM performance_snapshots"))
        for i in range(0, len(rows), 100):
            session.add_all(rows[i:i+100])
        session.commit()
    logger.info("  ✅ performance_snapshots: 插入 %d 行", len(rows))
    return len(rows)


def sync_accuracy(dry_run: bool = False) -> int:
    """prediction_log.csv 已结算结果 → model_accuracy 表"""
    path = DATA_DIR / "prediction_log.csv"
    if not path.exists():
        logger.warning("  ⏭️  prediction_log.csv 不存在")
        return 0

    stats = {}  # (model_name, target) → {"total": N, "correct": N}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            status = row.get("status", "")
            if status not in ("won", "lost"):
                continue
            model = row.get("source", "unknown")
            target = row.get("sport", "unknown")
            key = (model, target)
            if key not in stats:
                stats[key] = {"total": 0, "correct": 0}
            stats[key]["total"] += 1
            if status == "won":
                stats[key]["correct"] += 1

    if not stats:
        return 0

    rows = []
    for (model, target), s in stats.items():
        acc = s["correct"] / s["total"] if s["total"] > 0 else 0.0
        rows.append(ModelAccuracy(
            model_name=model,
            target=target,
            total_predictions=s["total"],
            correct=s["correct"],
            accuracy=round(acc, 4),
            updated_at=_utcnow(),
        ))

    if dry_run:
        logger.info("  📊 model_accuracy: 将插入 %d 行", len(rows))
        return len(rows)

    with db.Session() as session:
        session.execute(text("DELETE FROM model_accuracy"))
        for i in range(0, len(rows), 100):
            session.add_all(rows[i:i+100])
        session.commit()
    logger.info("  ✅ model_accuracy: 插入 %d 行", len(rows))
    return len(rows)


def sync_odds_cache(dry_run: bool = False) -> int:
    """odds/*.json → odds_cache 表（实时盘口快照）"""
    odds_dir = DATA_DIR / "odds"
    if not odds_dir.exists():
        logger.warning("  ⏭️  odds/ 目录不存在")
        return 0

    rows = []
    for fpath in sorted(odds_dir.glob("*.json")):
        sport_key = fpath.stem.replace("_odds", "").replace("live_", "")
        try:
            with open(fpath, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, Exception):
            continue

        games = data if isinstance(data, list) else data.get("data", [])
        for g in games:
            home = g.get("home_team") or g.get("home", "")
            away = g.get("away_team") or g.get("away", "")
            if not home or not away:
                continue
            rows.append(OddsCache(
                sport_key=sport_key[:64],
                home_team=str(home)[:128],
                away_team=str(away)[:128],
                bookmaker="",
                commence_time=g.get("commence_time", ""),
                fetched_at=_utcnow(),
            ))

    if not rows:
        logger.info("  ⏭️  odds_cache: 无可解析数据")
        return 0
    if dry_run:
        logger.info("  📊 odds_cache: 将插入 %d 行（来自 %d 文件）", len(rows), len(list(odds_dir.glob("*.json"))))
        return len(rows)

    with db.Session() as session:
        session.execute(text("DELETE FROM odds_cache"))
        for i in range(0, len(rows), 100):
            session.add_all(rows[i:i+100])
        session.commit()
    logger.info("  ✅ odds_cache: 插入 %d 行", len(rows))
    return len(rows)


# ─── main ──────────────────────────────────────────────────────────────────

TABLE_MAP = {
    "predictions": sync_predictions,
    "clv": sync_clv,
    "performance": sync_performance,
    "accuracy": sync_accuracy,
    "odds_cache": sync_odds_cache,
}


def main():
    parser = argparse.ArgumentParser(description="数据库回填 — CSV/JSON → SQLAlchemy")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入")
    parser.add_argument("--table", choices=list(TABLE_MAP.keys()) + ["all"],
                        default="all", help="指定同步的表")
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("  DB 同步 %s", "(DRY RUN)" if args.dry_run else "")
    logger.info("=" * 50)

    total = 0
    tables = list(TABLE_MAP.keys()) if args.table == "all" else [args.table]
    for name in tables:
        fn = TABLE_MAP[name]
        try:
            cnt = fn(dry_run=args.dry_run)
            total += cnt
        except Exception as e:
            logger.error("  ❌ %s 同步失败: %s", name, e, exc_info=True)

    mode = "（模拟）" if args.dry_run else ""
    logger.info("=" * 50)
    logger.info("  完成%s: 共处理 %d 行", mode, total)
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
