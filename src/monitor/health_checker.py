"""系统健康自检机器人 — 不定期自动巡检所有子系统。

检查维度:
  1. 连通性: Pinnacle API, BB API, 钉钉
  2. 数据新鲜度: 对比文件, BB提取, 联赛缓存
  3. 流水线: orchestrator运行, 增量扫描频率, 推送频率
  4. 结算: 追踪投注结算率, 卡住投注, 超时投注
  5. 映射: 联赛关键词覆盖率, 队名映射覆盖率
  6. 盘口健康: 各运动机会数, 新增盘口(BTTS/OE/DNB)
  7. 矩阵: 数据源覆盖率
  8. CLV: 采集成功率
  9. Git: 未提交, 未推送

用法: python -m src.monitor.health_checker [--push] [--quiet]
"""
import json, os, sys, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR, send_dingtalk
from config.logging_config import get_logger

logger = get_logger(__name__)

# ── 阈值定义 ──
COMPARISON_MAX_AGE_MIN = 60       # 对比文件超过60分钟 → 警告
BB_EXTRACT_MAX_AGE_MIN = 90       # BB提取超过90分钟 → 警告
PUSH_MIN_PER_DAY = 2              # 每天至少2次推送 → 低于警告
SETTLE_MIN_RATE = 0.7             # 结算率 < 70% → 警告
STUCK_BET_MAX_HOURS = 72          # 投注超过72h未结算 → 警告
LEAGUE_MAP_MIN_COVERAGE = 0.6     # 联赛映射 < 60% → 警告
CLV_COLLECT_MIN_RATE = 0.01       # CLV采集率 < 1% → 警告
UNPUSHED_COMMITS_MAX = 10         # 未推送提交 > 10 → 警告


class HealthReport:
    def __init__(self):
        self.issues = []
        self.warnings = []
        self.ok = []
        self.stats = {}

    def add_issue(self, msg):
        self.issues.append(msg)

    def add_warning(self, msg):
        self.warnings.append(msg)

    def add_ok(self, msg):
        self.ok.append(msg)

    @property
    def score(self):
        total = len(self.ok) + len(self.warnings) + len(self.issues)
        if total == 0:
            return 100
        return round(len(self.ok) / total * 100)

    @property
    def is_healthy(self):
        return len(self.issues) == 0


def check_connectivity(report):
    """检查各API连通性。"""
    import requests

    # Pinnacle API
    try:
        session = requests.Session()
        session.trust_env = False
        session.proxies = {"http": "", "https": ""}
        session.headers.update({
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "X-API-Key": "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R",
        })
        resp = session.get("https://guest.api.arcadia.pinnacle.com/0.1/sports", timeout=10)
        if resp.status_code == 200:
            sports = resp.json()
            report.add_ok(f"Pinnacle API: {len(sports)} sports")
            report.stats["pinnacle_sports"] = len(sports)
        else:
            report.add_issue(f"Pinnacle API: HTTP {resp.status_code}")
    except Exception as e:
        report.add_issue(f"Pinnacle API: {e}")

    # DingTalk
    try:
        ok = send_dingtalk("健康检查", "系统自检中...", timeout=5)
        if ok:
            report.add_ok("钉钉: 连通")
        else:
            report.add_warning("钉钉: 推送失败(关键词?)")
    except Exception as e:
        report.add_issue(f"钉钉: {e}")

    # Cookie 有效性 — 真实 API 探测 (非仅文件时间)
    cookie_file = DATA_DIR / "pinnacle_cf_clearance.txt"
    if not cookie_file.exists():
        report.add_issue("Pinnacle Cookie: 文件不存在")
    else:
        try:
            from src.scrapers.pinnacle_api import SESSION, API_BASE
            resp = SESSION.get(f"{API_BASE}/sports", timeout=8)
            if resp.status_code == 200:
                n_sports = len(resp.json()) if isinstance(resp.json(), list) else 0
                report.add_ok(f"Pinnacle Cookie: 有效 ({n_sports}运动)")
            else:
                report.add_issue(f"Pinnacle Cookie: 失效 (HTTP {resp.status_code})")
        except Exception as e:
            report.add_issue(f"Pinnacle Cookie: 探测失败 ({str(e)[:40]})")


def check_data_freshness(report):
    """检查数据文件新鲜度。"""
    # BB extraction
    bb_file = DATA_DIR / "bb_odds_extracted.json"
    if bb_file.exists():
        age_min = (time.time() - bb_file.stat().st_mtime) / 60
        report.stats["bb_extract_age_min"] = round(age_min)
        if age_min > BB_EXTRACT_MAX_AGE_MIN:
            report.add_warning(f"BB提取: {age_min:.0f}min 未更新")
        else:
            report.add_ok(f"BB提取: {age_min:.0f}min")
    else:
        report.add_issue("BB提取: 文件不存在")

    # Comparison file
    cmp_file = DATA_DIR / "bb_vs_pinnacle_comparison.json"
    if cmp_file.exists():
        age_min = (time.time() - cmp_file.stat().st_mtime) / 60
        report.stats["comparison_age_min"] = round(age_min)
        if age_min > COMPARISON_MAX_AGE_MIN:
            report.add_warning(f"对比文件: {age_min:.0f}min 未更新")
        else:
            report.add_ok(f"对比文件: {age_min:.0f}min")

        # Check opportunities
        try:
            d = json.loads(cmp_file.read_text())
            report.stats["total_opps"] = d.get("opportunities_total", 0)
            report.stats["matched"] = d.get("matched_matches", 0)
        except:
            pass
    else:
        report.add_issue("对比文件: 不存在")

    # League cache
    lc_file = DATA_DIR / "pinnacle_league_structure.json"
    if lc_file.exists():
        d = json.loads(lc_file.read_text())
        n = len(d)
        report.stats["pin_leagues"] = n
        if n < 50:
            report.add_issue(f"联赛缓存: 仅{n}条, 需重建")
        elif n < 100:
            report.add_warning(f"联赛缓存: {n}条, 偏少")
        else:
            report.add_ok(f"联赛缓存: {n}条")
    else:
        report.add_issue("联赛缓存: 文件不存在")


def check_pipeline(report):
    """检查流水线运行状态。"""
    import subprocess
    result = subprocess.run(
        ["pgrep", "-f", "pipeline_orchestrator"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        pids = result.stdout.strip().split('\n')
        report.add_ok(f"Orchestrator: 运行中 (PID {pids[0]})")
        report.stats["orchestrator_running"] = True
    else:
        report.add_issue("Orchestrator: 未运行!")

    # Check recent push count
    log_file = ROOT / "logs" / "sportsbetting.log"
    if log_file.exists():
        today = datetime.now().strftime("%Y-%m-%d")
        push_count = 0
        with open(log_file) as f:
            for line in f:
                if today in line and "钉钉推送成功" in line:
                    push_count += 1
        report.stats["pushes_today"] = push_count
        if push_count < PUSH_MIN_PER_DAY:
            report.add_warning(f"今日推送: {push_count}次")
        else:
            report.add_ok(f"今日推送: {push_count}次")

        # Incremental scan frequency
        inc_count = 0
        with open(log_file) as f:
            for line in f:
                if today in line and "[incremental] ====== DONE" in line:
                    inc_count += 1
        report.stats["incremental_scans"] = inc_count


def check_settlement(report):
    """检查结算系统健康度。"""
    tb_file = DATA_DIR / "tracked_bets.json"
    if not tb_file.exists():
        report.add_issue("追踪文件: 不存在")
        return

    try:
        tb = json.loads(tb_file.read_text())
        bets = tb.get("bets", [])
        settled = [b for b in bets if b.get("status") == "settled"]
        pending = [b for b in bets if b.get("status") == "pending"]
        total = len(bets)

        report.stats["tracked_bets"] = total
        report.stats["settled_bets"] = len(settled)
        report.stats["pending_bets"] = len(pending)

        settle_rate = len(settled) / total if total > 0 else 0
        if settle_rate < SETTLE_MIN_RATE and total > 10:
            report.add_warning(f"结算率: {settle_rate:.0%} ({len(settled)}/{total})")
        else:
            report.add_ok(f"结算率: {settle_rate:.0%} ({len(settled)}/{total})")

        # Stuck bets (>72h pending)
        now = time.time()
        stuck = [b for b in pending
                 if b.get("match_epoch", 0) > 0 and (now - b.get("match_epoch", 0)) > STUCK_BET_MAX_HOURS * 3600]
        if stuck:
            total_stuck = sum(b.get("stake", 0) for b in stuck)
            report.add_issue(f"卡住投注: {len(stuck)}笔, ¥{total_stuck:,.0f} (>72h未结算)")

        # P&L
        if settled:
            total_stake = sum(b["stake"] for b in settled)
            total_profit = sum(b.get("profit", 0) or 0 for b in settled)
            roi = total_profit / total_stake * 100 if total_stake > 0 else 0
            report.stats["pnl_roi"] = round(roi, 1)
            report.stats["pnl_profit"] = round(total_profit, 0)
            won = sum(1 for b in settled if b.get("result") == "won")
            lost = sum(1 for b in settled if b.get("result") == "lost")
            wr = won / (won + lost) * 100 if (won + lost) > 0 else 0
            report.add_ok(f"P&L: ROI={roi:.1f}%, WR={wr:.0f}%, ¥{total_profit:+,.0f}")

    except Exception as e:
        report.add_issue(f"追踪文件: {e}")


def check_mappings(report):
    """检查联赛和队名映射覆盖率。"""
    bb_file = DATA_DIR / "bb_odds_extracted.json"
    kw_file = DATA_DIR / "league_keywords.json"
    tm_file = DATA_DIR / "team_name_map.json"
    pin_file = DATA_DIR / "pinnacle_league_structure.json"

    if not all(f.exists() for f in [bb_file, kw_file, pin_file]):
        return

    bb = json.loads(bb_file.read_text())
    kw = json.loads(kw_file.read_text())
    pin = json.loads(pin_file.read_text())

    from src.scrapers.pinnacle_league_map import find_pinnacle_league_ids

    bb_leagues = defaultdict(int)
    bb_teams = set()
    for m in bb.get("matches", []):
        bb_leagues[m.get("league", "")] += 1
        bb_teams.add(m.get("home", ""))
        bb_teams.add(m.get("away", ""))

    # League mapping
    mapped = sum(1 for lg in bb_leagues if find_pinnacle_league_ids(lg, pin))
    total = len(bb_leagues)
    pct = mapped / total * 100 if total > 0 else 0
    report.stats["league_coverage"] = round(pct)
    if pct < LEAGUE_MAP_MIN_COVERAGE:
        report.add_warning(f"联赛映射: {mapped}/{total} ({pct:.0f}%)")
    else:
        report.add_ok(f"联赛映射: {mapped}/{total} ({pct:.0f}%)")

    # Team name mapping
    tm = json.loads(tm_file.read_text()) if tm_file.exists() else {}
    tm_count = len([v for v in tm.values() if isinstance(v, str)])
    mapped_teams = sum(1 for t in bb_teams if t in tm)
    team_pct = mapped_teams / len(bb_teams) * 100 if bb_teams else 0
    report.stats["team_coverage"] = round(team_pct)
    report.stats["team_map_size"] = tm_count
    if team_pct < 70:
        report.add_warning(f"队名映射: {mapped_teams}/{len(bb_teams)} ({team_pct:.0f}%)")
    else:
        report.add_ok(f"队名映射: {mapped_teams}/{len(bb_teams)} ({team_pct:.0f}%)")


def check_market_health(report):
    """检查各盘口健康度。"""
    cmp_file = DATA_DIR / "bb_vs_pinnacle_comparison.json"
    if not cmp_file.exists():
        return

    d = json.loads(cmp_file.read_text())
    per_sport = d.get("per_sport_matched", {})
    per_sport_opp = d.get("per_sport_opportunities", {})

    # Silent sports = matched but 0 opps
    for sport in per_sport:
        if per_sport[sport] > 0 and per_sport_opp.get(sport, 0) == 0:
            report.add_warning(f"{sport}: {per_sport[sport]}场匹配但0机会")

    # New market types
    detail_mk = defaultdict(int)
    for det in d.get("details", []):
        for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
            for opp in det.get(mk, []):
                detail_mk[opp.get("_market", mk)] += 1

    report.stats["market_btts"] = detail_mk.get("btts", 0)
    report.stats["market_oe"] = detail_mk.get("oe", 0)
    report.stats["market_dnb"] = sum(1 for det in d.get("details", [])
                                     for _ in det.get("draw_no_bet", []))

    if detail_mk.get("btts", 0) == 0:
        report.add_warning("BTTS: 0条机会")
    if detail_mk.get("oe", 0) == 0:
        report.add_warning("OE: 0条机会")


def check_v4_matrix(report):
    """检查 V4 矩阵数据源覆盖。"""
    try:
        from config.weight_matrix_v4 import (
            PIN_1X2_DATA, PIN_OU_DATA, PIN_HC_DATA, TENNIS_DATA,
            NBA_DATA, MLB_DATA, NFL_DATA, NHL_DATA
        )
        btb = False
        try:
            from config.btb_calibrated import BTB_1X2_DATA
            btb = len(BTB_1X2_DATA) > 10
        except ImportError:
            pass
        sbr = False
        try:
            from config.sbr_calibrated import SBR_DATA
            sbr = len(SBR_DATA) >= 4
        except ImportError:
            pass
        betfair = False
        try:
            from config.betfair_basketball_calibrated import BETFAIR_BASKETBALL
            betfair = len(BETFAIR_BASKETBALL) > 0
        except ImportError:
            pass

        report.stats["v4_football"] = len(PIN_1X2_DATA)
        report.stats["v4_football_ou"] = len(PIN_OU_DATA)
        report.stats["v4_tennis"] = len(TENNIS_DATA)
        report.stats["v4_nba_bins"] = len(NBA_DATA)
        report.stats["v4_mlb_bins"] = len(MLB_DATA)
        report.stats["v4_btb"] = btb
        report.stats["v4_sbr"] = sbr
        report.stats["v4_betfair_bball"] = betfair

        sports_ok = sum([bool(PIN_1X2_DATA), bool(TENNIS_DATA), bool(NBA_DATA),
                         bool(MLB_DATA), bool(NFL_DATA), bool(NHL_DATA), btb, sbr])
        report.add_ok(f"V4矩阵: {sports_ok}个数据源 (BTB={btb}, SBR={sbr}, Betfair={betfair})")
    except Exception as e:
        report.add_warning(f"V4矩阵: {e}")


def check_clv(report):
    """检查 CLV 采集健康度。"""
    clv_file = DATA_DIR / "clv_tracking.csv"
    if not clv_file.exists():
        report.add_ok("CLV: 无追踪文件(新系统)")
        return

    import csv
    rows = []
    with open(clv_file, newline='') as f:
        for r in csv.DictReader(f):
            rows.append(r)

    total = len(rows)
    now = time.time()
    in_window = sum(1 for r in rows
                    if 900 < int(r.get("match_epoch", 0)) - now < 21600)

    report.stats["clv_total"] = total
    report.stats["clv_in_window"] = in_window

    if total > 100 and in_window == 0:
        report.add_warning(f"CLV: {total}条追踪, 0条在采集窗口内")
    else:
        report.add_ok(f"CLV: {total}条追踪, {in_window}条在窗口内")


def check_git(report):
    """检查 git 状态。"""
    import subprocess
    result = subprocess.run(
        ["git", "log", "--oneline", f"origin/main..HEAD"],
        cwd=ROOT, capture_output=True, text=True
    )
    unpushed = len(result.stdout.strip().split('\n')) if result.stdout.strip() else 0
    report.stats["unpushed_commits"] = unpushed

    if unpushed > UNPUSHED_COMMITS_MAX:
        report.add_warning(f"Git: {unpushed}个未推送提交")
    elif unpushed > 0:
        report.add_ok(f"Git: {unpushed}个未推送提交")

    # Uncommitted changes
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT, capture_output=True, text=True
    )
    dirty = len(result.stdout.strip().split('\n')) if result.stdout.strip() else 0
    report.stats["uncommitted_files"] = dirty
    if dirty > 5:
        report.add_warning(f"Git: {dirty}个未提交文件")
    elif dirty > 0:
        report.add_ok(f"Git: {dirty}个未提交文件")


def run_health_check(push: bool = False, quiet: bool = False) -> HealthReport:
    """运行全量健康检查。"""
    report = HealthReport()

    checks = [
        ("连通性", check_connectivity),
        ("数据新鲜度", check_data_freshness),
        ("流水线", check_pipeline),
        ("结算", check_settlement),
        ("映射", check_mappings),
        ("盘口健康", check_market_health),
        ("V4矩阵", check_v4_matrix),
        ("CLV", check_clv),
        ("Git", check_git),
    ]

    for name, check_fn in checks:
        try:
            check_fn(report)
        except Exception as e:
            report.add_issue(f"{name}: 检查异常 - {e}")

    if not quiet:
        # Print report
        print(f"\n{'='*50}")
        print(f"🩺 系统健康检查 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*50}")
        print(f"健康度: {report.score}/100")

        if report.issues:
            print(f"\n🔴 问题 ({len(report.issues)}):")
            for i in report.issues:
                print(f"  ❌ {i}")
        if report.warnings:
            print(f"\n🟡 警告 ({len(report.warnings)}):")
            for w in report.warnings:
                print(f"  ⚠️ {w}")
        if report.ok:
            print(f"\n🟢 正常 ({len(report.ok)}):")
            for o in report.ok:
                print(f"  ✅ {o}")

    # Push to DingTalk
    if push and (report.issues or report.warnings):
        lines = [f"🩺 系统健康度: {report.score}/100"]
        if report.issues:
            lines.append(f"\n🔴 问题:")
            for i in report.issues:
                lines.append(f"  ❌ {i}")
        if report.warnings:
            lines.append(f"\n🟡 警告:")
            for w in report.warnings:
                lines.append(f"  ⚠️ {w}")
        body = "\n".join(lines)
        try:
            send_dingtalk(f"系统健康报告 {report.score}/100", body, timeout=10)
            logger.info("健康报告已推送")
        except:
            pass

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--push", action="store_true", help="推送到钉钉")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    args = parser.parse_args()
    run_health_check(push=args.push, quiet=args.quiet)
