"""BB投注赔率 vs Pin收盘价 验证 + 实盘ROI归因。

回答两个核心问题:
1. 实盘是否盈利? — 对已结算注按 市场/联赛/tier/EV/赔率 分桶看真实 ROI (ground truth)。
2. BB赔率是否长期跑赢 Pin 收盘价? — CLV (正CLV率 + 均值), 这是套利模型有效的金标准。

数据源:
- tracked_bets.json: 已结算注 (bb_odds, pin_odds, fair_price, ev_pct, result, profit, stake)
- clv_results.csv: 实盘 Pin 收盘 CLV (clv_collector 产出, 样本随采集增长)
- clv_backfill.json: OddsPortal 收盘 CLV (clv_backfill 产出, 代理 Pin 收盘)

用法: python3 -m src.monitor.bb_closing_validation [--detail]
"""
import json, csv, statistics, argparse, math
from pathlib import Path
from collections import defaultdict

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

TRACKED_BETS = DATA_DIR / "tracked_bets.json"
CLV_RESULTS = DATA_DIR / "clv_results.csv"
CLV_BACKFILL = DATA_DIR / "clv_backfill.json"
REPORT_FILE = DATA_DIR / "bb_closing_validation.json"


# ── 1. 实盘 ROI 归因 ──

def _load_settled():
    if not TRACKED_BETS.exists():
        return []
    try:
        bets = json.loads(TRACKED_BETS.read_text()).get("bets", [])
    except Exception:
        return []
    # 只看有真实结算结果且有 stake 的注 (void 不计盈亏)
    out = []
    for b in bets:
        if b.get("status") != "settled":
            continue
        stake = b.get("stake") or 0
        if not stake:
            continue
        profit = b.get("profit")
        if profit is None:
            continue
        out.append(b)
    return out


def _roi_of(bets):
    st = sum(b.get("stake") or 0 for b in bets)
    pr = sum(b.get("profit") or 0 for b in bets)
    return (pr, st, (pr / st * 100) if st else 0.0)


def _bucketize(bets, keyfn):
    groups = defaultdict(list)
    for b in bets:
        groups[keyfn(b)].append(b)
    return groups


def _ev_bucket(ev):
    ev = ev or 0
    if ev < 2: return "<2%"
    if ev < 4: return "2-4%"
    if ev < 6: return "4-6%"
    if ev < 8: return "6-8%"
    if ev < 10: return "8-10%"
    return ">10%"


def _odds_bucket(o):
    o = o or 0
    if o < 1.5: return "<1.5"
    if o < 2.0: return "1.5-2.0"
    if o < 2.5: return "2.0-2.5"
    if o < 3.5: return "2.5-3.5"
    if o < 5.0: return "3.5-5.0"
    return ">5.0"


def _ttest_pvalue(rois):
    """单样本 t 检验: 均值是否显著 > 0。返回 p 值。"""
    n = len(rois)
    if n < 2:
        return None
    mean = statistics.mean(rois)
    sd = statistics.stdev(rois) if n > 1 else 0
    if sd == 0:
        return 0.0 if mean > 0 else 1.0
    t = mean / (sd / math.sqrt(n))
    # 用正态近似 (n 大时 t≈z)
    return 1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))


def analyze_realized_roi():
    bets = _load_settled()
    if not bets:
        return {"status": "insufficient", "n": 0}

    non_void = [b for b in bets if b.get("result") != "void"]
    pr, st, roi = _roi_of(non_void)
    won = sum(1 for b in non_void if b.get("result") == "won")
    lost = sum(1 for b in non_void if b.get("result") == "lost")

    # per-bet ROI (用 stake 归一) 用于显著性检验
    rois = [b.get("profit") / b.get("stake") for b in non_void if b.get("stake")]
    p_value = _ttest_pvalue(rois)

    slices = {}
    for name, keyfn in [
        ("by_sport", lambda b: b.get("sport", "?")),
        ("by_market", lambda b: b.get("sub_market", "?")),
        ("by_tier", lambda b: f"T{b.get('tier', '?')}"),
        ("by_ev", lambda b: _ev_bucket(b.get("ev_pct", 0))),
        ("by_odds", lambda b: _odds_bucket(b.get("bb_odds", 0))),
    ]:
        g = _bucketize(non_void, keyfn)
        slices[name] = {k: {"n": len(v), **_roi_asdict(v)} for k, v in sorted(g.items(), key=lambda x: -sum(y.get("stake") or 0 for y in x[1]))}

    # 联赛 top10 (按 stake)
    by_league = _bucketize(non_void, lambda b: b.get("league", "?"))
    top_league = {k: {"n": len(v), **_roi_asdict(v)} for k, v in
                  sorted(by_league.items(), key=lambda x: -sum(y.get("stake") or 0 for y in x[1]))[:10]}

    return {
        "status": "ok", "n_settled": len(bets), "n_non_void": len(non_void),
        "won": won, "lost": lost, "void": len(bets) - len(non_void),
        "win_rate": round(won / (won + lost) * 100, 1) if (won + lost) else 0,
        "total_profit": round(pr, 2), "total_stake": round(st, 2),
        "roi_pct": round(roi, 2),
        "mean_per_bet_roi": round(statistics.mean(rois), 4) if rois else 0,
        "p_value_roi_gt_0": round(p_value, 4) if p_value is not None else None,
        "significant_95": (p_value < 0.05) if p_value is not None else None,
        **slices, "top_leagues": top_league,
    }


def _roi_asdict(bets):
    pr, st, roi = _roi_of(bets)
    return {"profit": round(pr, 2), "stake": round(st, 2), "roi_pct": round(roi, 2)}


# ── 2. EV 信号验证: 推送时 EV 是否预测真实 ROI ──

def analyze_ev_signal():
    bets = [b for b in _load_settled() if b.get("result") != "void" and b.get("stake")]
    if len(bets) < 20:
        return {"status": "insufficient", "n": len(bets)}
    evs = [b.get("ev_pct", 0) or 0 for b in bets]
    rois = [b.get("profit") / b.get("stake") for b in bets]
    corr = _pearson(evs, rois) if len(evs) > 1 else 0.0
    # EV 分桶 ROI
    buckets = _bucketize(bets, lambda b: _ev_bucket(b.get("ev_pct", 0)))
    by_ev = {k: {"n": len(v), **_roi_asdict(v)} for k, v in sorted(buckets.items())}
    return {"status": "ok", "n": len(bets), "corr_ev_roi": round(corr, 4), "by_ev": by_ev}


def _pearson(x, y):
    n = len(x)
    if n < 2:
        return 0.0
    mx, my = sum(x) / n, sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = (sum((a - mx) ** 2 for a in x)) ** 0.5
    dy = (sum((b - my) ** 2 for b in y)) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


# ── 3. BB vs Pin收盘价 (CLV) ──

def analyze_clv():
    """合并实盘 Pin 收盘 CLV + OddsPortal 代理 CLV。"""
    clvs = []

    # 实盘 Pin 收盘 (clv_collector)
    if CLV_RESULTS.exists():
        try:
            for r in csv.DictReader(open(CLV_RESULTS)):
                try:
                    clv = float(r.get("true_clv_pct", 0) or 0)
                except (ValueError, TypeError):
                    clv = 0.0
                clvs.append({"src": "pin_close", "clv": clv,
                             "price_source": r.get("bb_price_source", ""),
                             "bb": float(r.get("bb_odds", 0) or 0),
                             "close": float(r.get("close_fair_price", 0) or 0)})
        except Exception:
            pass

    # OddsPortal 代理收盘 (clv_backfill)
    if CLV_BACKFILL.exists():
        try:
            for m in json.loads(CLV_BACKFILL.read_text()).get("samples", []):
                clvs.append({"src": "oddsportal", "clv": m.get("clv", 0),
                             "bb": m.get("bb_odds", 0), "close": m.get("close_odds", 0)})
        except Exception:
            pass

    if not clvs:
        return {"status": "insufficient", "n": 0,
                "message": "无收盘价样本 — 需 clv_collector 累积实盘 Pin 收盘价"}

    vals = [c["clv"] for c in clvs]
    pos = sum(1 for v in vals if v > 0)
    # 按 BB/FB 来源拆分 (仅 pin_close 有 bb_price_source)
    pin_srcs = [c.get("price_source", "") for c in clvs if c["src"] == "pin_close" and c.get("price_source")]
    return {
        "status": "ok", "n": len(clvs),
        "mean_clv": round(statistics.mean(vals), 3),
        "median_clv": round(statistics.median(vals), 3),
        "positive_pct": round(pos / len(vals) * 100, 1),
        "by_src": {s: {"n": sum(1 for c in clvs if c["src"] == s),
                       "mean_clv": round(statistics.mean([c["clv"] for c in clvs if c["src"] == s]), 3)}
                   for s in set(c["src"] for c in clvs)},
        "by_bb_source": {s: {"n": sum(1 for c in clvs if c.get("price_source") == s),
                             "mean_clv": round(statistics.mean([c["clv"] for c in clvs if c.get("price_source") == s]), 3)}
                         for s in set(pin_srcs)} if pin_srcs else {},
    }


# ── 汇总 ──

def run():
    roi = analyze_realized_roi()
    ev = analyze_ev_signal()
    clv = analyze_clv()
    report = {"roi": roi, "ev_signal": ev, "clv": clv}
    try:
        REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning("写报告失败: %s", e)
    return report


def _print(report):
    roi = report["roi"]
    ev = report["ev_signal"]
    clv = report["clv"]
    print("=" * 70)
    print("实盘 ROI 归因 (tracked_bets.json 已结算注)")
    print("=" * 70)
    if roi.get("status") != "ok":
        print("  无足够结算样本"); return
    print(f"  已结算 {roi['n_settled']} 笔 (非走盘 {roi['n_non_void']}): "
          f"赢 {roi['won']} / 输 {roi['lost']} / 走 {roi['void']}  胜率 {roi['win_rate']}%")
    print(f"  盈亏 {roi['total_profit']:+.2f} / 本金 {roi['total_stake']:.0f} → ROI {roi['roi_pct']:+.2f}%")
    if roi.get("p_value_roi_gt_0") is not None:
        sig = "✅ 显著" if roi["significant_95"] else "❌ 不显著(样本不足/噪声)"
        print(f"  ROI>0 显著性 p={roi['p_value_roi_gt_0']} {sig}")

    print("\n  按市场:")
    for k, v in roi.get("by_market", {}).items():
        print(f"    {k:<10} n={v['n']:<4} ROI {v['roi_pct']:+.2f}%  (盈亏 {v['profit']:+.0f}/{v['stake']:.0f})")
    print("\n  按 EV 分桶:")
    for k, v in roi.get("by_ev", {}).items():
        print(f"    {k:<8} n={v['n']:<4} ROI {v['roi_pct']:+.2f}%")
    print("\n  按赔率分桶:")
    for k, v in roi.get("by_odds", {}).items():
        print(f"    {k:<10} n={v['n']:<4} ROI {v['roi_pct']:+.2f}%")

    print("\n" + "=" * 70)
    print("EV 信号验证 (推送时 EV 是否预测真实 ROI)")
    print("=" * 70)
    if ev.get("status") != "ok":
        print("  样本不足");
    else:
        print(f"  corr(EV, 单笔ROI) = {ev['corr_ev_roi']}  (越正 = EV 越有效)")
        for k, v in ev.get("by_ev", {}).items():
            print(f"    {k:<8} n={v['n']:<4} ROI {v['roi_pct']:+.2f}%")

    print("\n" + "=" * 70)
    print("BB vs Pin收盘价 (CLV)")
    print("=" * 70)
    if clv.get("status") != "ok":
        print(f"  ⚠️ {clv.get('message','无数据')}")
    else:
        print(f"  {clv['n']} 样本: 均值 CLV {clv['mean_clv']:+.2f}%  正CLV率 {clv['positive_pct']}%")
        for s, v in clv.get("by_src", {}).items():
            print(f"    {s:<12} n={v['n']:<4} 均值CLV {v['mean_clv']:+.2f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()
    rep = run()
    _print(rep)
    if args.detail:
        print("\n完整报告 →", REPORT_FILE)
