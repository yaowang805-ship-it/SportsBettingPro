#!/usr/bin/env python3
"""置换检验 — 验证「正 CLV 是否显著, 而非选择规则/运气造出来的」。

原理: 对每个已下注样本, 随机翻转下注侧(反侧 CLV 近似取负, 2-way 盘严格成立), 得到
「随机选侧」的 CLV 均值分布。真实 CLV 均值落在分布右尾之外(p 小) = 我们的选择有真实 edge;
落在分布中间(p 大) = 正 CLV 是噪声/选择规则造出来的, 不可采信。

输入: clv_results.csv 的 bb_clv_pct(BB 同源 CLV: BB 下注价 vs BB 收盘价, 不依赖去水方法)。
用法: .venv312/bin/python scripts/permutation_test_clv.py
"""
import csv
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CLV_CSV = ROOT / "data" / "storage" / "clv_results.csv"

N_PERM = 5000


def _load_clv():
    vals = []
    with open(CLV_CSV, newline="") as f:
        for r in csv.DictReader(f):
            v = r.get("bb_clv_pct")
            if v in ("", None):
                continue
            try:
                vals.append(float(v))
            except (ValueError, TypeError):
                continue
    return vals


def main():
    vals = _load_clv()
    if len(vals) < 30:
        print(f"样本 {len(vals)} < 30, 不足做置换检验, 继续积累")
        return
    vals = np.array(vals)
    real_mean = vals.mean()
    n = len(vals)

    rng = random.Random(20261117)
    perm_means = []
    for _ in range(N_PERM):
        # 随机翻转符号(模拟「随机选侧」, 反侧 CLV ≈ -原侧 CLV)
        signs = rng.choices([1.0, -1.0], k=n)
        perm_means.append((vals * signs).mean())
    perm_means = np.array(perm_means)

    # p 值: 置换分布里 >= 真实均值的比例(单尾)
    p_upper = (perm_means >= real_mean).mean()
    p_lower = (perm_means <= real_mean).mean()

    print(f"CLV 样本 n={n}")
    print(f"真实 CLV 均值 = {real_mean:+.3f}%")
    print(f"置换分布(随机选侧)均值 = {perm_means.mean():+.3f}% ± {perm_means.std():.3f}%")
    print(f"单尾 p(真实 ≥ 随机) = {p_upper:.4f}")
    print(f"单尾 p(真实 ≤ 随机) = {p_lower:.4f}")

    if real_mean > 0 and p_upper < 0.05:
        print("\n✅ 正 CLV 显著(真实均值显著高于随机选侧), 选择有真实 edge")
    elif real_mean < 0 and p_lower < 0.05:
        print("\n⚠️ CLV 显著为负(选侧系统性反了), 需排查选择方向")
    else:
        print("\n⚠️ CLV 不显著(和随机选侧无差别), 正 CLV 是噪声/选择规则造出来的, 不可采信")


if __name__ == "__main__":
    main()
