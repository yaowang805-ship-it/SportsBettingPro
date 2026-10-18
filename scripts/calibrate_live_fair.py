#!/usr/bin/env python3
"""校准 Pin 滚球公平价: 把「下注时公平价」映射到「真实胜率」。

问题: 滚球下注时 Pin 公平价系统性高估热门(隐含胜率 > 实际胜率), 导致
      ev=(bb-fair)/fair 虚高 ~10pp, 投的是假溢价。

方法: 用历史已结算注(fair + 实际输赢)拟合校准曲线, 得到收缩系数,
      把 p_implied=1/fair 校准成 p_cal, 再回代 fair_cal=1/p_cal。

模型:
  1. raw            无校准 p_hat = p_implied (基准)
  2. platt          logit(p_hat) = a + b*logit(p_implied)  (逻辑校准)
  3. isotonic       保序回归(非参数, 单调)
  4. shrink_05_prob p_hat = alpha*p_implied + (1-alpha)*0.5  (概率空间收缩到 0.5)
  5. shrink_05_logit logit(p_hat) = gamma*logit(p_implied)  (log-odds 空间收缩)

评估: Brier score 与 log-loss, 越低越好(比 raw 改善越多 = 校准越有效)。

用法: python3 scripts/calibrate_live_fair.py [--all 包含观察库] [--save]
"""
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "data" / "storage"

EPS = 1e-6


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def brier(p_hat, y):
    return float(np.mean((p_hat - y) ** 2))


def logloss(p_hat, y):
    p_hat = np.clip(p_hat, EPS, 1 - EPS)
    return float(-np.mean(y * np.log(p_hat) + (1 - y) * np.log(1 - p_hat)))


def load_bets(include_paper=False):
    """返回 [(p_implied, won), ...], p_implied=1/fair, won∈{0,1}."""
    real = json.load(open(STORAGE / "real_bets.json"))
    real = list(real.get("bets", real).values())
    rows = []
    for b in real:
        if b.get("result") in ("won", "lost") and b.get("fair"):
            rows.append((1.0 / b["fair"], 1.0 if b["result"] == "won" else 0.0))
    if include_paper:
        paper = json.load(open(STORAGE / "live_paper_bets.json"))
        for b in paper:
            if b.get("result") in ("won", "lost") and b.get("fair"):
                rows.append((1.0 / b["fair"], 1.0 if b["result"] == "won" else 0.0))
    return rows


def calibration_curve(p, y, edges):
    """按 p_implied 分桶, 输出 (bin_label, n, implied_avg, actual_winrate)."""
    out = []
    for lo, hi, lbl in edges:
        m = (p >= lo) & (p < hi)
        if m.sum() == 0:
            continue
        out.append((lbl, int(m.sum()), float(p[m].mean()), float(y[m].mean())))
    return out


def fit_models(p, y):
    """拟合各模型, 返回 {name: (predict_fn, coeff_str)}."""
    models = {}

    models["raw"] = (lambda x: x, "无")

    # platt (逻辑校准)
    lr = LogisticRegression(C=1e6, max_iter=10000)
    X = logit(p).reshape(-1, 1)
    lr.fit(X, y)
    a = lr.intercept_[0]
    b = lr.coef_[0][0]
    models["platt"] = (lambda x, a=a, b=b: sigmoid(a + b * logit(x)), f"a={a:+.3f} b={b:.3f}")

    # isotonic
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(p, y)
    models["isotonic"] = (lambda x, iso=iso: iso.predict(x), "非参数")

    # shrink to 0.5 (概率空间), 闭式解 alpha
    x = p - 0.5
    yc = y - 0.5
    alpha = float(np.sum(x * yc) / np.sum(x * x)) if np.sum(x * x) > 0 else 1.0
    models["shrink05_prob"] = (
        lambda x, alpha=alpha: alpha * x + (1 - alpha) * 0.5,
        f"alpha={alpha:+.3f}",
    )

    # shrink to 0.5 (log-odds 空间), gamma
    z = logit(p)
    # 闭式: 在 log-odds 空间, y 与 z 的回归斜率 (穿过原点, 但 y 是 0/1 用 sigmoid 更对)
    # 用 scipy 数值最小化 brier
    from scipy.optimize import minimize_scalar

    def obj(g):
        return brier(sigmoid(g * z), y)

    res = minimize_scalar(obj, bounds=(-1, 3), method="bounded")
    gamma = res.x
    models["shrink05_logit"] = (
        lambda x, gamma=gamma: sigmoid(gamma * logit(x)),
        f"gamma={gamma:+.3f}",
    )

    return models


def main():
    include_paper = "--all" in sys.argv
    save = "--save" in sys.argv
    rows = load_bets(include_paper=include_paper)
    p = np.array([r[0] for r in rows])
    y = np.array([r[1] for r in rows])
    print(f"样本: n={len(p)}  (实盘 838{' + 观察库' if include_paper else ''})")
    print(f"  实际胜率={100*y.mean():.1f}%   p_implied均值={100*p.mean():.1f}%  "
          f"(偏差 {100*(p.mean()-y.mean()):+.1f}pp)\n")

    # 校准曲线
    edges = [(0, 0.3, "<0.3 大冷"), (0.3, 0.4, "0.3-0.4"),
             (0.4, 0.5, "0.4-0.5 五五开"), (0.5, 0.6, "0.5-0.6 微热门"),
             (0.6, 0.7, "0.6-0.7 热"), (0.7, 0.8, "0.7-0.8 大热"),
             (0.8, 1.01, ">0.8")]
    print("=== 校准曲线 (p_implied 分桶) ===")
    print(f"{'桶':16s} {'n':>5s} {'隐含胜率':>8s} {'实际胜率':>8s} {'偏差':>7s}")
    for lbl, n, imp, act in calibration_curve(p, y, edges):
        print(f"{lbl:16s} {n:5d} {100*imp:7.1f}% {100*act:7.1f}% {100*(imp-act):+6.1f}pp")

    # 拟合各模型
    models = fit_models(p, y)
    print("\n=== 模型拟合 (越低越好) ===")
    print(f"{'模型':18s} {'Brier':>7s} {'LogLoss':>8s} {'系数':>20s}")
    best = (1e9, None)
    for name, (fn, coeff) in models.items():
        p_hat = fn(p)
        br = brier(p_hat, y)
        ll = logloss(p_hat, y)
        print(f"{name:18s} {br:7.4f} {ll:8.4f}  {coeff:>20s}")
        if br < best[0]:
            best = (br, name)
    print(f"\n最佳(按Brier): {best[1]}")

    # 热门档专属收缩系数 (p_implied ∈ 0.5-0.7)
    m = (p >= 0.5) & (p < 0.7)
    if m.sum():
        pp, yy = p[m], y[m]
        x = pp - 0.5
        alpha_fav = float(np.sum(x * (yy - 0.5)) / np.sum(x * x))
        print(f"\n热门档(p 0.5-0.7, n={m.sum()}) 概率空间收缩系数 alpha={alpha_fav:+.3f}")
        print(f"  含义: p_cal = {alpha_fav:+.3f}*p_implied + {1-alpha_fav:+.3f}*0.5")

    # 回代示例: 校准前后 ev 对比 (bb 取中位 1.79)
    bb = 1.79
    print("\n=== 回代示例 (bb=1.79 固定) ===")
    for lbl, lo, hi in [("微热门", 0.5, 0.6), ("热", 0.6, 0.7)]:
        mm = (p >= lo) & (p < hi)
        if not mm.sum():
            continue
        p_imp = p[mm].mean()
        fair_raw = 1 / p_imp
        p_cal = models["isotonic"][0](np.array([p_imp]))[0]
        fair_cal = 1 / p_cal
        ev_raw = (bb - fair_raw) / fair_raw * 100
        ev_cal = (bb - fair_cal) / fair_cal * 100
        print(f"  {lbl}: p_implied={100*p_imp:.1f}%  ev_raw={ev_raw:+.1f}%  "
              f"p_cal={100*p_cal:.1f}%  ev_cal={ev_cal:+.1f}%")

    if save:
        # 保存 isotonic 查找表 (bin 中心 -> p_cal)
        iso_fn = models["isotonic"][0]
        bins = np.linspace(0.05, 0.95, 37)
        table = [{"p_implied": round(float(b), 4), "p_cal": round(float(iso_fn(np.array([b]))[0]), 4)} for b in bins]
        out = {"model": "isotonic", "n": len(p), "table": table}
        dest = STORAGE / "fair_calibration.json"
        json.dump(out, open(dest, "w"), ensure_ascii=False, indent=1)
        print(f"\n已保存校准表 -> {dest}")


if __name__ == "__main__":
    main()
