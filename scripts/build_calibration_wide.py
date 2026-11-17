#!/usr/bin/env python3
"""宽样本校准库构建 — 全市场 Pin 比赛的方向级去水样本(不做 ev/drop 筛选)。

目的(2026-11-17): 区分「去水 bias」vs「选择 bias」。现有 refit 校准用的 real_bets+paper_bets
都是「BB 偏离 Pin 才记录」的选择样本, 分不清 +5.8~12pp 偏差是去水方法错、还是选择动作本身制造。
宽样本 = pin_odds_cache.json 里全部有 1x2(money_line) 赔率的比赛, 每个方向(home/draw/away)
一行, 四种去水(Power/Shin/比例/additive)各算概率。赛后用 result_fetcher 回填比分 + 方向胜负,
供 compare_devig_methods.py 对比哪种去水在宽样本上 log-loss 最低(无偏)。

用法:
    .venv312/bin/python scripts/build_calibration_wide.py              # 落盘/追加当前缓存比赛
    .venv312/bin/python scripts/build_calibration_wide.py --settle     # 回填已开赛但无结果的行

字段: event_id,sport,league,home,away,match_epoch,side,pin_raw,p_power,p_shin,p_prop,p_add,home_score,away_score,y
"""
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STORAGE = ROOT / "data" / "storage"

PIN_CACHE = STORAGE / "pin_odds_cache.json"
WIDE_CSV = STORAGE / "calibration_wide.csv"

FIELDS = ["event_id", "sport", "league", "home", "away", "match_epoch", "side",
          "pin_raw", "p_power", "p_shin", "p_prop", "p_add",
          "home_score", "away_score", "y"]

# Pin league.sport.name → 系统 sport 名(与 result_fetcher / 观察库口径对齐)
SPORT_MAP = {
    "Soccer": "football",
    "Basketball": "basketball",
    "Baseball": "baseball",
    "Ice Hockey": "ice_hockey",
    "Hockey": "ice_hockey",
    "Tennis": "tennis",
    "American Football": "american_football",
    "Football": "american_football",  # 美式足球(Pin 里 football 可能是美足, 需 league 名辅助, 保守映射)
    "Table Tennis": "table_tennis",
    "Volleyball": "volleyball",
    "MMA": "mma",
    "Boxing": "boxing",
}


def _sport(ev):
    """从 event 提取系统 sport 名。优先 p_sport 字段, 回退 league.sport.name。"""
    ps = ev.get("p_sport")
    if ps:
        return SPORT_MAP.get(str(ps), str(ps).lower().replace(" ", "_"))
    lg = ev.get("league") or {}
    sp = (lg.get("sport") or {}).get("name")
    return SPORT_MAP.get(str(sp), str(sp).lower().replace(" ", "_"))


def _league(ev):
    return (ev.get("league") or {}).get("name", "")


def _match_epoch(ev):
    st = ev.get("startTime")
    if not st:
        return ""
    try:
        return datetime.fromisoformat(str(st).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return ""


def _devig_probs(odds_list):
    """四种去水 → 概率列表(按输入顺序)。返回 (p_power, p_shin, p_prop, p_add) 四元组。"""
    from src.scrapers.devig import devig_power, devig_shin, devig_mult, devig_additive
    return (devig_power(odds_list), devig_shin(odds_list),
            devig_mult(odds_list), devig_additive(odds_list))


def _extract_rows(ev):
    """单个 event → 方向级行 list(每方向一行, 四种去水概率)。"""
    eid = ev.get("id")
    p0 = (ev.get("periods") or {}).get("num_0") or {}
    ml = p0.get("money_line") or {}
    if not ml or not eid:
        return []
    home = ev.get("home", "")
    away = ev.get("away", "")
    if not home or not away:
        return []
    sport = _sport(ev)
    league = _league(ev)
    epoch = _match_epoch(ev)

    # 方向顺序固定: home/draw/away (2-way 无 draw)
    sides = ["home", "draw", "away"] if "draw" in ml else ["home", "away"]
    odds_list = []
    for s in sides:
        o = ml.get(s)
        if not o or float(o) <= 1.0:
            return []  # 缺腿, 整个盘口丢弃(去水需要完整向量)
        odds_list.append(float(o))

    try:
        p_pw, p_sh, p_pr, p_ad = _devig_probs(odds_list)
    except Exception:
        return []

    rows = []
    for i, s in enumerate(sides):
        rows.append({
            "event_id": str(eid), "sport": sport, "league": league,
            "home": home, "away": away, "match_epoch": epoch, "side": s,
            "pin_raw": round(odds_list[i], 4),
            "p_power": round(p_pw[i], 6), "p_shin": round(p_sh[i], 6),
            "p_prop": round(p_pr[i], 6), "p_add": round(p_ad[i], 6),
            "home_score": "", "away_score": "", "y": "",
        })
    return rows


def _existing_ids():
    """读现有 CSV 的 event_id 集合(upsert 去重)。"""
    ids = set()
    if WIDE_CSV.exists():
        try:
            with open(WIDE_CSV, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("event_id"):
                        ids.add(row["event_id"])
        except Exception:
            pass
    return ids


def build():
    d = json.loads(PIN_CACHE.read_text())
    cache = d.get("cache", d)
    existing = _existing_ids()
    rows = []
    for e in cache.values():
        ev = e.get("event", {})
        eid = str(ev.get("id") or "")
        if not eid or eid in existing:
            continue
        rows.extend(_extract_rows(ev))
    if not rows:
        print(f"无新增比赛(缓存 {len(cache)} 场, 已有 {len(existing)} 场已落盘)")
        return
    write_header = not WIDE_CSV.exists()
    with open(WIDE_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(rows)
    print(f"落盘 {len(rows)} 行方向级样本 (新增 {len({r['event_id'] for r in rows})} 场比赛)")


def settle():
    """回填赛果: 读 CSV 里已开赛但 y 为空的行, 用 result_fetcher 匹配比分, 判定方向胜负。"""
    from src.monitor.result_fetcher import fetch_results, find_match_result

    rows = []
    if not WIDE_CSV.exists():
        print("宽样本 CSV 不存在, 先跑 build")
        return
    with open(WIDE_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    now = time.time()
    pending = [r for r in rows if r.get("y") in ("", None)
               and r.get("match_epoch") and float(r["match_epoch"]) < now - 3600]
    if not pending:
        print("无待回填赛果的行(已开赛且无结果)")
        return

    # 按 league 分组抓赛果
    by_league = {}
    for r in pending:
        by_league.setdefault((r.get("sport", ""), r.get("league", "")), []).append(r)

    settled = 0
    for (sport, league), grp in by_league.items():
        results = fetch_results(league, days_back=4)
        if not results:
            continue
        for r in grp:
            bet = {"home_pin": r["home"], "away_pin": r["away"],
                   "home": r["home"], "away": r["away"],
                   "match_epoch": float(r["match_epoch"]), "league": league}
            mr = find_match_result(bet, results)
            if not mr:
                continue
            hs = mr.get("home_score")
            as_ = mr.get("away_score")
            if hs is None or as_ is None:
                continue
            hs, as_ = int(hs), int(as_)
            r["home_score"], r["away_score"] = hs, as_
            if hs > as_:
                outcome = "home"
            elif as_ > hs:
                outcome = "away"
            else:
                outcome = "draw"
            r["y"] = 1 if r["side"] == outcome else 0
            settled += 1

    with open(WIDE_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"回填 {settled} 行赛果 (待回填 {len(pending)} 行)")


if __name__ == "__main__":
    if "--settle" in sys.argv:
        settle()
    else:
        build()
