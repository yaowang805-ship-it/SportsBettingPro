#!/usr/bin/env python3
"""edge 健康监控(2026-11-01): 两个「长期稳定正收益」的预警指标。

1. edge 衰减: 释放格「最近14天实盘ROI」转负(但累计仍正) → 说明 edge 被市场吃掉, 预警。
2. 拒单率: oddsChange=0 拒单是「市场朝不利方向走」的实时信号, 拒单率飙升的格 → 预警。

用法: python3 scripts/monitor_edge_health.py
只打印预警, 不自动改清单(撤回仍走 compute_market_release 的 REAL_ROI_GATE)。
"""
import json
import time
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "data" / "storage"

SPORT_CN = {'football': '足球', 'basketball': '篮球', 'tennis': '网球', 'baseball': '棒球',
            'ice_hockey': '冰球', 'american_football': '美足', 'volleyball': '排球'}
SM_CN = {'1x2': '独赢', 'hc': '让球', 'ou': '大小球', 'dc': '双机会', 'ht': '上半场独赢',
         'ht_hc': '上半场让球', 'ht_ou': '上半场大小球', 'hc_games': '让局'}


def _sport(b):
    s = b.get('sport')
    return SPORT_CN.get(s, str(s))


def _sm(b):
    sub = str(b.get('sub', '?'))
    return SM_CN.get(sub, sub)


def main():
    # 读释放格
    rel = set()
    try:
        d = json.load(open(STORAGE / "market_release.json"))
        for x in d.get("observe_released", []):
            if isinstance(x, list) and len(x) >= 5:
                rel.add((str(x[0]), str(x[1]), str(x[2])))
    except Exception:
        pass

    # 读实盘
    rb = json.load(open(STORAGE / "real_bets.json"))
    bets = list(rb.get("bets", rb).values())
    now = time.time()
    win14 = now - 14 * 86400

    # 按 (sport,sm,dir) 聚合: 累计 vs 最近14天
    agg = defaultdict(lambda: {'cum_n': 0, 'cum_stk': 0.0, 'cum_pnl': 0.0,
                               'w14_n': 0, 'w14_stk': 0.0, 'w14_pnl': 0.0})
    for b in bets:
        if b.get('result') not in ('won', 'lost'):
            continue
        key = (_sport(b), _sm(b), str(b.get('desig', '?')))
        stk = float(b.get('stake') or 0); pnl = float(b.get('profit') or 0)
        ts = b.get('ts') or 0
        a = agg[key]
        a['cum_n'] += 1; a['cum_stk'] += stk; a['cum_pnl'] += pnl
        if ts >= win14:
            a['w14_n'] += 1; a['w14_stk'] += stk; a['w14_pnl'] += pnl

    print("=== edge 衰减预警(最近14天转负, 但累计仍正) ===")
    for key, a in sorted(agg.items(), key=lambda x: (x[1]['w14_pnl'] / x[1]['w14_stk'] if x[1]['w14_stk'] else 0)):
        cum_roi = a['cum_pnl'] / a['cum_stk'] * 100 if a['cum_stk'] else 0
        w14_roi = a['w14_pnl'] / a['w14_stk'] * 100 if a['w14_stk'] else 0
        if a['w14_n'] >= 5 and w14_roi < 0 and cum_roi > 0:
            print(f"  ⚠️ {key[0]}|{key[1]}|{key[2]}: 累计{cum_roi:+.1f}% 但最近14天{w14_roi:+.1f}% (n={a['w14_n']}) — edge 在衰减")

    # 拒单率(从 reject_domain.jsonl)
    rej = defaultdict(int)
    try:
        rp = STORAGE / "reject_domain.jsonl"
        if rp.exists():
            for line in open(rp):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("reject_reason") in ("risk_block", "mutex", "below_threshold"):
                    continue  # 只算「下单被拒」的, 不算「gate跳过」的
                key = (_sport(r), str(r.get("sub", '?')), str(r.get("direction", '?')))
                rej[key] += 1
    except Exception:
        pass

    print("\n=== 拒单率预警(拒绝数 > 成交数的一半) ===")
    for key, rn in rej.items():
        a = agg.get(key)
        if not a or a['w14_n'] < 5:
            continue
        rate = rn / (rn + a['w14_n']) * 100
        if rate > 33:
            print(f"  ⚠️ {key[0]}|{key[1]}|{key[2]}: 最近14天成交{a['w14_n']} + 拒绝{rn} → 拒单率{rate:.0f}% — 逆向选择信号")


if __name__ == "__main__":
    main()
