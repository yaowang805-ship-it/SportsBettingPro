"""推荐记录追踪器：记录每次推送的所有推荐比赛，支持结算反查和全统计。"""

import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data/storage"))
RECOMMENDATION_LOG = DATA_DIR / "recommendation_log.csv"
RECOMMENDATION_STATS = DATA_DIR / "recommendation_stats.json"

FIELDS = [
    "push_date", "scan_type", "sport", "league",
    "home_cn", "away_cn", "home_team", "away_team",
    "market_type", "sub_market", "designation",
    "bb_odds", "pin_odds", "fair_price", "ev_pct",
    "stake", "was_placed", "bet_id",
    "status", "result", "profit",
    "start_time",
]


def _ensure_file():
    """确保 CSV 文件存在且有表头。"""
    RECOMMENDATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not RECOMMENDATION_LOG.exists():
        with open(RECOMMENDATION_LOG, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(FIELDS)


def _make_fingerprint(op: dict) -> str:
    """生成推荐指纹，用于去重。包含子市场类型防止跨市场误杀。"""
    sport = op.get("sport", "")
    league = op.get("league", "")
    home = op.get("home_cn", "") or op.get("home_team", "")
    away = op.get("away_cn", "") or op.get("away_team", "")
    desig = op.get("designation", "")
    # _sub_market 区分同一 designation 的不同市场（如 1X2 客胜 vs HT 客胜）
    sub = op.get("_sub_market", op.get("_market", ""))
    return f"{sport}|{league}|{home}|{away}|{desig}|{sub}"


def log_recommendations(opportunities: list, scan_type: str = "full"):
    """记录一次推送的所有推荐比赛。

    Args:
        opportunities: bb_ev_push 推送的合格机会列表（含 _stake/_market_type 等字段）
        scan_type: "full" 或 "incremental"
    """
    _ensure_file()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 读取已有指纹，避免重复记录（含 sub_market 区分跨市场）
    existing_fps = set()
    if RECOMMENDATION_LOG.exists():
        with open(RECOMMENDATION_LOG, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sub = row.get("sub_market", "")
                fp = f"{row.get('push_date', '')}|{row.get('sport', '')}|{row.get('league', '')}|{row.get('home_cn', '')}|{row.get('away_cn', '')}|{row.get('designation', '')}|{sub}"
                existing_fps.add(fp)

    new_rows = []
    for op in opportunities:
        fp = _make_fingerprint(op)
        if f"{today}|{fp}" in existing_fps:
            continue

        was_placed = bool(op.get("_stake", 0))
        row = {
            "push_date": today,
            "scan_type": scan_type,
            "sport": op.get("sport", ""),
            "league": op.get("league", ""),
            "home_cn": op.get("home_cn", ""),
            "away_cn": op.get("away_cn", ""),
            "home_team": op.get("home_team", ""),
            "away_team": op.get("away_team", ""),
            "market_type": op.get("_market_type", ""),
            "sub_market": op.get("_sub_market", op.get("_market", "")),
            "designation": op.get("designation", ""),
            "bb_odds": op.get("bb_odds", 0),
            "pin_odds": op.get("pin_odds", 0),
            "fair_price": op.get("fair_price", 0),
            "ev_pct": op.get("ev_pct", 0),
            "stake": op.get("_stake", 0),
            "was_placed": "yes" if was_placed else "no",
            "bet_id": op.get("_bet_id", ""),
            "status": "pending",
            "result": "",
            "profit": "",
            "start_time": op.get("start_time_bb", ""),
        }
        new_rows.append(row)

    if new_rows:
        with open(RECOMMENDATION_LOG, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writerows(new_rows)
        logger.info("推荐记录: 新增 %d 条 (累计 %d 条)", len(new_rows), _count_rows())
    else:
        logger.info("推荐记录: 无新增")


def _count_rows() -> int:
    """返回 CSV 行数（不含表头）。"""
    if not RECOMMENDATION_LOG.exists():
        return 0
    with open(RECOMMENDATION_LOG, "r", encoding="utf-8") as f:
        return sum(1 for _ in f) - 1


def load_all():
    """加载全部推荐记录，返回 list[dict]。"""
    if not RECOMMENDATION_LOG.exists():
        return []
    with open(RECOMMENDATION_LOG, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_by_date(date_str: str):
    """按日期加载推荐记录。"""
    return [r for r in load_all() if r.get("push_date") == date_str]


def get_statistics(days: Optional[int] = None):
    """获取推荐统计。

    Args:
        days: 最近 N 天，None = 全部

    Returns:
        dict with keys: total, placed, settled, won, lost, win_rate, total_stake,
                        total_profit, roi, by_scan_type, by_sport
    """
    records = load_all()
    if days:
        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        records = [r for r in records if r.get("push_date", "") >= cutoff]

    total = len(records)
    placed = [r for r in records if r.get("was_placed") == "yes"]
    settled = [r for r in placed if r.get("status") == "settled"]
    won = [r for r in settled if r.get("result") == "won"]
    lost = [r for r in settled if r.get("result") == "lost"]

    total_stake = sum(float(r.get("stake", 0)) for r in placed)
    total_profit = sum(float(r.get("profit", 0)) for r in settled)

    # 按扫描类型分类
    by_scan = {}
    for r in records:
        st = r.get("scan_type", "unknown")
        by_scan.setdefault(st, {"total": 0, "placed": 0})
        by_scan[st]["total"] += 1
        if r.get("was_placed") == "yes":
            by_scan[st]["placed"] += 1

    # 按运动分类
    by_sport = {}
    for r in records:
        sp = r.get("sport", "unknown")
        by_sport.setdefault(sp, {"total": 0, "placed": 0})
        by_sport[sp]["total"] += 1
        if r.get("was_placed") == "yes":
            by_sport[sp]["placed"] += 1

    stats = {
        "total": total,
        "placed": len(placed),
        "settled": len(settled),
        "won": len(won),
        "lost": len(lost),
        "win_rate": round(len(won) / len(settled) * 100, 1) if settled else 0,
        "total_stake": round(total_stake, 2),
        "total_profit": round(total_profit, 2),
        "roi": round(total_profit / total_stake * 100, 1) if total_stake else 0,
        "by_scan_type": by_scan,
        "by_sport": by_sport,
    }
    return stats


def update_settlement(bet_id: str, status: str, result: str = "", profit: float = 0):
    """更新推荐记录中的结算状态（通过 bet_id 匹配）。

    用于 auto_settle.py 结算后回写。
    """
    if not RECOMMENDATION_LOG.exists():
        return
    rows = load_all()
    updated = 0
    for row in rows:
        if row.get("bet_id") == bet_id and row.get("status") != "settled":
            row["status"] = status
            row["result"] = result
            row["profit"] = str(round(profit, 2))
            updated += 1
    if updated:
        _rewrite_all(rows)
        logger.info("推荐记录结算: 更新 %d 条 bet_id=%s", updated, bet_id)


def update_bet_id(bet_id: str, opportunity_fp: str):
    """投注后回写 bet_id（由 bb_virtual_bet.py 调用）。

    通过指纹定位记录。
    """
    if not RECOMMENDATION_LOG.exists():
        return
    rows = load_all()
    updated = 0
    for row in rows:
        fp = _make_fingerprint_from_row(row)
        if fp == opportunity_fp and not row.get("bet_id"):
            row["bet_id"] = bet_id
            row["was_placed"] = "yes"
            updated += 1
    if updated:
        _rewrite_all(rows)
        logger.debug("推荐记录 bet_id 回写: %d 条", updated)


def _make_fingerprint_from_row(row: dict) -> str:
    sport = row.get("sport", "")
    league = row.get("league", "")
    home = row.get("home_cn", "") or row.get("home_team", "")
    away = row.get("away_cn", "") or row.get("away_team", "")
    desig = row.get("designation", "")
    sub = row.get("sub_market", "")
    return f"{sport}|{league}|{home}|{away}|{desig}|{sub}"


def _rewrite_all(rows: list):
    """覆写整个 CSV。"""
    _ensure_file()
    with open(RECOMMENDATION_LOG, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(days: Optional[int] = None):
    """打印统计摘要到 stdout。"""
    stats = get_statistics(days)
    print(f"📊 推荐统计{' (近{天数}天)'.format(天数=days) if days else ' (全部)'}")
    print(f"  {'':-<40}")
    print(f"  总推荐:      {stats['total']} 条")
    print(f"  已投注:      {stats['placed']} 条")
    print(f"  已结算:      {stats['settled']} 条")
    print(f"  胜:          {stats['won']}")
    print(f"  负:          {stats['lost']}")
    if stats["settled"]:
        print(f"  胜率:        {stats['win_rate']}%")
    print(f"  总投注额:    ¥{stats['total_stake']:,.2f}")
    print(f"  总盈亏:      ¥{stats['total_profit']:+,.2f}")
    if stats["total_stake"]:
        print(f"  ROI:         {stats['roi']:+.1f}%")
    if stats["by_scan_type"]:
        print(f"  {'':-<40}")
        print(f"  按扫描类型:")
        for st, v in stats["by_scan_type"].items():
            print(f"    {st}: {v['total']} 推荐 / {v['placed']} 已投注")
    if stats["by_sport"]:
        print(f"  按运动:")
        for sp, v in sorted(stats["by_sport"].items()):
            print(f"    {sp}: {v['total']} 推荐 / {v['placed']} 已投注")


if __name__ == "__main__":
    import sys
    from config.logging_config import setup_logging
    setup_logging()
    if "--stats" in sys.argv:
        days_arg = None
        for i, a in enumerate(sys.argv):
            if a == "--days" and i + 1 < len(sys.argv):
                days_arg = int(sys.argv[i + 1])
        print_summary(days_arg)
    elif "--list-today" in sys.argv:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = load_by_date(today)
        print(f"今日推荐 ({today}): {len(rows)} 条")
        for r in rows:
            print(f"  [{r['sport']}] {r['home_cn']} vs {r['away_cn']} | {r['designation']} | EV={r['ev_pct']}% | {'✅已投' if r['was_placed']=='yes' else '❌未投'}")
    else:
        print("用法: python3 -m src.report.recommendation_tracker --stats [--days N]")
