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

# ── 2026-09-12 观察库释放改造(用户要求): 样本>100 且 赢率>隐含 才释放 ──
# 判据从「CLV中位>0 + 正率>55% + 纸面ROI>0」改为「赢率 > 隐含」(CURRENT_STATUS 核心判据),
# 样本门槛从 n≥30 提到 n>100。滚球+早盘观察库合并统计, 滚球 league 归 "滚球"。
OBS_N_MIN = 100            # 观察库释放采信最小结算样本数(>100)
OBS_WINRATE_EDGE_MIN = 0.0 # 赢率>隐含(差>0pp 即释放)
LIVE_PAPER = DATA / "live_paper_bets.json"
OBS_STATE = DATA / "observe_release_state.json"  # 释放状态(首次释放时间 + cap)

# 滚球观察库口径映射: BB 运动 id → 英文运动名; 滚球 sub → sub_market
BB_SPORT_MAP = {1: "football", 3: "basketball", 5: "tennis", 7: "baseball", 6: "american_football"}
BB_SUB_MAP = {"over_under": "ou", "handicap": "hc", "opportunities": "1x2"}
LIVE_LEAGUE = "滚球"

# 运动中文名(2026-09-12 用户要求: 释放盘口必须标注具体运动)。释放清单 sport 字段是英文,
# 展示/推送时用此表转中文, 避免 ht/ou/hc 等盘口在足球/篮球/网球间混淆。
SPORT_CN = {
    "football": "⚽足球", "basketball": "🏀篮球", "tennis": "🎾网球",
    "baseball": "⚾棒球", "american_football": "🏈美足", "ice_hockey": "🏒冰球",
}

# 投注额分阶段上限(用户要求): 新释放 150, 实盘满一周 ROI>4% 提 300
OBS_CAP_NEW = 150
OBS_CAP_MATURE = 300
OBS_MATURE_DAYS = 7        # 实盘投一周(7天)后评估提额

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


def _window(match_epoch, push_ts):
    """时间窗分档: 临场<6h / 近场6-24h / 早盘24-72h / None(已开赛或超72h)。"""
    if not match_epoch or not push_ts:
        return None
    lead = match_epoch - push_ts
    if lead < 0:
        return None
    if lead < 6 * 3600:
        return "临场"
    if lead < 24 * 3600:
        return "近场"
    if lead < 72 * 3600:
        return "早盘"
    return None


def _dir_threshold(roi, n):
    """方向 ROI → EV 门槛(数据驱动, 替代 bb_ev_push 硬编码 DIRECTION_MIN_EV)。

    下限 3.0 = 职业底线(历史教训: <3% 会放行大量临场漂移假机会)。
    ROI 越正门槛越低(充分信任), 样本越少越保守。
    """
    if roi >= 30.0:
        thr = 3.0
    elif roi >= 15.0:
        thr = 4.0
    elif roi >= 5.0:
        thr = 5.0
    else:
        thr = 6.0
    # 样本置信度折扣: n<15 太不可信不设方向门槛(回退基础), n<30 加保守折扣
    if n < 15:
        return None
    if n < 30:
        thr += 1.0
    return round(thr, 1)


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


def load_real_direction_window_roi():
    """实盘方向×时间窗 ROI: tracked_bets.json settled → {(sport,sub_market,direction,window): agg}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled"]

    def _key(b):
        w = _window(_f(b.get("match_epoch")), _ts(b.get("push_time")))
        if w is None:
            return None
        return (b.get("sport") or "?", b.get("sub_market") or "?",
                _direction(b.get("designation"), b.get("sub_market")), w)

    return _agg_bets(bets, _key)


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


def _read_paper_bets():
    """读早盘观察库(paper_bets.json)记录。"""
    if not PAPER.exists():
        return []
    try:
        raw = json.loads(PAPER.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return raw.get("bets", []) if isinstance(raw, dict) else raw


def _read_live_paper_bets():
    """读滚球观察库(live_paper_bets.json)记录。"""
    if not LIVE_PAPER.exists():
        return []
    try:
        raw = json.loads(LIVE_PAPER.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return raw.get("bets", []) if isinstance(raw, dict) else raw


def load_observe_winrate():
    """观察库赢率 vs 隐含(合并滚球+早盘纸面结算): {(sport,league,sub_market): {n,winrate,implied}}。

    判据(2026-09-12 用户要求): 赢率 = won/(won+lost) 去 void/push; 隐含 = 1/平均赔率。
    滚球 league 归 "滚球"; sport 数字→英文; sub(over_under/handicap/opportunities)→sub_market。
    """
    by = defaultdict(lambda: {"won": 0, "lost": 0, "odds_sum": 0.0})

    def _feed(k, result, odds):
        if result not in ("won", "lost") or not odds or odds <= 1.0:
            return
        d = by[k]
        if result == "won":
            d["won"] += 1
        else:
            d["lost"] += 1
        d["odds_sum"] += odds

    for b in _read_paper_bets():
        _feed((b.get("sport") or "?", (b.get("league") or "").strip() or "?",
               b.get("sub_market") or "?"), b.get("result"), _f(b.get("bb_odds")))

    for b in _read_live_paper_bets():
        sport = BB_SPORT_MAP.get(b.get("sport"))
        sm = BB_SUB_MAP.get(b.get("sub"))
        if not sport or not sm:
            continue
        _feed((sport, LIVE_LEAGUE, sm), b.get("result"), _f(b.get("bb_odds")))

    out = {}
    for k, d in by.items():
        n = d["won"] + d["lost"]
        if n == 0:
            continue
        avg_odds = d["odds_sum"] / n
        out[k] = {
            "n": n,
            "winrate": d["won"] / n * 100.0,
            "implied": 1.0 / avg_odds * 100.0,
        }
    return out


def load_live_real_roi():
    """滚球实盘 ROI: BB 官方已结算订单(isSettled=true, uwl) 滚球部分(mt>bt), 按(运动,盘口)聚合。

    mgn 映射: "大/小"→ou, "让球"→hc, "独赢"→1x2; sid 用 BB_SPORT_MAP。
    调 BB API 可能失败(token 过期/网络), 失败返回空(提额判定保守回退 cap=150)。
    """
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from scripts.daily_review import _fetch_settled_orders
        orders = _fetch_settled_orders() or []
    except Exception:
        return {}
    _mgn_map = {"大/小": "ou", "让球": "hc", "独赢": "1x2"}
    agg = defaultdict(lambda: {"n": 0, "stake": 0.0, "profit": 0.0})
    for o in orders:
        op = (o.get("ops") or [{}])[0]
        if o.get("mt", 0) <= op.get("bt", 0):  # 只要滚球(下单晚于开赛)
            continue
        sport = BB_SPORT_MAP.get(op.get("sid"))
        sm = _mgn_map.get(op.get("mgn"))
        if not sport or not sm:
            continue
        try:
            stake = float(o.get("sat", 0))
            profit = float(o.get("uwl", 0))
        except (TypeError, ValueError):
            continue
        d = agg[(sport, sm)]
        d["n"] += 1
        d["stake"] += stake
        d["profit"] += profit
    return {k: {"n": d["n"], "roi": (d["profit"] / d["stake"] * 100.0) if d["stake"] > 0 else 0.0}
            for k, d in agg.items()}


def _load_obs_state():
    """读释放状态(首次释放时间 + cap)。文件不存在返回空。"""
    if not OBS_STATE.exists():
        return {}
    try:
        return json.loads(OBS_STATE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_obs_state(state):
    tmp = OBS_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(OBS_STATE)


def main():
    market_roi, league_roi = load_real_roi()
    dir_roi = load_real_direction_roi()
    dir_window_roi = load_real_direction_window_roi()
    obs_mkt_roi = load_observe_roi_market()
    obs_winrate = load_observe_winrate()

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

    # 方向级 EV 门槛(数据驱动, 替代 bb_ev_push 硬编码 DIRECTION_MIN_EV):
    # 只对"释放的方向"(整盘释放盘口的正方向 + 方向级释放)设门槛, ROI 越正门槛越低(下限3%职业底线)。
    direction_min_ev = []
    released_dir_set = {tuple(x) for x in direction_released}
    for (sport, sm, dr), d in sorted(dir_roi.items()):
        if d["roi"] <= 0:
            continue
        if (sport, sm) not in released_set and (sport, sm, dr) not in released_dir_set:
            continue  # 既没整盘释放也没方向释放 → 不设门槛(不投)
        thr = _dir_threshold(d["roi"], d["n"])
        if thr is not None:
            direction_min_ev.append([sport, sm, dr, thr])

    # 时间窗级封杀(2026-09-05): 方向级 ROI 仍掩盖时间窗分化 —— 1x2平 近场-47.5% / hc客 临场-11.9%
    # 都是负的, 但被"平整体+17.1%"平均掉。已释放方向里, 某时间窗实盘 ROI 强负的封杀该时间窗。
    direction_window_blocked = []
    for (sport, sm, dr, w), d in sorted(dir_window_roi.items()):
        if d["n"] < DIR_N_MIN:
            continue
        # 只有"该方向是释放的"(整盘释放 或 方向级释放)才需要时间窗级封杀
        if (sport, sm) not in released_set and (sport, sm, dr) not in released_dir_set:
            continue
        if d["roi"] < DIR_ROI_MIN:
            direction_window_blocked.append([sport, sm, dr, w])

    # 时间窗级释放(2026-09-07): 方向整体没释放, 但某时间窗实盘 ROI 强正(如 近场ht+53% / 远场hc+53%),
    # 单独释放该时间窗 —— "时间窗×盘口"才是真 edge 的精确颗粒度(整盘/方向级 ROI 会把时间窗分化平均掉)。
    direction_window_released = []
    for (sport, sm, dr, w), d in sorted(dir_window_roi.items()):
        if d["n"] < DIR_N_MIN:
            continue
        # 只有"该方向既没整盘释放也没方向级释放"时, 才需要时间窗级释放(已释放的无需重复)
        if (sport, sm) in released_set or (sport, sm, dr) in released_dir_set:
            continue
        # 观察库交叉验证护栏(防高赔少数命中的假正)
        o = obs_mkt_roi.get((sport, sm))
        if o and o["n"] >= OBS_CROSS_N_MIN and o["roi"] < OBS_CROSS_ROI_MIN:
            continue
        if d["roi"] > REAL_ROI_MIN:
            direction_window_released.append([sport, sm, dr, w])

    league_released = []
    league_blocked = []
    for (sport, lg, sm), d in sorted(league_roi.items()):
        if d["n"] >= N_REAL_MIN:
            if d["roi"] > REAL_ROI_MIN:
                league_released.append([sport, lg, sm])
            else:
                league_blocked.append([sport, lg, sm])

    # 观察库释放(2026-09-12 用户要求): 结算样本 n>100 且 赢率>隐含 → 释放。
    # 判据从 CLV 三条件改为「赢率 vs 隐含」(CURRENT_STATUS 核心判据), 滚球+早盘合并。
    observe_released = []
    for (sport, lg, sm), d in sorted(obs_winrate.items()):
        if d["n"] < OBS_N_MIN:
            continue
        if d["winrate"] > d["implied"] + OBS_WINRATE_EDGE_MIN:
            observe_released.append([sport, lg, sm])

    # 释放状态维护 + 投注额 cap 分阶段(用户要求): 新释放 150 → 实盘满 7 天 ROI>4% → 300。
    # 状态持久化到 observe_release_state.json, 跨运行保留 first_released_at(重新释放才重置)。
    obs_state = _load_obs_state()
    now_ts = datetime.now().timestamp()
    live_real_roi = load_live_real_roi()
    observe_release_caps = {}
    new_state = {}
    for (sport, lg, sm) in observe_released:
        key = f"{sport}|{lg}|{sm}"
        prev = obs_state.get(key)
        first = (prev or {}).get("first_released_at")
        cap = (prev or {}).get("cap", OBS_CAP_NEW)
        if not first:
            first = datetime.now().isoformat()
            cap = OBS_CAP_NEW
        else:
            try:
                first_ts = datetime.fromisoformat(str(first)).timestamp()
            except (ValueError, TypeError):
                first_ts = now_ts
            if now_ts - first_ts >= OBS_MATURE_DAYS * 86400:
                # 满一周: 看实盘 ROI(早盘用 tracked_bets 两维, 滚球用 BB 官方订单两维)
                if lg == LIVE_LEAGUE:
                    r = live_real_roi.get((sport, sm))
                else:
                    r = market_roi.get((sport, sm))
                if r and r.get("n", 0) > 0 and r.get("roi", 0) > REAL_ROI_MIN:
                    cap = OBS_CAP_MATURE
        new_state[key] = {"first_released_at": first, "cap": cap}
        observe_release_caps[key] = cap
    _save_obs_state(new_state)

    out = {
        "generated_at": datetime.now().isoformat(),
        "market_released": market_released,
        "league_released": league_released,
        "league_blocked": league_blocked,
        "observe_released": observe_released,
        "observe_release_caps": observe_release_caps,
        "direction_released": direction_released,
        "direction_blocked": direction_blocked,
        "direction_min_ev": direction_min_ev,
        "direction_window_blocked": direction_window_blocked,
        "direction_window_released": direction_window_released,
        "sport_cn": SPORT_CN,
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(OUT)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


if __name__ == "__main__":
    main()
