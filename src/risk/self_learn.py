"""策略自进化模块 — 用实际结算数据驱动联赛分层 + Kelly 自动调整。

工作流:
  1. 读取 settlement_log.csv（每笔已结算投注）
  2. 按联赛维度统计：场次、胜率、期望胜率(1/赔率)、盈亏、ROI
  3. 对比实际 ROI 与预期：表现差 → 降级 / 收紧 Kelly；表现好 → 升级 / 放松
  4. 更新 league_tiers.json + 输出调整报告

用法:
    python3 -m src.risk.self_learn                     # 只分析+输出报告
    python3 -m src.risk.self_learn --apply              # 分析并应用调整
"""
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
import sys; sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR

logger = get_logger(__name__)

_SETTLEMENT_LOG = DATA_DIR / "settlement_log.csv"
_LEAGUE_TIERS_FILE = DATA_DIR / "league_tiers.json"
_STRATEGY_FILE = DATA_DIR / "strategy_optimizer.json"
_MIN_SAMPLES = 10          # 最少样本数才调整
_DEMOTE_ROI = -0.08        # ROI < -8% 降级
_PROMOTE_ROI = 0.05        # ROI > +5% 升级
_MAX_TIER = 4
_KELLY_TIER_MULT = {1: 1.0, 2: 0.9, 3: 0.7, 4: 0.5}


def _load_settlements() -> list:
    """从 CSV 加载所有结算记录。"""
    if not _SETTLEMENT_LOG.exists():
        logger.warning("结算日志不存在: %s", _SETTLEMENT_LOG)
        return []
    with open(_SETTLEMENT_LOG) as f:
        return list(csv.DictReader(f))


def _load_tiers() -> dict:
    if _LEAGUE_TIERS_FILE.exists():
        return json.loads(_LEAGUE_TIERS_FILE.read_text())
    return {}


def _save_tiers(tiers: dict):
    _LEAGUE_TIERS_FILE.write_text(json.dumps(tiers, ensure_ascii=False, indent=2))


def analyze() -> dict:
    """综合分析结算数据，返回每个联赛的统计和调整建议。"""
    rows = _load_settlements()
    if not rows:
        return {"status": "no_data"}

    # 按联赛分组
    by_league = defaultdict(list)
    for r in rows:
        league = r.get("league", "?").strip()
        by_league[league].append(r)

    tiers = _load_tiers()
    recommendations = []
    league_stats = {}

    for league, entries in sorted(by_league.items()):
        n = len(entries)
        wins = sum(1 for e in entries if e.get("outcome", "").strip() == "won")
        losses = sum(1 for e in entries if e.get("outcome", "").strip() in ("lost", "loss"))
        # 期望概率: 1/odds 的平均值
        total_expected = 0.0
        total_stake = 0.0
        total_pnl = 0.0
        valid_odds = 0
        for e in entries:
            try:
                odds = float(e.get("odds", 0))
                stake = float(e.get("stake", 0))
                profit = float(e.get("profit", 0))
            except (ValueError, TypeError):
                continue
            if odds > 1 and stake > 0:
                total_expected += 1.0 / odds
                total_stake += stake
                total_pnl += profit
                valid_odds += 1

        win_rate = wins / n if n > 0 else 0
        avg_expected = total_expected / valid_odds if valid_odds > 0 else 0
        roi = total_pnl / total_stake if total_stake > 0 else 0

        current_tier = _find_tier(league, tiers)
        suggested_tier = current_tier
        reason = ""

        if n >= _MIN_SAMPLES and valid_odds >= _MIN_SAMPLES:
            # 亏损过多 → 降级
            if roi < _DEMOTE_ROI:
                suggested_tier = min(current_tier + 1, _MAX_TIER)
                reason = f"ROI={roi:.1%} < {_DEMOTE_ROI:.0%} → 降级至 T{suggested_tier}"
            # 表现良好 → 升级（不低于 T1）
            elif roi > _PROMOTE_ROI and current_tier > 1:
                suggested_tier = max(current_tier - 1, 1)
                reason = f"ROI={roi:.1%} > {_PROMOTE_ROI:.0%} → 升级至 T{suggested_tier}"

        league_stats[league] = {
            "total_bets": n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "avg_expected_prob": round(avg_expected, 4),
            "roi": round(roi, 4),
            "total_pnl": round(total_pnl, 2),
            "total_stake": round(total_stake, 2),
            "current_tier": current_tier,
            "suggested_tier": suggested_tier,
            "adjust_reason": reason,
        }
        if reason:
            recommendations.append({
                "league": league,
                "from_tier": current_tier,
                "to_tier": suggested_tier,
                "reason": reason,
            })

    return {
        "status": "ok",
        "total_bets": len(rows),
        "leagues_analyzed": len(league_stats),
        "leagues": league_stats,
        "recommendations": recommendations,
    }


def _find_tier(league: str, tiers: dict) -> int:
    """从 league_tiers.json 中查找联赛当前层级。"""
    for kw, tier in tiers.items():
        if kw in league:
            return tier
    return 3  # 默认 Tier 3


def apply_adjustments(report: dict) -> int:
    """应用报告中的调整建议到 league_tiers.json。"""
    tiers = _load_tiers()
    changes = 0
    for rec in report.get("recommendations", []):
        league = rec["league"]
        new_tier = rec["to_tier"]
        # 移除旧映射（避免冲突）
        to_delete = [kw for kw in tiers if kw in league]
        for kw in to_delete:
            del tiers[kw]
        tiers[league] = new_tier
        changes += 1
        logger.info("  %s: T%s → T%s (%s)", league, rec["from_tier"], new_tier, rec["reason"])
    if changes:
        _save_tiers(tiers)
        logger.info("已更新 %d 个联赛的层级", changes)
    return changes


def print_report(report: dict):
    """打印分析报告。"""
    if report.get("status") == "no_data":
        print("\n❌ 无结算数据，请先运行自动结算")
        return

    print(f"\n{'=' * 60}")
    print(f"📊 策略自进化分析报告")
    print(f"{'=' * 60}")
    print(f"总投注数: {report['total_bets']}")
    print(f"联赛数: {report['leagues_analyzed']}")

    recs = report.get("recommendations", [])
    stats = report.get("leagues", {})

    if recs:
        print(f"\n🔧 调整建议 ({len(recs)} 条):")
        for r in recs:
            print(f"  {r['league']}: T{r['from_tier']} → T{r['to_tier']} ({r['reason']})")
    else:
        print("\n✅ 无需调整（所有联赛表现正常）")

    # 打印每个联赛的详细统计
    print(f"\n📋 联赛详细统计:")
    print(f"  {'联赛':<30} {'场次':>5} {'胜率':>7} {'期望':>7} {'ROI':>8} {'Tier':>5}")
    print(f"  {'-'*30} {'-'*5} {'-'*7} {'-'*7} {'-'*8} {'-'*5}")
    for league, s in sorted(stats.items()):
        if s["total_bets"] < _MIN_SAMPLES:
            continue
        print(f"  {league[:28]:<30} {s['total_bets']:>5} {s['win_rate']:>6.0%} "
              f"{s['avg_expected_prob']:>6.0%} {s['roi']:>7.1%} "
              f"{s['current_tier']:>2}→{s['suggested_tier']:<1}")


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    report = analyze()
    print_report(report)
    if "--apply" in sys.argv:
        n = apply_adjustments(report)
        print(f"\n已应用 {n} 条调整")
