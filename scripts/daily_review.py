#!/usr/bin/env python3
"""实盘投注复盘 — 每天结算后分析 + 复盘。

数据源: BB 官方已结算订单(isSettled=true, uwl 官方盈亏, 最准)。
复盘维度:
  1. 总体(笔数/注额/盈亏/ROI/赢率)
  2. 滚球 vs 早盘分开
  3. 分盘口×方向: 赢率 vs 隐含概率(核心判据)
  4. 赔率区间(longshot bias 检查)
核心判据: 赢率 > 隐含 = 真溢价(放量); 赢率 < 隐含 = 假溢价(砍)。

用法: python3 scripts/daily_review.py [--days N]  # N=复盘最近N天(默认1天)
"""
import sys
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR, send_dingtalk
from config.logging_config import get_logger

logger = get_logger(__name__)
BJ = timezone(timedelta(hours=8))
SPORT_CN = {1: "足球", 3: "篮球", 5: "网球", 7: "棒球", 6: "美式足球", 4: "冰球"}


def _fetch_settled_orders():
    from src.betting.bb_auto_bet import read_token, read_domain, _session, _UA
    tok = read_token(); dom = read_domain()
    if not tok:
        return []
    s = _session()
    all_recs = []
    for page in range(1, 12):
        try:
            r = s.post(f"{dom}/v1/order/new/bet/list",
                       json={"languageType": "CMN", "isSettled": True, "current": page, "size": 50},
                       headers={"Content-Type": "application/json", "Authorization": tok,
                                "User-Agent": _UA}, timeout=20, verify=False)
            d = r.json()
            if d.get("code") != 0:
                break
            recs = (d.get("data") or {}).get("records") or []
            if not recs:
                break
            all_recs.extend(recs)
            if len(all_recs) >= (d.get("data") or {}).get("total", 0):
                break
        except Exception:
            break
    return all_recs


def _winrate(items):
    """items=[(stake, pnl, odds)], 返回 (n, stake, pnl, won, winrate, implied)。"""
    n = len(items)
    stk = sum(i[0] for i in items)
    pnl = sum(i[1] for i in items)
    won = sum(1 for i in items if i[1] > 0)
    lost = sum(1 for i in items if i[1] < 0)
    # 赢率用去退款口径(退款=盈亏0, 不算输)
    real = [i for i in items if i[1] != 0]
    wr = won / len(real) * 100 if real else 0
    avg_od = sum(i[2] for i in items) / n if n else 0
    imp = 1 / avg_od * 100 if avg_od > 1 else 0
    return n, stk, pnl, won, lost, wr, imp


def _review(orders, days):
    """复盘最近 days 天的投注。返回文本报告。"""
    now = datetime.now(BJ)
    cutoff = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_ms = cutoff.timestamp() * 1000

    recs = [o for o in orders if (o.get("mt") or 0) >= cutoff_ms]
    if not recs:
        return None

    lines = [f"**📊 实盘复盘 {cutoff.strftime('%m-%d')} ~ {now.strftime('%m-%d')}**", ""]

    # 1. 总体
    live = []; early = []
    for o in recs:
        op = o["ops"][0] if o.get("ops") else {}
        mt = o.get("mt", 0); bt = op.get("bt", 0)
        stake = float(o.get("sat", 0)); pnl = float(o.get("uwl", 0)); odds = float(op.get("od", 0))
        (live if mt > bt else early).append((stake, pnl, odds))

    _n, _s, _p, _w, _l, _wr, _imp = _winrate(live + early)
    roi = _p / _s * 100 if _s else 0
    lines.append(f"总体: {_n}笔 ¥{_s:.0f} 盈亏{_p:+.0f} ROI{roi:+.1f}% 赢率{_wr:.0f}% (赢{_w}输{_l})")
    lines.append("")

    # 2. 滚球 vs 早盘 分盘口×方向
    for label, grp in [("滚球", live), ("早盘", early)]:
        if not grp:
            lines.append(f"【{label}】无投注")
            continue
        n, s, p, w, l, wr, imp = _winrate(grp)
        roi = p / s * 100 if s else 0
        lines.append(f"【{label}】{n}笔 盈亏{p:+.0f} ROI{roi:+.1f}% 赢率{wr:.0f}%")
        # 分盘口×方向
        bydir = defaultdict(list)
        for o in recs:
            op = o["ops"][0] if o.get("ops") else {}
            mt = o.get("mt", 0); bt = op.get("bt", 0)
            is_live = mt > bt
            if (label == "滚球") != is_live:
                continue
            key = f"{SPORT_CN.get(op.get('sid',0), op.get('sid','?'))}/{op.get('mgn','?')}/{op.get('on','?')}"
            bydir[key].append((float(o.get("sat",0)), float(o.get("uwl",0)), float(op.get("od",0))))
        for key, items in sorted(bydir.items(), key=lambda x: -sum(i[0] for i in x[1])):
            bn, bs, bp, bw, bl, bwr, bimp = _winrate(items)
            if bn < 3:
                continue
            diff = bwr - bimp
            flag = "✅真溢价" if diff > 8 else ("❌假溢价" if diff < -8 else "中性")
            lines.append(f"  {key}: {bn}笔 赢率{bwr:.0f}% 隐含{bimp:.0f}% 差{diff:+.0f}pp {flag}")
        lines.append("")

    # 3. 赔率区间(longshot 检查)
    bins = [(1.0, 2.0, "1.0-2.0"), (2.0, 3.0, "2.0-3.0"), (3.0, 5.0, "3.0-5.0"), (5.0, 99, ">5")]
    lines.append("【赔率区间】")
    for lo, hi, lab in bins:
        sub = [i for i in live + early if lo <= i[2] < hi]
        if not sub:
            continue
        n, s, p, w, l, wr, imp = _winrate(sub)
        diff = wr - imp
        lines.append(f"  {lab}: {n}笔 赢率{wr:.0f}% 隐含{imp:.0f}% 差{diff:+.0f}pp")

    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--push", action="store_true", help="推钉钉")
    args = ap.parse_args()

    orders = _fetch_settled_orders()
    if not orders:
        logger.info("无已结算订单")
        return
    body = _review(orders, args.days)
    if not body:
        logger.info("复盘期内无投注")
        return
    print(body)
    if args.push:
        send_dingtalk(f"📊 实盘复盘 {datetime.now(BJ).strftime('%m-%d')}", body, urgent=True)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
