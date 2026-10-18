#!/usr/bin/env python3
"""试探格 cap 审查(2026-10-18): 11 个滚球格 cap=30 试探, 实盘投注数达 30 笔后裁决。

用法: .venv312/bin/python scripts/check_trial_caps.py [--apply]
  无 --apply 时只报告各格实盘投注数+ROI(试运行);
  --apply 时执行裁决:
    - 达 30 笔且实盘 ROI >= 0 → cap 从 30 提回 60(恢复常规额度)
    - 达 30 笔且实盘 ROI < 0  → 撤回观察库(从 observe_released 移除 + 加 observe_blocked)

统计口径: real_bets.json 按 sport|sub|desig|概率桶, 只算释放后(ts > started_at)新增投注。
实盘 ROI 只统计已结算(result in won/lost/push)注单的 profit/stake。
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.odds_interval import prob_bucket_from_fair  # noqa: E402

SPORT = {1: "football", 2: "ice_hockey", 3: "basketball", 5: "tennis", 7: "baseball", 13: "volleyball"}
SUB = {"opportunities": "1x2", "handicap": "hc", "over_under": "ou", "double_chance": "dc",
       "handicap_games": "hc_games", "ht": "ht", "ht_ou": "ht_ou"}
DIR = {"主胜": "主", "客胜": "客", "和局": "平", "让球主胜": "主", "让球客胜": "客",
       "大球": "大", "小球": "小", "让局主胜": "主", "让局客胜": "客",
       "上半场主胜": "主", "上半场客胜": "客", "上半场和局": "平"}

TRIAL_THRESHOLD = 30  # 达 30 笔后裁决
RESTORE_CAP = 60

MARKET_RELEASE = ROOT / "data" / "storage" / "market_release.json"
REAL_BETS = ROOT / "data" / "storage" / "real_bets.json"
STARTED_AT_FILE = ROOT / "data" / "storage" / "trial_cells_started_at.txt"

# 2026-10-18 释放的 11 个试探格(明确列出, 不用 cap==30 推断, 避免误算现有 cap=30 的 tennis ou 小)
TRIAL_CELLS = [
    "volleyball|ou|小|60-80%",
    "football|1x2|客|10-20%",
    "basketball|hc|主|45-55%",
    "basketball|ou|小|45-55%",
    "basketball|hc|客|45-55%",
    "football|hc|主|50-55%",
    "football|hc|客|40-50%",
    "football|ou|小|40-50%",
    "tennis|1x2|客|20-40%",
    "tennis|hc_games|主|60-80%",
    "football|1x2|平|20-30%",
]


def main():
    apply = "--apply" in sys.argv
    mr = json.loads(MARKET_RELEASE.read_text())
    caps = mr.get("observe_release_caps", {})
    released = [tuple(x) for x in mr.get("observe_released", [])]
    blocked = [tuple(x) for x in mr.get("observe_blocked", [])]

    started_at = 0.0
    if STARTED_AT_FILE.exists():
        try:
            started_at = float(STARTED_AT_FILE.read_text().strip())
        except (ValueError, OSError):
            started_at = 0.0

    trial_cells = [c + "|live" for c in TRIAL_CELLS]

    # 统计释放后新增投注: n + 已结算 stake/profit
    bets = json.loads(REAL_BETS.read_text()).get("bets", {})
    stats = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0})
    for b in bets.values():
        if b.get("ts", 0) <= started_at:
            continue
        sp = SPORT.get(b.get("sport"), "other")
        sub = SUB.get(b.get("sub"), b.get("sub"))
        di = DIR.get(b.get("desig"), b.get("desig"))
        bucket = prob_bucket_from_fair(b.get("fair"), sp)
        if bucket == "?":
            continue
        key = f"{sp}|{sub}|{di}|{bucket}|live"
        stats[key]["n"] += 1
        if b.get("result") in ("won", "lost", "push"):
            stats[key]["stake"] += b.get("stake") or 0
            stats[key]["profit"] += b.get("profit") or 0

    print(f"试探格共 {len(trial_cells)} 个(只统计释放后新增投注):")
    promote, withdraw = [], []
    for k in sorted(trial_cells):
        s = stats.get(k, {"n": 0, "stake": 0.0, "profit": 0.0})
        n = s["n"]
        roi = s["profit"] / s["stake"] * 100 if s["stake"] else 0.0
        flag = ""
        if n >= TRIAL_THRESHOLD:
            if roi >= 0:
                flag = f"  ← 达{n}笔 ROI{roi:+.1f}% → 恢复 cap={RESTORE_CAP}"
                promote.append(k)
            else:
                flag = f"  ← 达{n}笔 ROI{roi:+.1f}% → 撤回观察库"
                withdraw.append(k)
        print(f"  {k:<45} 实盘{n:>3}笔 ROI{roi:>+6.1f}%{flag}")

    if not (promote or withdraw):
        print("\n无格达到 30 笔(试运行, 加 --apply 才写盘)")
        return

    if not apply:
        print(f"\n达 30 笔: 恢复 {len(promote)} 格 / 撤回 {len(withdraw)} 格, 用 --apply 执行")
        return

    changed = False
    for k in promote:
        caps[k] = RESTORE_CAP
        changed = True
    for k in withdraw:
        e = tuple(k.split("|"))
        if len(e) == 5:
            if e in released:
                released.remove(e)
            if e not in blocked:
                blocked.append(e)
            caps.pop(k, None)
            changed = True
    if changed:
        mr["observe_release_caps"] = caps
        mr["observe_released"] = [list(x) for x in released]
        mr["observe_blocked"] = [list(x) for x in blocked]
        MARKET_RELEASE.write_text(json.dumps(mr, ensure_ascii=False, indent=1))
        print(f"\n✅ 执行: 恢复 {len(promote)} 格 cap={RESTORE_CAP}, 撤回 {len(withdraw)} 格观察库")


if __name__ == "__main__":
    main()
