#!/usr/bin/env python3
"""全量校准库构建 — 全运动 × 全盘口(1x2/hc/ou) 的方向级去水样本(不做 ev/drop 筛选)。

目的(2026-11-17 起, 2026-11-28 扩全盘口): 区分「去水 bias」vs「选择 bias」。
原版只采 1x2(home/draw/away), 早盘 hc/ou 无校准样本 → ou 公平价高估赢率 10-19pp 却无数据拟合。
2026-11-28 扩到 hc/ou: 每场 BB∩Pin 比赛采 1x2(home/draw/away) + hc(home/away+line) + ou(over/under+line)。

数据源:
  - 公平价: Pinnacle(pinnacle_fair_price 的 _raw 原始价 → 自己算四种去水)
  - 赛果:   BB getList type=6(fetch_bb_scores 的 id_map, 按 bb_match_id 精确匹配)

用法:
    .venv312/bin/python scripts/build_calibration_wide.py              # 落盘/追加当前 BB 全量比赛
    .venv312/bin/python scripts/build_calibration_wide.py --settle     # 回填已开赛且无结果的行

字段: bb_match_id,sport,league,home,away,match_epoch,sub_market,side,line,
      pin_raw,p_power,p_shin,p_prop,p_add,home_score,away_score,y
  sub_market: 1x2/hc/ou; side: 1x2=home/draw/away, hc=home/away, ou=over/under;
  line: hc=主队让分(主让=负)/ou=总分线; y: 1=赢 0=输 ""=走盘(push, 不进拟合)
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

FIELDS = ["bb_match_id", "sport", "league", "home", "away", "match_epoch", "sub_market", "side", "line",
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


def _rows_for_submarket(mid, sport_slug, league, home, away, epoch, sub, fair):
    """单个盘口的公平价(fair) → 方向级行 list。失败返回 []。"""
    if not fair or not fair.get("_raw"):
        return []
    raw = fair["_raw"]
    if sub == "1x2":
        sides = ["home", "draw", "away"] if "draw" in raw else ["home", "away"]
        line_val = ""
    elif sub == "hc":
        sides = ["home", "away"]
        line_val = fair.get("line")
        if line_val is None:
            return []
        line_val = str(line_val)
    elif sub == "ou":
        sides = ["over", "under"]
        line_val = fair.get("line")
        if line_val is None:
            return []
        line_val = str(line_val)
    else:
        return []

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

    rows = []
    for i, s in enumerate(sides):
        rows.append({
            "bb_match_id": str(mid), "sport": sport_slug, "league": league,
            "home": home, "away": away, "match_epoch": epoch,
            "sub_market": sub, "side": s, "line": line_val,
            "pin_raw": round(odds_list[i], 4),
            "p_power": round(p_pw[i], 6), "p_shin": round(p_sh[i], 6),
            "p_prop": round(p_pr[i], 6), "p_add": round(p_ad[i], 6),
            "home_score": "", "away_score": "", "y": "",
        })
    return rows


def _extract_rows(m):
    """单个 BB 比赛 → 全盘口(1x2/hc/ou)方向级行 list。失败返回 []。"""
    sport_slug = m.get("sport", "")
    sport_id = SLUG_TO_ID.get(sport_slug)
    home = (m.get("home") or "").strip()
    away = (m.get("away") or "").strip()
    mid = m.get("id")
    if not sport_id or not home or not away or not mid:
        return []

    from src.scrapers.pinnapi_client import pinnacle_fair_price
    bt = m.get("bt")
    try:
        epoch = float(bt) / 1000.0 if bt else ""
    except (TypeError, ValueError):
        epoch = ""
    league = m.get("league", "")

    rows = []
    for sub in ("1x2", "hc", "ou"):
        try:
            fair = pinnacle_fair_price(home, away, sport_id, sub, event_type="prematch")
        except Exception:
            fair = None
        rows.extend(_rows_for_submarket(mid, sport_slug, league, home, away, epoch, sub, fair))
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
    n_sub = len(rows)
    print(f"落盘 {n_sub} 行方向级样本 (新增 {n_match} 场 BB∩Pin 比赛, 含 1x2/hc/ou)")


def settle():
    """回填赛果: 用 BB getList type=6 的 id_map(bb_match_id→比分) 精确回填。按 sub_market 判输赢。"""
    from src.monitor.bb_score_settle import fetch_bb_scores

    if not WIDE_CSV.exists():
        print("宽样本 CSV 不存在, 先跑 build")
        return
    with open(WIDE_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    now = time.time()
    # pending = 无结果(y空 且 没回填过比分) 且已开赛
    pending = [r for r in rows if r.get("y") in ("", None)
               and not r.get("home_score")
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
        sub = r.get("sub_market", "1x2")
        side = r.get("side")
        y = ""
        if sub == "1x2":
            if hs > as_:
                outcome = "home"
            elif as_ > hs:
                outcome = "away"
            else:
                outcome = "draw"
            y = 1 if side == outcome else 0
        elif sub == "hc":
            try:
                ln = float(r.get("line"))
            except (TypeError, ValueError):
                ln = 0.0
            # line = 主队让分(主让=负); 主胜 hc = (主净胜 + line) > 0
            diff = (hs - as_) + ln
            if abs(diff) < 1e-6:
                y = ""  # push
            elif side == "home":
                y = 1 if diff > 0 else 0
            else:  # away
                y = 1 if diff < 0 else 0
        elif sub == "ou":
            try:
                ln = float(r.get("line"))
            except (TypeError, ValueError):
                ln = 0.0
            total = hs + as_
            if abs(total - ln) < 1e-6:
                y = ""  # push
            elif side == "over":
                y = 1 if total > ln else 0
            else:  # under
                y = 1 if total < ln else 0
        r["y"] = y
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
