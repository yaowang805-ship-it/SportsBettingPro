#!/usr/bin/env python3
"""全量交叉验证: 纸面ROI(观察库) vs 实盘ROI(真金白银) 按格子(运动×盘口×方向×概率桶×来源)。

目的(2026-11-01 用户要求): 照职业团队做法(模型EV vs 实际盈利持续对比, 背离=高估edge),
对所有滚球+早盘观察库及实盘做交叉验证, 找出:
  - 假正(纸面正实盘负, 不该释放却释放)
  - 假负(纸面负实盘正, 该释放却没释放)

口径与 compute_market_release.py 完全一致(同一 _direction/_odds_interval/BB_SPORT_MAP/BB_SUB_MAP)。
关键修正(2026-11-01): 实盘按 trigger_source 拆来源——
  滚球(live) = pin/poll/None(旧数据无字段); 早盘(early) = pin_drop/ws_scan。
  (compute_market_release.load_real_roi_cell 目前把 real_bets.json 全算成 live, 早盘实盘被污染;
   早盘实盘只读 tracked_bets.json(167笔,10-11~10-24), 漏了 real_bets 里 707 笔早盘。这里一并补齐。)
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "storage"

from src.scrapers.odds_interval import prob_bucket

BB_SPORT_MAP = {1: "football", 3: "basketball", 5: "tennis", 7: "baseball", 6: "american_football",
                2: "ice_hockey", 13: "volleyball", 15: "pingpong", 18: "mma", 19: "boxing", 47: "badminton",
                "1": "football", "3": "basketball", "5": "tennis", "7": "baseball", "6": "american_football",
                "2": "ice_hockey", "13": "volleyball", "15": "pingpong", "18": "mma", "19": "boxing", "47": "badminton",
                "football": "football", "basketball": "basketball", "tennis": "tennis", "baseball": "baseball",
                "american_football": "american_football", "ice_hockey": "ice_hockey", "volleyball": "volleyball",
                "pingpong": "pingpong", "mma": "mma", "boxing": "boxing", "badminton": "badminton"}
BB_SUB_MAP = {"over_under": "ou", "handicap": "hc", "opportunities": "1x2", "double_chance": "dc", "btts": "btts",
              "ht": "ht", "ht_ou": "ht_ou", "ou": "ou", "hc": "hc", "1x2": "1x2", "dc": "dc", "ml": "1x2",
              "hc_games": "hc_games", "handicap_games": "hc_games", "ht_hc": "ht_hc", "ht_dc": "ht_dc", "dnb": "dnb"}
SPORT_CN = {"football": "⚽足球", "basketball": "🏀篮球", "tennis": "🎾网球", "baseball": "⚾棒球",
            "american_football": "🏈美足", "ice_hockey": "🏒冰球", "volleyball": "🏐排球", "pingpong": "🏓乒乓球",
            "mma": "🥊MMA", "boxing": "👊拳击", "badminton": "🏸羽毛球"}
SUB_CN = {"1x2": "独赢", "hc": "让球", "ou": "大小球", "dc": "双机会", "ht": "半场独赢", "ht_ou": "半场大小",
          "ht_hc": "半场让球", "hc_games": "让局", "btts": "双边进球", "ht_dc": "半场双机会"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _direction(desig, sub_market):
    d = (desig or "")
    if sub_market == "dc":
        if "主" in d and "和" in d:
            return "1X"
        if "主" in d and "客" in d:
            return "12"
        if "和" in d and "客" in d:
            return "X2"
        return "其他"
    if sub_market == "htft":
        return "其他"
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


def _odds_interval(fair_or_odds, sport=None):
    p = _f(fair_or_odds)
    if p is None or p <= 1.0:
        return "?"
    return prob_bucket(1.0 / p, sport)


LIVE_REAL_ROI_CUTOFF_TS = 1793404800.0  # 2026-10-31T00:00:00Z 统一入库鲜价生效


def _agg(bets, scope, fmt):
    """fmt='live': sub/desig/sport(数字)/fair (real_bets.json 滚球+早盘统一格式);
       fmt='early': sub_market/designation/sport(字符串)/fair_price (tracked_bets.json / paper_bets_betfair)."""
    out = {}
    for b in bets:
        if fmt == "early":
            sp = b.get("sport") or "?"
            sm = b.get("sub_market") or "?"
            fair = _f(b.get("fair_price")) or _f(b.get("bb_odds"))
            dr = _direction(b.get("designation"), sm)
        else:
            sp = BB_SPORT_MAP.get(b.get("sport"))
            sm = BB_SUB_MAP.get(b.get("sub"))
            if not sp or not sm:
                continue
            fair = _f(b.get("fair")) or _f(b.get("bb_odds"))
            dr = _direction(b.get("desig") or b.get("designation"), sm)
        iv = _odds_interval(fair, sp)
        if iv == "?":
            continue
        d = out.setdefault((sp, sm, dr, iv, scope), {"n": 0, "stake": 0.0, "profit": 0.0, "won": 0})
        d["n"] += 1
        d["stake"] += _f(b.get("stake")) or 0.0
        d["profit"] += _f(b.get("profit")) or 0.0
        if b.get("result") == "won":
            d["won"] += 1
    r = {}
    for k, d in out.items():
        r[k] = {"n": d["n"], "roi": d["profit"] / d["stake"] * 100.0 if d["stake"] > 0 else 0.0,
                "winrate": d["won"] / d["n"] * 100.0 if d["n"] else 0.0}
    return r


def load_real():
    """实盘按 trigger_source 拆来源。返回 {cell_key: {n, roi, winrate}}。

    real_bets.json 里早盘(trigger_source=pin_drop/ws_scan)和滚球(pin/poll/None)同用 sub/desig/sport(数字)/fair 格式,
    只是 scope 不同; tracked_bets.json 是早盘老口径(sub_market/designation/sport字符串/fair_price)。
    """
    live, early = [], []
    raw = json.loads((DATA / "real_bets.json").read_text())
    d = raw.get("bets", raw) if isinstance(raw, dict) else {}
    vals = list(d.values()) if isinstance(d, dict) else d
    for b in vals:
        if not isinstance(b, dict) or b.get("result") not in ("won", "lost"):
            continue
        src = b.get("trigger_source")
        if src in ("pin_drop", "ws_scan"):
            early.append(b)
        else:  # pin / poll / None(旧数据) 都是滚球
            live.append(b)
    live = [b for b in live if (b.get("ts") or 0) >= LIVE_REAL_ROI_CUTOFF_TS]  # 滚球只认鲜价后
    out = {}
    out.update(_agg(live, "live", "live"))
    out.update(_agg(early, "early", "live"))  # real_bets 的早盘是 live 格式
    # 早盘老口径 tracked_bets.json(settled anchor=pin) 补充(compute_market_release 官方早盘实盘源)
    try:
        t = json.loads((DATA / "tracked_bets.json").read_text())
        tbets = [b for b in t.get("bets", []) if b.get("status") == "settled" and b.get("anchor") == "pin"]
        out.update(_agg(tbets, "early", "early"))
    except (json.JSONDecodeError, OSError):
        pass
    return out


def load_paper():
    """观察库纸面。滚球=live_paper_bets(去sim_rejected); 早盘=paper_bets.json(paper_settle 结算结果)。"""
    out = {}
    # 滚球纸面
    try:
        raw = json.loads((DATA / "live_paper_bets.json").read_text())
        bets = raw.get("bets", raw) if isinstance(raw, dict) else raw
        bets = [b for b in bets if b.get("anchor") == "pin" and not b.get("sim_rejected")
                and b.get("result") in ("won", "lost")]
        out.update(_agg(bets, "live", "live"))
    except (json.JSONDecodeError, OSError):
        pass
    # 早盘纸面(paper_settle 结算结果; 10-31 前旧数据已归档, 只认鲜价后)
    try:
        raw = json.loads((DATA / "paper_bets.json").read_text())
        bets = [b for b in raw.get("bets", []) if b.get("result") in ("won", "lost")]
        out.update(_agg(bets, "early", "early"))
    except (json.JSONDecodeError, OSError):
        pass
    return out


def load_current_status():
    mr = json.loads((DATA / "market_release.json").read_text())
    released = {tuple(e[:5]) for e in mr.get("observe_released", [])}
    blocked = {tuple(e[:5]) for e in mr.get("observe_blocked", [])}
    pure_released = released - blocked  # 冲突格(released∩blocked)实际被 _is_market_released 拦, 视为 blocked
    return released, blocked, pure_released


def main():
    real = load_real()
    paper = load_paper()
    released, blocked, pure_released = load_current_status()

    print(f"实盘格子数(live+early) = {len(real)}; 纸面格子数 = {len(paper)}")
    print(f"释放格: released={len(released)} 纯released={len(pure_released)} blocked={len(blocked)} 冲突={len(released & blocked)}")
    print()

    # 合并所有出现过的格子
    all_cells = set(real) | set(paper) | released | blocked

    rows = []
    for c in sorted(all_cells):
        sport, sm, dr, iv, scope = c
        rn, rroi = real.get(c, {"n": 0, "roi": 0})["n"], real.get(c, {"n": 0, "roi": 0})["roi"]
        pn, proi = paper.get(c, {"n": 0, "roi": 0})["n"], paper.get(c, {"n": 0, "roi": 0})["roi"]
        is_rel = c in pure_released
        status = "已释放" if is_rel else ("冲突格" if c in (released & blocked) else ("已拦" if c in blocked else "未定"))
        # 判定(基于「实际在投」=纯released)
        flag = ""
        if is_rel and rn >= 10 and rroi < 0:
            flag = "🔴假正(该撤)"
        elif is_rel and rn >= 10 and rroi > 0:
            flag = "✅释放正确"
        elif (not is_rel) and rn >= 10 and rroi > 0 and proi > 0:
            flag = "🟢假负(该放)"
        elif (not is_rel) and rn >= 10 and rroi > 0:
            flag = "🟡实盘正(纸面负/未覆盖)"
        rows.append((sport, sm, dr, iv, scope, rn, rroi, pn, proi, status, flag))

    # 打印：实盘 n>=10 的格子，按实盘 ROI 排序
    print("=" * 120)
    print("【交叉验证】实盘样本 n>=10 的格子，按实盘 ROI 排序 (纸面ROI 对照)")
    print("=" * 120)
    print(f"{'格子':<38}{'来源':<6}{'实盘n':>6}{'实盘ROI':>9}{'实盘胜率':>8}{'纸面n':>6}{'纸面ROI':>9}  状态/判定")
    shown = [r for r in rows if r[5] >= 10]
    shown.sort(key=lambda r: -r[6])
    for sport, sm, dr, iv, scope, rn, rroi, pn, proi, status, flag in shown:
        rw = real.get((sport, sm, dr, iv, scope), {}).get("winrate", 0)
        cn = f"{SPORT_CN.get(sport,sport)}{SUB_CN.get(sm,sm)}-{dr}({iv})"
        print(f"{cn:<38}{scope:<6}{rn:>6}{rroi:>+9.1f}{rw:>8.1f}{pn:>6}{proi:>+9.1f}  {status} {flag}")

    # 聚焦：假正 + 假负 清单
    print()
    print("=" * 120)
    print("🔴 假正(不该释放却释放, 实盘n>=10且ROI<0)")
    print("=" * 120)
    for sport, sm, dr, iv, scope, rn, rroi, pn, proi, status, flag in rows:
        if flag.startswith("🔴"):
            cn = f"{SPORT_CN.get(sport,sport)}{SUB_CN.get(sm,sm)}-{dr}({iv})"
            print(f"  {cn:<40}{scope} 实盘n={rn} ROI={rroi:+.1f}%  纸面n={pn} ROI={proi:+.1f}%")

    print()
    print("=" * 120)
    print("🟢 假负(该释放却没释放, 实盘n>=10且ROI>0 且 纸面ROI>0)")
    print("=" * 120)
    for sport, sm, dr, iv, scope, rn, rroi, pn, proi, status, flag in rows:
        if flag.startswith("🟢"):
            cn = f"{SPORT_CN.get(sport,sport)}{SUB_CN.get(sm,sm)}-{dr}({iv})"
            print(f"  {cn:<40}{scope} 实盘n={rn} ROI={rroi:+.1f}%  纸面n={pn} ROI={proi:+.1f}%  [当前:{status}]")

    print()
    print("=" * 120)
    print("🟡 实盘正但纸面负/未覆盖(实盘n>=10, ROI>0, 但纸面ROI<=0, 需进一步核实)")
    print("=" * 120)
    for sport, sm, dr, iv, scope, rn, rroi, pn, proi, status, flag in rows:
        if flag.startswith("🟡"):
            cn = f"{SPORT_CN.get(sport,sport)}{SUB_CN.get(sm,sm)}-{dr}({iv})"
            print(f"  {cn:<40}{scope} 实盘n={rn} ROI={rroi:+.1f}%  纸面n={pn} ROI={proi:+.1f}%  [当前:{status}]")


if __name__ == "__main__":
    main()
