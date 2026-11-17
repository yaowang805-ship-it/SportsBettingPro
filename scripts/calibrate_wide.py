#!/usr/bin/env python3
"""宽样本分桶校准 + 时间外验证 — 从全市场 BB∩Pin 比赛(无选择)估校准表并验证。

与 refit_power_devig.py 的区别:
  - refit 用 real_bets+paper_bets(选择样本), 分不清「去水 bias」vs「选择 bias」;
  - 本脚本用 calibration_wide.csv(全市场, 无 ev/drop 筛选), 校准方向不会被选择动作污染。
  - 不加 PAV 强单调约束(让数据说话, 现代固定赔率可能是反向 tilt);
  - 时间外切分: 按 match_epoch 前 70% 估表, 后 30% 评估, 杜绝 in-sample 乐观;
  - 报聚类有效样本量(比赛数, 非方向数 —— 同场 home/draw/away 不独立)。

用法:
    .venv312/bin/python scripts/calibrate_wide.py                 # 预览(不写表)
    .venv312/bin/python scripts/calibrate_wide.py --save          # 通过验证则写 fair_calibration_prematch.json
    .venv312/bin/python scripts/calibrate_wide.py --method p_shin # 换去水方法(默认 p_power)

通过标准(测试集, 时间外): 校准后 ECE 和 log-loss 都比校准前低(校准确实更准, 且非 in-sample 乐观)。
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STORAGE = ROOT / "data" / "storage"
WIDE_CSV = STORAGE / "calibration_wide.csv"
OUT_JSON = STORAGE / "fair_calibration_prematch.json"

MIN_BUCKET = 20   # 每桶最少样本
N_BINS = 8        # 分桶数
SPLIT = 0.7       # 时间外切分比例(前70%估表)
MIN_TRAIN_N = 100  # 训练集最少方向级样本才估表(低于此提示继续积累)


def _load_settled(method):
    rows = []
    with open(WIDE_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("y") in ("", None):
                continue
            p = r.get(method)
            if p in ("", None):
                continue
            rows.append({
                "p": float(p), "y": float(r["y"]),
                "mid": r["bb_match_id"], "epoch": float(r.get("match_epoch") or 0),
            })
    return rows


def _ece(ps, ys):
    """分桶 ECE(按预测概率分位分桶)。返回 ece + 桶明细。"""
    order = np.argsort(ps)
    ps, ys = np.array(ps)[order], np.array(ys)[order]
    n = len(ps)
    ece = 0.0
    for i in range(N_BINS):
        lo = int(n * i / N_BINS)
        hi = int(n * (i + 1) / N_BINS)
        if hi <= lo:
            continue
        bp, by = ps[lo:hi].mean(), ys[lo:hi].mean()
        ece += (hi - lo) / n * abs(bp - by)
    return ece


def _logloss(ps, ys):
    ps = np.clip(np.array(ps), 1e-9, 1 - 1e-9)
    ys = np.array(ys)
    return float(-(ys * np.log(ps) + (1 - ys) * np.log(1 - ps)).mean())


def _empirical_table(rows):
    """分位数分桶经验校准: 每桶 p_cal = 实际胜率, 无单调约束, 滑动平均降噪。"""
    ps = np.array([r["p"] for r in rows])
    ys = np.array([r["y"] for r in rows])
    edges = list(np.percentile(ps, np.linspace(0, 100, N_BINS + 1)[1:-1]))
    centers, rates = [], []
    lo = 0.0
    for e in edges + [1.0 + 1e-9]:
        mask = (ps >= lo) & (ps < e)
        if mask.sum() >= MIN_BUCKET:
            centers.append(float(ps[mask].mean()))
            rates.append(float(ys[mask].mean()))
        lo = e
    if len(centers) < 3:
        return None
    # 滑动平均(窗口3)降噪, 不加强单调约束(让数据展示真实方向)
    sm = rates[:]
    for i in range(1, len(rates) - 1):
        sm[i] = (rates[i - 1] + rates[i] + rates[i + 1]) / 3.0
    return [(c, r) for c, r in zip(centers, sm)]


def _apply_table(table, p):
    xs = [t[0] for t in table]
    ys = [t[1] for t in table]
    if p <= xs[0]:
        return ys[0]
    if p >= xs[-1]:
        return ys[-1]
    import bisect
    i = bisect.bisect_right(xs, p) - 1
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    return y0 + (y1 - y0) * (p - x0) / (x1 - x0)


def main():
    method = "p_power"
    if "--method" in sys.argv:
        method = sys.argv[sys.argv.index("--method") + 1]
    save = "--save" in sys.argv

    rows = _load_settled(method)
    if not rows:
        print(f"无已结算样本(方法 {method})。等 calibration_wide 赛果积累。")
        return
    rows.sort(key=lambda r: r["epoch"] or 0)
    n_train = int(len(rows) * SPLIT)
    train, test = rows[:n_train], rows[n_train:]

    print(f"宽样本已结算(方向级) {len(rows)} 行 | 比赛数 {len({r['mid'] for r in rows})}")
    print(f"时间外切分: 训练 {len(train)} 行 ({len({r['mid'] for r in train})} 场) | 测试 {len(test)} 行 ({len({r['mid'] for r in test})} 场)")
    if len(train) < MIN_TRAIN_N:
        print(f"训练集 {len(train)} < {MIN_TRAIN_N}, 样本不足, 继续积累(暂不估表)")
        return

    table = _empirical_table(train)
    if not table:
        print("分桶后桶数不足, 样本不足, 继续积累")
        return

    print(f"\n分桶校准表(隐含概率 → 校准概率, 训练集 {len(train)} 行):")
    for imp, cal in table:
        arrow = "↑抬" if cal > imp + 0.005 else ("↓压" if cal < imp - 0.005 else "=")
        print(f"  隐含 {imp*100:4.0f}% → 校准 {cal*100:5.1f}% ({arrow} {(cal-imp)*100:+.1f}pp)")

    # 校准前/后 ECE + log-loss
    tp = [r["p"] for r in test]; ty = [r["y"] for r in test]
    trp = [r["p"] for r in train]; try_ = [r["y"] for r in train]
    tc_p = [r["p"] for r in test]
    tc_cal = [_apply_table(table, p) for p in tc_p]
    tr_cal = [_apply_table(table, p) for p in trp]

    print(f"\n验证(校准前 p vs 校准后 p_cal):")
    print(f"  训练集: log-loss {_logloss(trp, try_):.4f} → {_logloss(tr_cal, try_):.4f} | ECE {_ece(trp, try_)*100:.2f}% → {_ece(tr_cal, try_)*100:.2f}%")
    print(f"  测试集: log-loss {_logloss(tp, ty):.4f} → {_logloss(tc_cal, ty):.4f} | ECE {_ece(tp, ty)*100:.2f}% → {_ece(tc_cal, ty)*100:.2f}%")

    # 通过标准: 测试集(时间外)校准后 ECE 和 log-loss 都更低
    ece_before, ece_after = _ece(tp, ty), _ece(tc_cal, ty)
    ll_before, ll_after = _logloss(tp, ty), _logloss(tc_cal, ty)
    passed = ece_after < ece_before and ll_after < ll_before

    if passed:
        print(f"\n✅ 时间外验证通过(测试集 ECE {ece_before*100:.2f}%→{ece_after*100:.2f}%, log-loss {ll_before:.4f}→{ll_after:.4f})")
    else:
        print(f"\n⚠️ 时间外验证未通过(测试集校准未显著改善), 不产出表")

    if save and passed:
        out = {"model": "empirical_bucket_wide", "tables": {"1x2": {"table": [
            {"p_implied": round(c, 4), "p_cal": round(r, 4)} for c, r in table]}}}
        json.dump(out, open(OUT_JSON, "w"), ensure_ascii=False, indent=1)
        print(f"已保存 1x2 宽样本校准表 -> {OUT_JSON}")
    elif save:
        print("未保存(验证未通过)")


if __name__ == "__main__":
    main()
