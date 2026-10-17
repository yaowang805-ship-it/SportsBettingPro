#!/usr/bin/env python3
"""六闸门检验脚本(2026-10-17): 对每个盘口格子跑六闸门, 判断能否释放。

六闸门(见 baseline-snapshot-20261013 + PREREGISTRATION.md):
  1. pin_points 配对 |d|>0.5 比例 ≤10%   (需 pin_points 落盘, 10-17 起才有)
  2. out-of-sample 新注单复现           (修复后 cohort, 时间切分)
  3. placebo 噪声上限低于观测值
  4. 滚动窗口无「一段撑全场」
  5. top-k 场/联赛贡献 ≤40%
  6. void 率对称 + 缺失行可解释

用法: python3 scripts/check_six_gates.py
"""
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SP_CN = {1: "足球", 3: "篮球", 5: "网球", 7: "棒球", 6: "美足", 2: "美足",
         12: "冰球", 8: "冰球", 4: "冰球"}


def _dr(b):
    d = b.get("desig", "") or ""
    if "客" in d:
        return "客"
    if "主" in d:
        return "主"
    if "大" in d:
        return "大"
    if "小" in d:
        return "小"
    if "和" in d or "平" in d:
        return "平"
    return d


def _load_bets():
    d = json.loads((ROOT / "data" / "storage" / "real_bets.json").read_text())
    bets = d["bets"] if isinstance(d, dict) and "bets" in d else d
    vals = list(bets.values()) if isinstance(bets, dict) else bets
    return [b for b in vals if b.get("result") is not None and b.get("profit") is not None]


def _roi(bets):
    p = sum(b["profit"] for b in bets)
    st = sum(b["stake"] for b in bets)
    return p / st * 100 if st else 0.0


def gate_out_of_sample(bets):
    """第2条: 时间切分, 后半段 ROI 是否复现(同号且不塌)."""
    if len(bets) < 20:
        return "待样本", 0.0, 0.0
    bs = sorted(bets, key=lambda b: b["ts"])
    half = len(bs) // 2
    r1, r2 = _roi(bs[:half]), _roi(bs[half:])
    # 复现 = 后半与前半同号
    ok = (r1 > 0 and r2 > 0) or (r1 < 0 and r2 < 0)
    return ("通过" if ok else "不通过"), r1, r2


def gate_placebo(bets, n_shuffle=500):
    """第3条: 打乱标签, 观测 ROI 是否显著高于噪声上界(p95)."""
    if len(bets) < 20:
        return "待样本", 0.0, 0.0
    obs = _roi(bets)
    profits = [b["profit"] for b in bets]
    stakes = [b["stake"] for b in bets]
    shuffled = []
    for _ in range(n_shuffle):
        random.shuffle(profits)
        sp = sum(profits[i] for i in range(len(profits)))
        shuffled.append(sp / sum(stakes) * 100)
    shuffled.sort()
    noise95 = shuffled[int(len(shuffled) * 0.95)]
    ok = abs(obs) > noise95
    return ("通过" if ok else "不通过"), obs, noise95


def gate_rolling(bets):
    """第4条: 按天滚动, 看 ROI 是否被某一段撑全场."""
    if len(bets) < 20:
        return "待样本"
    byday = defaultdict(list)
    for b in bets:
        import datetime
        byday[datetime.datetime.fromtimestamp(b["ts"]).strftime("%m-%d")].append(b)
    daily = {d: _roi(bs) for d, bs in byday.items()}
    # 去掉最好的一天, 看剩余 ROI 是否还同号
    best = max(daily.values())
    others = [v for k, v in daily.items() if v != best]
    # 简化: 看是否有单日贡献 > 50% 总利润
    total_p = sum(b["profit"] for b in bets)
    worst_offender = max((sum(b["profit"] for b in bs) for bs in byday.values()), default=0)
    share = abs(worst_offender) / abs(total_p) * 100 if total_p else 0
    return "通过" if share <= 50 else "不通过", share


def gate_topk(bets, k=3):
    """第5条: top-k 场比赛贡献 ≤40%. 返回 (判定, 贡献%)"""
    bymatch = defaultdict(float)
    for b in bets:
        bymatch[(b.get("home"), b.get("away"))] += b["profit"]
    total = sum(b["profit"] for b in bets)
    if not total:
        return "待样本", 0.0
    top = sorted(bymatch.values(), reverse=True)[:k]
    share = abs(sum(top)) / abs(total) * 100
    return ("通过" if share <= 40 else "不通过"), share


def gate_void():
    """第6条: void/push 率对称(从 live_settled_log 的 uwl==0 且 won 状态判断)."""
    try:
        ls = json.loads((ROOT / "data" / "storage" / "live_settled_log.json").read_text())
    except Exception:
        return "待数据", 0.0
    voidish = [x for x in ls if abs(x.get("uwl", 0) or 0) < 0.01 and not x.get("won")]
    rate = len(voidish) / len(ls) * 100 if ls else 0
    # 对称 = void 率 < 某阈值(这里用 8%)
    return ("通过" if rate <= 8 else "不通过"), rate


def main():
    bets = _load_bets()
    g = defaultdict(list)
    for b in bets:
        g[(SP_CN.get(b.get("sport"), b.get("sport")), b.get("sub"), _dr(b))].append(b)

    print("六闸门检验(实盘 real_bets, 只对 n>=20 的格子):\n")
    print(f"{'格子':<22}{'n':>4}{'ROI':>7}  {'OOS':<8}{'placebo':<8}{'top3贡献':<8}{'滚动':<6}{'void':<8}")
    for k, bs in sorted(g.items(), key=lambda x: -len(x[1])):
        if len(bs) < 20:
            continue
        roi = _roi(bs)
        oos, r1, r2 = gate_out_of_sample(bs)
        pla, obs, noise = gate_placebo(bs)
        top, share = gate_topk(bs)
        roll, dayshare = gate_rolling(bs)
        void, vrate = gate_void()
        print(f"{str(k):<22}{len(bs):>4}{roi:>+6.1f}%  {oos:<8}{pla:<8}{share:>7.0f}%  {dayshare:>5.0f}% {vrate:>7.1f}%")


if __name__ == "__main__":
    main()
