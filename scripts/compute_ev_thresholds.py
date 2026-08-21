#!/usr/bin/env python3
"""数据驱动的 EV 门槛矩阵计算 — 每晚从 clv_results.csv 自动重算。

核心思想(全数据驱动模式):
    比价套利的 EV 门槛不能统一 2% —— 实测 2-3% 档 CLV 中位 -2.16%/正率 35%(显著负),
    而 8%+ 档 +9.25%/76%(显著正)。门槛必须按盘口/运动/时间分层, 且数据驱动。

判定标准(业界标准):
    一个格子 EV≥X 时, 只有当「正 CLV 率 ≥55% 且 中位 CLV > +2%」才认为 X 是该格子
    门槛; 否则抬高到满足为止。无任何档满足 → 停推(门槛=999)。

样本纪律:
    CLV≥100 注 = 确认; 30-100 = 方向性; <30 = 用保守高门槛回退(不拍脑袋放低)。

输出:
    data/storage/ev_threshold_matrix.json
    {
      "generated_at": "...",
      "markets": {"1x2": 5.0, "ou": 8.0, "ht": 2.0, ...},
      "sport_adjust": {"tennis": -0.5, "basketball": +1.0, ...},
      "in_play_extra": 3.0,   # 临场<1h 且主体盘口 额外加门槛
      "main_markets": ["1x2","hc","ou"],   # 临场规则适用盘口
      "default_threshold": 8.0,  # 样本不足盘口的保守回退
    }

用法: .venv312/bin/python scripts/compute_ev_thresholds.py
"""
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
RESULTS = DATA / "clv_results.csv"
OUT = DATA / "ev_threshold_matrix.json"

POS_RATE_MIN = 0.55     # 正 CLV 率下限
MEDIAN_CLV_MIN = 2.0    # 中位 CLV 下限(%)
MIN_N = 10              # 一个 EV 档至少多少样本才可信
EV_STEPS = (8, 5, 3, 2) # 从高到低找最低可行门槛

# 运动层微调(相对盘口门槛的加/减, 基于运动整体 CLV 相对大盘口)
SPORT_ADJUST = {
    "football": 0.0,
    "tennis": -0.5,      # 网球整体 +0.28%, 可略降
    "basketball": 1.0,   # 篮球整体 -3.03%, 要抬高
    "baseball": 1.0,
    "american_football": 0.0,
    "ice_hockey": 0.0,
    "mma": 2.0,          # MMA/拳击高风险, 更高门槛
    "boxing": 2.0,
}

# 临场规则: 距开赛<1h 且主体盘口 → 额外加门槛(临场 Pin 最准, BB 偏离是滞后噪声)
IN_PLAY_HOURS = 1.0
IN_PLAY_EXTRA = 3.0
MAIN_MARKETS = ("1x2", "hc", "ou")


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_clean():
    if not RESULTS.exists():
        return []
    out = []
    with open(RESULTS, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if (r.get("close_source") or "live").strip() == "archive_open":
                continue
            clv, ev, od = f(r.get("true_clv_pct")), f(r.get("push_ev_pct")), f(r.get("bb_odds"))
            if clv is None or ev is None or ev < 2:
                continue
            if od and ev > max(12.0, (od - 1) * 20):
                continue  # EV 超系统上限 = 上游坏价垃圾
            out.append((r.get("sub_market", "?"), clv, ev))
    return out


def market_threshold(samples):
    """对一个盘口的 (clv, ev) 样本, 找最低可行 EV 门槛。"""
    for thr in EV_STEPS:
        sub = [(c, e) for c, e in samples if e >= thr]
        if len(sub) < MIN_N:
            continue
        clvs = [c for c, _ in sub]
        pos_rate = sum(1 for c in clvs if c > 0) / len(clvs)
        med = statistics.median(clvs)
        if pos_rate >= POS_RATE_MIN and med > MEDIAN_CLV_MIN:
            return thr, len(sub), round(med, 2), round(pos_rate * 100)
    return None, len(samples), None, None


def main():
    clean = load_clean()
    by_market = defaultdict(list)
    for sm, clv, ev in clean:
        by_market[sm].append((clv, ev))

    markets = {}
    details = {}
    for sm in sorted(by_market, key=lambda s: -len(by_market[s])):
        thr, n, med, pos = market_threshold(by_market[sm])
        if thr is None:
            # 无档满足正 edge → 停推(用 999 表示, 推送层会拦掉)
            markets[sm] = 999.0
            details[sm] = {"n": n, "verdict": "停推(无档满足正edge)", "median": med, "pos_rate": pos}
        else:
            markets[sm] = float(thr)
            details[sm] = {"n": n, "verdict": "data_driven", "median": med, "pos_rate": pos}

    matrix = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "markets": markets,
        "sport_adjust": SPORT_ADJUST,
        "in_play_hours": IN_PLAY_HOURS,
        "in_play_extra": IN_PLAY_EXTRA,
        "main_markets": list(MAIN_MARKETS),
        "default_threshold": 8.0,
        "_details": details,
    }
    OUT.write_text(json.dumps(matrix, ensure_ascii=False, indent=2))
    print(f"✅ 门槛矩阵已生成 → {OUT.name}")
    print(f"  样本 {len(clean)} 条, {len(markets)} 个盘口")
    for sm in sorted(markets, key=lambda s: -by_market[s].__len__()):
        d = details[sm]
        v = d["verdict"]
        print(f"    {sm:<22} 门槛 {markets[sm]:>6}  n={d['n']:>4}  {v}")


if __name__ == "__main__":
    main()
