#!/usr/bin/env python3
"""盘口产出新鲜度监控 — 每个盘口最近一次产出超阈值就钉钉告警。

教训(2026-08-19/20):
    角球因 NameError 静默断了 2 天、bb_score_settle 因 ms=0 bug 死 4 天、near 扫描
    竞态卡 89 分钟 —— 全是"进程健康但产出恒为 0", 传统看门狗测不出。本脚本盯
    clv_tracking.csv 里每个 sub_market 的最近记录时间, 超过阈值(默认 36h)告警。

    注意: 只对"系统在用"的盘口告警。correct_score/htft 是故意禁用, 不告警。

用法: .venv312/bin/python scripts/market_coverage_check.py [--no-push]
"""
import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
TRACKING = DATA / "clv_tracking.csv"

# 故意禁用的盘口, 不告警(见 pinnacle_opportunities / bb_vs_pinnacle 注释)
INTENTIONALLY_DISABLED = {"correct_score", "htft"}
# 超过这个小时数没产出 → 告警
STALE_HOURS = 36
# 至少要有这么多历史记录才算"系统在用的盘口"(样本极少的特殊盘口不告警)
MIN_HISTORICAL = 10


def main(push: bool):
    if not TRACKING.exists():
        print("❌ 无 clv_tracking.csv")
        return

    last = {}       # sub_market -> 最近记录时间
    total = defaultdict(int)
    with open(TRACKING, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sm = r.get("sub_market", "?")
            total[sm] += 1
            t = r.get("timestamp", "")
            if sm not in last or t > last[sm]:
                last[sm] = t

    now = time.time()
    stale = []
    for sm, t in sorted(last.items()):
        if sm in INTENTIONALLY_DISABLED:
            continue
        if total[sm] < MIN_HISTORICAL:
            continue  # 样本极少, 本来就是稀有盘口, 不告警
        try:
            ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        age_h = (now - ts) / 3600
        if age_h > STALE_HOURS:
            stale.append((sm, age_h, total[sm]))

    print(f"盘口产出检查: {len(last)} 个盘口, 超 {STALE_HOURS}h 未产出的 {len(stale)} 个")
    for sm, age_h, n in stale:
        print(f"  ⚠️ {sm}: 最近 {age_h:.0f}h 前, 历史 {n} 条")

    if stale and push:
        lines = [f"⚠️ 盘口静默失效告警\n\n{len(stale)} 个盘口超过 {STALE_HOURS} 小时无产出:\n"]
        for sm, age_h, n in stale:
            lines.append(f"  • {sm}: {age_h:.0f}h 无产出 (历史 {n} 条)")
        lines.append("\n可能原因: 匹配断链/异常被吞/数据源停。排查对应 fetch 函数。")
        try:
            # 用 config.settings 入口(自动注入关键词, 签名匹配)。原先误用 config.dingtalk
            # 且传 timeout= → TypeError 被吞, 告警从未发出。
            from config.settings import send_dingtalk
            if send_dingtalk("盘口静默失效", "\n".join(lines), urgent=True):
                print("  已推送钉钉告警")
            else:
                print("  ⚠️ 钉钉告警未送达")
        except Exception as e:
            print(f"  告警推送失败: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="只打印不推送")
    main(push=not ap.parse_args().no_push)
