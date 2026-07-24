"""ROI 自进化联赛分层 — 用结算数据驱动 league_tiers.json 自动调整。

从 settlement_log.csv 读取每个联赛的结算数据，
按 ROI 自动升级/降级联赛层级，持久化到 league_tiers.json。

用法:
    python3 -m src.report.auto_tier_updater           # 执行更新
    python3 -m src.report.auto_tier_updater --dry-run  # 只输出不写入
"""
import csv, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

# 升级/降级规则（≥20 笔才触发调整）
MIN_BETS_FOR_TIER_CHANGE = 20
MIN_BETS_FOR_T2_UPGRADE = 30  # ROI>+5% 需更多样本才升 T2

# Tier 边界
TIER_CAP = {1: 1, 2: 2, 3: 3}  # 最高可升到的层级


def _load_settlement_csv():
    """加载 settlement_log.csv，返回 list[dict]。"""
    fpath = DATA_DIR / "settlement_log.csv"
    if not fpath.exists():
        logger.error("settlement_log.csv not found at %s", fpath)
        return []
    with open(fpath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for r in reader:
            league = r.get("league", "").strip()
            if not league:
                continue
            try:
                r["_stake"] = float(r.get("stake", 0))
                r["_profit"] = float(r.get("profit", 0))
            except (ValueError, TypeError):
                continue
            rows.append(r)
    return rows


def _compute_league_metrics(rows):
    """按联赛聚合结算数据，返回 {league: metrics_dict}。"""
    by_league = defaultdict(lambda: {"bets": 0, "wins": 0, "total_stake": 0.0, "total_profit": 0.0})
    for r in rows:
        league = r["league"]
        m = by_league[league]
        m["bets"] += 1
        m["total_stake"] += r["_stake"]
        m["total_profit"] += r["_profit"]
        if r.get("outcome", "").strip().lower() == "won":
            m["wins"] += 1

    metrics = {}
    for league, m in by_league.items():
        roi = m["total_profit"] / m["total_stake"] if m["total_stake"] else 0
        win_rate = m["wins"] / m["bets"] if m["bets"] else 0
        metrics[league] = {
            "bets": m["bets"],
            "wins": m["wins"],
            "total_stake": round(m["total_stake"], 2),
            "total_profit": round(m["total_profit"], 2),
            "roi": round(roi * 100, 2),
            "win_rate": round(win_rate * 100, 2),
        }
    return metrics


def _load_current_tiers():
    """加载当前 league_tiers.json。"""
    fpath = DATA_DIR / "league_tiers.json"
    if fpath.exists():
        return json.loads(fpath.read_text())
    return {}


def _save_tiers(tiers):
    """写入 league_tiers.json。"""
    fpath = DATA_DIR / "league_tiers.json"
    fpath.write_text(json.dumps(tiers, ensure_ascii=False, indent=2))
    logger.info("已更新 %s (%d 个联赛)", fpath, len(tiers))


def _suggest_new_tier(league, metrics, current_tier):
    """根据 ROI 规则建议新 tier。

    Tier numbering: 1=best, 4=worst(banned).
    升级 = new_tier < current_tier (number goes down)
    降级 = new_tier > current_tier (number goes up)
    """
    bets = metrics["bets"]
    roi = metrics["roi"]

    if bets < MIN_BETS_FOR_TIER_CHANGE:
        return current_tier, None  # 样本不足 → 不触发变更

    new_tier = current_tier

    # 升级：ROI 好 → 提高优先级（降低 tier number）
    if roi > 10.0:
        new_tier = min(new_tier, 1)  # 升到 T1（封顶）
    elif roi > 5.0 and bets >= MIN_BETS_FOR_T2_UPGRADE:
        new_tier = min(new_tier, 2)  # 升到 T2（封顶）

    # 降级：ROI 差 → 降低优先级（提高 tier number）
    if roi < -20.0:
        new_tier = 4  # 拉黑
    elif roi < -10.0:
        new_tier = max(new_tier, 3)  # 最多留 T3

    if new_tier == current_tier:
        return current_tier, None

    if new_tier < current_tier:
        reason = "ROI {:.1f}% → 升级 T{}→T{}".format(roi, current_tier, new_tier)
    else:
        reason = "ROI {:.1f}% → 降级 T{}→T{}".format(roi, current_tier, new_tier)
    return new_tier, reason


def compute_tier_updates(dry_run=False):
    """主逻辑：计算并应用联赛分层更新。

    Args:
        dry_run: True 时只输出不写入。

    Returns:
        (changes: list[dict], report: dict)
        changes — 每条变更含 league, old_tier, new_tier, reason, metrics
        report — 汇总报告
    """
    rows = _load_settlement_csv()
    if not rows:
        logger.warning("无结算数据，跳过")
        return [], {}

    metrics = _compute_league_metrics(rows)
    current_tiers = _load_current_tiers()

    changes = []
    leagues_with_data = set()
    for league, m in sorted(metrics.items()):
        leagues_with_data.add(league)
        current_tier = current_tiers.get(league, 3)
        new_tier, reason = _suggest_new_tier(league, m, current_tier)
        if reason:
            changes.append({
                "league": league,
                "old_tier": current_tier,
                "new_tier": new_tier,
                "reason": reason,
                "metrics": m,
            })
            if not dry_run:
                current_tiers[league] = new_tier

    # 从未出现在 tiers 文件中的联赛，用默认 tier 3
    for league in leagues_with_data - set(current_tiers.keys()):
        current_tiers.setdefault(league, 3)

    if not dry_run and changes:
        _save_tiers(current_tiers)

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": dry_run,
        "total_leagues_with_data": len(metrics),
        "changes_count": len(changes),
        "changes": changes,
        "per_league_metrics": metrics,
    }
    return changes, report


def _save_report(report):
    """保存报告到 data/reports/tier_update_YYYY-MM-DD.json。"""
    report_dir = ROOT / "data" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fpath = report_dir / f"tier_update_{date_str}.json"
    fpath.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("报告已保存: %s", fpath)
    return fpath


def main():
    dry_run = "--dry-run" in sys.argv
    logger.info("=== ROI 自进化联赛分层 (%s) ===", "dry-run" if dry_run else "执行")
    changes, report = compute_tier_updates(dry_run=dry_run)

    if not changes:
        print("\n✅ 无联赛层级变更")
        return

    print(f"\n{'='*60}")
    print(f"联赛层级变更 ({len(changes)} 条):")
    print(f"{'='*60}")
    for c in changes:
        arrow = "↑" if c["new_tier"] < c["old_tier"] else "↓"
        print(f"  {arrow} {c['league']}: T{c['old_tier']} → T{c['new_tier']}")
        print(f"     {c['reason']}")
        print(f"     笔数={c['metrics']['bets']}, ROI={c['metrics']['roi']}%, "
              f"胜率={c['metrics']['win_rate']}%, "
              f"利润=¥{c['metrics']['total_profit']:+.0f}")

    if dry_run:
        print("\n⚠️  Dry-run 模式，未写入 league_tiers.json")
    else:
        print(f"\n✅ league_tiers.json 已更新")

    # 保存报告（无论 dry_run）
    _save_report(report)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
