#!/usr/bin/env python3
"""Power 去水分段经验校准 + fair_cal ≥ raw 验证（替代旧的 platt 拟合）。

背景(2026-11-13): Power 去水后仍残留 favorite-longshot bias(devig_bias_check 显示
隐含 vs 实际 +5.8~12pp)。旧 platt(logit 线性) 的 b<1 收缩方向与 FLB 修正方向冲突
(收缩=抬冷门, 而 FLB 需压冷门), 已被关(CALIBRATE_FAIR=False)。

本脚本用「分桶经验校准」: 直接按隐含概率分桶, 每桶 p_cal = 该桶实际胜率, 再单调约束
+ 滑动平滑。方向天然正确(冷门压低、热门抬升)。并逐注验证校准后公平价 fair_cal ≥
Pin 原始价(pin_odds), 杜绝「公平价跌破原价」的低级错误。

数据: 11-08 后干净数据(real_bets + paper_bets), 分盘口(1x2/hc/ou), 每盘口 n≥50 才拟合。
用法: .venv312/bin/python scripts/refit_power_devig.py [--save] [--market prematch|live]
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "data" / "storage"

EPS = 1e-6
# 干净数据起点: 2026-11-08 00:00 +08:00 (之前是 Shin 时代 + 线 bug 脏数据)
CLEAN_CUTOFF = datetime.fromisoformat("2026-11-08T00:00:00+08:00").timestamp()
MIN_N = 50       # 每盘口最少样本才拟合
MIN_BUCKET = 20  # 每桶最少样本, 不足的桶并入相邻桶


def _load(p):
    d = json.load(open(p))
    return d.get("bets", d) if isinstance(d, dict) else d


def _aslist(x):
    return list(x.values()) if isinstance(x, dict) else x


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _settled_ts(b):
    v = b.get("settled_at")
    if not v:
        return 0.0
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def load_clean(market="prematch"):
    """加载 11-08 后干净已结算注。market=prematch → 早盘(实盘+观察库); live → 滚球(实盘)。"""
    rows = []  # (p_imp, won, fair, raw, sub)
    rb = _aslist(_load(STORAGE / "real_bets.json"))
    if market == "prematch":
        real = [b for b in rb if isinstance(b, dict)
                and b.get("trigger_source") in ("pin_drop", "ws_scan")
                and (b.get("ts") or 0) >= CLEAN_CUTOFF
                and b.get("result") in ("won", "lost")]
        # 观察库 paper_bets 同逻辑(更全, 选择偏差小)
        paper = _aslist(_load(STORAGE / "paper_bets.json"))
        paper = [b for b in paper if isinstance(b, dict)
                 and b.get("sub_market") not in ("ht", "ht_hc", "ht_ou")
                 and b.get("result") in ("won", "lost")
                 and _settled_ts(b) >= CLEAN_CUTOFF]
        bets = real + paper
    else:
        bets = [b for b in rb if isinstance(b, dict)
                and b.get("trigger_source") not in ("pin_drop", "ws_scan")
                and (b.get("ts") or 0) >= CLEAN_CUTOFF
                and b.get("result") in ("won", "lost")]
    for b in bets:
        fair = _f(b.get("fair") or b.get("fair_price"))
        if not fair or fair <= 1.0:
            continue
        raw = _f(b.get("pin_odds"))  # real_bets 有; paper_bets 无(home_pin/away_pin 需按方向取, 先不用)
        sub = b.get("sub") or b.get("sub_market") or "?"
        rows.append((1.0 / fair, 1.0 if b.get("result") == "won" else 0.0, fair, raw, sub))
    return rows


def empirical_calibrate(rows, n_bins=8):
    """分桶经验校准: 每桶实际胜率 + 单调约束 + 滑动平滑。返回 table(list of dict) 或 None。"""
    rows = sorted(rows, key=lambda r: r[0])
    p = np.array([r[0] for r in rows])
    y = np.array([r[1] for r in rows])
    # 分位数分桶(保证每桶样本均衡)
    edges = list(np.percentile(p, np.linspace(0, 100, n_bins + 1)[1:-1]))
    centers, rates = [], []
    lo = 0.0
    for e in edges + [1.0 + 1e-9]:
        mask = (p >= lo) & (p < e)
        if mask.sum() >= MIN_BUCKET:
            centers.append(float(p[mask].mean()))
            rates.append(float(y[mask].mean()))
        lo = e
    if len(centers) < 3:
        return None
    centers = np.array(centers)
    rates = np.array(rates)
    # 单调约束(PAV 上修): 校准曲线必须单调不减(否则低概率段反而更高=方向错)
    for i in range(1, len(rates)):
        if rates[i] < rates[i - 1]:
            rates[i] = rates[i - 1]
    # 滑动平均(窗口3) 平滑单桶噪声
    sm = rates.copy()
    for i in range(1, len(rates) - 1):
        sm[i] = (rates[i - 1] + rates[i] + rates[i + 1]) / 3.0
    return [{"p_implied": round(float(c), 4), "p_cal": round(float(r), 4)}
            for c, r in zip(centers, sm)]


def _p_cal_at(table, p_imp):
    """线性插值查 table(与 fair_calibration._load_table 消费端一致)。"""
    xs = [t["p_implied"] for t in table]
    ys = [t["p_cal"] for t in table]
    if p_imp <= xs[0]:
        return ys[0]
    if p_imp >= xs[-1]:
        return ys[-1]
    import bisect
    i = bisect.bisect_right(xs, p_imp) - 1
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    return y0 + (y1 - y0) * (p_imp - x0) / (x1 - x0)


def verify_fair_ge_raw(rows, table):
    """逐注验证校准后公平价 ≥ Pin 原价。返回 (可验证注数, 跌破注数, 最大跌破幅度%)。"""
    n_check = 0
    n_violate = 0
    max_drop = 0.0
    for p_imp, won, fair, raw, sub in rows:
        if not raw or raw <= 1.0:
            continue
        n_check += 1
        p_cal = _p_cal_at(table, p_imp)
        fair_cal = 1.0 / max(p_cal, 0.01)
        if fair_cal < raw - 0.005:
            n_violate += 1
            max_drop = max(max_drop, (raw - fair_cal) / raw * 100.0)
    return n_check, n_violate, max_drop


def main():
    market = "live" if "--market" in sys.argv and sys.argv[sys.argv.index("--market") + 1] == "live" else "prematch"
    save = "--save" in sys.argv
    rows = load_clean(market)
    print(f"[{market}] 干净样本(11-08后): {len(rows)} 注")

    from collections import defaultdict
    by_sm = defaultdict(list)
    for r in rows:
        by_sm[r[4]].append(r)

    tables = {}
    for sm in sorted(by_sm):
        rs = by_sm[sm]
        if len(rs) < MIN_N:
            print(f"[{sm}] n={len(rs)} < {MIN_N}, 跳过(样本不足)")
            continue
        table = empirical_calibrate(rs, n_bins=max(3, min(8, len(rs) // 20)))
        if not table:
            print(f"[{sm}] n={len(rs)} 分桶后不足, 跳过")
            continue
        n_chk, n_viol, max_drop = verify_fair_ge_raw(rs, table)
        # 分桶方向诊断: 打印每桶 隐含→校准
        print(f"\n[{sm}] n={len(rs)} (可验证 raw 的 {n_chk} 注)")
        for t in table:
            imp, cal = t["p_implied"], t["p_cal"]
            arrow = "↑抬" if cal > imp + 0.005 else ("↓压" if cal < imp - 0.005 else "=")
            print(f"  隐含 {imp*100:4.0f}% → 校准 {cal*100:5.1f}% ({arrow} {(cal-imp)*100:+.1f}pp)")
        if n_viol:
            print(f"  ⚠️ fair_cal < raw: {n_viol}/{n_chk} 注, 最大跌破 {max_drop:.1f}% (护栏会 clamp)")
        else:
            print(f"  ✅ fair_cal ≥ raw 全部通过({n_chk} 注)")
        tables[sm] = table

    if save and tables:
        dest = STORAGE / ({"prematch": "fair_calibration_prematch.json",
                           "live": "fair_calibration.json"}[market])
        out = {"model": "empirical_bucket", "tables": tables}
        json.dump(out, open(dest, "w"), ensure_ascii=False, indent=1)
        print(f"\n已保存 {market} 校准表(分桶经验, {len(tables)} 盘口) -> {dest}")
    elif not tables:
        print("\n无盘口样本足够, 不产出表(保持 CALIBRATE_FAIR=False 现状)")


if __name__ == "__main__":
    main()
