"""每日系统健康检查 — 每天早上跑一次，确保系统不出倒退。

检查项目:
  1. 推送格式是否完整（复用 ev_push._validate_format）
  2. 各玩法数据分布 — 所有 5 个核心市场都有数据
  3. TRUSTED_LEAGUES — 检查关键联赛是否在名单中
  4. 虚拟组合 — 余额/待结算/已结算笔数+利润
  5. English检测 — 推送内容无遗留英文标签
  6. 扫描是否正常 — 最近一次扫描时间
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging, get_logger
from config.settings import DATA_DIR, TRUSTED_LEAGUES
from src.report.ev_push import build_ev_report, _validate_format

logger = get_logger(__name__)

RESULTS_FILE = DATA_DIR / "line_shopping_results.json"
VP_FILE = DATA_DIR / "virtual_portfolio.json"

# 所有玩法配置一致性检查
# 扫描端(line_shopping)支持全部，投注端(place_line_shops)只投以下5个
CORE_MARKETS = {"1x2", "over_under", "btts", "corners_1x2", "double_chance"}
# 投注端和推送端都应包含这些
PLACE_ALLOWED = {"1x2", "over_under", "corners_1x2", "btts", "double_chance"}

# 关键联赛（如果这些不在TRUSTED_LEAGUES里算异常）
KEY_LEAGUES = {
    "Premier League": "英超",
    "La Liga": "西甲",
    "Bundesliga": "德甲",
    "Serie A": "意甲",
    "Ligue 1": "法甲",
    "Champions League": "欧冠",
}

# 推送中不应出现的英文标签
ENGLISH_TAGS = {
    "Edge:",       # 应为"溢价:"
    "Pinnacle:",   # 已移除
}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def check_push_format(results: list) -> list:
    """检查 1: 推送格式验证。"""
    issues = []
    body = build_ev_report()[0]
    if body.startswith("no") or body.startswith("line"):
        issues.append("⚠️ 无可用推送数据（line_shopping_results 为空或无+EV机会）")
        return issues

    if not _validate_format(body):
        issues.append("❌ 推送格式验证失败！_validate_format(body)=False")
        return issues

    # 额外检查：无英文标签残留
    for tag in ENGLISH_TAGS:
        if tag in body:
            issues.append(f"❌ 推送中有英文标签残留: '{tag}'")

    # 检查比赛分组结构：每场比赛一个 "#####"，每个投注一个 ">"
    entries = body.count("##### #")
    bet_lines = body.count("\n> [")
    if entries > 0 and bet_lines < entries:
        issues.append(f"⚠️ 推送结构异常: {entries}场比赛, {bet_lines}条投注行")

    if not issues:
        issues.append(f"✅ 推送格式正确（{entries}场比赛, {bet_lines}条投注）")
    return issues


def check_market_data(results: list) -> list:
    """检查 2: 各玩法数据分布。"""
    issues = []
    if not results:
        issues.append("⚠️ 无数据，跳过玩法分布检查")
        return issues

    from collections import Counter
    markets = Counter(o.get("market", "?") for o in results)

    # 检查核心市场
    for mkt in sorted(CORE_MARKETS):
        count = markets.get(mkt, 0)
        if count == 0:
            issues.append(f"⚠️ 核心市场 '{mkt}' 当前数据为 0 条")
        else:
            issues.append(f"✅ {mkt}: {count} 条")

    # 报告其他市场
    others = {k: v for k, v in markets.items() if k not in CORE_MARKETS}
    for mkt, count in sorted(others.items()):
        issues.append(f"📋 其他 '{mkt}': {count} 条")

    return issues


def check_trusted_leagues(results: list) -> list:
    """检查 3: TRUSTED_LEAGUES 配置。"""
    issues = []

    # 检查关键联赛是否都在
    for league, cn_name in KEY_LEAGUES.items():
        if league not in TRUSTED_LEAGUES:
            issues.append(f"❌ 关键联赛 '{league}'({cn_name}) 不在 TRUSTED_LEAGUES 中！")

    # 检查联赛名称一致性（数据中实际出现的联赛 vs 配置）
    if results:
        data_leagues = set(o.get("league", "") for o in results)
        unmatched = data_leagues - TRUSTED_LEAGUES
        if unmatched:
            issues.append(f"📋 数据中 {len(unmatched)} 个联赛不在可信名单（被过滤）")
            for u in sorted(unmatched):
                issues.append(f"   过滤: {u}")

    if not issues:
        issues.append("✅ TRUSTED_LEAGUES 配置正确")
    return issues


def check_virtual_portfolio() -> list:
    """检查 4: 虚拟组合状态。"""
    issues = []
    vp = _load_json(VP_FILE)
    if not vp:
        issues.append("⚠️ virtual_portfolio.json 不存在或为空")
        return issues

    pending = vp.get("pending_bets", [])
    settled = vp.get("settled", {})
    balance = vp.get("balance", 0)

    issues.append(f"💰 余额: ¥{balance:,.2f}")
    issues.append(f"📊 待结算: {len(pending)} 笔")
    issues.append(f"📊 已结算: {len(settled)} 笔")

    # 检查待结算是否有过期的（超过7天未开赛）
    now = datetime.now(timezone.utc)
    old_pending = 0
    for b in pending:
        ct = b.get("commence_time", "")
        if ct:
            try:
                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if (now - dt).days > 7:
                    old_pending += 1
            except Exception:
                pass
    if old_pending > 0:
        issues.append(f"⚠️ {old_pending} 笔待结算已过开赛时间超过7天，可能需手动处理")

    if balance < 100:
        issues.append(f"⚠️ 余额不足¥100")

    return issues


def check_recent_scan(results: list, data: dict) -> list:
    """检查 5: 最近扫描时间。"""
    issues = []
    updated = data.get("updated", "")
    if updated:
        issues.append(f"🕐 上次扫描: {updated[:19]}")
    else:
        issues.append("⚠️ 扫描时间未知")

    total = data.get("total", 0)
    issues.append(f"📈 总机会数: {total} 条")

    if results:
        # 检查边过滤后的机会分布
        from collections import Counter
        league_dist = Counter(o.get("league", "?") for o in results)
        issues.append(f"📋 联赛分布:")
        for league, count in league_dist.most_common(5):
            issues.append(f"   {league}: {count} 条")

    return issues


def run_all_checks() -> list:
    """运行全部检查，返回问题列表。"""
    all_issues = []
    all_issues.append("=" * 50)
    all_issues.append("  每日健康检查")
    all_issues.append(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    all_issues.append("=" * 50)
    all_issues.append("")

    data = _load_json(RESULTS_FILE)
    results = data.get("opportunities", [])

    # 1. 推送格式
    all_issues.append("【推送格式】")
    all_issues.extend(check_push_format(results))
    all_issues.append("")

    # 2. 玩法数据分布
    all_issues.append("【玩法数据分布】")
    all_issues.extend(check_market_data(results))
    all_issues.append("")

    # 3. TRUSTED_LEAGUES
    all_issues.append("【联赛配置】")
    all_issues.extend(check_trusted_leagues(results))
    all_issues.append("")

    # 4. 虚拟组合
    all_issues.append("【虚拟组合】")
    all_issues.extend(check_virtual_portfolio())
    all_issues.append("")

    # 5. 扫描状态
    all_issues.append("【扫描状态】")
    all_issues.extend(check_recent_scan(results, data))
    all_issues.append("")

    # 汇总
    all_issues.append("=" * 50)
    errors = [l for l in all_issues if l.startswith("❌")]
    warnings = [l for l in all_issues if l.startswith("⚠️")]
    if errors:
        all_issues.append(f"结果: ❌ {len(errors)} 个错误, {len(warnings)} 个警告")
    elif warnings:
        all_issues.append(f"结果: ⚠️ {len(warnings)} 个警告（无错误）")
    else:
        all_issues.append("结果: ✅ 全部正常")
    all_issues.append("=" * 50)

    return all_issues


def main():
    setup_logging()
    issues = run_all_checks()
    for line in issues:
        print(line)
    print()

    errors = [l for l in issues if l.startswith("❌")]
    if errors:
        logger.warning("健康检查: %d 个错误", len(errors))
        sys.exit(1)


if __name__ == "__main__":
    main()
