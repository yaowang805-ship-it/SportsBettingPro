#!/usr/bin/env python3
"""对比 72h 扫描 vs 现在的赔率变化 — 重新抓取零售赔率重新算 Edge。

用法:
    python3 scripts/scan_window_compare.py
"""

import json, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fetchers.bsd_fetcher import fetch_upcoming_events, fetch_event_odds

DATA_DIR = ROOT / "data" / "storage"

BOOKMAKERS = [
    "pinnacle", "bet365", "william-hill", "1xbet", "bwin",
    "betway", "unibet", "marathon", "888sport", "betano",
    "sportingbet", "betvictor", "ladbrokes", "betsson",
    "interwetten", "novibet", "coral", "betfair",
]


def de_vig(h_odds, d_odds, a_odds):
    """去抽水，返回公平概率。"""
    total = 1/h_odds + 1/d_odds + 1/a_odds
    return {
        "home": (1/h_odds) / total,
        "draw": (1/d_odds) / total,
        "away": (1/a_odds) / total,
    }


def main():
    now = datetime.now(timezone.utc)
    print(f"当前时间: {now.strftime('%m-%d %H:%M')} UTC")
    print()

    # 读取 72h 扫描结果
    scan_path = DATA_DIR / "line_shopping_results.json"
    scan_data = json.loads(scan_path.read_text())
    old_opps = scan_data.get("opportunities", [])
    print(f"72h 扫描共有 {len(old_opps)} 个 +EV 机会")
    print()

    # 拉取当前 BSD 数据
    events = fetch_upcoming_events(hours_ahead=72)
    ev_map = {}
    for ev in events:
        ev_map[(ev.get("home_team",""), ev.get("away_team",""))] = ev

    # 按比赛统计 72h 机会
    from collections import defaultdict
    match_opps = defaultdict(list)
    for o in old_opps:
        match_opps[(o.get("home_team",""), o.get("away_team",""))].append(o)

    print(f"涉及 {len(match_opps)} 场比赛")
    print()

    # 逐一对比
    total_72h = 0
    total_now = 0
    total_dead = 0
    total_new = 0

    for (home, away), opps in sorted(match_opps.items(), key=lambda x: x[0][0]):
        ev = ev_map.get((home, away))
        if not ev:
            continue

        # 剩余时间
        ct = ev.get("commence_time") or ev.get("start_time", "")
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            h_remain = (dt - now).total_seconds() / 3600
        except:
            h_remain = 0

        # 重新抓取
        try:
            raw = fetch_event_odds(ev["id"])
        except:
            continue

        new_markets = raw.get("markets", {})
        new_h2h = new_markets.get("1x2")
        if not new_h2h:
            continue

        # 取出当前 Pinnacle 1x2 赔率
        def get_pinny(side):
            d = new_h2h.get(side, {}).get("bookmakers", {}).get("pinnacle", {})
            return float(d.get("decimal_odds", 0)) if d else 0

        p_h = get_pinny("HOME")
        p_d = get_pinny("DRAW")
        p_a = get_pinny("AWAY")
        if not (p_h and p_d and p_a):
            continue

        # 当前公平概率
        fp = de_vig(p_h, p_d, p_a)
        fair_map = {"home": fp["home"], "draw": fp["draw"], "away": fp["away"]}

        # 找出当前最好的零售赔率
        def best_retail(side):
            best = 0
            bm_data = new_h2h.get(side, {}).get("bookmakers", {})
            for slug in BOOKMAKERS:
                if slug == "pinnacle":
                    continue
                d = bm_data.get(slug, {})
                odds = float(d.get("decimal_odds", 0)) if d else 0
                if odds > best:
                    best = odds
            return best

        r_h = best_retail("HOME")
        r_d = best_retail("DRAW")
        r_a = best_retail("AWAY")
        retail_map = {"home": r_h, "draw": r_d, "away": r_a}

        # 计算当前的 Edge
        edge_map = {}
        for side in ["home", "draw", "away"]:
            if retail_map[side] > 0 and fair_map[side] > 0:
                edge_map[side] = (retail_map[side] / (1/fair_map[side]) - 1) * 100
            else:
                edge_map[side] = None

        # 对比 72h 的每个机会
        print(f"\n【{home} vs {away}】开赛剩余 {h_remain:.0f}h")
        side_cn = {"home": "主胜", "draw": "平局", "away": "客胜"}

        for o in opps:
            mkt = o.get("market", "")
            if mkt not in ("1x2",):
                continue  # 只对比 1x2

            outcome = o.get("outcome", "")
            old_retail = o.get("odds", 0)
            old_edge = o.get("edge_pct", 0)

            new_edge_val = edge_map.get(outcome)
            new_retail = retail_map.get(outcome, 0)

            total_72h += 1
            if new_edge_val is not None and new_edge_val > 0:
                status = "✅ 仍在"
                total_now += 1
            else:
                status = "❌ 消失"
                total_dead += 1

            # Pinnacle 变化
            old_p_odds = 1/o.get("pinny_prob", 1) if o.get("pinny_prob", 0) > 0 else 0
            new_p_odds = {"home": p_h, "draw": p_d, "away": p_a}.get(outcome, 0)

            print(f"  {status} {side_cn.get(outcome,outcome)}: "
                  f"Pinnacle {old_p_odds:.2f}→{new_p_odds:.2f} | "
                  f"零售 {old_retail:.2f}→{new_retail:.2f} | "
                  f"Edge {old_edge:+.1f}%→{new_edge_val:+.1f}%" if new_edge_val is not None else "无数据")

        # 检查是否有新出现的 +EV 机会（72h 时没有的）
        for side in ["home", "draw", "away"]:
            new_ev = edge_map.get(side)
            if new_ev and new_ev > 0:
                # 检查 72h 时是否有这个机会
                had_it = any(o.get("outcome") == side and o.get("market") == "1x2" for o in opps)
                if not had_it:
                    print(f"  🆕 新增 {side_cn[side]}: 零售@{retail_map[side]:.2f} Edge{new_ev:+.1f}%")
                    total_new += 1

    print()
    print("=" * 60)
    print(f"  72h 原来的 1x2 机会:        {total_72h} 个")
    print(f"  现在仍然 +EV:               {total_now} 个 ({total_now/total_72h*100:.1f}%)" if total_72h else "")
    print(f"  已变为 -EV:                 {total_dead} 个 ({total_dead/total_72h*100:.1f}%)" if total_72h else "")
    print(f"  新增的 +EV 机会:            {total_new} 个")
    print("=" * 60)


if __name__ == "__main__":
    main()
