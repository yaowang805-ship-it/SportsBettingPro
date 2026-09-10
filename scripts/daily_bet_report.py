#!/usr/bin/env python3
"""实盘结算日报 — 直接读 BB 官方已结算订单(isSettled=true)。

BB 官方订单的 uwl 字段是 BB 自己算好的盈亏(最准), 不再用本地 tracked_bets 自己算
(那样会漏结算/算错)。每天 9 点由 pipeline bet_report 定时调用, 发钉钉。

报告内容(简洁): 今日投注/盈亏/ROI + 分盘口 + 近7天累计。
"""
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import send_dingtalk
from config.logging_config import get_logger

logger = get_logger(__name__)
BJ = timezone(timedelta(hours=8))


def _fetch_settled_orders():
    """拉全部 BB 已结算订单(isSettled=true)。"""
    from src.betting.bb_auto_bet import read_token, read_domain, _session, _UA
    tok = read_token()
    dom = read_domain()
    if not tok:
        return []
    s = _session()
    all_recs = []
    for page in range(1, 10):
        try:
            r = s.post(f"{dom}/v1/order/new/bet/list",
                       json={"languageType": "CMN", "isSettled": True, "current": page, "size": 50},
                       headers={"Content-Type": "application/json", "Authorization": tok,
                                "User-Agent": _UA}, timeout=20, verify=False)
            d = r.json()
            recs = (d.get("data") or {}).get("records") or []
            if not recs:
                break
            all_recs.extend(recs)
            if len(all_recs) >= (d.get("data") or {}).get("total", 0):
                break
        except Exception:
            break
    return all_recs


def _norm(o):
    """订单 → (投注时间戳, 盘口, 注额, 盈亏)。"""
    op = (o.get("ops") or [{}])[0]
    mt = o.get("mt") or 0
    mgn = op.get("mgn", "?")
    sat = float(o.get("sat", 0) or 0)
    pnl = float(o.get("uwl", 0) or 0)
    return mt, mgn, sat, pnl


def _summ(items):
    """items = [(mt, mgn, sat, pnl)] → (n, stake, pnl, won, lost, push, roi)。"""
    n = len(items)
    stk = sum(i[2] for i in items)
    pnl = sum(i[3] for i in items)
    won = sum(1 for i in items if i[3] > 0)
    lost = sum(1 for i in items if i[3] < 0)
    push = n - won - lost
    roi = pnl / stk * 100 if stk else 0
    return n, stk, pnl, won, lost, push, roi


def main():
    orders = _fetch_settled_orders()
    if not orders:
        logger.info("无已结算订单, 不发日报")
        return
    norm = [_norm(o) for o in orders if o.get("mt")]

    now = datetime.now(BJ)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day0_ms = today0.timestamp() * 1000
    week0_ms = day0_ms - 6 * 86400 * 1000  # 近7天(含今天)

    today = [i for i in norm if i[0] >= day0_ms]
    week = [i for i in norm if i[0] >= week0_ms]

    lines = [f"**📊 实盘结算 {now.strftime('%m-%d')}**", ""]

    if today:
        n, stk, pnl, won, lost, push, roi = _summ(today)
        lines.append(f"今日: {n}笔 ¥{stk:.0f} | 盈亏 {pnl:+.0f} | ROI {roi:+.1f}% | 赢{won}输{lost}退{push}")
        # 分盘口(按注额降序)
        by_mkt = defaultdict(list)
        for i in today:
            by_mkt[i[1]].append(i)
        lines.append("分盘口:")
        for mgn, bs in sorted(by_mkt.items(), key=lambda x: -sum(i[2] for i in x[1])):
            _, bstk, bpnl, _, _, _, broi = _summ(bs)
            lines.append(f"  {mgn}: {len(bs)}笔 {bpnl:+.0f} (ROI {broi:+.0f}%)")
    else:
        lines.append("今日暂无已结算投注")

    if len(week) > len(today):
        wn, wstk, wpnl, _, _, _, wroi = _summ(week)
        lines.append(f"\n近7天: {wn}笔 盈亏 {wpnl:+.0f} (ROI {wroi:+.1f}%)")

    body = "\n".join(lines)
    # urgent=True: 定时日报是用户明确要的例行报告, 绕过非投注消息每日6条限流(否则被挤掉)
    ok = send_dingtalk(f"📊 实盘结算报告 {now.strftime('%m-%d')}", body, urgent=True)
    logger.info("实盘结算报告发送: %s", ok)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
