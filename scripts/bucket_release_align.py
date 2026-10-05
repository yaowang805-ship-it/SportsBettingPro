#!/usr/bin/env python3
"""滚球「LEV>0 且 ROI>0」的概率桶 × 当前释放清单对齐 — 看哪些已释放/漏了/被拦。

方向归一化与 compute_market_release._direction 同口径, 概率桶同 prob_bucket_from_fair。
释放清单来源:
  observe_released (market_release.json, 数据驱动 + 手动合入)
  MANUAL_OBSERVE_RELEASE / MANUAL_OBSERVE_RELEASE_LIMITED / MANUAL_OBSERVE_BLOCK (源码硬编码)

用法: .venv312/bin/python scripts/bucket_release_align.py
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.scrapers.odds_interval import prob_bucket_from_fair

DATA = ROOT / "data" / "storage"

BB_SPORT_MAP = {1: "football", 3: "basketball", 5: "tennis", 7: "baseball", 6: "american_football",
                2: "ice_hockey", 13: "volleyball", 15: "pingpong", 18: "mma", 19: "boxing",
                47: "badminton"}
BB_SUB_MAP = {"over_under": "ou", "handicap": "hc", "opportunities": "1x2",
              "double_chance": "dc", "btts": "btts"}
SPORT_CN = {"football": "足球", "basketball": "篮球", "tennis": "网球", "baseball": "棒球",
            "ice_hockey": "冰球", "american_football": "美足", "volleyball": "排球",
            "pingpong": "乒乓", "mma": "MMA", "boxing": "拳击", "badminton": "羽毛球"}
SUB_CN = {"1x2": "独赢", "hc": "让球", "ou": "大小", "dc": "双机会", "btts": "双边进球"}


def _direction(desig, sm):
    """与 compute_market_release._direction 同口径(加 btts 特例: 双方进球方向保留原名)。"""
    d = desig or ""
    if sm == "dc":
        if "主" in d and "和" in d:
            return "1X"
        if "主" in d and "客" in d:
            return "12"
        if "和" in d and "客" in d:
            return "X2"
        return "其他"
    if sm == "btts":
        return "双方进球" if "双方" in d else d
    dl = d.lower()
    if "大" in d or "over" in dl:
        return "大"
    if "小" in d or "under" in dl:
        return "小"
    if ("和" in d or "平" in d or "draw" in dl) and "客" not in d and "主" not in d:
        return "平"
    if "客" in d or "away" in dl:
        return "客"
    if "主" in d or "home" in dl:
        return "主"
    return "其他"


def _bucket(fair, sport):
    return prob_bucket_from_fair(fair, sport)


def _load_manual_sets():
    sys.path.insert(0, str(ROOT / "scripts"))
    import compute_market_release as cmr
    manual_release = set()
    manual_limited = set()
    manual_block = set()
    for k in cmr.MANUAL_OBSERVE_RELEASE:
        p = k.split("|")
        if len(p) == 5 and p[4] == "live":
            manual_release.add((p[0], p[1], p[2], p[3]))
    for k in cmr.MANUAL_OBSERVE_RELEASE_LIMITED:
        p = k.split("|")
        if len(p) == 5 and p[4] == "live":
            manual_limited.add((p[0], p[1], p[2], p[3]))
    for k in cmr.MANUAL_OBSERVE_BLOCK:
        p = k.split("|")
        if len(p) == 5 and p[4] == "live":
            manual_block.add((p[0], p[1], p[2], p[3]))
    return manual_release, manual_limited, manual_block


def main():
    released = set()
    rel_file = DATA / "market_release.json"
    if rel_file.exists():
        d = json.loads(rel_file.read_text())
        for r in d.get("observe_released", []):
            if len(r) == 5 and r[4] == "live":
                released.add((r[0], r[1], r[2], r[3]))
    manual_release, manual_limited, manual_block = _load_manual_sets()

    bets = json.loads((DATA / "live_paper_bets.json").read_text())
    settled = [b for b in bets if b.get("settled") and b.get("anchor") == "betfair"]
    grid = defaultdict(list)
    for b in settled:
        sp = BB_SPORT_MAP.get(b.get("sport"))
        sm = BB_SUB_MAP.get(b.get("sub"))
        if not sp or not sm:
            continue
        dr = _direction(b.get("designation"), sm)
        bk = _bucket(b.get("fair"), sp)
        if bk == "?":
            continue
        grid[(sp, sm, dr, bk)].append(b)

    rows = []
    for (sp, sm, dr, bk), bs in grid.items():
        n = len(bs)
        lev = sum(b.get("clv") or 0 for b in bs) / n
        pnl = sum(b.get("profit") or 0 for b in bs)
        stk = sum(b.get("stake") or 0 for b in bs)
        roi = pnl / stk * 100 if stk else 0
        if not (lev > 0 and roi > 0):
            continue
        key = (sp, sm, dr, bk)
        if key in manual_release:
            status = "已释放·手动"
        elif key in manual_limited:
            status = "已释放·限"
        elif key in released:
            status = "已释放·数据"
        elif key in manual_block:
            status = "⚠已拦截"
        else:
            status = "★漏了(未释放)"
        rows.append((sp, sm, dr, bk, n, lev, roi, pnl, status))

    # 排序: 漏了的排最前(最值得看), 然后按 n 降序
    order = {"★漏了(未释放)": 0, "⚠已拦截": 1, "已释放·限": 2, "已释放·手动": 3, "已释放·数据": 3}
    rows.sort(key=lambda r: (order.get(r[8], 9), -r[4]))

    print("=" * 80)
    print("滚球「LEV>0 且 ROI>0」的桶 × 释放清单对齐")
    print("=" * 80)
    print(f"{'运动':6s}{'盘口':8s}{'方向':8s}{'概率桶':9s}{'n':>5s}  {'LEV':>7s}  {'ROI':>8s}  {'盈亏':>9s}  状态")
    print("-" * 80)
    for sp, sm, dr, bk, n, lev, roi, pnl, status in rows:
        print(f"{SPORT_CN.get(sp, sp):6s}{SUB_CN.get(sm, sm):8s}{dr:8s}{bk:9s}{n:5d}  {lev:+6.2f}%  {roi:+7.1f}%  {pnl:+9.1f}  {status}")

    # 汇总
    n_miss = sum(1 for r in rows if r[8] == "★漏了(未释放)")
    n_block = sum(1 for r in rows if r[8] == "⚠已拦截")
    n_rel = len(rows) - n_miss - n_block
    print("-" * 80)
    print(f"双正桶共 {len(rows)} 个: 已释放 {n_rel} | 已拦截 {n_block} | 漏了(未释放) {n_miss}")


if __name__ == "__main__":
    main()
