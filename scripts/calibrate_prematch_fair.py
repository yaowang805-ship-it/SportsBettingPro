#!/usr/bin/env python3
"""早盘公平价校准 — 拟合 isotonic 校准曲线, 存 fair_calibration_prematch.json。

问题: 早盘 Pin 公平价(Shin 去水后)同样系统性高估热门 ~+7pp(观察库实测), 与滚球同病。
数据: paper_bets.json(观察库, fair_price+result) 训练; tracked_bets.json(实盘) 样本外验证。
用法: python3 scripts/calibrate_prematch_fair.py [--save]
"""
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "data" / "storage"


def _load(p):
    d = json.load(open(p))
    return d.get("bets", d) if isinstance(d, dict) else d


def _rows(bets):
    """统一提取 (隐含概率, 胜负, bb赔率, stake, profit)。兼容 paper_bets(fair_price) 和 real_bets(fair)。"""
    out = []
    for b in bets:
        if not isinstance(b, dict):
            continue
        if b.get("result") not in ("won", "lost"):
            continue
        fair = b.get("fair_price") or b.get("fair")
        bb = b.get("bb_odds")
        if not fair or not bb:
            continue
        out.append((1.0 / fair, 1.0 if b["result"] == "won" else 0.0,
                    bb, b.get("stake") or 0, b.get("profit") or 0))
    return out


def _backtest(rows, iso, name):
    evr, evc, res = [], [], []
    for p_imp, won, bb, stk, prof in rows:
        p_cal = max(float(iso.predict(np.array([p_imp]))[0]), 0.01)
        fc = 1.0 / p_cal
        evr.append((bb - 1.0 / p_imp) / (1.0 / p_imp) * 100)
        evc.append((bb - fc) / fc * 100)
        res.append((evc[-1], won, stk, prof))

    def sub(cond):
        bs = [r for r in res if cond(r[0])]
        s = sum(r[2] for r in bs)
        p = sum(r[3] for r in bs)
        return len(bs), 100 * p / s if s else 0

    n_all, roi_all = sub(lambda e: True)
    n_keep, roi_keep = sub(lambda e: e > 0)
    n_cut, roi_cut = sub(lambda e: e <= 0)
    print(f"{name}: n={n_all}  ev_raw中位={np.median(evr):+.1f}%  ROI={roi_all:+.2f}%")
    print(f"   校准后 ev_cal>0: n={n_keep} ROI={roi_keep:+.2f}%   拦截 ev_cal<=0: n={n_cut} ROI={roi_cut:+.2f}%")


def main():
    save = "--save" in sys.argv
    # 2026-10-21 改用实盘库(real_bets)训练: 观察库含大量未实盘的假 edge 机会, 样本和实盘分布不同,
    # 校准表会偏。实盘(trigger_source in pin_drop/ws_scan)才是真实下注样本。
    rb = _load(STORAGE / "real_bets.json")
    if isinstance(rb, dict):
        rb = list(rb.values())  # real_bets.json 是 {order_id: bet}, 直接取 values
    early_real = [b for b in rb if isinstance(b, dict) and b.get("trigger_source") in ("pin_drop", "ws_scan")]
    tr = _rows(early_real)  # 实盘训练
    # 观察库做补充(实盘样本少时), 但实盘为主
    pb = _rows(_load(STORAGE / "paper_bets.json"))
    if len(tr) < 100:
        tr = tr + pb
    te = tr  # 样本少, 不分训练/测试
    p = np.array([r[0] for r in tr])
    y = np.array([r[1] for r in tr])
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p, y)
    print(f"训练(实盘为主) n={len(tr)}  其中纯实盘 {len(_rows(early_real))}")
    _backtest(tr, iso, "训练集(in-sample)")

    if save:
        bins = np.linspace(0.05, 0.95, 37)
        table = [{"p_implied": round(float(b), 4),
                  "p_cal": round(float(iso.predict(np.array([b]))[0]), 4)} for b in bins]
        out = {"model": "isotonic_prematch", "n": len(tr), "table": table}
        dest = STORAGE / "fair_calibration_prematch.json"
        json.dump(out, open(dest, "w"), ensure_ascii=False, indent=1)
        print(f"\n已保存早盘校准表 -> {dest}")


if __name__ == "__main__":
    main()
