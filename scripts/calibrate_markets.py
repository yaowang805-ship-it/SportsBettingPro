"""OddsPortal 各盘口(H C/OU/DC/DNB/BTTS/OE/正确比分)收盘价 → V5 权重矩阵校准。

读取 data/oddsportal_markets/<sport>/<league>__<season>_markets.csv (op_market_batch 下载),
按赔率区间计算各联赛各盘口的胜率, 补 1X2 没覆盖的盘口历史数据。

输出: config/oddsportal_markets_calibrated.py
  ODDSPORTAL_MARKET_DATA = {盘口: {中文联赛名: {bin: (wr, avg_odds, n)}}}

盘口结果判定(根据 final score home_score/away_score):
  hc 让球:  line 是主队让球线, home 赢 = home_score+line > away_score
  ou 大小:  over 赢 = home+away > line
  dc 双重:  home/draw=主不败, home/away=分胜负, draw/away=客不败
  dnb 平局退款: 平局 void(不算样本)
  btts 双边进球: yes = 双方都进球
  oe 单双:  odd = 总进球为奇数
  correct_score 正确比分:  side="3-1", 命中 = final 比分 == side

用法: python3 scripts/calibrate_markets.py
"""
import csv, sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
MARKETS_DIR = ROOT / "data" / "oddsportal_markets"
OUTPUT = ROOT / "config" / "oddsportal_markets_calibrated.py"

# 与 calibrate_oddsportal.py 一致的赔率区间
ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.30, 2.50, 2.70, 2.90,
    3.10, 3.30, 3.50, 3.70, 3.90, 4.20, 4.50, 4.80,
    5.20, 5.60, 6.00, 6.50, 7.00, 7.50, 8.00, 9.00,
    10.00, 12.00, 15.00, 20.00, float('inf'),
]

# 纳入校准的盘口(htft 半全场需要半场比分, 市场 CSV 没有, 跳过)
MARKETS = ["hc", "ou", "dc", "dnb", "btts", "oe", "correct_score"]


def bin_index(odds):
    for i, t in enumerate(ODDS_BINS):
        if odds < t:
            return i
    return len(ODDS_BINS) - 1


def _f(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def result_of(market, side, line, hs, as_):
    """返回 1=赢, -1=输, 0=走盘/void(不计入样本)。"""
    if market == "ou":
        total = hs + as_
        if side == "over":
            return 1 if total > line else (-1 if total < line else 0)
        return 1 if total < line else (-1 if total > line else 0)
    if market == "hc":
        # line 是主队让球线(负=主队让), home 赢条件: home_score + line > away_score
        if side == "home":
            return 1 if hs + line > as_ else (-1 if hs + line < as_ else 0)
        return 1 if hs + line < as_ else (-1 if hs + line > as_ else 0)
    if market == "dc":
        res = 1 if hs > as_ else (-1 if as_ > hs else 0)  # 1=主胜 0=平 -1=客胜
        if side == "home/draw":
            return 1 if res >= 0 else -1
        if side == "home/away":
            return 1 if res != 0 else -1
        return 1 if res <= 0 else -1  # draw/away
    if market == "dnb":
        if hs == as_:
            return 0  # 平局退款
        return 1 if (side == "home") == (hs > as_) else -1
    if market == "btts":
        yes = (hs > 0 and as_ > 0)
        return 1 if (side == "yes") == yes else -1
    if market == "oe":
        odd = (hs + as_) % 2 == 1
        return 1 if (side == "odd") == odd else -1
    if market == "correct_score":
        return 1 if f"{hs}-{as_}" == side else -1
    return None


def main():
    # slug → 中文联赛名, 复用 calibrate_oddsportal 的映射
    sys.path.insert(0, str(ROOT / "scripts"))
    from calibrate_oddsportal import SLUG_TO_CN

    # data[market][league][bin] = [wins, bets, sum_odds]
    data = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0])))
    totals = defaultdict(int)

    for sport_dir in sorted(MARKETS_DIR.iterdir()) if MARKETS_DIR.exists() else []:
        if not sport_dir.is_dir():
            continue
        sport = sport_dir.name
        for p in sorted(sport_dir.glob("*.csv")):
            slug = p.stem.split("__")[0]
            cn = SLUG_TO_CN.get(slug)
            if not cn:
                continue
            try:
                rows = list(csv.DictReader(open(p, encoding="utf-8")))
            except Exception:
                continue
            for r in rows:
                market = (r.get("market") or "").strip()
                if market not in MARKETS:
                    continue
                hs = _f(r.get("home_score"))
                as_ = _f(r.get("away_score"))
                odds = _f(r.get("avg_odds"))
                if hs is None or as_ is None or odds is None:
                    continue
                if not (1.01 <= odds <= 1000):
                    continue
                line = _f(r.get("line"))
                side = (r.get("side") or "").strip()
                if market in ("ou", "hc") and line is None:
                    continue
                res = result_of(market, side, line if line is not None else 0, hs, as_)
                if res is None or res == 0:
                    continue  # 走盘/void 不计入
                bi = bin_index(odds)
                data[market][cn][bi][1] += 1
                data[market][cn][bi][2] += odds
                if res == 1:
                    data[market][cn][bi][0] += 1
                totals[market] += 1

    # 逐盘口输出 {league: {bin: (wr, avg_odds, n)}}
    out_lines = [
        '"""OddsPortal 各盘口收盘价校准数据 (op_market_batch 下载的盘口历史)。',
        "格式: {盘口: {中文联赛名: {bin: (win_rate, avg_odds, n)}}}。",
        "盘口: hc/ou/dc/dnb/btts/oe/correct_score (htft 缺半场比分未纳入)。",
        '"""',
        "",
    ]

    def _write_market(market, min_n=20):
        lines = [f'    "{market}": {{']
        agg = defaultdict(lambda: [0, 0, 0.0])
        for lg, bins in sorted(data[market].items()):
            # 联赛级(样本>=min_n 才保留)
            entry = {}
            for bi in sorted(bins):
                w, b, s = bins[bi]
                agg[bi][0] += w; agg[bi][1] += b; agg[bi][2] += s
                if b >= min_n:
                    entry[bi] = (round(w / b, 4), round(s / b, 4), b)
            if entry:
                lines.append(f'        "{lg}": {{')
                for bi in sorted(entry):
                    wr, ao, n = entry[bi]
                    lines.append(f"            {bi}: ({wr}, {ao}, {n}),")
                lines.append("        },")
        # _AGGREGATE
        lines.append('        "_AGGREGATE": {')
        for bi in sorted(agg):
            w, b, s = agg[bi]
            if b >= min_n:
                lines.append(f"            {bi}: ({round(w / b, 4)}, {round(s / b, 4)}, {b}),")
        lines.append("        },")
        lines.append("    },")
        return lines

    out_lines.append("ODDSPORTAL_MARKET_DATA = {")
    for market in MARKETS:
        if totals[market] > 0:
            out_lines += _write_market(market)
    out_lines.append("}")

    OUTPUT.write_text("\n".join(out_lines), encoding="utf-8")
    summary = ", ".join(f"{m}={totals[m]:,}" for m in MARKETS if totals[m] > 0)
    print(f"✅ 盘口校准完成: {summary} → {OUTPUT.name}")


if __name__ == "__main__":
    main()
