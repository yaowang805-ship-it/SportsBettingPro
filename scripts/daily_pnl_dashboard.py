#!/usr/bin/env python3
"""每日盈亏仪表盘 —— 读 real_bets.json 汇总当日+近7天盈亏, 按来源(滚球/drop/ws)拆分, 推钉钉。

2026-10-27 用户要求: 每天看真实盈亏, 不再靠感觉。与 daily_bet_report(BB官方口径)互补:
本脚本读本地 real_bets.json(本地结算口径), 拆 trigger_source, 供「哪个来源在出血」一目了然。

用法: .venv312/bin/python scripts/daily_pnl_dashboard.py
"""
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import send_dingtalk

REAL_BETS = ROOT / "data" / "storage" / "real_bets.json"
BJ = timezone(timedelta(hours=8))


def _grp(src):
    if src in ("pin_drop",):
        return "早盘drop"
    if src in ("ws_scan",):
        return "早盘ws"
    return "滚球"  # pin/poll/betfair_ws/其他 都算滚球


def main():
    if not REAL_BETS.exists():
        return
    d = json.loads(REAL_BETS.read_text())
    bets = d.get("bets", {})

    today = datetime.now(BJ).strftime("%Y-%m-%d")
    today_ts = datetime.now(BJ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day7_ts = today_ts - 7 * 86400

    def _agg(lo):
        g = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0})
        for oid, b in bets.items():
            ts = b.get("ts", 0)
            if ts < lo:
                continue
            res = b.get("result")
            if res is None:
                continue
            stake = float(b.get("stake", 0) or 0)
            profit = float(b.get("profit", 0) or 0)
            k = _grp(b.get("trigger_source"))
            g[k]["n"] += 1
            g[k]["stake"] += stake
            g[k]["profit"] += profit
        return g

    today_g = _agg(today_ts)
    day7_g = _agg(day7_ts)

    def _fmt(g):
        lines = []
        total_p = total_s = total_n = 0
        for k in ("滚球", "早盘drop", "早盘ws"):
            r = g.get(k)
            if not r or r["n"] == 0:
                continue
            roi = r["profit"] / r["stake"] * 100 if r["stake"] > 0 else 0
            lines.append(f"  {k:<8} {r['n']:>3}笔 注额¥{r['stake']:.0f} 盈亏¥{r['profit']:+.0f} ROI{roi:+.1f}%")
            total_p += r["profit"]; total_s += r["stake"]; total_n += r["n"]
        return lines, total_p, total_s, total_n

    t_lines, t_p, t_s, t_n = _fmt(today_g)
    w_lines, w_p, w_s, w_n = _fmt(day7_g)
    t_roi = t_p / t_s * 100 if t_s > 0 else 0
    w_roi = w_p / w_s * 100 if w_s > 0 else 0

    # 未结算笔数
    pending = sum(1 for b in bets.values() if b.get("result") is None)

    body = (f"📊 每日盈亏 {today}\n\n"
            f"【今日】{t_n}笔 注额¥{t_s:.0f} 盈亏¥{t_p:+.0f} ROI{t_roi:+.1f}%\n"
            + ("\n".join(t_lines) if t_lines else "  (今日无结算)") +
            f"\n\n【近7天】{w_n}笔 注额¥{w_s:.0f} 盈亏¥{w_p:+.0f} ROI{w_roi:+.1f}%\n"
            + "\n".join(w_lines) +
            f"\n\n未结算 {pending} 笔")
    send_dingtalk("📊 每日盈亏", body)


if __name__ == "__main__":
    main()
