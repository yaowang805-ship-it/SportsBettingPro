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
import time
from pathlib import Path
from collections import defaultdict

LOSS_ALERT_THRESHOLD = 25.0  # 采集丢失率超过这个百分比就告警

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


def compute_coverage():
    """采集覆盖率: 已开赛的 tracking 记录里, 有多少真的采到了收盘价。

    这个数以前根本没人算 —— 统计只看 clv_results.csv 有多少条, 看不见
    分母。实测丢失率一度 57%, 全程无告警。
    Returns: (采到, 应采, 丢失率%)
    """
    if not TRACKING_FILE.exists():
        return 0, 0, 0.0
    done = set()
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                done.add((r.get("home", "").strip(), r.get("away", "").strip(),
                          r.get("sub_market", "").strip(), r.get("designation", "").strip()))
    now = time.time()
    started, got, seen = 0, 0, set()
    with open(TRACKING_FILE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ep = int(r.get("match_epoch") or 0)
            if not ep or ep > now:
                continue  # 还没开赛, 不算分母
            k = (r.get("home", "").strip(), r.get("away", "").strip(),
                 r.get("sub_market", "").strip(), r.get("designation", "").strip())
            if k in seen:
                continue
            seen.add(k)
            started += 1
            if k in done:
                got += 1
    loss = (started - got) / started * 100 if started else 0.0
    return got, started, loss


def _print_coverage(close_src):
    got, started, loss = compute_coverage()
    print("### 采集覆盖率")
    print(f"  已开赛记录 {started} 条 → 采到收盘价 {got} 条, 丢失 {started - got} 条 ({loss:.0f}%)")
    if close_src:
        label = {"live": "实时窗口", "archive": "归档回捞", "archive_open": "仅开盘价(不计入CLV)"}
        print("  收盘价来源: " + ", ".join(
            f"{label.get(k, k)} {v}" for k, v in sorted(close_src.items(), key=lambda x: -x[1])))
    if loss > LOSS_ALERT_THRESHOLD:
        print(f"  ⚠️ 丢失率超过 {LOSS_ALERT_THRESHOLD:.0f}% — CLV 样本严重不完整, 结论不可信")
    print()


def main():
    if not RESULTS_FILE.exists():
        print("❌ 无 clv_results.csv，先等 CLV 采集器跑出数据")
        return

    src_map = _load_source_map()
    groups = defaultdict(list)  # (source, bucket) -> [clv]

    close_src = defaultdict(int)
    with open(RESULTS_FILE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                clv = float(r.get("true_clv_pct", 0))
                ev = float(r.get("push_ev_pct", 0))
            except (ValueError, TypeError):
                continue
            if ev < 2.0:  # 只统计 EV>=2% (比价门槛)
                continue
            # V5.10: 只有真收盘价能算 CLV。archive_open 是归档库的开盘价
            # (让球/大小球受 UNIQUE 约束只留首见价), 掺进来会把 CLV 算成噪声。
            cs = (r.get("close_source") or "live").strip() or "live"
            close_src[cs] += 1
            if cs == "archive_open":
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

    _print_coverage(close_src)

    print()
    print("判定标准: 中位CLV > +1% 且 正CLV率 > 55% → 比价 edge 成立")

    _check_limit_risk()


def _check_limit_risk():
    """2026-08-29 限注预警(职业团队): 博彩公司对持续平均CLV>+10% 且 n>300 的账户自动降注额上限。

    我们跟踪自己 push 侧的平均 CLV, 超过阈值就告警"被限注风险"。
    """
    push_clvs = []
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("source") != "push":
                    continue
                try:
                    push_clvs.append(float(r["true_clv_pct"]))
                except (TypeError, ValueError):
                    continue
    if not push_clvs:
        return
    n = len(push_clvs)
    avg = statistics.mean(push_clvs)
    print()
    if n > 300 and avg > 10.0:
        print(f"⚠️ 限注预警: push 侧平均 CLV={avg:+.2f}% 超 +10%, n={n} 超 300 注 — 被博彩公司限注风险高!")
    else:
        print(f"限注检查: push 侧平均 CLV={avg:+.2f}% (n={n}), 未触发限注预警阈值(+10%/300注)")


if __name__ == "__main__":
    main()
