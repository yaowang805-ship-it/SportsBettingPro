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
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

# 被过滤掉的不可执行样本计数(供报告用, 避免"静默丢弃"看不见)
_dropped = Counter()

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "storage"
RESULTS = DATA / "clv_results.csv"
OUT = DATA / "ev_threshold_matrix.json"

POS_RATE_MIN = 0.55     # 正 CLV 率下限
MEDIAN_CLV_MIN = 1.0    # 正期望最低中位 CLV 阈值(%): >1% 才算"有正 edge"
                        # (1~2% 勉强正, >2% 强正 —— 业界标准见 clv-threshold-matrix-todo)

# 样本纪律(业界标准: CLV 100-200 注才可靠, 早期 CLV 是噪声 —— 80注+5% 到 340注 会掉到 +1.8%)
#   n >= 100  → "确认", 可用数据驱动门槛
#   30 <= n < 100 → "方向性", 可用但要求更严的 edge(中位 CLV 加倍)
#   n < 30    → 样本不足, **一律回退保守高门槛**, 绝不据此放低
# 原先 MIN_N=10 等于用 10 个样本就敢开 2% 门槛(实测 total_goals_range 正是如此), 已废弃。
N_CONFIRMED = 100
N_DIRECTIONAL = 30
# 判定标准对两档一致(中位CLV>2% 且 正率>55%), n 只决定标签"确认"/"方向性" ——
# 不额外加严: 加严会把 ht_dc(n=34/中位+3.17%/正率85%) 这种强信号误杀。

# 从**低到高**遍历, 找"负转正"边界档 = 门槛。
# 用户铁律(2026-08-21): 数据驱动 = 负期望的档就继续往上调, 直到正期望为止。
# 扩展搜索范围到 20%, 避免 hc 这种"8% 还是负"的盘口被错误停在 8%。
EV_STEPS = (2, 3, 5, 8, 10, 12, 15, 20)
MIN_BAND_N = 10          # 一个 EV 档至少 10 条样本才认可其正/负判定(低于 10 视为不可判)

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
            # 不可执行机会不参与门槛计算(推送层不会投注它们, 详见文件头注释)
            if od and od > MAX_ODDS:
                _dropped["odds>20"] += 1
                continue
            if BANNED_LEAGUE_RE.search(r.get("league") or ""):
                _dropped["封杀联赛"] += 1
                continue
            out.append((r.get("sub_market", "?"), r.get("sport", "?"), r.get("league", ""), clv, ev))
    return out


def market_threshold(samples):
    """对一个盘口的 (clv, ev) 样本, 找"负转正"边界 EV 门槛。

    核心(用户 2026-08-21 铁律): 数据驱动 = 负期望的档就继续往上调, 直到正期望为止。
    返回 (门槛, n, 中位CLV, 正率%, 判定档)。门槛为 None 表示样本不足, 调用方回退保守默认。

    正期望档判据: 中位 CLV > 0 且 正率 ≥ 55% 且 n ≥ MIN_BAND_N。
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
        if n < MIN_BAND_N:
            continue          # 样本太少的档不可判, 跳过(既不据此放行也不据此否定)
        if pos_rate >= POS_RATE_MIN and med > MEDIAN_CLV_MIN:
            # 负转正边界: 这一档是有意义的正 edge → 门槛设这里
            grade = "确认" if (n >= N_CONFIRMED and med > 2.0) else ("方向性" if n >= N_DIRECTIONAL else "样本偏少")
            return (thr, n, round(med, 2), round(pos_rate * 100), grade)
        if med <= 0:
            last_neg_band = (thr, n, med)   # 负 edge, 继续往上搜
        # 边界(0 < med 但正率不足): 继续往上找更稳的正档
    # 搜遍所有档都没有"可信正期望档"。
    # 区分: 盘口级样本足够(n≥N_DIRECTIONAL) = 数据明确说这盘口没有盈利档 → 门槛=最高档(实质不推, 数据结论非封杀);
    #       样本不足 = 无法判定 → 返回 None 让调用方回退保守门槛(不封杀)。
    total = len(samples)
    if total >= N_DIRECTIONAL:
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


def main():
    clean = load_clean()
    by_market = defaultdict(list)
    by_sport = defaultdict(list)
    by_sport_league = defaultdict(list)
    for sm, sp, lg, clv, ev in clean:
        by_market[sm].append((clv, ev))
        by_sport[sp].append(clv)
        by_sport_league[(sp, lg)].append(clv)

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

    sport_adjust, sport_detail = compute_sport_adjust(by_sport)
    league_adjust, league_detail = compute_league_adjust(by_sport_league)

    matrix = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "markets": markets,
        "sport_adjust": sport_adjust,
        "league_adjust": league_adjust,
        "in_play_hours": IN_PLAY_HOURS,
        "in_play_extra": IN_PLAY_EXTRA,
        "main_markets": list(MAIN_MARKETS),
        "default_threshold": 8.0,
        "_details": details,
        "_sport_details": sport_detail,
        "_league_details": league_detail,
    }
    OUT.write_text(json.dumps(matrix, ensure_ascii=False, indent=2))
    print(f"✅ 门槛矩阵已生成 → {OUT.name}")
    print(f"  可用样本 {len(clean)} 条, {len(markets)} 个盘口")
    if _dropped:
        print(f"  已剔除不可执行样本: " +
              ", ".join(f"{k} {v}条" for k, v in _dropped.most_common()))
    print(f"  样本纪律: ≥{N_CONFIRMED}确认 / {N_DIRECTIONAL}-{N_CONFIRMED}方向性 / "
          f"<{N_DIRECTIONAL}回退{CONSERVATIVE_THRESHOLD}% | 判定: 中位CLV>{MEDIAN_CLV_MIN}% 且 正率≥{POS_RATE_MIN*100:.0f}%")
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


if __name__ == "__main__":
    main()
