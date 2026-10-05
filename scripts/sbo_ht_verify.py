#!/usr/bin/env python3
"""SBO 半场盘口 CLV 校准验证 — 验证 SBO 能否补 Betfair 半场公平价缺口。

背景(2026-10-05): Betfair 无半场盘口(ML HT/Totals HT 实测 None), SBO 有 Totals HT/Spread HT
但「仅大比赛」(实测 Japan vs NZ 有, 低级别联赛无), 且 SBO 无 ML HT(上半独赢谁也补不了)。
本脚本验证: 对 SBO 有半场盘口的比赛, SBO 收盘线 CLV 是否可靠(能当公平价/CLV 尺子)。

机制(赛前盘, 三阶段):
  1. collect  — 赛前拍一次 SBO 半场公平价(去水), 存 sbo_ht_verify.json
  2. closing  — 临开赛重拍一次 SBO 半场公平价(收盘线), 补进同一条记录
  3. analyze  — 等赛果(手动/结算)后, 算 CLV(收盘vs下注) 与 结果 的相关性

用法:
  .venv312/bin/python scripts/sbo_ht_verify.py collect    # 赛前拍快照
  .venv312/bin/python scripts/sbo_ht_verify.py closing    # 临开赛重拍(收盘线)
  .venv312/bin/python scripts/sbo_ht_verify.py analyze    # 结算后分析 CLV 校准

数据文件: data/storage/sbo_ht_verify.json
  [{event_id, home, away, date, sub, line, bet_fair, bet_ts, close_fair, close_ts, result}]
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.scrapers import odds_api_io as oa

DATA = ROOT / "data" / "storage"
OUT = DATA / "sbo_ht_verify.json"

# 只验证 SBO 有的两个半场盘口(ML HT 没人有, 跳过)
HT_SUBS = ("ht_ou", "ht_hc")
SUB_CN = {"ht_ou": "上半大小", "ht_hc": "上半让球"}


def _load():
    if OUT.exists():
        try:
            return json.loads(OUT.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(rows):
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2))


def collect():
    """赛前拍快照: 找有 SBO 半场盘的未来足球赛, 记 SBO 半场去水公平价。"""
    now = time.time()
    evs = oa.get_events("football", status=None) or []
    from datetime import datetime
    def _ts(e):
        try:
            return datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0
    upcoming = [e for e in evs if _ts(e) > now]
    print(f"未来足球赛 {len(upcoming)} 场, 扫描 SBO 半场覆盖...")

    rows = _load()
    seen = {(r.get("event_id"), r.get("sub")) for r in rows}
    added = 0
    for ev in upcoming[:200]:
        eid = ev.get("id")
        home, away, date = ev.get("home"), ev.get("away"), ev.get("date")
        for sub in HT_SUBS:
            if (eid, sub) in seen:
                continue
            f = oa.sbo_fair_price(eid, sub, use_rest=True)
            if not f:
                continue
            line = f.get("line")
            bet_fair = {k: v for k, v in f.items() if k != "line"}
            rows.append({"event_id": eid, "home": home, "away": away, "date": date,
                         "sub": sub, "line": line, "bet_fair": bet_fair,
                         "bet_ts": int(time.time()), "close_fair": None,
                         "close_ts": None, "result": None})
            added += 1
            print(f"  + {home} vs {away} [{SUB_CN[sub]}] line={line} fair={bet_fair}")
    _save(rows)
    print(f"新增 {added} 条 | 累计 {len(rows)} 条 → {OUT}")


def closing():
    """临开赛重拍收盘线: 对已收集但未收盘的记录, 重拍 SBO 半盘公平价。"""
    rows = _load()
    now = time.time()
    updated = 0
    for r in rows:
        if r.get("close_fair"):
            continue
        f = oa.sbo_fair_price(r["event_id"], r["sub"], use_rest=True)
        if not f:
            continue
        r["close_fair"] = {k: v for k, v in f.items() if k != "line"}
        r["close_ts"] = int(now)
        updated += 1
    _save(rows)
    print(f"收盘重拍 {updated} 条 | 累计 {len(rows)} 条")


def analyze():
    """对已收盘的记录, 按「下注方向」算 CLV, 统计各方向 CLV 分布(等 result 填了才算校准)。

    未填 result 时只展示 CLV 分布(价格朝哪个方向走), 供判断 SBO 收盘线是否 sharp。
    """
    rows = [r for r in _load() if r.get("close_fair") and r.get("bet_fair")]
    print(f"已收盘记录 {len(rows)} 条")
    if not rows:
        print("(暂无收盘数据, 先跑 collect 攒赛前快照, 临开赛再跑 closing)")
        return
    print()
    print(f"{'比赛':30s}{'盘口':8s}{'线':>6s}  {'方向':6s}  下注价  收盘价   CLV")
    print("-" * 78)
    for r in rows:
        for d in r["bet_fair"]:
            bf = r["bet_fair"].get(d)
            cf = (r["close_fair"] or {}).get(d)
            if not bf or not cf:
                continue
            clv = (cf - bf) / bf * 100
            print(f"{r['home'][:18]+' vs '+r['away'][:12]:30s}{SUB_CN.get(r['sub'],r['sub']):8s}"
                  f"{str(r.get('line')):>6s}  {d:6s}  {bf:6.3f}  {cf:6.3f}  {clv:+7.2f}%")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    {"collect": collect, "closing": closing, "analyze": analyze}.get(mode, analyze)()
