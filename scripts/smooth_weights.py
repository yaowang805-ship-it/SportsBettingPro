#!/usr/bin/env python3
"""
V4.5 权重平滑引擎 — 让 Kelly 曲线单调、连续、保守

对校准后的原始 bin 数据应用:
1. Bayesian shrinkage — 小样本 bin 向聚合均值收缩
2. Isotonic regression — 强制 WR 单调递减 (高赔率=低胜率)
3. Wilson CI lower bound — 用 95% 置信下界替代点估计
4. Linear fill — 填补缺失 bin (线性插值)
"""
import json, math
from collections import defaultdict
from pathlib import Path
import sys

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

ODDS_BINS = [1.25, 1.39, 1.60, 1.80, 2.00, 2.19, 2.40, 2.60, 2.80, 3.00,
             3.20, 3.40, 3.60, 3.80, 4.05, 4.35, 4.65, 5.00, 5.40, 5.80,
             6.25, 6.75, 7.25, 7.75, 8.50, 9.50, 11.00, 13.50, 17.50, 25.00]

OU_BINS = [1.42, 1.62, 1.80, 1.99, 2.18, 2.38, 2.58, 2.78, 3.00, 3.20, 3.38]


def wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    """Wilson 95% CI lower bound."""
    if total == 0: return 0.0
    p = wins / total
    n = total
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.001, center - margin)


def bayesian_shrinkage(bins_data: dict, prior_weight: int = 200) -> dict:
    """向市场隐含概率收缩 (1/avg_o = 公平概率)."""
    shrunk = {}
    for bi, (wr, avg_o, n) in bins_data.items():
        # 用 Pinnacle 收盘价隐含的公平概率作为先验
        if avg_o > 1:
            prior_wr = 1.0 / avg_o * 0.97  # 去3% margin
        else:
            prior_wr = 0.5
        post_wr = (wr * n + prior_wr * prior_weight) / (n + prior_weight)
        shrunk[bi] = (round(post_wr, 4), avg_o, n)
    return shrunk


def isotonic_regression(bins_data: dict) -> dict:
    """PAVA isotonic — 强制 WR 随 bin index 单调递减."""
    if not bins_data: return bins_data
    # Collect bins in order
    sorted_bins = sorted(bins_data.items())
    if len(sorted_bins) <= 1: return bins_data

    # Pool Adjacent Violators
    pools = []
    for bi, (wr, avg_o, n) in sorted_bins:
        pools.append({"bins": [bi], "wr_sum": wr * n, "n_sum": n,
                       "avg_o_sum": avg_o * n, "data": [(bi, wr, avg_o, n)]})

    # Merge upward violations
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(pools) - 1:
            wr_i = pools[i]["wr_sum"] / pools[i]["n_sum"]
            wr_j = pools[i + 1]["wr_sum"] / pools[i + 1]["n_sum"]
            if wr_i < wr_j:  # Violation: earlier bin should have HIGHER WR
                # Merge
                pools[i]["bins"].extend(pools[i + 1]["bins"])
                pools[i]["wr_sum"] += pools[i + 1]["wr_sum"]
                pools[i]["n_sum"] += pools[i + 1]["n_sum"]
                pools[i]["avg_o_sum"] += pools[i + 1]["avg_o_sum"]
                pools[i]["data"].extend(pools[i + 1]["data"])
                pools.pop(i + 1)
                changed = True
            else:
                i += 1

    # Output
    result = {}
    for pool in pools:
        pooled_wr = pool["wr_sum"] / pool["n_sum"]
        pooled_avg_o = pool["avg_o_sum"] / pool["n_sum"]
        pooled_n = pool["n_sum"]
        for bi, _, orig_avg_o, orig_n in pool["data"]:
            result[bi] = (round(pooled_wr, 4), round(pooled_avg_o, 2), orig_n)
    return result


def linear_fill(bins_data: dict, max_bin: int = 29) -> dict:
    """线性插值填补缺失 bin."""
    if not bins_data: return bins_data
    filled = dict(bins_data)
    sorted_existing = sorted(bins_data.keys())

    for bi in range(max_bin + 1):
        if bi in filled: continue
        # Find nearest bins below and above
        below = max([b for b in sorted_existing if b < bi], default=None)
        above = min([b for b in sorted_existing if b > bi], default=None)

        if below is not None and above is not None:
            # Linear interpolate WR
            wr_lo = bins_data[below][0]
            wr_hi = bins_data[above][0]
            frac = (bi - below) / (above - below)
            wr = round(wr_lo + (wr_hi - wr_lo) * frac, 4)
            # Interpolate avg odds
            avg_o_lo = bins_data[below][1]
            avg_o_hi = bins_data[above][1]
            avg_o = round(avg_o_lo + (avg_o_hi - avg_o_lo) * frac, 2)
            filled[bi] = (wr, avg_o, 0)  # n=0 marks interpolated
        elif below is not None:
            filled[bi] = bins_data[below]
        elif above is not None:
            filled[bi] = bins_data[above]

    return filled


def smooth_market(bins_data: dict, label: str = "") -> dict:
    """完整平滑管线."""
    if not bins_data:
        return {}

    original_bins = len(bins_data)
    total_n = sum(v[2] for v in bins_data.values())

    # Step 1: Wilson CI lower bound (保守)
    wilson = {}
    for bi, (wr, avg_o, n) in bins_data.items():
        wins = int(wr * n)
        wl = wilson_lower(wins, n)
        wilson[bi] = (wl, avg_o, n)

    # Step 2: Bayesian shrinkage (用市场隐含概率做先验)
    shrunk = bayesian_shrinkage(wilson)

    # Step 3: Isotonic regression
    isotonic = isotonic_regression(shrunk)

    # Step 4: Linear fill gaps
    filled = linear_fill(isotonic)

    # Log
    n_after = len(filled)
    n_interp = sum(1 for v in filled.values() if v[2] == 0)
    if label:
        print(f"  {label}: {original_bins}→{n_after} bins ({n_interp} interpolated, {total_n} bets)")

    return filled


def process_all():
    """处理校准文件中的所有市场."""
    cal_path = SRC / "data" / "storage" / "v4_calibrated_weights.json"
    if not cal_path.exists():
        print("校准文件不存在, 先运行 calibrate_v4_weights.py")
        return

    with open(cal_path) as f:
        data = json.load(f)

    smoothed = {}

    # ── Football 1X2 (per league + aggregate) ──
    print("=== 平滑处理 ===")
    if "PIN_1X2_DATA" in data:
        smoothed["PIN_1X2_DATA"] = {}
        for lg, bins in data["PIN_1X2_DATA"].items():
            # Convert JSON format: str key → int, list → tuple
            raw = {}
            for bi, val in bins.items():
                bi_int = int(bi)
                raw[bi_int] = (val[0], val[1], val[2]) if isinstance(val, list) else val
            smoothed["PIN_1X2_DATA"][lg] = smooth_market(raw, f"1X2 {lg}")

    # ── Football OU ──
    if "PIN_OU_DATA" in data:
        smoothed["PIN_OU_DATA"] = {}
        for lg, bins in data["PIN_OU_DATA"].items():
            raw = {}
            for bi, val in bins.items():
                bi_int = int(bi)
                raw[bi_int] = (val[0], val[1], val[2]) if isinstance(val, list) else val
            smoothed["PIN_OU_DATA"][lg] = smooth_market(raw, f"OU  {lg}")

    # ── NBA ML ──
    if "NBA_ML_DATA" in data:
        raw = {}
        for bi, val in data["NBA_ML_DATA"].items():
            bi_int = int(bi)
            raw[bi_int] = (val[0], val[1], val[2]) if isinstance(val, list) else val
        smoothed["NBA_ML_DATA"] = smooth_market(raw, "NBA ML")

    # ── MLB ──
    if "MLB_ODDSPORTAL_DATA" in data:
        raw = {}
        for bi, val in data["MLB_ODDSPORTAL_DATA"].items():
            bi_int = int(bi)
            raw[bi_int] = (val[0], val[1], val[2]) if isinstance(val, list) else val
        smoothed["MLB_ODDSPORTAL_DATA"] = smooth_market(raw, "MLB ML")

    # ── Football AH ──
    if "PIN_AH_DATA" in data:
        raw = {}
        for bi, val in data["PIN_AH_DATA"].items():
            bi_int = int(bi)
            raw[bi_int] = (val[0], val[1], val[2]) if isinstance(val, list) else val
        smoothed["PIN_AH_DATA"] = smooth_market(raw, "AH")

    # ── 保存 ──
    out_path = SRC / "data" / "storage" / "v4_smoothed_weights.json"
    # Convert back to JSON-safe format
    json_out = {}
    for market, lg_data in smoothed.items():
        if isinstance(lg_data, dict) and any(isinstance(v, dict) for v in lg_data.values()):
            # Per-league data
            json_out[market] = {}
            for lg, bins in lg_data.items():
                json_out[market][lg] = {str(bi): [wr, avg_o, n] for bi, (wr, avg_o, n) in bins.items()}
        else:
            # Flat data
            json_out[market] = {str(bi): [wr, avg_o, n] for bi, (wr, avg_o, n) in lg_data.items()}

    with open(out_path, "w") as f:
        json.dump(json_out, f, ensure_ascii=False)
    print(f"\n✅ 已保存平滑权重到 {out_path}")

    # 对比: 原始 vs 平滑
    print("\n=== 对比: 原始 → 平滑 (1X2 _AGGREGATE) ===")
    raw_agg = data["PIN_1X2_DATA"].get("_AGGREGATE", {})
    smooth_agg = smoothed.get("PIN_1X2_DATA", {}).get("_AGGREGATE", {})
    for odds_test in [1.3, 1.7, 2.2, 3.5, 5.0, 12.0]:
        bi = 0
        for i, t in enumerate(ODDS_BINS):
            if odds_test <= t: bi = i; break
        r = raw_agg.get(str(bi), [0,0,0])
        s = smooth_agg.get(bi, (0,0,0))
        r_kelly = max(0, (r[0]*r[1]-1)/(r[1]-1)*0.75) if len(r)>=2 and r[1]>1 else 0
        s_kelly = max(0, (s[0]*s[1]-1)/(s[1]-1)*0.75) if len(s)>=2 and s[1]>1 else 0
        print(f"  @{odds_test:.1f}: raw WR={r[0]:.3f} Kelly={r_kelly:.3f} → smooth WR={s[0]:.3f} Kelly={s_kelly:.3f}")


if __name__ == "__main__":
    process_all()
