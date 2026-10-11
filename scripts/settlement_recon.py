#!/usr/bin/env python3
"""盘后对账(2026-10-11) —— 只读检测版。

拉 BB 已结算订单 → 匹配 real_bets.json 未结算持仓(按 order_id) → 标 won/lost/void/partial/unmatched
→ 落 settlement_recon_YYYYMMDD.jsonl。**只读**: 不写 real_bets.json, 不触发任何下单动作。
每日一次错峰跑(避开滚球高峰), 目标: 量化 void 率(>5% 则 EV/slippage 分析全部打折)。

void 是软书进球回溯取消的常态, 现在实盘 P&L 被 void/partial 污染, 无法判断策略真伪。
本脚本不碰热路径、零竞态, 只产出对账流水 + void 率汇总。
"""
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.bb_auto_bet import read_token, read_domain, _session, _UA  # noqa: E402

REAL_BETS = ROOT / "data" / "storage" / "real_bets.json"
RECON_OUT = ROOT / "data" / "storage"
UNMATCHED_AGE = 24 * 3600  # 未结算且 >24h 查不到 → unmatched(赛事早该结束)


def _fetch_settled_orders():
    """分页拉全量已结算订单(对齐 _check_settled)。"""
    tok = read_token()
    dom = read_domain()
    if not tok:
        print("[recon] 无 token, 跳过", flush=True)
        return []
    records = []
    try:
        _page = 1
        while _page <= 20:
            r = _session().post(
                f"{dom}/v1/order/new/bet/list",
                json={"languageType": "CMN", "isSettled": True, "current": _page, "size": 100},
                headers={"Content-Type": "application/json", "Authorization": tok, "User-Agent": _UA},
                timeout=15, verify=False)
            d = r.json()
            if d.get("code") != 0:
                break
            _data = d.get("data") or {}
            _recs = _data.get("records") or []
            if not _recs:
                break
            records.extend(_recs)
            _total = _data.get("total", 0)
            if _total > 0 and len(records) >= _total:
                break
            _page += 1
    except Exception as e:
        print(f"[recon] 拉结算异常: {type(e).__name__} {str(e)[:80]}", flush=True)
    return records


def _classify(stake, pnl, odds):
    """BB uwl(净盈亏) → status。

    void: pnl==0(本金退回, 进球回溯取消/盘口作废)
    partial: 部分结算(赢了但不足全额, 或输了但没全输)——P&L 偏差主要来源
    won/lost: 全额
    """
    if pnl == 0:
        return "void"
    if odds and odds > 1 and stake > 0:
        full_win = stake * (odds - 1)
        if pnl > 0 and abs(pnl - full_win) / full_win > 0.02:
            return "partial"   # 赢但不足全额
        if pnl < 0 and abs(pnl + stake) / stake > 0.02:
            return "partial"   # 输但没全输
    return "won" if pnl > 0 else "lost"


def main():
    # 读本地持仓
    bets = {}
    try:
        bets = json.loads(REAL_BETS.read_text()).get("bets", {})
    except Exception:
        print("[recon] real_bets.json 读失败", flush=True)
        return
    unsettled = {str(k): v for k, v in bets.items() if not v.get("result")}

    # 拉 BB 已结算订单
    orders = _fetch_settled_orders()
    if not orders:
        print("[recon] 无已结算订单", flush=True)
        return

    now = time.time()
    recs = []
    settled_oids = set()
    for o in orders:
        oid = str(o.get("id", ""))
        if not oid:
            continue
        settled_oids.add(oid)
        ops = o.get("ops") or []
        op = ops[0] if ops else {}
        stake = o.get("sat", 0)
        pnl_raw = o.get("uwl", "0")
        try:
            pnl = float(pnl_raw)
        except (TypeError, ValueError):
            pnl = 0.0
        od = op.get("od", 0) or 0
        status = _classify(float(stake), pnl, float(od))
        # settled_amount = 返还金额: 赢=stake+pnl, void=stake, 输=0
        returned = max(0.0, float(stake) + pnl)
        recs.append({
            "oid": oid, "status": status, "stake": stake, "pnl": pnl,
            "odds": od, "returned": round(returned, 2),
            "mn": op.get("mn", ""), "on": op.get("on", ""), "mgn": op.get("mgn", ""),
            "void_reason": "", "matched_at": now,
        })

    # 本地未结算且查不到的 → pending(未结束) 或 unmatched(>24h 仍无, 疑映射错)
    for oid, v in unsettled.items():
        if oid not in settled_oids:
            _age = now - float(v.get("ts", 0))
            status = "unmatched" if _age > UNMATCHED_AGE else "pending"
            recs.append({
                "oid": oid, "status": status, "stake": v.get("stake", 0),
                "pnl": None, "odds": v.get("bb_odds", 0), "returned": None,
                "mn": f"{v.get('home','')} vs {v.get('away','')}", "on": v.get("desig", ""),
                "mgn": v.get("sub", ""), "void_reason": "", "matched_at": now,
            })

    # 落盘(append, 可重复跑)
    out = RECON_OUT / f"settlement_recon_{datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')}.jsonl"
    try:
        with open(out, "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[recon] 写盘失败: {e}", flush=True)
        return

    # 汇总 void 率(只看已结算的)
    settled = [r for r in recs if r["status"] in ("won", "lost", "void", "partial")]
    c = Counter(r["status"] for r in settled)
    total = len(settled)
    void_rate = c.get("void", 0) / total * 100 if total else 0
    partial_rate = c.get("partial", 0) / total * 100 if total else 0
    unmatched_n = sum(1 for r in recs if r["status"] == "unmatched")
    print(f"[recon] 已结算 {total} 笔: {dict(c)} | void率 {void_rate:.1f}% | partial率 {partial_rate:.1f}% | unmatched {unmatched_n} 笔", flush=True)
    if void_rate > 5:
        print(f"[recon] ⚠️ void率 {void_rate:.1f}% > 5%: 滚球 EV/slippage 分析需打折, 优先级重排", flush=True)


if __name__ == "__main__":
    main()
