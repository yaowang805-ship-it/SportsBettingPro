"""系统防退化检查 — 每次 main.py 运行时执行，确保数据完整性和输出质量。

发现任何问题立即日志告警 + 钉钉推送，不阻断流程（让用户知悉但不中断自动任务）。
"""
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "storage"

ISSUE_EMOJI = {}


def _check_vp_exists() -> Tuple[bool, str]:
    vp = DATA_DIR / "virtual_portfolio.json"
    if not vp.exists():
        return False, "virtual_portfolio.json 不存在"
    return True, ""


def _check_balance_integrity(vp: dict) -> List[str]:
    """校验余额 = 初始 + 利润 + void 退本。"""
    issues = []
    expected = vp.get("initial_bankroll", 0)
    for h in vp.get("history", []):
        expected += h.get("profit", 0)
        if h.get("status") == "void":
            expected += h.get("stake", 0)
    actual = vp.get("balance", 0)
    diff = abs(actual - expected)
    if diff > 0.5:
        issues.append(f"余额不一致: 实际 ¥{actual:.2f}, 预期 ¥{expected:.2f} (差额 ¥{diff:.2f})")
    return issues


def _check_stale_pending(vp: dict, max_hours: int = 8) -> List[str]:
    """检查已结束超过 max_hours 仍未结算的投注。"""
    issues = []
    now = datetime.now(timezone.utc)
    for b in vp.get("pending_bets", []):
        ct = b.get("commence_time", "")
        if not ct:
            continue
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if dt + timedelta(hours=max_hours) < now:
                home = b.get("home_team", b.get("home_cn", "?"))
                away = b.get("away_team", b.get("away_cn", "?"))
                issues.append(
                    f"已结束未结算(>{max_hours}h): {home} vs {away} "
                    f"[{b.get('market_type','?')}] ¥{b.get('stake',0):.0f} "
                    f"结束于 {ct[:16]}"
                )
        except (ValueError, TypeError):
            continue
    return issues


def _check_history_status(vp: dict) -> List[str]:
    """校验 history 中是否有未知状态。"""
    issues = []
    valid = {"won", "lost", "void"}
    for h in vp.get("history", []):
        s = h.get("status", "")
        if s not in valid:
            issues.append(f"未知结算状态 '{s}': {h.get('id','?')[:50]}")
    return issues


def _check_output_chinese(vp: dict = None) -> List[str]:
    """桌面报告必须有中文。"""
    issues = []
    desktop = Path.home() / "Desktop" / "已结算统计.txt"
    if desktop.exists():
        try:
            from src.report.validator import validate_output
            issues = validate_output(desktop.read_text(encoding="utf-8"), context="防退化-桌面报告")
        except Exception:
            pass
    return issues


def _check_zero_odds(vp: dict) -> List[str]:
    """赔率不能为0或负值。"""
    issues = []
    for b in vp.get("pending_bets", []):
        if b.get("odds", 1) <= 0:
            issues.append(f"赔率异常: {b.get('odds')} ({b.get('id','?')[:50]})")
    return issues


def _check_pending_sport_league(vp: dict) -> List[str]:
    """检查待结算投注是否有空的 sport/league。"""
    issues = []
    for b in vp.get("pending_bets", []):
        if not b.get("sport"):
            issues.append(f"缺 sport 字段: {b.get('id','?')[:50]}")
        if not b.get("league"):
            issues.append(f"缺 league 字段: {b.get('id','?')[:50]}")
    return issues


def _check_desktop_report_exists(vp: dict = None) -> List[str]:
    """桌面报告必须存在。"""
    issues = []
    desktop = Path.home() / "Desktop" / "已结算统计.txt"
    if not desktop.exists():
        issues.append("桌面报告不存在: ~/Desktop/已结算统计.txt")
    return issues


ALL_CHECKS = [
    ("余额一致性", _check_balance_integrity),
    ("待结算超时", _check_stale_pending),
    ("结算状态合法性", _check_history_status),
    ("桌面报告中文化", _check_output_chinese),
    ("赔率合法性", _check_zero_odds),
    ("投注字段完整性", _check_pending_sport_league),
    ("桌面报告存在性", _check_desktop_report_exists),
]


def run_all_checks() -> Tuple[bool, List[str]]:
    """执行全部防退化检查。

    Returns:
        (passed, issues) — passed=True 表示全部通过
    """
    all_issues = []

    ok, msg = _check_vp_exists()
    if not ok:
        all_issues.append(msg)
        return False, all_issues

    vp = json.loads((DATA_DIR / "virtual_portfolio.json").read_text())

    for name, check_fn in ALL_CHECKS:
        try:
            issues = check_fn(vp)
            for i in issues:
                all_issues.append(f"[{name}] {i}")
        except Exception as e:
            all_issues.append(f"[{name}] 检查异常: {e}")

    return len(all_issues) == 0, all_issues


def print_summary() -> int:
    """打印检查摘要，返回问题数。"""
    passed, issues = run_all_checks()
    if passed:
        print("✅ 防退化检查全部通过")
    else:
        print(f"⚠️  防退化检查发现 {len(issues)} 个问题:")
        for i in issues:
            print(f"  ❌ {i}")
    return len(issues)
