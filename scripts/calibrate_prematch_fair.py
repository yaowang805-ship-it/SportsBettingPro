#!/usr/bin/env python3
"""早盘公平价校准 — 拟合平滑 platt 校准曲线(逻辑校准), 存 fair_calibration_prematch.json。

问题: 早盘 Pin 公平价(去水后)同样系统性高估热门 ~+7pp, 与滚球同病。
数据: real_bets.json 早盘实盘(trigger_source in pin_drop/ws_scan)训练。
2026-11-02 改: 之前 isotonic 逐段拟合在早盘小样本上过拟合(样本外 -10.54%), 换成
platt(logit 空间线性, 2参数)平滑单调, 不会过拟合。
用法: python3 scripts/calibrate_prematch_fair.py [--save]
"""
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "data" / "storage"

EPS = 1e-6

# 2026-11-07: 校准只用干净数据(11-05 后), 剔除 Shin 时代+线 bug 脏数据
CLEAN_DATA_CUTOFF = 1794009600  # 2026-11-07 00:00 UTC


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


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
        fair = b.get("fair") or b.get("fair_price")  # 2026-11-09 优先原始 fair(real_bets 字段), paper_bets 回退 fair_price
        bb = b.get("bb_odds")
        if not fair or not bb:
            continue
        out.append((1.0 / fair, 1.0 if b["result"] == "won" else 0.0,
                    bb, b.get("stake") or 0, b.get("profit") or 0))
    return out


def _backtest(rows, predict_fn, name):
    evr, evc, res = [], [], []
    for p_imp, won, bb, stk, prof in rows:
        p_cal = max(float(predict_fn(np.array([p_imp]))[0]), 0.01)
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
    rb = _load(STORAGE / "real_bets.json")
    if isinstance(rb, dict):
        rb = list(rb.values())
    # 2026-11-07: 干净数据(11-05 后) + 补早盘观察库 paper_bets(实盘干净样本只有个位数, 观察库同逻辑更全)
    early_real = [b for b in rb if isinstance(b, dict) and b.get("trigger_source") in ("pin_drop", "ws_scan")
                  and (b.get("ts") or 0) >= CLEAN_DATA_CUTOFF]
    paper = _load(STORAGE / "paper_bets.json")
    if isinstance(paper, dict):
        paper = list(paper.values())
    def _settled_ts(b):
        v = b.get("settled_at")
        if not v:
            return 0.0
        try:
            from datetime import datetime
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0.0

    early_paper = [b for b in paper if isinstance(b, dict)
                   and b.get("sub_market") not in ("ht", "ht_hc", "ht_ou")  # ht 半场无比分源, 排除
                   and b.get("result") in ("won", "lost")
                   and _settled_ts(b) >= CLEAN_DATA_CUTOFF]
    all_bets = early_real + early_paper

    def _fit(rows):
        if len(rows) < 50:
            return None
        p = np.array([r[0] for r in rows])
        y = np.array([r[1] for r in rows])
        lr = LogisticRegression(C=1e6, max_iter=10000)
        lr.fit(logit(p).reshape(-1, 1), y)
        a = float(lr.intercept_[0])
        b = float(lr.coef_[0][0])
        _pred = lambda x: sigmoid(a + b * logit(x))
        bins = np.linspace(0.05, 0.95, 37)
        table = [{"p_implied": round(float(x), 4), "p_cal": round(float(_pred(np.array([x]))[0]), 4)} for x in bins]
        return {"n": len(rows), "a": round(a, 4), "b": round(b, 4), "table": table}

    # 2026-11-09 分盘口校准(agent2 S3): 全局单曲线掩盖 hc(-81%) vs 1x2(-30%) 的 bias 差, 按盘口分表拟合。
    from collections import defaultdict
    by_sm = defaultdict(list)
    for _b in all_bets:
        _sm = (_b.get("sub_market") or "") if isinstance(_b, dict) else ""
        if _sm:
            by_sm[_sm].append(_b)
    tables = {}
    _t_all = _fit(_rows(all_bets))
    if _t_all:
        tables["all"] = _t_all
        print(f"[all] n={_t_all['n']} a={_t_all['a']:+.3f} b={_t_all['b']:.3f}")
    for sm in sorted(by_sm):
        _t = _fit(_rows(by_sm[sm]))
        if _t:
            tables[sm] = _t
            print(f"[{sm}] n={_t['n']} a={_t['a']:+.3f} b={_t['b']:.3f}")

    if save:
        out = {"model": "platt_prematch", "tables": tables}
        dest = STORAGE / "fair_calibration_prematch.json"
        json.dump(out, open(dest, "w"), ensure_ascii=False, indent=1)
        print(f"\n已保存早盘校准表(分盘口 platt, {len(tables)} 表) -> {dest}")


if __name__ == "__main__":
    main()
