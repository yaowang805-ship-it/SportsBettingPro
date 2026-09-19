#!/usr/bin/env python3
"""盘口释放清单计算 — 真实结算赢率 vs 隐含 + 早盘收盘线 CLV 闸门。

核心思想:
    比价套利的"哪些盘口能投"看两层: 真实结算赢率是否 > 隐含, 且早盘格子收盘线 CLV 必须为正。

    2026-09-15 用户纠正(重要): 早盘「收盘线 CLV」是职业团队唯一最看重的 edge 指标, 不能
    用「赢率 vs Pin下注时价隐含」去推翻它 —— 那个隐含是 Pin 下注时价, 自带 favorite-longshot
    bias(冷门虚高/热门虚低), 差值会被偏差污染。负 CLV = 逆向选择(线朝你反向走), 即使
    「赢率>下注时隐含」为正也可能是 Pin 自身偏差造的假象。故加 CLV 闸门: 早盘格子中位
    true_clv_pct < 0 一律不释放(见 live-clv-capture-rate-fix-20260915)。
    注: 旧头「CLV 假正」结论只针对滚球「稍后价 CLV」(软书滞后), 不适用早盘「收盘线 CLV」。

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
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
TRACKED = DATA / "tracked_bets.json"
PAPER = DATA / "paper_bets.json"
OUT = DATA / "market_release.json"

REAL_ROI_MIN = 4.0     # 实盘 ROI 释放阈值(%)
N_REAL_MIN = 50        # 实盘赢率采信最小样本量(2026-09-12 30→100→50: 实盘真金白银质量高, 50够; 观察库才要100)

# ── 2026-09-12 观察库释放改造(用户要求): 样本>100 且 赢率>隐含 才释放 ──
# 判据从「CLV中位>0 + 正率>55% + 纸面ROI>0」改为「赢率 > 隐含」(CURRENT_STATUS 核心判据),
# 样本门槛从 n≥30 提到 n>100。滚球+早盘观察库合并统计, 滚球 league 归 "滚球"。
# 2026-09-13: n>100 → n>200, 对齐职业 CLV 有效标准(CLV 从 n≥200 才统计有效, <100 是噪声)。
OBS_N_MIN = 200            # 观察库释放采信最小结算样本数(>200, 对齐职业CLV标准)
OBS_CLV_MIN = 2.0          # 早盘释放中位CLV阈值(>2%): 职业sharp选手平均+2~5% no-vig CLV,
                            # >0太松(+0.5%以下是devig/匹配噪声), >2 既严格又留容错(2026-09-15 用户定)
OBS_WINRATE_EDGE_MIN = 3.0 # 赢率>隐含(差>3pp 才释放)。2026-09-15 0→3pp: 观察库整体负edge(逆向选择),
                            # 差0pp会把打平的噪声格子释放; 对齐实盘 REAL_WINRATE_EDGE_MIN=3pp 去噪声
REAL_WINRATE_EDGE_MIN = 3.0  # 实盘主开关/方向释放赢率vs隐含差值阈值(差>3pp 去噪声)

# 放弃的特殊盘口(2026-09-15 用户决定): 这些盘口 margin 15%+ (vs 主盘口 3-8%), 收盘线不 sharp,
# 且 clv_collector 里 close_fair 用的是 proportional devig(非 Shin), CLV 虚高无意义。
# 套利团队放弃: 正确比分/半全场/先进球/精确进球/净胜球/总进球区间 + 角球(前缀 corner)。
SPECIAL_MARKETS = {'correct_score', 'correct_score_ht', 'htft', 'first_to_score',
                   'exact_goals_ht', 'winning_margin', 'total_goals_range'}
SPECIAL_MARKET_PREFIX = ('corner',)
# 实盘数据切分点(2026-09-12 用户要求): 实盘释放只用「重收后」的干净数据, 不用之前被结算bug
# (ms=7漏判/league_cn对不上)污染的旧数据。切分点=观察库重收日 9-10。
REAL_DATA_CUTOFF_TS = datetime.fromisoformat("2026-09-10T00:00:00+00:00").timestamp()
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

# 观察库释放粒度(2026-09-12 用户要求): 从「运动×联赛×盘口」改为「运动×盘口×方向」+
# 来源(早盘/滚球分开, 因滚球小球真溢价 vs 早盘小球假溢价方向相反不能混)。
# 早盘聚合所有联赛(样本易凑100), 方向归一化(ou大/小、hc主/客分开)。
SCOPE_EARLY = "early"   # 早盘观察库(paper_bets, 聚合所有联赛)
SCOPE_LIVE = "live"     # 滚球观察库(live_paper_bets)

# 投注额分阶段上限(用户要求): 新释放 150, 实盘满一周 ROI>4% 提 300
OBS_CAP_NEW = 150
OBS_CAP_MATURE = 300
OBS_MATURE_DAYS = 7        # 实盘投一周(7天)后评估提额
# 新释放盘口当日累计投注额上限(2026-09-12 用户要求): 当天总投注额≤2000(2026-09-15 1000→2000), 次日实盘ROI>4% 解除
DAILY_STAKE_LIMIT = 1000  # 2026-09-18 新数据源风控: 早盘每天投注≤1000, 原2000

# 已验证方向(硬编码真edge, 2026-09-13 统一进观察库释放机制): grandfathered 进 observe_released,
# 固定 cap(不走 150→300 分阶段), 无当日累计上限(limit_removed 恒 True)。之前散落在
# second_level_monitor._try_live_auto_bet 的硬编码 _bettable, 统一到这里单一事实来源。
# 赔率区间用 "*" 表示"所有区间"(已验证方向不分区间的历史口径)。
MANUAL_OBSERVE_RELEASE = {
    # 2026-09-19 撤销小球2格释放: 之前「edge +4.8pp/+3.5pp 真edge」是低流动性联赛 Betfair 薄盘
    # 漂出来的假溢价(今日实盘小球[2.0-2.3) 45注-31%ROI坐实)。撤销后交还数据驱动判据
    # (CLV>2%+n>200+赢率>隐含3pp), 待深挖补「Betfair流动性门槛」后再由判据自动决定是否释放。
    # 大球 1.0-2.0 的"正盈亏"是注额加权假象(edge 实为 -2.3pp/-2.7pp), 放 BLOCK 拦截。
    "football|hc|主|*|live": 150,   # 2026-09-19 用户打开让球主胜, 每单封顶150
    "football|hc|客|*|live": 150,   # 2026-09-19 用户打开让球客胜, 每单封顶150
}

# 手动拦截的赔率区间(用户明确要求): 数据驱动「方向×赔率区间」按 edge(赢率vs隐含) 硬编码拦截(2026-09-19)。
# 优先于 MANUAL_OBSERVE_RELEASE 的区间。数据驱动拦截(observe_blocked)照常追加。
MANUAL_OBSERVE_BLOCK = {
    "football|1x2|主|>5.0|live",      # 主胜冷门 实盘 ROI -65.8%
    "football|1x2|主|3.0-5.0|live",   # 主胜 3.0-5.0 edge -3.3pp
    "football|1x2|客|3.0-5.0|live",   # 客胜 3.0-5.0 edge -3.3pp
    "football|1x2|客|>5.0|live",      # 客胜 >5.0 edge -2.2pp(8%胜率)
    "football|1x2|平|3.0-5.0|live",   # 和局 3.0-5.0 edge -5.5pp(头号巨亏)
    "football|1x2|平|>5.0|live",      # 和局 >5.0 edge -2.2pp
    "football|ou|大|1.0-1.5|live",    # 大球 1.0-1.5 edge -2.3pp(72%<74%隐含, 假edge)
    "football|ou|大|1.5-2.0|live",    # 大球 1.5-2.0 edge -2.7pp(59%<61%隐含, 假edge)
    "football|ou|大|2.0-3.0|live",    # 大球 2.0-3.0 edge -3.8pp
    "football|ou|大|3.0-5.0|live",    # 大球 3.0-5.0 edge -13.4pp(17%胜率)
    "football|ou|小|1.5-2.0|live",    # 小球 1.5-2.0 edge -7.6pp(负格子)
    # 2026-09-19 撤销让球3格拦截: 旧pin口径edge负(-2.7/-6.7/-18.5pp)是锚点切换前的假象,
    # betfair口径下让球主胜2.0-3.0赢率49%/客胜2.0-3.0赢率55%为正, 用户已打开让球方向(cap150)
    # 早盘(保留旧拦截)
    "football|ht|客|3.0-5.0|early",
    "football|1x2|平|3.0-5.0|early",
    "football|ht_dc|客|1.0-2.0|early",
}

# 手动释放 + 当日累计上限(2026-09-15 用户要求): 释放的是"有希望的格子"试探, 单注≤150, 当日累计≤2000。
# 2026-09-15 清空: 原 dc 早盘 1.0-3.0 是旧判据(赢率vs隐含)放的, 但 dc 在 CLV 口径下中位为负
# (-0.52%), 与「CLV唯一标准」矛盾, 撤掉交还数据驱动(负 CLV 不会释放)。
MANUAL_OBSERVE_RELEASE_LIMITED = {}

DIR_N_MIN = 30              # 方向级赢率采信最小样本量(2026-09-12 15→50→30: 实盘方向样本, 30够)


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


def _odds_interval(odds):
    """BB 赔率 → 赔率区间标签(1.0-1.5/1.5-2.0/2.0-3.0/3.0-5.0/>5.0)。

    2026-09-13 用户要求: 释放/拦截粒度加赔率区间维度。favorite-longshot bias
    (冷门被高估) 使同一盘口不同赔率区间 edge 分化巨大, 必须分区间判断。
    2026-09-19 4档改5档: 1.0-2.0 拆成 1.0-1.5/1.5-2.0(实测小球 1.0-1.5 是正edge +3.5pp,
    而 1.5-2.0 是负edge -7.6pp, 合并会掩盖分化)。
    """
    if odds is None or odds <= 1.0:
        return "?"
    if odds < 1.5:
        return "1.0-1.5"
    if odds < 2.0:
        return "1.5-2.0"
    if odds < 3.0:
        return "2.0-3.0"
    if odds < 5.0:
        return "3.0-5.0"
    return ">5.0"


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


def _dir_threshold(edge, n):
    """方向赢率vs隐含差值 → EV 门槛(2026-09-12 改赢率: 数据驱动, 替代 ROI 版)。

    下限 3.0 = 职业底线(历史教训: <3% 会放行大量临场漂移假机会)。
    赢率差越大门槛越低(充分信任), 样本越少越保守。
    """
    if edge >= 20.0:
        thr = 3.0
    elif edge >= 10.0:
        thr = 4.0
    elif edge >= 5.0:
        thr = 5.0
    else:
        thr = 6.0
    # 样本置信度折扣: n<30 太不可信不设方向门槛(回退基础), n<50 加保守折扣
    if n < 30:
        return None
    if n < 50:
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
    """实盘 ROI: tracked_bets.json settled 记录 → {(sport,sub_market): agg}。

    仅用于「满7天提额」的实盘 ROI 判定。league/方向/时间窗 ROI 已随判据统一
    「赢率 vs 隐含」废弃(2026-09-12)。
    """
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "betfair"]
    return _agg_bets(bets, lambda b: (b.get("sport") or "?", b.get("sub_market") or "?"))


def load_real_roi_direction():
    """实盘 ROI(方向级): tracked_bets.json settled → {(sport,sub_market,direction): {n,roi}}。

    2026-09-18 补: 「满7天提额」原来用 (sport,sub_market) 两级 ROI, 会把同盘口下
    方向相反的方向混在一起(如 ht 主胜 +31.7% 被 ht 客胜 -38.9% 拉低), 导致真赚的
    方向永远提不了额。改按 (sport,sub_market,direction) 三级, 让提额看方向级 ROI。
    """
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "betfair"]
    return _agg_bets(bets, lambda b: (
        b.get("sport") or "?", b.get("sub_market") or "?",
        _direction(b.get("designation"), b.get("sub_market"))))


def _winrate_threshold(n):
    """赢率vs隐含差值阈值(2026-09-12 按统计显著): 样本越少要求差越大。

    标准误 SE≈0.5/sqrt(n)(p≈0.5 二项分布), 差要 ≥1 SE 才不算纯噪声。
    n=100→5pp, n=50→7pp, 下限 REAL_WINRATE_EDGE_MIN=3pp。
    """
    se = 50.0 / math.sqrt(n) if n > 0 else 100.0
    return max(REAL_WINRATE_EDGE_MIN, se)


def _winrate_agg(bets, key_fn):
    """按 key 聚合实盘赢率 vs 隐含: 赢率=won/(won+lost) 去 void/push; 隐含=平均(1/Pin公平价)。

    2026-09-12 修正隐含基准: 之前用 bb_odds(软书价)算隐含, 判的是「BB价是否+EV」;
    但「真溢价」的定义是「真实赢率 > Pin公平价隐含」(Pin是sharp基准, Pin低估才是市场错误定价)。
    用 BB 赔率算隐含会高估 edge 1~4pp, 把「BB自己定价粗」误判成真溢价(假溢价源头)。改用 fair_price。
    2026-09-13 修口径: 隐含必须是 mean(1/fair), 不是 1/mean(fair)。1/x 是凸函数,
    1/mean(fair) < mean(1/fair), 旧口径系统性低估隐含、高估 edge(赔率方差越大虚高越多,
    1x2 独赢冷门多实测虚高 +13.5pp), 会误放行「BB定价粗」假溢价。
    """
    by = defaultdict(lambda: {"won": 0, "lost": 0, "inv_sum": 0.0})
    for b in bets:
        r = b.get("result")
        if r not in ("won", "lost"):
            continue
        # 时间切分(2026-09-12): 只用重收后的干净实盘数据, 切掉之前被结算bug污染的旧样本
        _pt = _ts(b.get("push_time"))
        if _pt is not None and _pt < REAL_DATA_CUTOFF_TS:
            continue
        o = _f(b.get("fair_price")) or _f(b.get("bb_odds")) or 0
        if o <= 1.0:
            continue
        k = key_fn(b)
        if k is None:
            continue
        if r == "won":
            by[k]["won"] += 1
        else:
            by[k]["lost"] += 1
        by[k]["inv_sum"] += 1.0 / o
    out = {}
    for k, d in by.items():
        n = d["won"] + d["lost"]
        if n == 0:
            continue
        out[k] = {
            "n": n,
            "winrate": d["won"] / n * 100.0,
            "implied": d["inv_sum"] / n * 100.0,
        }
    return out


def load_real_winrate_market():
    """实盘赢率 vs 隐含(两维): tracked_bets.json settled → {(sport,sub_market): {n,winrate,implied}}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "betfair"]
    return _winrate_agg(bets, lambda b: (b.get("sport") or "?", b.get("sub_market") or "?"))


def load_real_winrate_direction():
    """实盘赢率 vs 隐含(方向级): tracked_bets.json settled → {(sport,sub_market,direction): {n,winrate,implied}}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "betfair"]
    return _winrate_agg(bets, lambda b: (
        b.get("sport") or "?", b.get("sub_market") or "?",
        _direction(b.get("designation"), b.get("sub_market"))))


def load_real_winrate_league():
    """实盘赢率 vs 隐含(联赛三维): tracked_bets settled → {(sport,league,sub_market): {n,winrate,implied}}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "betfair"]
    return _winrate_agg(bets, lambda b: (
        b.get("sport") or "?",
        (b.get("league") or "").strip() or "?",
        b.get("sub_market") or "?"))


def load_real_winrate_direction_window():
    """实盘赢率 vs 隐含(方向×时间窗): tracked_bets settled → {(sport,sub_market,direction,window): {n,winrate,implied}}。"""
    if not TRACKED.exists():
        return {}
    try:
        raw = json.loads(TRACKED.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    bets = [b for b in (raw.get("bets", []) if isinstance(raw, dict) else raw)
            if b.get("status") == "settled" and b.get("anchor") == "betfair"]

    def _key(b):
        w = _window(_f(b.get("match_epoch")), _ts(b.get("push_time")))
        if w is None:
            return None
        return (b.get("sport") or "?", b.get("sub_market") or "?",
                _direction(b.get("designation"), b.get("sub_market")), w)

    return _winrate_agg(bets, _key)


def _read_paper_bets():
    """读早盘观察库(paper_bets.json)记录(只 Betfair 口径, 2026-09-19 锚点分区)。"""
    if not PAPER.exists():
        return []
    try:
        raw = json.loads(PAPER.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    bets = raw.get("bets", []) if isinstance(raw, dict) else raw
    return [b for b in bets if b.get("anchor") == "betfair"]


def _read_live_paper_bets():
    """读滚球观察库(live_paper_bets.json)记录(只 Betfair 口径, 2026-09-19 锚点分区)。"""
    if not LIVE_PAPER.exists():
        return []
    try:
        raw = json.loads(LIVE_PAPER.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    bets = raw.get("bets", []) if isinstance(raw, dict) else raw
    return [b for b in bets if b.get("anchor") == "betfair"]


def load_observe_winrate():
    """观察库赢率 vs 隐含(合并滚球+早盘纸面结算): {(sport,sub_market,direction,odds_interval,scope): {n,winrate,implied,roi}}。

    判据(2026-09-12): 赢率 = won/(won+lost) 去 void/push; 隐含 = 平均(1/赔率)。
    粒度「运动×盘口×方向×赔率区间×来源」: 早盘聚合所有联赛(scope=early), 滚球(scope=live);
    方向用 _direction 归一化; 赔率区间用 _odds_interval(BB赔率分4档)。
    """
    by = defaultdict(lambda: {"won": 0, "lost": 0, "inv_sum": 0.0, "stake": 0.0, "profit": 0.0})

    def _feed(k, result, odds, stake, profit):
        if result not in ("won", "lost") or not odds or odds <= 1.0:
            return
        d = by[k]
        if result == "won":
            d["won"] += 1
        else:
            d["lost"] += 1
        d["inv_sum"] += 1.0 / odds
        d["stake"] += stake
        d["profit"] += profit

    for b in _read_paper_bets():
        sm = b.get("sub_market") or "?"
        _iv = _odds_interval(_f(b.get("bb_odds")))
        _feed((b.get("sport") or "?", sm, _direction(b.get("designation"), sm), _iv, SCOPE_EARLY),
              b.get("result"), _f(b.get("fair_price")) or _f(b.get("bb_odds")),
              _f(b.get("stake")) or 0, _f(b.get("profit")) or 0)

    for b in _read_live_paper_bets():
        sport = BB_SPORT_MAP.get(b.get("sport"))
        sm = BB_SUB_MAP.get(b.get("sub"))
        if not sport or not sm:
            continue
        _iv = _odds_interval(_f(b.get("bb_odds")))
        _feed((sport, sm, _direction(b.get("designation"), sm), _iv, SCOPE_LIVE),
              b.get("result"), _f(b.get("fair")) or _f(b.get("bb_odds")),
              _f(b.get("stake")) or 0, _f(b.get("profit")) or 0)

    out = {}
    for k, d in by.items():
        n = d["won"] + d["lost"]
        if n == 0:
            continue
        out[k] = {
            "n": n,
            "winrate": d["won"] / n * 100.0,
            "implied": d["inv_sum"] / n * 100.0,
            "roi": d["profit"] / d["stake"] * 100.0 if d["stake"] > 0 else 0.0,
        }
    return out


def load_clv_median():
    """早盘收盘线 CLV 中位+n(运动×盘口×方向×赔率区间): clv_results.csv 的 true_clv_pct。

    2026-09-15 用户最终决定: CLV(打不打得过 Pin 收盘价)是职业团队唯一最看重的 edge 指标,
    释放判据改为「只看正 CLV + n>200」, 替换掉「赢率 vs Pin下注时价隐含 + ROI」——
    那个隐含带 favorite-longshot bias, 差值被偏差污染, 会把「热门假edge」当真。

    true_clv_pct = (BB下注价 - Pin收盘公平价) / Pin收盘公平价, 隐含基准就是「收盘价」
    (不是 Pin 下注时价), 所以这套判据同时落地「隐含用收盘价」这条铁律(正 CLV = 下注价打过收盘价)。
    滚球(scope=live)无收盘线 CLV, 此判据只对 scope=early 生效。

    Returns: {(sport, sub_market, direction, odds_interval): (median_clv, n)}
    """
    import csv as _csv, statistics as _st
    f = DATA / "clv_results.csv"
    if not f.exists():
        return {}
    by = defaultdict(list)
    with open(f, encoding="utf-8-sig") as fh:
        for r in _csv.DictReader(fh):
            try:
                clv = float(r.get("true_clv_pct") or 0)
                odds = float(r.get("bb_odds") or 0)
            except (ValueError, TypeError):
                continue
            key = (r.get("sport") or "?", r.get("sub_market") or "?",
                   _direction(r.get("designation"), r.get("sub_market")), _odds_interval(odds))
            by[key].append(clv)
    return {k: (_st.median(v), len(v)) for k, v in by.items() if v}


def load_live_clv():
    """滚球 LEV(live_paper_bets 的 clv 字段, 下注后复验 Pin 公平价算的 CLV)按(运动×盘口×方向×赔率区间)聚合。

    2026-09-15 用户定: 滚球和早盘一样用 CLV(LEV)判释放, 不用「赢率vs隐含」(带 favorite-longshot bias
    且向后看高方差)。LEV = 下注后复验 Pin 价, 正=抢到比市场后来定价更优的价格(真edge), 负=逆向选择。
    与 load_clv_median 区别: 早盘用 clv_results.csv(收盘线CLV), 滚球用 live_paper_bets 的 clv 字段(LEV)。
    注: clv 字段今天(9-15)才修好采集率(60s→3s), 样本极薄, 短期 n<200 不会释放, 等积累。

    Returns: {(sport, sub_market, direction, odds_interval): (median_clv, n)}
    """
    import statistics as _st
    by = defaultdict(list)
    for b in _read_live_paper_bets():
        clv = b.get("clv")
        if clv is None:
            continue
        try:
            clv = float(clv)
        except (ValueError, TypeError):
            continue
        sport = BB_SPORT_MAP.get(b.get("sport"))
        sm = BB_SUB_MAP.get(b.get("sub"))
        if not sport or not sm:
            continue
        _iv = _odds_interval(_f(b.get("bb_odds")))
        by[(sport, sm, _direction(b.get("designation"), sm), _iv)].append(clv)
    return {k: (_st.median(v), len(v)) for k, v in by.items() if v}


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


def _notify_release(newly_released, obs_winrate):
    """新释放盘口推钉钉通知(2026-09-12 用户要求)。"""
    try:
        from config.settings import send_dingtalk
    except Exception:
        return
    lines = []
    for (sport, sm, dr, interval, scope) in newly_released:
        sp_cn = SPORT_CN.get(sport, sport)
        scope_cn = "滚球" if scope == SCOPE_LIVE else "早盘"
        lines.append(
            f"{sp_cn} {sm}-{dr}({interval},{scope_cn}) | 单注≤{OBS_CAP_NEW} | 当日累计≤{DAILY_STAKE_LIMIT}")
    body = "🆕 新释放盘口\n\n" + "\n".join(lines)
    try:
        send_dingtalk("🆕 新释放盘口", body)
    except Exception:
        pass


def main():
    market_roi = load_real_roi()
    market_roi_dir = load_real_roi_direction()  # 方向级 ROI(满7天提额用, 2026-09-18 修混方向bug)
    obs_winrate = load_observe_winrate()
    clv_med = load_clv_median()  # 早盘收盘线 CLV 中位(释放闸门, 2026-09-15)
    live_clv = load_live_clv()   # 滚球 LEV(下注后复验Pin价的CLV, 释放闸门, 2026-09-15)

    # 2026-09-19 用户要求: 释放盘口不用实盘投注数据(不释放就没实盘=死循环), 只用观察库。
    # 实盘释放通道(主开关/方向/时间窗/联赛)废弃, 全设空; 实盘 ROI 仅保留「满7天提额」。
    market_released = []
    direction_released = []
    direction_blocked = []
    direction_min_ev = []
    direction_window_blocked = []
    direction_window_released = []
    league_released = []
    league_blocked = []

    # 观察库释放(2026-09-12 用户要求): 结算样本 n>100 且 赢率>隐含 → 释放。
    # 粒度「运动×盘口×方向×赔率区间×来源」(2026-09-13 加赔率区间): 早盘聚合所有联赛(scope=early),
    # 滚球(scope=live); 方向用 _direction 归一化, 赔率区间用 _odds_interval 分4档。
    # 每个赔率区间独立判据: n>200 + 赢率>隐含 + ROI>0 → 释放; 否则 → 拦截(表现差不投)。
    observe_released = []
    observe_blocked = []
    # 早盘(scope=early): CLV>2% + n>200 是唯一标准(2026-09-15 用户最终决定), 替换掉赢率vs隐含+ROI。
    # 特殊盘口(margin 15%+, 收盘线不 sharp, CLV 无意义)直接放弃, 不进清单。
    for (sport, sm, dr, interval), (med, n) in sorted(clv_med.items()):
        if sm in SPECIAL_MARKETS or sm.startswith(SPECIAL_MARKET_PREFIX):
            continue
        # 足球冷门封顶(2026-09-15): favorite-longshot bias 使足球冷门系统性高估, 真实赢率全负
        # (CLV 正是 devig 残差, 非真 edge)。足球 >5.0 不用 CLV 释放。冰球/棒球有 reverse bias,
        # 不套用此封顶(等那俩运动攒够样本单独判)。
        if sport == "football" and interval == ">5.0":
            observe_blocked.append([sport, sm, dr, interval, "early"])
            continue
        _roi = obs_winrate.get((sport, sm, dr, interval, "early"), {}).get("roi", 0.0)
        if med > OBS_CLV_MIN and n >= OBS_N_MIN and _roi > 0:
            observe_released.append([sport, sm, dr, interval, "early"])
        else:
            observe_blocked.append([sport, sm, dr, interval, "early"])
    # 滚球(scope=live): 2026-09-15 用户定, 和早盘一样用 CLV(LEV)判释放, 不用赢率vs隐含(带bias且向后看)。
    # LEV = 下注后复验 Pin 价算的 CLV, 正=真edge, 负=逆向选择。clv 字段今天才修好采集率(60s→3s),
    # 样本极薄, 短期 n<200 不会释放, 等积累(释放清单暂时由 MANUAL_OBSERVE_RELEASE 的滚球方向撑)。
    # MANUAL_OBSERVE_RELEASE 的「已验证方向」是用户显式开放的(如小球全区间), 数据驱动的 LEV 拦截
    # 对它们不适用(LEV 薄样本 n<200 会误拦已验证方向, 与用户「全量打开」冲突)。
    _manual_dirs = set()
    for _mkey in MANUAL_OBSERVE_RELEASE:
        _p = _mkey.split("|")
        _manual_dirs.add((_p[0], _p[1], _p[2]))  # (sport, sub_market, direction)
    for (sport, sm, dr, interval), (med, n) in sorted(live_clv.items()):
        if (sport, sm, dr) in _manual_dirs:
            continue  # 已验证方向, 数据驱动不拦(用户显式开放, 全区间)
        _roi = obs_winrate.get((sport, sm, dr, interval, "live"), {}).get("roi", 0.0)
        if med > OBS_CLV_MIN and n >= OBS_N_MIN and _roi > 0:
            observe_released.append([sport, sm, dr, interval, "live"])
        else:
            observe_blocked.append([sport, sm, dr, interval, "live"])

    # 已验证方向(硬编码真edge)统一进释放机制(2026-09-13): grandfathered, 不走 n>200 判据。
    for _mkey in MANUAL_OBSERVE_RELEASE:
        _p = _mkey.split("|")
        _entry = [_p[0], _p[1], _p[2], _p[3], _p[4]]
        if _entry not in observe_released:
            observe_released.append(_entry)

    # 手动释放 + 当日累计上限(2026-09-15 用户要求): dc 早盘 1.0-3.0 试探, 走日限额。
    for _mkey in MANUAL_OBSERVE_RELEASE_LIMITED:
        _p = _mkey.split("|")
        _entry = [_p[0], _p[1], _p[2], _p[3], _p[4]]
        if _entry not in observe_released:
            observe_released.append(_entry)

    # 手动拦截的赔率区间(用户明确要求, 2026-09-13): 已验证方向里表现巨差的区间, 优先于"*"释放。
    for _mkey in MANUAL_OBSERVE_BLOCK:
        _p = _mkey.split("|")
        _entry = [_p[0], _p[1], _p[2], _p[3], _p[4]]
        if _entry not in observe_blocked:
            observe_blocked.append(_entry)

    # 释放状态维护 + 投注额 cap 分阶段 + 当日累计上限 + 释放通知(2026-09-12 用户要求)。
    # 状态持久化到 observe_release_state.json: first_released_at/cap/daily_stake/limit_removed。
    obs_state = _load_obs_state()
    now_ts = datetime.now().timestamp()
    today = datetime.now().strftime("%Y-%m-%d")
    live_real_roi = load_live_real_roi()
    observe_release_caps = {}
    new_state = {}
    newly_released = []  # 新释放的盘口(推钉钉通知)
    for (sport, sm, dr, interval, scope) in observe_released:
        key = f"{sport}|{sm}|{dr}|{interval}|{scope}"
        _manual = MANUAL_OBSERVE_RELEASE.get(key)  # 已验证方向(固定cap+无日限额, 不走分阶段)
        _manual_limited = MANUAL_OBSERVE_RELEASE_LIMITED.get(key)  # 手动释放+有日限额(固定cap, 不走150→300提额)
        prev = obs_state.get(key)
        first = (prev or {}).get("first_released_at")
        cap = _manual if _manual else (_manual_limited if _manual_limited else (prev or {}).get("cap", OBS_CAP_NEW))
        daily_stake = (prev or {}).get("daily_stake", 0)
        daily_date = (prev or {}).get("daily_stake_date", "")
        limit_removed = True if _manual else (prev or {}).get("limit_removed", False)
        if not first:
            first = datetime.now().isoformat()
            if not _manual and not _manual_limited:
                cap = OBS_CAP_NEW
                newly_released.append([sport, sm, dr, interval, scope])
        elif not _manual and not _manual_limited:
            try:
                first_ts = datetime.fromisoformat(str(first)).timestamp()
            except (ValueError, TypeError):
                first_ts = now_ts
            if now_ts - first_ts >= OBS_MATURE_DAYS * 86400:
                # 满一周: 看实盘 ROI(滚球用 BB 官方订单两维, 早盘用 tracked_bets 两维)
                r = (live_real_roi.get((sport, sm)) if scope == SCOPE_LIVE
                     else market_roi_dir.get((sport, sm, dr)))
                if r and r.get("n", 0) > 0 and r.get("roi", 0) > REAL_ROI_MIN:
                    cap = OBS_CAP_MATURE
            # 次日解除 1000 限制(2026-09-12 用户要求): 实盘 ROI>4% → 解除当日累计上限
            # 不用加实盘样本门槛: 观察库释放已用 n>100 测过真溢价, 释放即已验证, 实盘 ROI>4% 就解除。
            if not limit_removed:
                r = (live_real_roi.get((sport, sm)) if scope == SCOPE_LIVE
                     else market_roi_dir.get((sport, sm, dr)))
                if r and r.get("n", 0) > 0 and r.get("roi", 0) > REAL_ROI_MIN:
                    limit_removed = True
        # 当日累计跨天重置
        if daily_date != today:
            daily_stake = 0
            daily_date = today
        new_state[key] = {"first_released_at": first, "cap": cap,
                          "daily_stake": daily_stake, "daily_stake_date": daily_date,
                          "limit_removed": limit_removed}
        observe_release_caps[key] = cap
    _save_obs_state(new_state)
    # 释放通知: 新释放盘口推钉钉
    if newly_released:
        _notify_release(newly_released, obs_winrate)

    out = {
        "generated_at": datetime.now().isoformat(),
        "market_released": market_released,
        "league_released": league_released,
        "league_blocked": league_blocked,
        "observe_released": observe_released,
        "observe_blocked": observe_blocked,
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
