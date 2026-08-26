#!/usr/bin/env python3
"""数据驱动的 EV 门槛矩阵计算 — 每晚从 clv_results.csv 自动重算。

核心思想(全数据驱动模式):
    比价套利的 EV 门槛不能统一 2% —— 实测 2-3% 档 CLV 中位 -2.16%/正率 35%(显著负),
    而 8%+ 档 +9.25%/76%(显著正)。门槛必须按盘口/运动/时间分层, 且数据驱动。

判定标准(业界标准):
    一个格子 EV≥X 时, 只有当「正 CLV 率 ≥55% 且 中位 CLV > +2%」才认为 X 是该格子
    门槛; 否则抬高到满足为止。无任何档满足 → 停推(门槛=最高档 20%, 实质不推非封杀)。

样本纪律(shrinkage: 样本越少要求越高的中位):
    n≥100 确认(中位>2%); 30-100 方向性(中位>3%); <30 用保守高门槛回退(不拍脑袋放低)。

输出:
    data/storage/ev_threshold_matrix.json
    {
      "generated_at": "...",
      "markets": {"1x2": 5.0, "ou": 8.0, "ht": 2.0, ...},
      "sport_adjust": {"tennis": -0.5, "basketball": +1.0, ...},
      "league_adjust": {"football|MLS": 2.0, ...},
      "time_adjust": [{"min_minutes":0,"max_minutes":60,"label":"临场<1h","adjust":2.0}, ...],
      "main_markets": ["1x2","hc","ou"],   # 时间维度适用盘口
      "default_threshold": 8.0,  # 样本不足盘口的保守回退
    }

用法: .venv312/bin/python scripts/compute_ev_thresholds.py
"""
import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# 被过滤掉的不可执行样本计数(供报告用, 避免"静默丢弃"看不见)
_dropped = Counter()

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
RESULTS = DATA / "clv_results.csv"
OUT = DATA / "ev_threshold_matrix.json"

POS_RATE_MIN = 0.55     # 正 CLV 率下限
MEDIAN_CLV_MIN = 2.0    # 正期望最低中位 CLV 阈值(%): >2% 才算"强正" edge
                        # (业界标准: +2%~+4% 强/勉强, +4% 卓越。1~2% 只是勉强正, 不足以解锁低门槛)
SHRINK_MARGIN = 1.0     # 方向性档(30≤n<100)要求的中位 CLV 额外余量: 小样本点估计不可靠, 需更高中位补偿

# 样本纪律(业界标准: CLV 100-200 注才可靠, 早期 CLV 是噪声 —— 80注+5% 到 340注 会掉到 +1.8%)
#   n >= 100  → "确认",   中位 CLV > 2%          且 正率≥55%
#   30<=n<100 → "方向性", 中位 CLV > 2%+1%(shrink) 且 正率≥55% —— 小样本要更严
#   n < 30    → 样本不足, **一律回退保守高门槛(8%)**, 绝不据此放低
N_CONFIRMED = 100
N_DIRECTIONAL = 30

# 从**低到高**遍历, 找"负转正"边界档 = 门槛。
# 用户铁律(2026-08-21): 数据驱动 = 负期望的档就继续往上调, 直到正期望为止。
# 扩展搜索范围到 20%, 避免 hc 这种"8% 还是负"的盘口被错误停在 8%。
EV_STEPS = (2, 3, 5, 8, 10, 12, 15, 20)

# 时间维度: 距开赛分钟数分桶, 数据驱动调整门槛。
# 2026-08-25 改 3 窗(与扫描/下注窗口对齐: 临场<6h / 近6-24h / 早24-72h),
# 用于"时间窗×盘口"独立门槛; time_adjust 保留作样本不足格子的 fallback。
TIME_BUCKETS = (
    (0, 360, "临场<6h"),
    (360, 1440, "近6-24h"),
    (1440, 4320, "早24-72h"),
)

# 不可执行机会过滤 —— clv_results.csv 里 ~80% 是 source=validate(所有 ≥2%EV 机会),
# 并非真实投注。其中被封杀类别/超赔率上限的机会**永远不会被下注**, 拿它们算门槛
# 等于让门槛建立在不可执行的信号上(实测 1x2 中位 CLV +23.92% 几乎全由这类样本撑起)。
MAX_ODDS = 20.0         # 与 config/constants.py max_odds 一致: >20 全部负期望, 推送层已 _stake=0
# 样本不足时的回退门槛(= default_threshold)。宁可少推不可推错。
CONSERVATIVE_THRESHOLD = 8.0
BANNED_LEAGUE_RE = re.compile(
    r"ITF|Challenger|W15|W25|W35|W50|W75|W100|M15|M25|World Tennis", re.I)

# 运动层微调(相对盘口门槛的加/减)。
# 样本≥30 的运动由 compute_sport_adjust 按实测 CLV 重算覆盖 —— 不再拍脑袋。
# 以下仅是样本不足(n<30)时的**保守回退值**: 宁可更严, 不凭印象放低。
SPORT_ADJUST_FALLBACK = {
    "football": 0.0,
    "tennis": 0.0,        # 实测 -0.4% 负, 之前 -0.5 降门槛是拍脑袋, 回退改 0
    "basketball": 2.0,    # 实测 -3.03% 显著负, 回退保守更严
    "baseball": 1.0,
    "american_football": 0.0,
    "ice_hockey": 0.0,
    "mma": 2.0,           # MMA/拳击高风险, 更高门槛
    "boxing": 2.0,
}

# 主体盘口定义(时间维度调整只对主体盘口生效; 临场/早盘见 TIME_BUCKETS + compute_time_adjust)
MAIN_MARKETS = ("1x2", "hc", "ou")


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ts(v):
    """ISO 时间串 → epoch 秒; 解析失败返回 None。"""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def load_clean(mode="all"):
    """读取干净样本用于算门槛。

    注(2026-08-23): 用户最终目标是"门槛用真实投注(push)数据"。但现在 push 只有 ~300 条,
    分到各盘口样本不足(n<30)算不出可靠门槛。暂用全部干净样本(validate+push), 等 push 积累到
    足够样本(各盘口 n≥30)再切换到只用 push。validate 单独统计见 load_validate(不参与门槛)。

    mode: all=validate+push(线上口径) / push=只用真实投注 / validate=只用观察样本。
          push/validate 是**实验口径**, 结果默认只写 candidate 文件, 不覆盖线上矩阵
          (2026-08-23 有过一次 push-only 实验产物静默覆盖线上矩阵, 把 ht/ht_dc 从 2%
           抬到 8% —— 唯一被验证的正 edge 盘口被关掉, 真实投注库跟着停止积累)。
    """
    if not RESULTS.exists():
        return []
    out = []
    with open(RESULTS, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if (r.get("close_source") or "live").strip() == "archive_open":
                continue
            if mode != "all" and (r.get("source") or "push").strip() != mode:
                continue
            clv, ev, od = f(r.get("true_clv_pct")), f(r.get("push_ev_pct")), f(r.get("bb_odds"))
            if clv is None or ev is None or ev < 2:
                continue
            if od and ev > max(12.0, (od - 1) * 20):
                continue  # EV 超系统上限 = 上游坏价垃圾
            # 不可执行机会不参与门槛计算(推送层不会投注它们, 详见文件头注释)
            if od and od > MAX_ODDS:
                _dropped["odds>20"] += 1
                continue
            if BANNED_LEAGUE_RE.search(r.get("league") or ""):
                _dropped["封杀联赛"] += 1
                continue
            # 距开赛分钟数(用 match_epoch-push_time 自己算; clv_results 里的 minutes_before_match 字段已坏, 恒<60)
            lead = None
            pt = ts(r.get("push_time"))
            me = f(r.get("match_epoch"))
            if pt and me:
                lead = (me - pt) / 60.0
            out.append((r.get("sub_market", "?"), r.get("sport", "?"), r.get("league", ""), clv, ev, lead))
    return out


def load_validate():
    """读取 validate(所有≥2%EV机会, 只记录不推送)样本, 单独统计做参考, 不参与门槛。

    用户 2026-08-23: 最终目标是用真实投注(push)算准门槛, validate 只是数据积累,
    观察"所有≥2%机会"的 CLV 分布(哪些盘口理论上该有机会), 等它们变成 push 才参与门槛。
    """
    if not RESULTS.exists():
        return []
    out = []
    with open(RESULTS, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if (r.get("source") or "").strip() != "validate":
                continue
            if (r.get("close_source") or "live").strip() == "archive_open":
                continue
            clv, ev, od = f(r.get("true_clv_pct")), f(r.get("push_ev_pct")), f(r.get("bb_odds"))
            if clv is None or ev is None or ev < 2:
                continue
            if od and ev > max(12.0, (od - 1) * 20):
                continue
            if od and od > MAX_ODDS:
                continue
            if BANNED_LEAGUE_RE.search(r.get("league") or ""):
                continue
            out.append((r.get("sub_market", "?"), r.get("sport", "?"), r.get("league", ""), clv, ev))
    return out


def market_threshold(samples):
    """对一个盘口的 (clv, ev) 样本, 找"负转正"边界 EV 门槛。

    核心(用户 2026-08-21 铁律): 数据驱动 = 负期望的档就继续往上调, 直到正期望为止。
    返回 (门槛, n, 中位CLV, 正率%, 判定档)。门槛为 None 表示样本不足, 调用方回退保守默认。

    正期望档判据(样本越多越精准, 小样本要求更高中位 = shrinkage):
      n >= N_CONFIRMED(100): 中位 CLV > MEDIAN_CLV_MIN(2%)         且 正率≥55%
      30 <= n < 100:         中位 CLV > MEDIAN_CLV_MIN+SHRINK_MARGIN(3%) 且 正率≥55%
      n < 30:                样本不足, 不可判, 不据此解锁低门槛(回退保守 8%)
    """
    n_max = 0
    last_neg_band = None   # 记录最后看到的负 edge 档(用于"无正期望档"时报告)
    for thr in EV_STEPS:   # 低 → 高, 第一个正期望档即门槛
        sub = [(c, e) for c, e in samples if e >= thr]
        n = len(sub)
        n_max = max(n_max, n)
        if n == 0:
            break
        clvs = [c for c, _ in sub]
        pos_rate = sum(1 for c in clvs if c > 0) / n
        med = statistics.median(clvs)
        if n < N_DIRECTIONAL:
            continue          # 样本不足的档不可判(既不据此放行也不据此否定)
        required = MEDIAN_CLV_MIN + (SHRINK_MARGIN if n < N_CONFIRMED else 0.0)
        if pos_rate >= POS_RATE_MIN and med > required:
            # 负转正边界: 这一档是有意义的正 edge → 门槛设这里
            grade = "确认" if n >= N_CONFIRMED else "方向性"
            return (thr, n, round(med, 2), round(pos_rate * 100), grade)
        if med <= 0:
            last_neg_band = (thr, n, med)   # 负 edge, 继续往上搜
        # 边界(0 < med 但正率不足): 继续往上找更稳的正档
    # 搜遍所有档都没有"可信正期望档"。
    # 区分: 盘口级样本足够(n≥N_DIRECTIONAL) = 数据明确说这盘口没有盈利档 → 门槛=最高档(实质不推, 数据结论非封杀);
    #       样本不足 = 无法判定 → 返回 None 让调用方回退保守门槛(不封杀)。
    total = len(samples)
    if total >= N_DIRECTIONAL:
        # 负edge判定收紧(2026-08-26): 只有"整个分布为负"(总体中位<0)才算"数据明确无盈利档"→封20%。
        # 若总体中位≥0但无单档过严格门槛(中位>2%且正率≥55%), 是"弱正/未确认", 硬封20%会把
        # 好窗口误封(ou 近6-24h 总体+0.4%/54%, 8%+档+4.5%/77%只是n=13判不出) → 返回 None 回退 base 门槛。
        overall_med = statistics.median([c for c, _ in samples])
        if overall_med >= 0:
            return None, n_max, None, None, f"弱正未确认(总体中位{overall_med:+.1f}%≥0, 无单档过严格门槛)→回退base"
        detail = f"负edge(全程负, 最高可测档EV≥{last_neg_band[0]}% 中位{last_neg_band[2]:+.1f}%)" if last_neg_band else "无正期望档"
        return EV_STEPS[-1], n_max, None, None, detail
    return None, n_max, None, None, f"样本不足 n={total}<{N_DIRECTIONAL}"


def compute_sport_adjust(by_sport):
    """按运动整体 CLV 数据驱动算运动层微调(相对盘口门槛的加/减)。

    样本 n≥30 才数据驱动; 否则回退 SPORT_ADJUST_FALLBACK 的保守值。
    映射(用 0.5% 容差带, 避免 -0.4% 这种噪声被误判成"该降/该抬"):
      中位 CLV < -2%            → +2.0 (显著负)
      中位 CLV < -0.5%          → +1.0 (轻微负)
      -0.5% ≤ 中位 ≤ +0.5%      →  0.0 (持平, 噪声带内)
      中位 > +0.5% 且 正率≥55%  → -1.0 (正 edge, 可降 1pp)
      中位 > +0.5% 但 正率<55%  →  0.0 (正但不显著)
    """
    adjust = dict(SPORT_ADJUST_FALLBACK)
    detail = {}
    for sp, clvs in sorted(by_sport.items()):
        if sp == "football":
            # 足球是基准(盘口层门槛主要就是足球数据定的), 固定 0, 不参与运动层调整。
            # 若也数据驱动, 足球整体中位 CLV 因主体盘口(1x2/hc/ou)负而被调成 +1,
            # 会连累 ht/ht_dc 从 2% 被抬到 3% —— 而 ht 的正 edge 已在盘口层单独给了 2%。
            adjust[sp] = 0.0
            detail[sp] = {"n": len(clvs), "verdict": "基准(固定0)", "adjust": 0.0}
            continue
        n = len(clvs)
        if n < N_DIRECTIONAL:
            detail[sp] = {"n": n, "verdict": "样本不足→保守回退", "adjust": adjust.get(sp, 0.0)}
            continue
        med = statistics.median(clvs)
        pos = sum(1 for c in clvs if c > 0) / n
        if med < -2.0:
            a = 2.0
        elif med < -0.5:
            a = 1.0
        elif med <= 0.5:
            a = 0.0
        elif pos >= POS_RATE_MIN:
            a = -1.0
        else:
            a = 0.0
        adjust[sp] = a
        detail[sp] = {"n": n, "median": round(med, 2), "pos_rate": round(pos * 100),
                      "verdict": f"数据驱动→{a:+.1f}", "adjust": a}
    return adjust, detail


def compute_league_adjust(by_sport_league):
    """联赛层调整: 该联赛整体 CLV 相对运动整体 CLV 的偏离, 映射到加/减 pp。

    这是朝 V5 权重矩阵(逐联赛逐盘口)迈出的第一步 —— 联赛层(sport×league)。
    三维格(sport×league×market)现 0 格样本≥30, 做不了, 故先做联赛层:
      n≥30 才数据驱动; 否则不设(推送层回退运动层)。
    key = "sport|league"(英文联赛名, 与推送层 match.get("league") 对齐)。
    映射(偏离 = 联赛中位 - 运动整体中位):
      偏离 < -2pp   → +2.0 (联赛显著比运动差, 抬门槛)
      偏离 < -0.5pp → +1.0
      ±0.5pp 内     →  0.0
      偏离 > +0.5pp 且 正率≥55% → -1.0 (联赛显著好, 可降)
      偏离 > +0.5pp 但 正率<55%  →  0.0 (偏离大但正率不够, 不降)
    """
    by_sport = defaultdict(list)
    for (sp, _lg), clvs in by_sport_league.items():
        by_sport[sp].extend(clvs)
    sport_med = {sp: statistics.median(v) for sp, v in by_sport.items()}

    adjust = {}
    detail = {}
    for (sp, lg), clvs in sorted(by_sport_league.items()):
        n = len(clvs)
        if n < N_DIRECTIONAL:
            continue        # 样本不足, 不设联赛层(回退运动层)
        med = statistics.median(clvs)
        pos = sum(1 for c in clvs if c > 0) / n
        dev = med - sport_med.get(sp, 0.0)
        if dev < -2.0:
            a = 2.0
        elif dev < -0.5:
            a = 1.0
        elif dev <= 0.5:
            a = 0.0
        elif pos >= POS_RATE_MIN:
            a = -1.0
        else:
            a = 0.0
        key = f"{sp}|{lg}"
        adjust[key] = a
        detail[key] = {"n": n, "median": round(med, 2), "dev": round(dev, 2),
                       "adjust": a}
    return adjust, detail


def compute_time_adjust(by_time):
    """时间维度调整(主体盘口 1x2/hc/ou): 按距开赛时间分档, 相对基准(中6-24h)的偏离映射加/减 pp。

    数据驱动(样本 n≥30/桶才设), 否则回退 0(不动)。
    基准 = 中6-24h 桶(样本最多最稳)。映射(偏离 = 桶中位 - 基准中位):
      偏离 < -2pp   → +2.0 (显著比基准差, 抬门槛)
      偏离 < -0.5pp → +1.0
      ±0.5pp 内     →  0.0
      偏离 > +0.5pp 且 正率≥55% → -1.0 (显著好, 可降)
      偏离 > +0.5pp 但 正率<55%  →  0.0
    返回 (adjust, detail)。adjust 是 label→pp 的 dict。
    """
    base = None
    if len(by_time.get("近6-24h", [])) >= N_DIRECTIONAL:
        base = statistics.median(by_time["近6-24h"])
    adjust = {}
    detail = {}
    for lo, hi, label in TIME_BUCKETS:
        clvs = by_time.get(label, [])
        n = len(clvs)
        if n < N_DIRECTIONAL:
            detail[label] = {"n": n, "verdict": "样本不足→回退0", "adjust": 0.0}
            continue
        med = statistics.median(clvs)
        pos = sum(1 for c in clvs if c > 0) / n
        dev = med - base if base is not None else None
        if dev is None:
            a = 0.0
        elif dev < -2.0:
            a = 2.0
        elif dev < -0.5:
            a = 1.0
        elif dev <= 0.5:
            a = 0.0
        elif pos >= POS_RATE_MIN:
            a = -1.0
        else:
            a = 0.0
        adjust[label] = a
        detail[label] = {"n": n, "median": round(med, 2),
                         "dev": round(dev, 2) if dev is not None else None,
                         "verdict": f"数据驱动→{a:+.1f}", "adjust": a}
    return adjust, detail


def write_matrix(matrix, mode="all", force=False, dry_run=False):
    """落盘门槛矩阵 —— 原子写 + 防误覆盖护栏。

    护栏(2026-08-23 事故后加): 线上矩阵只接受 mode=all 的产出, 且样本数不得比上一版
    骤降 >50%。不满足就写到 *_candidate.json 并告警, 不动线上文件 —— 一次口径实验
    不该在没人察觉的情况下把全系统门槛换掉。--force 可显式覆盖。
    """
    target = OUT
    reason = None
    if mode != "all" and not force:
        reason = f"实验口径 mode={mode}"
    else:
        prev_n = None
        if OUT.exists():
            try:
                prev_n = json.loads(OUT.read_text()).get("n_samples")
            except (json.JSONDecodeError, ValueError, OSError):
                prev_n = None
        n_new = matrix.get("n_samples", 0)
        if prev_n and n_new < prev_n * 0.5 and not force:
            reason = f"样本骤降 {prev_n}→{n_new}(<50%)"
    if reason:
        target = OUT.with_name(OUT.stem + "_candidate.json")
        print(f"\n⚠️ 不覆盖线上矩阵({reason}) → 写入 {target.name}; 确认无误加 --force")
    if dry_run:
        print(f"\n(演练)未落盘。将写 {target.name}")
        return target
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(matrix, ensure_ascii=False, indent=2))
    tmp.replace(target)      # 原子替换: 推送侧随时在读, 不能读到半截
    return target


def main(mode="all", force=False, dry_run=False):
    clean = load_clean(mode)
    by_market = defaultdict(list)
    by_sport = defaultdict(list)
    by_sport_league = defaultdict(list)
    by_time = defaultdict(list)
    by_market_time = defaultdict(list)   # (盘口, 时间窗) → [(clv, ev)], 时间窗×盘口独立门槛
    by_sport_market = defaultdict(list)  # (运动, 盘口) → [(clv, ev)], 运动×盘口独立门槛
    for sm, sp, lg, clv, ev, lead in clean:
        by_market[sm].append((clv, ev))
        by_sport[sp].append(clv)
        by_sport_league[(sp, lg)].append(clv)
        by_sport_market[(sp, sm)].append((clv, ev))
        if sm in MAIN_MARKETS and lead is not None:
            for lo, hi, label in TIME_BUCKETS:
                if lo <= lead < hi:
                    by_time[label].append(clv)
                    break
        # 时间窗×盘口: 所有盘口都按时间窗分桶(不只主体盘口)
        if lead is not None:
            for lo, hi, label in TIME_BUCKETS:
                if lo <= lead < hi:
                    by_market_time[(sm, label)].append((clv, ev))
                    break

    markets = {}
    details = {}
    for sm in sorted(by_market, key=lambda s: -len(by_market[s])):
        thr, n, med, pos, grade = market_threshold(by_market[sm])
        if grade.startswith(("负edge", "无正期望")):
            # 数据明确说这盘口没有盈利档(样本够但全程负 CLV) → 门槛=最高档(实质不推)。
            # 这是数据结论, 不是 999 代码封杀: 数据转正时门槛会自动降。
            markets[sm] = float(thr)
            details[sm] = {"n": n, "verdict": f"无正期望档→{thr}%({grade})",
                           "median": med, "pos_rate": pos}
        elif thr is not None:
            markets[sm] = float(thr)
            details[sm] = {"n": n, "verdict": f"data_driven({grade})",
                           "median": med, "pos_rate": pos}
        else:
            # 样本不足 → 保守高门槛回退。不封杀(可能只是没测出来), 也不放低(没有证据)。
            markets[sm] = CONSERVATIVE_THRESHOLD
            details[sm] = {"n": n, "verdict": f"保守回退({grade})",
                           "median": med, "pos_rate": pos}

    # 时间窗×盘口独立门槛(2026-08-25): 每格(盘口,时间窗) n≥30 才数据驱动设门槛,
    # 否则不设 → 推送侧回退到 base 盘口门槛 + time_adjust pp。
    market_time = {}
    market_time_details = {}
    for (sm, label), samples in sorted(by_market_time.items()):
        thr, n, med, pos, grade = market_threshold(samples)
        if thr is not None:
            market_time.setdefault(sm, {})[label] = float(thr)
            market_time_details[f"{sm}|{label}"] = {"n": n, "verdict": grade,
                                                    "median": med, "pos_rate": pos}

    # 运动×盘口独立门槛(2026-08-25): 每格(运动,盘口) n≥30 才数据驱动设门槛,
    # 否则不设 → 推送侧回退 base 盘口门槛 + sport_adjust pp。足球是 base(全盘口 n≥100),
    # 这里也为其算, 但推送侧只对非足球用(足球走时间×盘口维度)。
    sport_market = {}
    sport_market_details = {}
    for (sp, sm), samples in sorted(by_sport_market.items()):
        thr, n, med, pos, grade = market_threshold(samples)
        if thr is not None:
            sport_market.setdefault(sp, {})[sm] = float(thr)
            sport_market_details[f"{sp}|{sm}"] = {"n": n, "verdict": grade,
                                                  "median": med, "pos_rate": pos}

    sport_adjust, sport_detail = compute_sport_adjust(by_sport)
    league_adjust, league_detail = compute_league_adjust(by_sport_league)
    time_adjust, time_detail = compute_time_adjust(by_time)

    matrix = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "markets": markets,
        "market_time": market_time,
        "sport_market": sport_market,
        "sport_adjust": sport_adjust,
        "league_adjust": league_adjust,
        "time_adjust": [
            {"min_minutes": lo, "max_minutes": hi, "label": label,
             "adjust": time_adjust.get(label, 0.0)}
            for lo, hi, label in TIME_BUCKETS
        ],
        "main_markets": list(MAIN_MARKETS),
        "default_threshold": 8.0,
        # 口径留档: 这版矩阵是用哪批样本、多少条算出来的。没有这两个字段就无法回溯
        # "门槛为什么变了"(2026-08-23 就发生过实验口径静默覆盖线上矩阵)。
        "sample_mode": mode,
        "n_samples": len(clean),
        "_details": details,
        "_market_time_details": market_time_details,
        "_sport_market_details": sport_market_details,
        "_sport_details": sport_detail,
        "_league_details": league_detail,
        "_time_details": time_detail,
    }
    written = write_matrix(matrix, mode=mode, force=force, dry_run=dry_run)
    print(f"✅ 门槛矩阵已生成 → {written.name}")
    print(f"  口径 mode={mode}, 可用样本 {len(clean)} 条, {len(markets)} 个盘口")
    if _dropped:
        print(f"  已剔除不可执行样本: " +
              ", ".join(f"{k} {v}条" for k, v in _dropped.most_common()))
    print(f"  样本纪律: ≥{N_CONFIRMED}确认(中位>{MEDIAN_CLV_MIN}%) / {N_DIRECTIONAL}-{N_CONFIRMED}方向性(中位>{MEDIAN_CLV_MIN+SHRINK_MARGIN}%) / "
          f"<{N_DIRECTIONAL}回退{CONSERVATIVE_THRESHOLD}% | 正率≥{POS_RATE_MIN*100:.0f}%")
    print("  时间维度调整(数据驱动, 主体盘口):")
    for lo, hi, label in TIME_BUCKETS:
        d = time_detail.get(label, {})
        print(f"    {label:<10} adjust={time_adjust.get(label, 0.0):>+5}  n={d.get('n',0):>4}  {d.get('verdict','')}")
    print("  运动层微调(数据驱动):")
    for sp in sorted(sport_adjust, key=lambda s: -(sport_detail.get(s, {}).get("n", 0))):
        d = sport_detail.get(sp, {})
        print(f"    {sp:<18} adjust={sport_adjust[sp]:>+5}  {d.get('verdict','')}")
    if league_adjust:
        print("  联赛层调整(数据驱动, n≥30):")
        for key in sorted(league_adjust, key=lambda k: -league_detail[k]["n"]):
            d = league_detail[key]
            print(f"    {key:<40} adjust={league_adjust[key]:>+5}  n={d['n']} 中位{d['median']:+.1f}% 偏离{d['dev']:+.1f}pp")
    else:
        print("  联赛层: 无联赛 n≥30(回退运动层)")
    for sm in sorted(markets, key=lambda s: -by_market[s].__len__()):
        d = details[sm]
        v = d["verdict"]
        print(f"    {sm:<22} 门槛 {markets[sm]:>6}  n={d['n']:>4}  {v}")

    # validate 单独统计(参考, 不参与门槛): 观察"所有≥2%EV机会"的 CLV 分布,
    # 哪些盘口理论上该有机会(正CLV), 等它们变成 push(真实投注)才参与门槛。
    val = load_validate()
    if val:
        print("\n  validate 单独统计(参考, 不参与门槛):")
        val_by_market = defaultdict(list)
        for sm, sp, lg, clv, ev in val:
            val_by_market[sm].append(clv)
        for sm in sorted(val_by_market, key=lambda s: -len(val_by_market[s])):
            clvs = val_by_market[sm]
            n = len(clvs)
            if n < 10:
                continue
            med = statistics.median(clvs)
            pos = sum(1 for c in clvs if c > 0) / n * 100
            print(f"    {sm:<22} n={n:>4} 中位CLV={med:+.2f}% 正率={pos:.0f}%")


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(description="数据驱动 EV 门槛矩阵")
    _ap.add_argument("--mode", choices=("all", "push", "validate"), default="all",
                     help="样本口径: all=validate+push(线上) / push=只用真实投注 / validate=只用观察样本")
    _ap.add_argument("--force", action="store_true", help="强制覆盖线上矩阵(绕过护栏)")
    _ap.add_argument("--dry-run", action="store_true", help="只算不落盘")
    _a = _ap.parse_args()
    main(mode=_a.mode, force=_a.force, dry_run=_a.dry_run)
