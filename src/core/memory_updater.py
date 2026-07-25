"""记忆库自动更新 — 每日同步系统状态到 Claude 记忆文件。

在每日 10:00 结算报告后运行，确保下次 Claude 会话能读到最新状态。

用法：
    python3 -m src.core.memory_updater           # 更新记忆
    python3 -m src.core.memory_updater --dry     # 预览不写入
"""
import json
import sys
from datetime import datetime, timezone, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-wangyao-SportsBettingPro" / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _load_portfolio() -> dict:
    pf_path = DATA_DIR / "virtual_portfolio.json"
    if pf_path.exists():
        return json.loads(pf_path.read_text())
    return {}


def _load_settlement_stats() -> dict:
    """加载结算统计。"""
    from src.core.settleability import get_settleable_stats
    return get_settleable_stats()


def _load_recommendation_stats() -> dict:
    """加载推荐统计。"""
    from src.report.recommendation_tracker import get_statistics
    return get_statistics(days=30)


def update_current_state(dry_run: bool = False):
    """更新 current_state.md — 组合余额、待结算、ROI 等核心指标。"""
    pf = _load_portfolio()
    if not pf:
        return

    balance = pf.get("balance", 0)
    initial = pf.get("initial_bankroll", 50000)
    pending = pf.get("pending_bets", [])
    history = pf.get("history", [])

    pending_count = len(pending)
    pending_exposure = sum(b.get("stake", 0) for b in pending)
    history_count = len(history)
    settled_profit = sum(h.get("profit", 0) or 0 for h in history)

    won = sum(1 for h in history if h.get("status") in ("won", "win"))
    lost = sum(1 for h in history if h.get("status") in ("lost", "loss"))
    voided = sum(1 for h in history if h.get("status") == "void")
    real_bets = won + lost
    win_rate = round(won / real_bets * 100, 1) if real_bets else 0
    roi = round(settled_profit / (sum(h.get("stake", 0) for h in history if h.get("status") in ("won", "lost")) or 1) * 100, 1)

    # 按运动分 pending
    from collections import Counter
    by_sport = Counter(b.get("sport", "?") for b in pending)

    # 结算覆盖率
    settle_stats = _load_settlement_stats()

    today = date.today().isoformat()
    content = f"""---
name: 系统当前状态
description: {today} 自动更新：组合余额 ¥{balance:,.0f}，待结算 {pending_count} 笔
type: project
---

# 系统当前状态 ({today})

## 组合状态

| 指标 | 值 |
|---|---|
| 余额 | ¥{balance:,.0f} |
| 初始资金 | ¥{initial:,.0f} |
| 待结算 | {pending_count} 笔 (敞口 ¥{pending_exposure:,.0f}) |
| 已结算 | {history_count} 笔 |
| 胜/负/作废 | {won}/{lost}/{voided} |
| 胜率 | {win_rate}% |
| ROI | {roi:+.1f}% |
| 累计盈亏 | ¥{settled_profit:+,.0f} |

## 待结算分布

"""
    for sport, count in by_sport.most_common():
        stake = sum(b.get("stake", 0) for b in pending if b.get("sport") == sport)
        content += f"| {sport} | {count} 笔, ¥{stake:,.0f} |\n"

    content += f"""
## 结算覆盖率

| 指标 | 值 |
|---|---|
| 已验证可结算联赛 | {settle_stats.get('total_leagues', 0)} |
| 总体结算成功率 | {settle_stats.get('overall_rate', 0):.1%} |

## 关键参数

| 参数 | 值 |
|---|---|
| 日预算 | ¥50,000 |
| Kelly 分数 | 0.50 |
| 1X2 权重 | 1.00 |
| OU 权重 | 0.90 |
| BTTS 权重 | 0.85 |
| EV 上限 | 12% |
| 单注上限 | 6% (¥3,000) |
| 每日最多 | 50 笔 |

## 投注体系

| 层级 | 联赛数 | 说明 |
|---|---|---|
| ✅ 全額 | League 已证明可结算 | 正常投注 |
| 🔬 试用 5% | LEAGUE_SPORT_MAP 中的新联赛 | 小额测试，成功后升级 |
| 🚫 不下注 | 其余 | 仅推送信息 |
"""

    path = MEMORY_DIR / "current_state.md"
    if dry_run:
        print(f"[DRY RUN] 将写入 {path}")
        print(content[:500])
    else:
        path.write_text(content)
        logger.info("已更新 %s", path)


def update_completed_work(dry_run: bool = False):
    """追加今日完成的工作到 completed_work.md。"""
    today = date.today().isoformat()
    path = MEMORY_DIR / "completed_work.md"

    existing = ""
    if path.exists():
        existing = path.read_text()

    # 检查是否今天已更新过
    if f"## {today}" in existing:
        logger.info("completed_work.md 今天已更新，跳过")
        return

    # 从 git log 获取今天的提交
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "--since", f"{today}T00:00:00", "--until", f"{today}T23:59:59"],
            capture_output=True, text=True, cwd=str(ROOT)
        )
        commits = result.stdout.strip()
    except Exception:
        commits = ""

    if not commits:
        logger.info("今天无 git 提交，跳过 completed_work 更新")
        return

    entry = f"\n## {today}\n"
    for line in commits.split("\n"):
        if line.strip():
            # 格式: "hash 消息"
            parts = line.split(" ", 1)
            msg = parts[1] if len(parts) > 1 else line
            entry += f"- {msg}\n"

    # 在第一个 ## 标题前插入（第二个 ## 的位置）
    if existing:
        lines = existing.split("\n")
        # 找到 frontmatter 结束后的第一个 ##
        in_frontmatter = False
        new_lines = []
        found_first_header = False
        for i, line in enumerate(lines):
            new_lines.append(line)
            if line.strip() == "---" and not found_first_header:
                if in_frontmatter:
                    in_frontmatter = False
                else:
                    in_frontmatter = True
            if not in_frontmatter and line.startswith("## ") and not found_first_header:
                # After first header, insert today's entry
                new_lines.append(entry.strip())
                found_first_header = True
        new_content = "\n".join(new_lines)
    else:
        new_content = f"---\nname: 已完成工作\ndescription: 系统主要已完成工作\ntype: project\n---\n\n# 已完成工作\n\n{entry}"

    if dry_run:
        print(f"[DRY RUN] 将更新 {path}")
        print(entry[:300])
    else:
        path.write_text(new_content)
        logger.info("已更新 %s", path)


def update_all(dry_run: bool = False):
    """更新所有需要每日同步的记忆文件。"""
    logger.info("=== 记忆库自动更新 ===")
    update_current_state(dry_run=dry_run)
    update_completed_work(dry_run=dry_run)
    logger.info("=== 记忆库更新完成 ===")


if __name__ == "__main__":
    import argparse
    from config.logging_config import setup_logging
    setup_logging()

    parser = argparse.ArgumentParser(description="记忆库自动更新")
    parser.add_argument("--dry", action="store_true", help="预览不写入")
    args = parser.parse_args()

    update_all(dry_run=args.dry)
