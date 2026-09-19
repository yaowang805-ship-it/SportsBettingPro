#!/usr/bin/env python3
"""实盘滚球结算流水分析: 按「方向×赔率区间」拆盈亏(读 live_settled_log.json)。

数据源: data/storage/live_settled_log.json(由 second_level_monitor._check_settled 落盘,
每笔实盘注带赔率区间 + 是否验价)。
用法: .venv312/bin/python scripts/live_settled_analysis.py
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LOG_FILE = ROOT / "data" / "storage" / "live_settled_log.json"


def _direction(mn, mgn, on):
    """mgn(盘口: 大/小/独赢/让球) + on(方向) + mn("主 vs 客") → 归一化方向。"""
    home = away = ""
    if " vs " in (mn or ""):
        home, away = (mn or "").split(" vs ", 1)
    on_s = (on or "").strip()
    if mgn == "大/小":
        return "大球" if "大" in on_s else "小球"
    if mgn == "独赢":
        if "和" in on_s or "平" in on_s:
            return "和局"
        return "主胜" if (home and on_s and on_s in home) else "客胜"
    if mgn == "让球":
        return "让球主胜" if (home and on_s and on_s in home) else "让球客胜"
    return on_s or "?"


def main():
    if not LOG_FILE.exists():
        print("无结算流水(live_settled_log.json), 等实盘结算几笔后再跑")
        return
    try:
        bets = json.loads(LOG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        print("结算流水读取失败")
        return
    by = defaultdict(lambda: {"n": 0, "won": 0, "profit": 0.0, "stake": 0.0, "verified": 0})
    for b in bets:
        dr = _direction(b.get("mn", ""), b.get("mgn", ""), b.get("on", ""))
        iv = b.get("interval", "?")
        s = by[(dr, iv)]
        s["n"] += 1
        s["profit"] += b.get("uwl", 0) or 0
        s["stake"] += b.get("sat", 0) or 0
        if b.get("won"):
            s["won"] += 1
        if b.get("verify"):
            s["verified"] += 1
    print(f"实盘滚球结算流水 {len(bets)} 笔, 按「方向×赔率区间」:")
    print(f"{'方向':<9}{'区间':<9}{'笔':>5}{'胜率':>6}{'盈亏':>9}{'ROI':>7}{'验价':>5}")
    for (dr, iv), s in sorted(by.items(), key=lambda x: -x[1]["profit"]):
        wr = s["won"] / s["n"] * 100 if s["n"] else 0
        roi = s["profit"] / s["stake"] * 100 if s["stake"] else 0
        print(f"{dr:<9}{iv:<9}{s['n']:>5}{wr:>5.0f}%{s['profit']:>+9.0f}{roi:>+6.1f}%{s['verified']:>5}")
    tot_p = sum(s["profit"] for s in by.values())
    tot_n = sum(s["n"] for s in by.values())
    print(f"\n合计 {tot_n} 笔, 盈亏 {tot_p:+.0f}")


if __name__ == "__main__":
    main()
