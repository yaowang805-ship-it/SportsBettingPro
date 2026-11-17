#!/usr/bin/env python3
"""宽样本校准库构建 — 全市场 BB∩Pin 比赛的方向级去水样本(不做 ev/drop 筛选)。

目的(2026-11-17): 区分「去水 bias」vs「选择 bias」。现有 refit 校准用的 real_bets+paper_bets
都是「BB 偏离 Pin 才记录」的选择样本, 分不清 +5.8~12pp 偏差是去水方法错、还是选择动作本身制造。
宽样本 = bb_odds_extracted.json 里全部 BB 比赛(1938 场), 用 Pin 赔率(队名匹配)算四种去水
(Power/Shin/比例/additive)的概率, 赛后用 BB getMatchDetail(bb_score_settle)按 bb_match_id 精确回填比分。

数据源:
  - 公平价: Pinnacle(pinnacle_fair_price 的 _raw 原始价 → 自己算四种去水)
  - 赛果:   BB getList type=6(fetch_bb_scores 的 id_map, 按 bb_match_id 精确匹配, 覆盖 ESPN 拿不到的联赛)

用法:
    .venv312/bin/python scripts/build_calibration_wide.py              # 落盘/追加当前 BB 全量比赛
    .venv312/bin/python scripts/build_calibration_wide.py --settle     # 回填已开赛且无结果的行

字段: bb_match_id,sport,league,home,away,match_epoch,side,pin_raw,p_power,p_shin,p_prop,p_add,home_score,away_score,y
"""
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STORAGE = ROOT / "data" / "storage"

BB_SNAPSHOT = STORAGE / "bb_odds_extracted.json"
WIDE_CSV = STORAGE / "calibration_wide.csv"

FIELDS = ["bb_match_id", "sport", "league", "home", "away", "match_epoch", "side",
          "pin_raw", "p_power", "p_shin", "p_prop", "p_add",
          "home_score", "away_score", "y"]

# bb_odds_extracted 的 sport slug(下划线) → 系统 sport id(与 BB_TO_PIN 一致)
SLUG_TO_ID = {
    "football": 1, "basketball": 3, "tennis": 5, "baseball": 7,
    "american_football": 6, "ice_hockey": 2, "volleyball": 13,
    "table_tennis": 15, "pingpong": 15, "mma": 18, "boxing": 19, "badminton": 47,
}


def _devig_probs(odds_list):
    """四种去水 → 概率列表(按输入顺序)。返回 (p_power, p_shin, p_prop, p_add)。"""
    from src.scrapers.devig import devig_power, devig_shin, devig_mult, devig_additive
    return (devig_power(odds_list), devig_shin(odds_list),
            devig_mult(odds_list), devig_additive(odds_list))


def _extract_rows(m):
    """单个 BB 比赛 → 方向级行 list(每方向一行, 四种去水概率)。失败返回 []。"""
    sport_slug = m.get("sport", "")
    sport_id = SLUG_TO_ID.get(sport_slug)
    home = (m.get("home") or "").strip()
    away = (m.get("away") or "").strip()
    mid = m.get("id")
    if not sport_id or not home or not away or not mid:
        return []

    from src.scrapers.pinnapi_client import pinnacle_fair_price
    try:
        fair = pinnacle_fair_price(home, away, sport_id, "1x2", event_type="prematch")
    except Exception:
        return []
    if not fair or not fair.get("_raw"):
        return []

    raw = fair["_raw"]
    sides = ["home", "draw", "away"] if "draw" in raw else ["home", "away"]
    odds_list = []
    for s in sides:
        o = raw.get(s)
        if not o or float(o) <= 1.0:
            return []  # 缺腿, 整盘口丢弃
        odds_list.append(float(o))

    try:
        p_pw, p_sh, p_pr, p_ad = _devig_probs(odds_list)
    except Exception:
        return []

    bt = m.get("bt")
    try:
        epoch = float(bt) / 1000.0 if bt else ""
    except (TypeError, ValueError):
        epoch = ""

    rows = []
    for i, s in enumerate(sides):
        rows.append({
            "bb_match_id": str(mid), "sport": sport_slug, "league": m.get("league", ""),
            "home": home, "away": away, "match_epoch": epoch, "side": s,
            "pin_raw": round(odds_list[i], 4),
            "p_power": round(p_pw[i], 6), "p_shin": round(p_sh[i], 6),
            "p_prop": round(p_pr[i], 6), "p_add": round(p_ad[i], 6),
            "home_score": "", "away_score": "", "y": "",
        })
    return rows


def _existing_ids():
    ids = set()
    if WIDE_CSV.exists():
        try:
            with open(WIDE_CSV, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("bb_match_id"):
                        ids.add(row["bb_match_id"])
        except Exception:
            pass
    return ids


def build():
    d = json.loads(BB_SNAPSHOT.read_text())
    matches = d.get("matches", d) if isinstance(d, dict) else d
    existing = _existing_ids()
    rows = []
    for m in matches:
        mid = str(m.get("id") or "")
        if not mid or mid in existing:
            continue
        rows.extend(_extract_rows(m))
    if not rows:
        print(f"无新增比赛(BB {len(matches)} 场, 已落盘 {len(existing)} 场)")
        return
    write_header = not WIDE_CSV.exists()
    with open(WIDE_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(rows)
    n_match = len({r["bb_match_id"] for r in rows})
    print(f"落盘 {len(rows)} 行方向级样本 (新增 {n_match} 场 BB∩Pin 比赛)")


def settle():
    """回填赛果: 用 BB getList type=6 的 id_map(bb_match_id→比分) 精确回填。"""
    from src.monitor.bb_score_settle import fetch_bb_scores

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

    _score_map, id_map = fetch_bb_scores()
    settled = 0
    for r in pending:
        hs_as = id_map.get(r["bb_match_id"])
        if not hs_as:
            continue
        hs, as_ = int(hs_as[0]), int(hs_as[1])
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
    print(f"回填 {settled}/{len(pending)} 行赛果 (id_map 覆盖 {len(id_map)} 场)")


if __name__ == "__main__":
    if "--settle" in sys.argv:
        settle()
    else:
        build()
