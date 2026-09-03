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

# 2026-09-03 双库交叉验证护栏 + 方向级封杀:
# - 主开关只靠"实盘 ROI>4%"会被高赔率盘假 ROI 骗(htft 实盘+5.2%但胜率3%, 观察库-86.6%真相是巨亏)。
#   加观察库 ROI 交叉验证: 观察库同盘口 ROI < OBS_CROSS_ROI_MIN 视为假正, 不释放。
# - 盘口级 ROI 掩盖方向级 edge(1x2 整体+0.5%, 但和局+37.4%强正 vs 主/客-7.5%/-9.4%负)。
#   加方向级封杀: 已释放盘口里, 实盘方向 ROI < DIR_ROI_MIN 且 n≥DIR_N_MIN 的方向封杀。
OBS_CROSS_N_MIN = 10        # 观察库交叉验证采信最小样本
OBS_CROSS_ROI_MIN = -20.0   # 观察库 ROI < -20% 视为假正(双库强分歧)
DIR_N_MIN = 15              # 方向级 ROI 采信最小样本量
DIR_ROI_MIN = -5.0          # 方向级封杀阈值(ROI < -5%)

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


def _direction(desig, sub_market):
    """从 designation 提取方向(主/平/客/大/小/其他), 供方向级释放/封杀。

    与 bb_ev_push._release_direction 保持同一归一化口径。
    1x2: 主胜/平/客胜 → 主/平/客; dc: 双重机会-主/和→主, 双重机会-和局/客→客。
    htft 半全场方向复杂, 由整盘护栏(观察库交叉验证)封杀, 这里归一化不拆分。
    """
    d = (desig or "")
    if sub_market == "htft":
        return "其他"
    dl = d.lower()
    if "大" in d or "over" in dl:
        return "大"
    if "小" in d or "under" in dl:
        return "小"
    if ("和" in d or "平" in d or "draw" in dl) and "客" not in d and "主" not in d:
        return "平"
    if "客" in d or "away" in dl:
        return "客"
    if "主" in d or "home" in dl:
        return "主"
    return "其他"


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


def load_real_direction_roi():
    """实盘方向级 ROI: tracked_bets.json settled → {(sport,sub_market,direction): agg}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled"]

    def _dir_key(b):
        return (b.get("sport") or "?", b.get("sub_market") or "?",
                _direction(b.get("designation"), b.get("sub_market")))

    return _agg_bets(bets, _dir_key)


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


def load_observe_roi_market():
    """观察库盘口级 ROI(聚合联赛, 供主开关双库交叉验证): paper_bets.json → {(sport,sub_market): agg}。"""
    if not PAPER.exists():
        return {}
    try:
        raw = json.loads(PAPER.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = raw.get("bets", []) if isinstance(raw, dict) else raw
    return _agg_bets(bets, lambda b: (b.get("sport") or "?", b.get("sub_market") or "?"))


def main():
    market_roi, league_roi = load_real_roi()
    dir_roi = load_real_direction_roi()
    obs_mkt_roi = load_observe_roi_market()
    obs_clv = load_observe_clv()
    obs_roi = load_observe_roi()

    market_released = []
    for (sport, sm), d in sorted(market_roi.items()):
        if d["n"] >= N_REAL_MIN and d["roi"] > REAL_ROI_MIN:
            # 双库交叉验证护栏(2026-09-03): 观察库同盘口 ROI 强负 → 实盘 ROI 是假正
            # (高赔率盘少数命中, 如 htft 实盘+5.2%但胜率3%/观察库-86.6%), 不释放。
            o = obs_mkt_roi.get((sport, sm))
            if o and o["n"] >= OBS_CROSS_N_MIN and o["roi"] < OBS_CROSS_ROI_MIN:
                continue
            market_released.append([sport, sm])

    # 方向级细分(2026-09-03): 盘口级 ROI 掩盖方向级 edge。
    # - direction_released: 整盘没过主开关, 但某方向实盘 ROI 强正(如 1x2 和局+37.4% vs 整盘+0.5%)。
    # - direction_blocked: 整盘已释放, 但某方向实盘 ROI 强负(如 dc 主-24.7%)。
    released_set = {tuple(m) for m in market_released}
    direction_released = []
    direction_blocked = []
    for (sport, sm, dr), d in sorted(dir_roi.items()):
        if d["n"] < DIR_N_MIN:
            continue
        if (sport, sm) in released_set:
            if d["roi"] < DIR_ROI_MIN:
                direction_blocked.append([sport, sm, dr])
        else:
            # 观察库交叉验证: 整盘观察库 ROI 强负的方向也不释放(htft 观察库-86.6% 假正)
            o = obs_mkt_roi.get((sport, sm))
            if o and o["n"] >= OBS_CROSS_N_MIN and o["roi"] < OBS_CROSS_ROI_MIN:
                continue
            if d["roi"] > REAL_ROI_MIN:
                direction_released.append([sport, sm, dr])

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
        "direction_released": direction_released,
        "direction_blocked": direction_blocked,
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(OUT)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


if __name__ == "__main__":
    main()
