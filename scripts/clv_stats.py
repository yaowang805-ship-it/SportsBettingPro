#!/usr/bin/env python3
"""CLV 统计脚本 — 按 source(push/validate) × EV 区间分组统计中位CLV、正CLV率、样本数。

数据源:
  clv_results.csv: true_clv_pct(真实CLV=收盘时BB赔率相对收盘公平价), push_ev_pct(推送时EV)
  clv_tracking.csv: source(push/validate), ev_pct, home/away/sub_market/designation

口径:
  验模型 → source=validate 的 EV→CLV 曲线 (EV 多高才真的正 CLV)
  验过滤 → source=push 的 CLV (实际推送那批是否也正)

用法: python3 scripts/clv_stats.py
"""
import csv
import statistics
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
RESULTS_FILE = DATA / "clv_results.csv"
TRACKING_FILE = DATA / "clv_tracking.csv"

EV_BUCKETS = [
    ("2-3%", 2.0, 3.0),
    ("3-5%", 3.0, 5.0),
    ("5-8%", 5.0, 8.0),
    ("8%+", 8.0, float("inf")),
]


def _load_source_map():
    """从 tracking 建 (home, away, sub_market, designation) → source 映射。

    results.csv 旧版本无 source 字段, 需回查 tracking 补 source。push 优先。
    """
    src_map = {}
    if not TRACKING_FILE.exists():
        return src_map
    with open(TRACKING_FILE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            key = (r.get("home", "").strip(), r.get("away", "").strip(),
                   r.get("sub_market", "").strip(), r.get("designation", "").strip())
            src = r.get("source", "push")
            if key not in src_map or src == "push":
                src_map[key] = src
    return src_map


def _bucket(ev):
    for name, lo, hi in EV_BUCKETS:
        if lo <= ev < hi:
            return name
    return None


def main():
    if not RESULTS_FILE.exists():
        print("❌ 无 clv_results.csv，先等 CLV 采集器跑出数据")
        return

    src_map = _load_source_map()
    groups = defaultdict(list)  # (source, bucket) -> [clv]

    with open(RESULTS_FILE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                clv = float(r.get("true_clv_pct", 0))
                ev = float(r.get("push_ev_pct", 0))
            except (ValueError, TypeError):
                continue
            if ev < 2.0:  # 只统计 EV>=2% (比价门槛)
                continue
            src = (r.get("source") or "").strip()
            if not src:
                key = (r.get("home", "").strip(), r.get("away", "").strip(),
                       r.get("sub_market", "").strip(), r.get("designation", "").strip())
                src = src_map.get(key, "push")
            bucket = _bucket(ev)
            if bucket is None:
                continue
            groups[(src, bucket)].append(clv)

    print("=" * 72)
    print("CLV 统计报告 (按 source × EV 区间)")
    print(f"  clv_results.csv 有效样本: {sum(len(v) for v in groups.values())} 条")
    print("=" * 72)
    print()

    for src, label in (("validate", "验模型(validate: 全量 EV>2%)"),
                       ("push", "验过滤(push: 实际推送)")):
        print(f"### {label}")
        print(f"  {'EV区间':<8} {'样本':>6} {'中位CLV':>10} {'正CLV率':>9}")
        print("  " + "-" * 36)
        for bucket_name, lo, hi in EV_BUCKETS:
            clvs = groups.get((src, bucket_name), [])
            if not clvs:
                continue
            median = statistics.median(clvs)
            pos_rate = sum(1 for c in clvs if c > 0) / len(clvs) * 100
            print(f"  {bucket_name:<8} {len(clvs):>6} {median:>+9.1f}% {pos_rate:>8.0f}%")
        print()

    print("### 汇总")
    for src in ("validate", "push"):
        all_clvs = [c for b, _, _ in EV_BUCKETS for c in groups.get((src, b), [])]
        if all_clvs:
            median = statistics.median(all_clvs)
            pos_rate = sum(1 for c in all_clvs if c > 0) / len(all_clvs) * 100
            print(f"  {src:<10} n={len(all_clvs):>3}  中位CLV={median:+.2f}%  正CLV率={pos_rate:.0f}%")

    print()
    print("判定标准: 中位CLV > +1% 且 正CLV率 > 55% → 比价 edge 成立")


if __name__ == "__main__":
    main()
