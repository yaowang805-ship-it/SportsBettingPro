#!/usr/bin/env python3
"""盘口释放清单计算 — 用真实 ROI 替代 CLV 作为投注准入。

核心思想(2026-09-01 用户确认):
    比价套利的"哪些盘口能投"不能只看 CLV —— 实测 CLV 是假正: 观察库 CLV 中位为正的
    盘口(ht/ht_dc/correct_score_ht/htft)实盘 ROI 全是负的, 而实盘 ROI 为正的(1x2/dc/btts)
    观察库 CLV 中位全是负的。故用真实结算 ROI 作为释放(投注)的最终标准。

释放规则(混合粒度, 三通道):
    1. 主开关(运动×盘口):      实盘 ROI > 4% 且 n≥30 → 释放。
    2. 联赛细化(运动×联赛×盘口): 某联赛三维格子 n≥30 时, 用该联赛自己的 ROI 覆盖主开关
                                (ROI>4% 释放, 否则封杀)。
    3. 观察库释放(运动×联赛×盘口): 观察库 CLV 中位>0 且 正率>55% 且 观察库纸面结算
                                ROI>0, 且 CLV n≥30 / ROI n≥5 → 释放(未释放盘口的动态释放通道)。
    其余盘口一律封杀(只观察积累, 不投注)。

数据源:
    实盘 ROI   = data/storage/tracked_bets.json (status=settled 的 profit/stake)
    观察库 CLV = data/storage/clv_results.csv   (source=validate 的 true_clv_pct, dc/btts 剔除改版前)
    观察库 ROI = data/storage/paper_bets.json   (纸面投注结算结果, 由 paper_settle 生成, 含 league)

输出:
    data/storage/market_release.json
    {
      "generated_at": "...",
      "market_released":  [["football","1x2"], ...],        # 主开关(运动×盘口)
      "league_released":  [["football","MLS","hc"],...],    # 联赛细化(释放)
      "league_blocked":   [["football","X","hc"],...],      # 联赛细化(封杀, 覆盖主开关)
      "observe_released": [["football","X","ht"], ...],     # 观察库三维释放
    }

用法: .venv312/bin/python scripts/compute_market_release.py
"""
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
TRACKED = DATA / "tracked_bets.json"
RESULTS = DATA / "clv_results.csv"
PAPER = DATA / "paper_bets.json"
OUT = DATA / "market_release.json"

REAL_ROI_MIN = 4.0     # 实盘 ROI 释放阈值(%)
N_REAL_MIN = 30        # 实盘 ROI 采信最小样本量
OBS_ROI_MIN = 0.0      # 观察库 ROI 释放阈值(%)
N_OBS_CLV_MIN = 30     # 观察库 CLV 采信最小样本量
N_OBS_ROI_MIN = 5      # 观察库 ROI 采信最小样本量
POS_RATE_MIN = 55.0    # 观察库正 CLV 率下限(%)
MEDIAN_MIN = 0.0       # 观察库 CLV 中位下限(%)

# 改版时间切分(复用 compute_ev_thresholds.py 口径): dc/btts 改版前由 1X2/team_total 推导,
# 公平价被系统性污染(负 CLV 是推导偏差, 不是真负 edge)。观察库 CLV 只统计改版后样本。
REVISION_CUTOFF_UTC = datetime.fromisoformat("2026-08-28T00:00:00+00:00").timestamp()
REVISION_MARKETS = {"btts", "dc"}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _agg_bets(bets, key_fn):
    agg = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0})
    for b in bets:
        stake = _f(b.get("stake")) or 0.0
        profit = _f(b.get("profit")) or 0.0
        k = key_fn(b)
        if k is None:
            continue
        agg[k]["n"] += 1
        agg[k]["stake"] += stake
        agg[k]["profit"] += profit
    for d in agg.values():
        d["roi"] = (d["profit"] / d["stake"] * 100.0) if d["stake"] > 0 else 0.0
    return agg


def load_real_roi():
    """实盘 ROI: tracked_bets.json settled 记录 → (market_roi, league_roi)。"""
    if not TRACKED.exists():
        return {}, {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}, {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled"]

    def _market_key(b):
        return (b.get("sport") or "?", b.get("sub_market") or "?")

    def _league_key(b):
        lg = (b.get("league") or "").strip()
        if not lg:
            return None
        return (b.get("sport") or "?", lg, b.get("sub_market") or "?")

    return _agg_bets(bets, _market_key), _agg_bets(bets, _league_key)


def load_observe_clv():
    """观察库 CLV 中位/正率: clv_results.csv source=validate → {(sport,league,sub_market): {n,median,pos_rate}}。"""
    by = defaultdict(list)
    if not RESULTS.exists():
        return {}
    with open(RESULTS, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if (r.get("source") or "").strip() != "validate":
                continue
            sm = r.get("sub_market") or "?"
            clv = _f(r.get("true_clv_pct"))
            if clv is None:
                continue
            pt = _ts(r.get("push_time"))
            if sm in REVISION_MARKETS and (pt is None or pt < REVISION_CUTOFF_UTC):
                continue
            by[(r.get("sport") or "?", r.get("league") or "?", sm)].append(clv)
    out = {}
    for k, vals in by.items():
        if not vals:
            continue
        out[k] = {
            "n": len(vals),
            "median": statistics.median(vals),
            "pos_rate": sum(1 for v in vals if v > 0) / len(vals) * 100.0,
        }
    return out


def load_observe_roi():
    """观察库纸面结算 ROI: paper_bets.json → {(sport,league,sub_market): roi聚合}。"""
    if not PAPER.exists():
        return {}
    try:
        raw = json.loads(PAPER.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = raw.get("bets", []) if isinstance(raw, dict) else raw
    return _agg_bets(bets, lambda b: (
        b.get("sport") or "?",
        (b.get("league") or "").strip() or "?",
        b.get("sub_market") or "?"))


def main():
    market_roi, league_roi = load_real_roi()
    obs_clv = load_observe_clv()
    obs_roi = load_observe_roi()

    market_released = []
    for (sport, sm), d in sorted(market_roi.items()):
        if d["n"] >= N_REAL_MIN and d["roi"] > REAL_ROI_MIN:
            market_released.append([sport, sm])

    league_released = []
    league_blocked = []
    for (sport, lg, sm), d in sorted(league_roi.items()):
        if d["n"] >= N_REAL_MIN:
            if d["roi"] > REAL_ROI_MIN:
                league_released.append([sport, lg, sm])
            else:
                league_blocked.append([sport, lg, sm])

    observe_released = []
    for k, clv in sorted(obs_clv.items()):
        sport, lg, sm = k
        if clv["n"] < N_OBS_CLV_MIN:
            continue
        if not (clv["median"] > MEDIAN_MIN and clv["pos_rate"] > POS_RATE_MIN):
            continue
        roi = obs_roi.get(k)
        if not roi or roi["n"] < N_OBS_ROI_MIN:
            continue
        if roi["roi"] > OBS_ROI_MIN:
            observe_released.append([sport, lg, sm])

    out = {
        "generated_at": datetime.now().isoformat(),
        "market_released": market_released,
        "league_released": league_released,
        "league_blocked": league_blocked,
        "observe_released": observe_released,
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(OUT)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


if __name__ == "__main__":
    main()
