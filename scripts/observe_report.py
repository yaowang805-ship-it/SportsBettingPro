#!/usr/bin/env python3
"""观察库盘口表现展示 — BB + FB 滚球观察库, 全运动×全盘口×全方向×全赔率区间 细粒度分析。

用法:
  python scripts/observe_report.py                 # 默认: 整体 + 分维度 + 盘口×区间交叉
  python scripts/observe_report.py --cross         # 追加最细: 运动×盘口×方向×区间 四维
  python scripts/observe_report.py --min-n 50      # 只展示样本 >=50 的格子(过滤噪声)
  python scripts/observe_report.py --fb            # 只看 FB
  python scripts/observe_report.py --bb            # 只看 BB

核心指标(对标职业团队, 判据 = 赢率 vs 隐含, 不是 ROI):
  隐含 = mean(1/fair)  —— 用 mean(1/fair) 而非 1/mean(fair), Jensen 口径修正(2026-09-13,
  见 implied-mean-method-fix)。ROI 受注额/赔率扭曲, 只作参考。
  edge = 胜率 - 隐含   —— >0 才是真溢价(真 edge), <0 是假溢价。

数据源:
  BB: data/storage/live_paper_bets.json (滚球观察库, sub=over_under/handicap/opportunities)
  FB: data/storage/fb_live_paper_bets.json (FB独立观察库, sub=ou/hc/1x2)
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BB_FILE = ROOT / "data" / "storage" / "live_paper_bets.json"
FB_FILE = ROOT / "data" / "storage" / "fb_live_paper_bets.json"

SPORT_CN = {1: "足球", 3: "篮球", 5: "网球", 7: "棒球", 6: "美式足球", 4: "冰球",
            15: "排球", 12: "羽毛球", 8: "拳击", 9: "MMA", 2: "板球", 16: "乒乓球"}
SUB_CN = {"over_under": "大小球", "handicap": "让球", "opportunities": "独赢",
          "ou": "大小球", "hc": "让球", "1x2": "独赢"}


def _odds_interval(odds):
    """BB 赔率 → 区间标签(与 compute_market_release._odds_interval 同口径)。"""
    if odds is None or odds <= 1.0:
        return "?"
    if odds < 2.0:
        return "1.0-2.0"
    if odds < 3.0:
        return "2.0-3.0"
    if odds < 5.0:
        return "3.0-5.0"
    return ">5.0"


def _direction(designation):
    """designation(大球/主胜/让球客胜…) → 归一化方向(大/小/主/客/和/其他)。"""
    d = designation or ""
    if "大" in d and "球" in d:
        return "大"
    if "小" in d and "球" in d:
        return "小"
    if "让球" in d and "主" in d:
        return "主"
    if "让球" in d and "客" in d:
        return "客"
    if d.endswith("主胜"):
        return "主"
    if d.endswith("客胜"):
        return "客"
    if "和" in d or "平" in d:
        return "和"
    return "其他"


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return []


def _norm(b, fb):
    """把 FB 的 sub 归一化成 BB 口径, 返回统一字段 dict(只保留已结算)。"""
    if not b.get("settled"):
        return None
    sub = SUB_CN.get(b.get("sub", ""), b.get("sub", "?"))
    sport = b.get("sport")
    fair = b.get("fair") or 0
    implied = 1.0 / fair if fair > 1.0 else 0.0
    return {
        "sport": sport,
        "sport_cn": SPORT_CN.get(sport, f"sp{sport}") if sport is not None else "未标",
        "market": sub,
        "dir": _direction(b.get("designation", "")),
        "desig": b.get("designation", ""),
        "interval": _odds_interval(b.get("bb_odds")),
        "odds": b.get("bb_odds") or 0,
        "stake": b.get("stake") or 0,
        "profit": b.get("profit") or 0,
        "won": b.get("result") == "won",
        "implied": implied,
        "clv": b.get("clv"),
    }


def _agg(recs):
    """一批记录 → 统计 dict。edge = 胜率 - mean(1/fair)。"""
    n = len(recs)
    if n == 0:
        return None
    stake = sum(r["stake"] for r in recs)
    wins = sum(1 for r in recs if r["won"])
    pnl = sum(r["profit"] for r in recs)
    implied = sum(r["implied"] for r in recs) / n  # mean(1/fair), Jensen 口径
    wr = wins / n
    return {
        "n": n, "stake": stake, "wins": wins, "loses": n - wins,
        "wr": wr, "implied": implied, "edge": wr - implied,
        "pnl": pnl, "roi": pnl / stake if stake else 0.0,
    }


def _fmt_meta(d):
    return (f"{d['n']:>5} {d['stake']:>9.0f} {d['wr']*100:>5.1f}% {d['implied']*100:>5.1f}% "
            f"{d['edge']*100:>+6.1f}pp {d['pnl']:>+9.0f} {d['roi']*100:>+6.1f}%")


def _print_table(title, groups, min_n, sort_key="edge"):
    print(f"\n### {title}")
    print(f"{'维度':<22}{'笔':>5} {'投注':>9} {'胜率':>6} {'隐含':>6} {'edge':>7} {'盈亏':>9} {'ROI':>7}")
    rows = []
    for key, recs in groups.items():
        d = _agg(recs)
        if d and d["n"] >= min_n:
            rows.append((key, d))
    if not rows:
        print("  (无 >=min-n 的格子)")
        return
    rows.sort(key=lambda x: x[1][sort_key], reverse=True)  # 正 edge 排最前, 一眼看出真溢价
    for key, d in rows:
        print(f"{key:<22}{_fmt_meta(d)}")


def _group(recs, key_fn):
    g = defaultdict(list)
    for r in recs:
        g[key_fn(r)].append(r)
    return g


def report(name, bets, min_n, cross):
    recs = [x for x in (_norm(b, name == "FB") for b in bets) if x]
    if not recs:
        print(f"\n### {name}: 无数据")
        return
    d = _agg(recs)
    print(f"\n===== {name} 观察库 (已结算 {len(recs)} 笔) =====")
    print(f"整体: {_fmt_meta(d)}")

    _print_table(f"{name} 按盘口", _group(recs, lambda r: r["market"]), min_n)
    _print_table(f"{name} 按运动", _group(recs, lambda r: r["sport_cn"]), min_n)
    _print_table(f"{name} 按方向", _group(recs, lambda r: r["dir"]), min_n)
    _print_table(f"{name} 按赔率区间", _group(recs, lambda r: r["interval"]), min_n)
    _print_table(f"{name} 盘口×方向×赔率区间",
                 _group(recs, lambda r: f"{r['market']}×{r['dir']}×{r['interval']}"), min_n)
    if cross:
        _print_table(f"{name} 运动×盘口×方向×区间",
                     _group(recs, lambda r: f"{r['sport_cn']}|{r['market']}|{r['dir']}|{r['interval']}"),
                     max(min_n, 20))
    # CLV 分桶(下注后 60s 复验 Pin 公平价算的 CLV, 见 second_level_monitor._check_clv)
    clv_recs = [r for r in recs if r.get("clv") is not None]
    if clv_recs:
        print(f"\n### {name} CLV 分桶(有CLV的 {len(clv_recs)} 笔)")
        print(f"{'CLV桶':<18}{'笔':>5} {'投注':>9} {'胜率':>6} {'隐含':>6} {'edge':>7} {'盈亏':>9} {'ROI':>7}")
        for label, pred in [('正CLV(>0,真edge)', lambda r: r['clv'] > 0),
                            ('负CLV(<0,逆向)', lambda r: r['clv'] < 0),
                            ('零CLV', lambda r: r['clv'] == 0)]:
            d = _agg([r for r in clv_recs if pred(r)])
            if d:
                print(f"{label:<18}{_fmt_meta(d)}")


def main():
    ap = argparse.ArgumentParser(description="观察库盘口表现展示(BB+FB)")
    ap.add_argument("--min-n", type=int, default=30, help="最小样本量(默认30, 对齐 DIR_N_MIN)")
    ap.add_argument("--bb", action="store_true", help="只看 BB")
    ap.add_argument("--fb", action="store_true", help="只看 FB")
    ap.add_argument("--cross", action="store_true", help="追加最细四维交叉")
    args = ap.parse_args()

    if not args.fb:
        report("BB 滚球", _load(BB_FILE), args.min_n, args.cross)
    if not args.bb:
        report("FB 滚球", _load(FB_FILE), args.min_n, args.cross)


if __name__ == "__main__":
    main()
