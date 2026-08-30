#!/usr/bin/env python3
"""ht 独赢双锚交叉验证 — 用 Betfair Exchange 真实赔率检测 Pin 的平局定价偏差。

背景: Pin 对 ht 独赢把平局定低(真实平局57% vs 隐含46%), 导致 ht_dc 等衍生盘 CLV 假正、
实盘 ROI 负。用 OddsPapi 的 Betfair Exchange(无margin真实市场)做第二锚点, 对比 ht 平局
隐含概率, 偏差≥3pp 标记"Pin 可能偏差"。

数据源:
  Pin 侧: pinnacle_odds_archive.db 最近抓取的 ht 独赢(home/draw/away, period=1)
  Betfair 侧: OddsPapi bookmaker=betfair-ex, market 10208 (First Half Result)

用法: .venv312/bin/python scripts/cross_validate_ht_anchor.py
"""
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "storage"

# 联赛映射: Pin league_id → OddsPapi tournamentId (只验证主流联赛)
PIN_TO_ODDSAPI = {
    "17": "Premier League",    # 待核对 Pin 侧 id
}


def _norm(name: str) -> str:
    """队名归一化: 小写、去 FC/AFC/SC/CF 后缀、去 &/./- 符号。"""
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"\b(fc|afc|sc|cf)\b", "", s)
    s = re.sub(r"[&.\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _similar(a: str, b: str) -> bool:
    """宽松队名匹配: 归一化后 精确 或 子串(一个包含另一个)。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def load_pin_ht(recent_hours: int = 12):
    """从归档库拿最近 N 小时的 Pin ht 独赢(home/draw/away, period=1)。"""
    arch = DATA / "pinnacle_odds_archive.db"
    if not arch.exists():
        return {}
    con = sqlite3.connect(f"file:{arch}?mode=ro", uri=True)
    cur = con.cursor()
    # 最近 N 小时抓取的 ht 独赢, 按 (home, away) 聚合
    cur.execute("""
        SELECT home, away, designation, price, MAX(fetched_at)
        FROM odds_archive
        WHERE period=1 AND designation IN ('home','draw','away') AND price > 1
          AND fetched_at > datetime('now', ?)
        GROUP BY home, away, designation
    """, (f"-{recent_hours} hours",))
    agg = defaultdict(dict)
    for home, away, desig, price, _ in cur.fetchall():
        key = (home, away)
        agg[key][desig] = float(price)
    con.close()
    out = {}
    for (home, away), legs in agg.items():
        if all(k in legs for k in ("home", "draw", "away")):
            out[(home, away)] = legs
    return out


def cross_validate():
    from src.scrapers.oddsapi_anchor import fetch_betfair_anchor, pin_ht_anchor_compare

    pin_ht = load_pin_ht(recent_hours=12)
    print(f"Pin ht 独赢(最近12h归档): {len(pin_ht)} 场")

    if not pin_ht:
        print("⚠️ 归档库无 Pin ht 独赢数据(可能 Pin 熔断期间无新抓取)")
        return

    # 查 Betfair(英超17/西甲8/意甲23) — Betfair Exchange 每次最多约3个tournamentId
    bf = fetch_betfair_anchor([17, 8, 23])
    print(f"Betfair ht 独赢: {len(bf)} 场")

    bf_names = {(r["home"], r["away"]): r for r in bf if r.get("ht")}

    print()
    print(f"{'比赛':<40}{'Pin平局%':>10}{'BF平局%':>10}{'偏差pp':>8}  判定")
    print("-" * 80)
    flagged = 0
    for (home, away), legs in pin_ht.items():
        # 队名匹配 + 方向对齐: Pin (home,away) vs Betfair (bh,ba)
        matched = None
        for (bh, ba), r in bf_names.items():
            if _similar(home, bh) and _similar(away, ba):
                matched = r  # 方向一致
                break
            if _similar(home, ba) and _similar(away, bh):
                # Betfair home/away 与 Pin 反了, 交换 ht 赔率
                matched = {**r, "ht": {"home": r["ht"]["away"], "draw": r["ht"]["draw"], "away": r["ht"]["home"]}}
                break
        if not matched:
            continue
        cmp = pin_ht_anchor_compare(
            [legs["home"], legs["draw"], legs["away"]], matched["ht"]
        )
        if not cmp:
            continue
        mark = "⚠️Pin偏差" if cmp["flagged"] else "✅一致"
        if cmp["flagged"]:
            flagged += 1
        print(f"{home[:20]} vs {away[:20]:<20}{cmp['pin_draw_imp']:>9}%{cmp['bf_draw_imp']:>9}%{cmp['draw_dev_pp']:>+7}pp  {mark}")

    print("-" * 80)
    print(f"检测到 Pin 偏差的比赛: {flagged} 场")


if __name__ == "__main__":
    cross_validate()
