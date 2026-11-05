#!/usr/bin/env python3
"""干净观察库 ROI 日报 — 只统计「line bug 修复 + Power 去水 + platt 校准」全生效后的观察记录。

验证逻辑(2026-11-05): 观察库与实盘走同一套匹配/公平价/校准, 只是观察库全量记录(含被拒单)且零风险,
比实盘更快攒够样本。用它验证最近调整是否有效:
  - 干净观察库 ROI 稳定 > +2%  → 真溢价存在, 可逐步加仓
  - 0 附近                    → edge 太薄
  - < 0                       → 无正 edge, 该停

数据源:
  滚球 = live_paper_bets.json (ts=决策时刻, 已排除 sim_rejected)
  早盘 = paper_bets.json (settled_at=结算时刻)

用法: .venv312/bin/python scripts/clean_observe_daily.py
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "storage"

# 与 compute_market_release.py 的 CLEAN_DATA_CUTOFF 保持一致(单一事实来源, 别改漂移)
CLEAN_DATA_CUTOFF = 1793836800  # 2026-11-05 00:00 UTC
CLEAN_DATA_LABEL = "2026-11-05 00:00 UTC"


def _ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _load_live():
    """滚球观察库(结算): live_paper_bets.json, 只 anchor=pin 且非 sim_rejected。"""
    p = DATA / "live_paper_bets.json"
    if not p.exists():
        return []
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    bets = d if isinstance(d, list) else d.get("bets", [])
    out = []
    for b in bets:
        if b.get("anchor") != "pin" or b.get("sim_rejected"):
            continue
        ts = _ts(b.get("ts"))
        if ts is None or ts < CLEAN_DATA_CUTOFF:
            continue
        if not b.get("settled"):
            continue
        out.append({
            "scope": "滚球", "sub": b.get("sub") or "?", "desig": b.get("designation") or "?",
            "result": b.get("result"), "profit": b.get("profit") or 0.0, "stake": b.get("stake") or 0.0,
        })
    return out


def _load_early():
    """早盘观察库(结算): paper_bets.json, 只 anchor=pin。用 settled_at(结算时刻)作时间口径。"""
    p = DATA / "paper_bets.json"
    if not p.exists():
        return []
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    bets = d.get("bets", []) if isinstance(d, dict) else d
    out = []
    for b in bets:
        if b.get("anchor") != "pin":
            continue
        ts = _ts(b.get("settled_at"))
        if ts is None or ts < CLEAN_DATA_CUTOFF:
            continue
        if b.get("result") not in ("won", "lost", "push", "void"):
            continue
        out.append({
            "scope": "早盘", "sub": b.get("sub_market") or "?", "desig": b.get("designation") or "?",
            "result": b.get("result"), "profit": b.get("profit") or 0.0, "stake": b.get("stake") or 0.0,
        })
    return out


def _agg(bets):
    by = defaultdict(lambda: {"won": 0, "lost": 0, "push": 0, "void": 0, "profit": 0.0, "stake": 0.0})
    for b in bets:
        c = by[(b["scope"], b["sub"])]
        r = b["result"]
        if r == "won":
            c["won"] += 1
        elif r == "lost":
            c["lost"] += 1
        elif r == "push":
            c["push"] += 1
        elif r == "void":
            c["void"] += 1
        c["profit"] += b["profit"]
        c["stake"] += b["stake"]
    return by


def main():
    bets = _load_live() + _load_early()
    print(f"=== 干净观察库日报 (只统计 {CLEAN_DATA_LABEL} 之后, 排除 sim_rejected) ===")
    print(f"当前干净样本: {len(bets)} 笔 (滚球 {sum(1 for b in bets if b['scope']=='滚球')} / 早盘 {sum(1 for b in bets if b['scope']=='早盘')})\n")

    if not bets:
        print("尚无干净样本 —— 修复刚上线, 等比赛结算攒样本。")
        print("判据: 干净 ROI 稳定 >+2% → 真溢价存在可加仓; <0 → 无正edge。")
        return

    by = _agg(bets)
    # 总体
    tot = {"won": 0, "lost": 0, "push": 0, "void": 0, "profit": 0.0, "stake": 0.0}
    for c in by.values():
        for k in tot:
            tot[k] += c[k]
    roi = (tot["profit"] / tot["stake"] * 100) if tot["stake"] > 0 else 0.0
    wr = (tot["won"] / (tot["won"] + tot["lost"]) * 100) if (tot["won"] + tot["lost"]) > 0 else 0.0
    print(f"总体: {tot['won']}胜 {tot['lost']}负 {tot['push']}走盘 {tot['void']}退款 | "
          f"净 {tot['profit']:+.1f} | ROI {roi:+.2f}% | 胜率 {wr:.1f}%")
    verdict = "✅ 真溢价(可加仓)" if roi > 2 else ("⚠️ edge太薄" if roi > -1 else "❌ 无正edge(该停)")
    print(f"判定: {verdict}\n")

    # 分 滚球/早盘 × 盘口
    for scope in ("滚球", "早盘"):
        rows = [(k, c) for k, c in by.items() if k[0] == scope]
        if not rows:
            continue
        sp = sum(c["profit"] for _, c in rows)
        sst = sum(c["stake"] for _, c in rows)
        sroi = (sp / sst * 100) if sst > 0 else 0.0
        print(f"【{scope}】净 {sp:+.1f} | ROI {sroi:+.2f}%")
        for (_, sub), c in sorted(rows, key=lambda x: -x[1]["profit"]):
            r = (c["profit"] / c["stake"] * 100) if c["stake"] > 0 else 0.0
            print(f"    {sub:<16} {c['won']:>3}胜 {c['lost']:>3}负 {c['push']:>2}走盘  {c['profit']:>+8.1f}  ROI {r:+.2f}%")
        print()


if __name__ == "__main__":
    main()
