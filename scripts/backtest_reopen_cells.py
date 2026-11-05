"""回测重算：用 platt 校准表把历史已结算注的 fair 校正到「真公平价」，量化哪些负格是被冤枉的。

背景：历史注的 fair 是 Shin 去水（favorite-longshot bias 高估赢率 6.5pp/冷门 11pp），
导致 EV 虚高 → 投了假溢价 → 负格。现在 Power 去水 + platt 校准已修。本脚本用校准表
（p_implied → p_cal 真实胜率）把 fair 校正到真公平价，重算 EV，看哪些 observe_blocked
的格其实是「真 EV 被 Shin 夸大而误拦」。

数据：live_paper_bets.json(滚球纸面) / real_bets.json(实盘) / clv_tracking.csv(早盘观察库)。
输出：按 (运动×盘口×方向×概率桶) 分格，对比 存储EV vs 校准EV vs 实际ROI。
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "storage"

# ── 校准表加载（p_implied -> p_cal，线性插值）──
def load_cal(market):
    p = DATA / ("fair_calibration.json" if market == "live" else "fair_calibration_prematch.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        tbl = d.get("table", [])
        return [(float(t["p_implied"]), float(t["p_cal"])) for t in tbl]
    except Exception:
        return []

def calibrate_p(p_imp, tbl):
    if not tbl or p_imp <= 0:
        return None
    xs = [t[0] for t in tbl]; ys = [t[1] for t in tbl]
    if p_imp <= xs[0]: return ys[0]
    if p_imp >= xs[-1]: return ys[-1]
    import bisect
    i = bisect.bisect_right(xs, p_imp) - 1
    x0, x1 = xs[i], xs[i+1]; y0, y1 = ys[i], ys[i+1]
    return y0 + (y1-y0)*(p_imp-x0)/(x1-x0)

CAL_LIVE = load_cal("live")
CAL_PRE = load_cal("prematch")

# ── 运动 id -> 名称 ──
SPORT_NAME = {1: "football", 3: "basketball", 5: "tennis", 7: "baseball",
              6: "american_football", 2: "ice_hockey", 8: "mma", 9: "boxing"}

# ── 盘口 sub -> observe_blocked 口径 ──
SUB_MAP = {"opportunities": "1x2", "handicap": "hc", "over_under": "ou",
           "double_chance": "dc", "ht": "ht", "ht_ou": "ht_ou", "ht_hc": "ht_hc"}

# ── 方向 designation -> observe_blocked 口径 ──
DIR_MAP = {"主胜": "主", "客胜": "客", "和局": "平", "让球主胜": "主", "让球客胜": "客",
           "大球": "大", "小球": "小", "主/和": "1X", "客/和": "X2", "主/客": "12",
           "上半场主胜": "主", "上半场客胜": "客", "上半场和局": "平", "上半场大球": "大", "上半场小球": "小"}

def prob_bucket(p):
    """隐含概率 -> 概率桶（复用 odds_interval 口径，sport 无关的通用版本）。"""
    from src.scrapers.odds_interval import prob_bucket as _pb
    return _pb(p)

# ── 已结算注收集 ──
# 每条: (sport, sub, dir, p_implied, stored_ev, cal_ev, result, profit, stake)
bets = []

def add_bet(sport, sub, desig, fair, bb_odds, result, profit, stake, market):
    if not fair or fair <= 1 or not bb_odds or bb_odds <= 1:
        return
    p_imp = 1.0 / fair
    tbl = CAL_LIVE if market == "live" else CAL_PRE
    p_cal = calibrate_p(p_imp, tbl)
    if p_cal is None or p_cal <= 0:
        return
    fair_cal = 1.0 / p_cal
    stored_ev = (bb_odds - fair) / fair * 100
    cal_ev = (bb_odds - fair_cal) / fair_cal * 100
    d = DIR_MAP.get(desig)
    if not d:
        return
    bets.append((sport, SUB_MAP.get(sub, sub), d, p_imp, stored_ev, cal_ev, result, profit, stake))

# 1) 滚球纸面 live_paper_bets.json
try:
    for v in json.load(open(DATA / "live_paper_bets.json")):
        if not v.get("settled"):
            continue
        add_bet(v.get("sport"), v.get("sub"), v.get("designation"), v.get("fair"),
                v.get("bb_odds"), v.get("result"), v.get("profit"), v.get("stake"), "live")
except Exception as e:
    print("live_paper_bets 加载失败:", e)

# 2) 实盘 real_bets.json
try:
    for v in json.load(open(DATA / "real_bets.json")).get("bets", {}).values():
        if v.get("result") not in ("won", "lost"):
            continue
        add_bet(v.get("sport"), v.get("sub"), v.get("desig"), v.get("fair"),
                v.get("bb_odds"), v.get("result"), v.get("profit"), v.get("stake"), "live")
except Exception as e:
    print("real_bets 加载失败:", e)

# 3) 早盘观察库 clv_tracking.csv (sport 是名称字符串)
try:
    for r in csv.DictReader(open(DATA / "clv_tracking.csv")):
        _sport = r.get("sport") or ""
        _sid = int(_sport) if _sport.lstrip("-").isdigit() else (_sport if _sport else 0)
        add_bet(_sid, r.get("sub_market"), r.get("designation"),
                float(r.get("fair_price") or 0), float(r.get("bb_odds") or 0),
                None, None, None, "prematch")
except Exception as e:
    print("clv_tracking 加载失败:", e)

print(f"总收集已结算注: {len(bets)}")

# ── 按格聚合 ──
cells = defaultdict(lambda: {"n": 0, "stored_ev": [], "cal_ev": [], "won": 0, "profit": 0.0, "stake": 0.0, "result_n": 0})
for sport, sub, d, p_imp, sev, cev, result, profit, stake in bets:
    _sport_name = SPORT_NAME.get(sport, str(sport)) if isinstance(sport, int) else str(sport)
    key = (_sport_name, sub, d, prob_bucket(p_imp))
    c = cells[key]
    c["n"] += 1
    c["stored_ev"].append(sev)
    c["cal_ev"].append(cev)
    if result in ("won", "lost"):
        c["result_n"] += 1
        if result == "won":
            c["won"] += 1
        if profit is not None:
            c["profit"] += float(profit)
        if stake is not None:
            c["stake"] += float(stake)

# ── 输出 ──
def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0

rows = []
for (sport, sub, d, bucket), c in cells.items():
    if c["n"] < 5:  # 太薄不采信
        continue
    roi = (c["profit"] / c["stake"] * 100) if c["stake"] > 0 else None
    rows.append((sport, sub, d, bucket, c["n"], avg(c["stored_ev"]), avg(c["cal_ev"]),
                 c["result_n"], c["won"], roi))

rows.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
print("\n=== 分格对比 (n>=5): 运动|盘口|方向|概率桶 | n | 存储EV(Shin) | 校准EV(Power+platt) | 结算n | 胜率 | 实盘ROI")
for sport, sub, d, bucket, n, sev, cev, rn, won, roi in rows:
    winrate = (won / rn * 100) if rn > 0 else None
    roi_s = f"{roi:+.1f}%" if roi is not None else "-"
    wr_s = f"{winrate:.0f}%" if winrate is not None else "-"
    # 标记：校准后 EV 由正转负 = 假溢价格；校准后仍正 = 真溢价格
    flag = ""
    if sev > 2 and cev < 0:
        flag = "  ⚠️假溢价(Shin正校准负)"
    elif cev > 2:
        flag = "  ✅真溢价(校准后仍正)"
    print(f"{sport:>12}|{sub:>6}|{d:>2}|{bucket:>8} | {n:>4} | {sev:>+7.1f} | {cev:>+7.1f} | {rn:>4} | {wr_s:>5} | {roi_s:>8}{flag}")

# ── 保存结果 ──
out = []
for sport, sub, d, bucket, n, sev, cev, rn, won, roi in rows:
    winrate = (won / rn * 100) if rn > 0 else None
    out.append({"sport": sport, "sub": sub, "dir": d, "bucket": bucket, "n": n,
                "stored_ev": round(sev, 2), "cal_ev": round(cev, 2),
                "result_n": rn, "winrate": round(winrate, 1) if winrate is not None else None,
                "roi": round(roi, 2) if roi is not None else None})
(DATA / "backtest_reopen_cells.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
print(f"\n结果已存 data/storage/backtest_reopen_cells.json ({len(out)} 格)")
