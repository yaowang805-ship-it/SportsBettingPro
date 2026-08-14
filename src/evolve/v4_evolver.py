"""V4 自我进化引擎 — 多源数据驱动迭代

进化维度:
  1. 外部数据更新: Pinnacle 历史数据定期下载 + 权重重算
  2. 结算数据反馈: 贝叶斯更新 — 用我们的实际结果校准 Pinnacle 先验
  3. BB 溢价累积: 每次扫描积累 BB/Pin 对比, 定期重算溢价表
  4. 联赛分层进化: 按 (league, odds_range, market) 粒度的 ROI 跟踪

运行频率:
  daily:   累积 BB 溢价数据
  weekly:  结算反馈 + 溢价重算 (周一 06:07 已有 tier-update)
  monthly: 全量权重重算 + 交叉验证
"""

import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STORAGE_DIR = DATA_DIR / "storage"
EVOLVE_DIR = STORAGE_DIR / "evolve"

# ── 进化状态文件 ──
BB_PREMIUM_ACCUMULATOR = EVOLVE_DIR / "bb_premium_accumulator.json"
SETTLEMENT_FEEDBACK = EVOLVE_DIR / "settlement_feedback.json"
EVOLVE_LOG = DATA_DIR / "logs" / "evolve.log"

import logging
logger = logging.getLogger("evolve")


def _ensure_dirs():
    EVOLVE_DIR.mkdir(parents=True, exist_ok=True)
    EVOLVE_LOG.parent.mkdir(parents=True, exist_ok=True)


# =====================================================================
# 1. BB 溢价累积 — 每次扫描自动运行
# =====================================================================
def accumulate_bb_premium():
    """从最新的 BB vs Pinnacle 对比文件中提取溢价数据并累积。

    每次扫描后调用，无额外 API 开销。
    """
    _ensure_dirs()
    comp_file = STORAGE_DIR / "bb_vs_pinnacle_comparison.json"
    fb_file = STORAGE_DIR / "bb_vs_pinnacle_comparison_FB.json"

    if not comp_file.exists():
        return

    # 加载已有累积
    accum = {"buckets": {}, "total_samples": 0, "last_updated": ""}
    if BB_PREMIUM_ACCUMULATOR.exists():
        accum = json.loads(BB_PREMIUM_ACCUMULATOR.read_text())

    new_samples = 0
    for fpath in [comp_file, fb_file]:
        if not fpath.exists():
            continue
        data = json.loads(fpath.read_text())
        for detail in data.get("details", []):
            if detail.get("match_type") != "name":
                continue
            if detail.get("match_score", 0) < 0.95:
                continue
            flags = detail.get("flags", [])
            if any("溢价异常高" in f for f in flags):
                continue

            for mk in ["opportunities"]:
                for opp in detail.get(mk, []):
                    bb = opp.get("bb_odds", 0)
                    pin = opp.get("pin_odds", 0)
                    if not (1.01 < bb < 30 and 1.01 < pin < 30):
                        continue
                    premium = (bb - pin) / pin
                    if abs(premium) > 0.5:
                        continue

                    # 分桶
                    if pin < 1.5: bucket = "<1.5"
                    elif pin < 2.0: bucket = "1.5-2.0"
                    elif pin < 2.5: bucket = "2.0-2.5"
                    elif pin < 3.0: bucket = "2.5-3.0"
                    elif pin < 4.0: bucket = "3.0-4.0"
                    elif pin < 5.0: bucket = "4.0-5.0"
                    elif pin < 7.0: bucket = "5.0-7.0"
                    elif pin < 10.0: bucket = "7.0-10.0"
                    else: bucket = ">10.0"

                    if bucket not in accum["buckets"]:
                        accum["buckets"][bucket] = []
                    accum["buckets"][bucket].append(premium)
                    new_samples += 1

    if new_samples > 0:
        # 每个桶最多保留 5000 个样本 (防内存膨胀)
        for bucket in accum["buckets"]:
            if len(accum["buckets"][bucket]) > 5000:
                accum["buckets"][bucket] = accum["buckets"][bucket][-5000:]

        accum["total_samples"] = sum(len(v) for v in accum["buckets"].values())
        accum["last_updated"] = datetime.now().isoformat()
        BB_PREMIUM_ACCUMULATOR.write_text(json.dumps(accum, indent=2))
        logger.info("BB 溢价累积: +%d 样本 (总计 %d)", new_samples, accum["total_samples"])


def get_evolved_premium(odds: float) -> Optional[float]:
    """从累积数据中读取进化的 BB 溢价 (中位数 × 0.8)。

    Returns None 如果该区间样本不足。
    """
    if not BB_PREMIUM_ACCUMULATOR.exists():
        return None
    accum = json.loads(BB_PREMIUM_ACCUMULATOR.read_text())

    if odds < 1.5: bucket = "<1.5"
    elif odds < 2.0: bucket = "1.5-2.0"
    elif odds < 2.5: bucket = "2.0-2.5"
    elif odds < 3.0: bucket = "2.5-3.0"
    elif odds < 4.0: bucket = "3.0-4.0"
    elif odds < 5.0: bucket = "4.0-5.0"
    elif odds < 7.0: bucket = "5.0-7.0"
    elif odds < 10.0: bucket = "7.0-10.0"
    else: bucket = ">10.0"

    vals = accum["buckets"].get(bucket, [])
    if len(vals) < 20:
        return None  # 样本不足，用 V4 硬编码

    vals_sorted = sorted(vals)
    median = vals_sorted[len(vals_sorted) // 2]
    return round(median * 0.8, 3)


# =====================================================================
# 2. 结算数据反馈 — 贝叶斯更新
# =====================================================================
def analyze_settlement_feedback():
    """用我们的实际投注结果验证 V4 的 Pinnacle 先验。

    贝叶斯更新:
      prior_alpha = pinnacle_wr × weight
      prior_beta  = (1 - pinnacle_wr) × weight
      posterior_wr = (prior_alpha + wins) / (prior_alpha + prior_beta + total)

    weight = min(samples, 100) — Pinnacle 样本越多，先验权重越大
    """
    _ensure_dirs()
    settle_file = STORAGE_DIR / "settlement_log.csv"
    if not settle_file.exists():
        logger.warning("无结算数据，跳过反馈分析")
        return None

    # 加载 V4 权重矩阵
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from config.weight_matrix_v5 import PIN_1X2_DATA, ODDS_BINS, _bin_index

    lines = settle_file.read_text().strip().split('\n')
    if len(lines) < 2:
        return None

    # 按 (league, odds_bin, market) 聚合
    bets = defaultdict(lambda: {"total": 0, "wins": 0})
    for line in lines[1:]:
        parts = line.split(',')
        if len(parts) < 11:
            continue
        sport = parts[2].strip()
        league = parts[3].strip()
        market = parts[5].strip()
        odds_str = parts[6].strip()
        result = parts[8].strip()
        try: odds = float(odds_str)
        except: continue

        if result not in ('win', 'lose'):
            continue
        if sport.lower() != 'football':
            continue

        idx = _bin_index(odds, ODDS_BINS)
        key = f"{league}|{idx}|{market}"
        bets[key]["total"] += 1
        if result == 'win':
            bets[key]["wins"] += 1

    # 贝叶斯更新
    feedback = {}
    for key, stats in bets.items():
        if stats["total"] < 3:
            continue
        parts = key.split('|')
        league = parts[0]
        bin_idx = int(parts[1])

        # Pinnacle 先验
        pinnacle_data = PIN_1X2_DATA.get(league, PIN_1X2_DATA.get("_AGGREGATE"))
        if not pinnacle_data:
            continue
        pin_entry = pinnacle_data.get(bin_idx)
        if not pin_entry:
            continue
        pin_wr, pin_odds, pin_n = pin_entry

        # 贝叶斯: 先验权重 = min(pin_n, 100)
        weight = min(pin_n, 100)
        prior_alpha = pin_wr * weight
        prior_beta = (1.0 - pin_wr) * weight

        actual_wr = stats["wins"] / stats["total"]
        posterior_wr = (prior_alpha + stats["wins"]) / (prior_alpha + prior_beta + stats["total"])

        deviation = actual_wr - pin_wr
        feedback[key] = {
            "league": league,
            "bin": bin_idx,
            "market": parts[2] if len(parts) > 2 else "1x2",
            "pinnacle_wr": round(pin_wr, 3),
            "pinnacle_n": pin_n,
            "our_bets": stats["total"],
            "our_wins": stats["wins"],
            "our_wr": round(actual_wr, 3),
            "posterior_wr": round(posterior_wr, 3),
            "deviation": round(deviation, 3),
            "flag": "🔴" if deviation < -0.1 else "🟡" if deviation < -0.05 else "🟢",
        }

    if feedback:
        SETTLEMENT_FEEDBACK.write_text(json.dumps(feedback, indent=2, ensure_ascii=False))

        # 打印摘要
        flagged = {k: v for k, v in feedback.items() if v["flag"] in ("🔴", "🟡")}
        if flagged:
            logger.warning("⚠️ 结算反馈: %d 个区间偏离 Pinnacle 预期", len(flagged))
            for k, v in sorted(flagged.items(), key=lambda x: x[1]["deviation"]):
                logger.warning("  %s %s: Pin=%.1f%% 我们=%.1f%% (n=%d)",
                             v["flag"], k, v["pinnacle_wr"]*100, v["our_wr"]*100, v["our_bets"])

    return feedback


def get_posterior_wr(league: str, bin_idx: int, market: str = "1x2") -> Optional[float]:
    """获取贝叶斯后验胜率。Returns None 如果无反馈数据。"""
    if not SETTLEMENT_FEEDBACK.exists():
        return None
    feedback = json.loads(SETTLEMENT_FEEDBACK.read_text())
    key = f"{league}|{bin_idx}|{market}"
    entry = feedback.get(key)
    if entry and entry.get("our_bets", 0) >= 5:
        return entry["posterior_wr"]
    return None


# =====================================================================
# 3. V4 权重矩阵自检+建议
# =====================================================================
def audit_v4_health() -> dict:
    """检查 V4 权重矩阵的健康状态并给出建议。"""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))

    issues = []

    # 检查 BB 溢价累积数据
    if BB_PREMIUM_ACCUMULATOR.exists():
        accum = json.loads(BB_PREMIUM_ACCUMULATOR.read_text())
        for bucket, vals in accum["buckets"].items():
            if len(vals) >= 100:
                median = sorted(vals)[len(vals)//2]
                issues.append({
                    "type": "premium_ready",
                    "bucket": bucket,
                    "samples": len(vals),
                    "median_premium": round(median * 100, 1),
                    "suggestion": f"BB溢价桶 {bucket} 已有 {len(vals)} 样本，可更新 V4",
                })

    # 检查结算反馈
    if SETTLEMENT_FEEDBACK.exists():
        fb = json.loads(SETTLEMENT_FEEDBACK.read_text())
        for key, entry in fb.items():
            if entry["flag"] == "🔴":
                issues.append({
                    "type": "settlement_alert",
                    "key": key,
                    "pinnacle_wr": entry["pinnacle_wr"],
                    "our_wr": entry["our_wr"],
                    "bets": entry["our_bets"],
                    "suggestion": f"严重偏离: {key} Pinnacle={entry['pinnacle_wr']:.1%} 我们={entry['our_wr']:.1%}",
                })

    # 检查 Pinnacle 数据新鲜度
    pin_dir = DATA_DIR / "pinnacle_historical"
    if pin_dir.exists():
        csv_files = list(pin_dir.glob("*.csv"))
        if csv_files:
            newest = max(csv_files, key=lambda p: p.stat().st_mtime)
            age_days = (datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)).days
            if age_days > 30:
                issues.append({
                    "type": "data_stale",
                    "age_days": age_days,
                    "suggestion": f"Pinnacle 数据 {age_days} 天未更新，建议下载新赛季",
                })

    return {"issues": issues, "checked_at": datetime.now().isoformat()}


# =====================================================================
# 4. 主入口 — 供 pipeline.sh 调用
# =====================================================================
def evolve_daily():
    """每日进化任务: 累积 BB 溢价。"""
    _ensure_dirs()
    logger.info("=== 每日进化: BB 溢价累积 ===")
    accumulate_bb_premium()
    logger.info("=== 每日进化完成 ===")


def evolve_weekly():
    """每周进化任务: 结算反馈 + 溢价重算 + 健康检查。"""
    _ensure_dirs()
    logger.info("=== 每周进化 ===")

    logger.info("Step 1/3: BB 溢价累积...")
    accumulate_bb_premium()

    logger.info("Step 2/3: 结算反馈贝叶斯更新...")
    fb = analyze_settlement_feedback()
    if fb:
        total = len(fb)
        flagged = sum(1 for v in fb.values() if v["flag"] in ("🔴", "🟡"))
        logger.info("  反馈: %d 个区间, %d 个偏离", total, flagged)

    logger.info("Step 3/3: V4 健康检查...")
    health = audit_v4_health()
    for issue in health["issues"]:
        logger.warning("  %s: %s", issue["type"], issue["suggestion"])

    logger.info("=== 每周进化完成 ===")


def evolve_monthly():
    """每月进化任务: 全量权重重算 + 赔率区间权重重算 + 交叉验证。"""
    _ensure_dirs()
    logger.info("=== 每月进化 ===")
    logger.info("Step 1/3: 重算赔率区间权重...")
    _recalibrate_odds_weights()
    logger.info("Step 2/3: 结算反馈贝叶斯更新...")
    analyze_settlement_feedback()
    logger.info("Step 3/3: V4 健康检查...")
    health = audit_v4_health()
    for issue in health["issues"]:
        logger.warning("  %s: %s", issue["type"], issue["suggestion"])
    logger.info("=== 每月进化完成 ===")


def _recalibrate_odds_weights():
    """用全量 Pinnacle 数据重算赔率区间权重。"""
    import csv
    from collections import defaultdict
    from config.weight_matrix_v5 import ODDS_BINS, _bb_premium_1x2

    pin_dir = DATA_DIR / "pinnacle_historical"
    bins = defaultdict(lambda: [0, 0, 0.0])  # wins, total, sum_odds

    def bi(o):
        for i, t in enumerate(ODDS_BINS):
            if o < t: return i
        return len(ODDS_BINS)-1

    for subdir in pin_dir.iterdir():
        if not subdir.is_dir() or subdir.name in ("sbr","oddsportal","scottfree","tennis_data","mma"): continue
        for f in subdir.glob("*.csv"):
            try:
                with open(f, encoding='utf-8-sig') as fh:
                    for r in csv.DictReader(fh):
                        try:
                            ph=float(r.get("PSH",0)or 0);pd=float(r.get("PSD",0)or 0);pa=float(r.get("PSA",0)or 0)
                            ftr=r.get("FTR","")
                        except: continue
                        if min(ph,pa)<=1.01: continue
                        for odds, won in [(ph,ftr=="H"),(pd,ftr=="D"),(pa,ftr=="A")]:
                            if odds<=1.01: continue
                            idx=bi(odds); bins[idx][1]+=1; bins[idx][2]+=odds
                            if won: bins[idx][0]+=1
            except: continue

    logger.info("赔率权重重算: %d 笔数据", sum(v[1] for v in bins.values()))
    for idx in sorted(bins.keys()):
        w, n, s = bins[idx]
        if n < 100: continue
        wr = w/n; avg_o = s/n
        bb_o = avg_o*(1+_bb_premium_1x2(avg_o))
        bb_ev = wr*bb_o - 1
        if bb_ev > 0.05: logger.info("  @%.1f: BB EV %+.1f%% → +30%% (n=%d)", avg_o, bb_ev*100, n)
        elif bb_ev < 0: logger.info("  @%.1f: BB EV %+.1f%% → 封杀 (n=%d)", avg_o, bb_ev*100, n)


# =====================================================================
# 5. 数据备份 — 保护不可再生数据
# =====================================================================
def backup_critical_data():
    """备份不可再生的核心数据文件。"""
    _ensure_dirs()
    backup_dir = EVOLVE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)

    critical_files = [
        STORAGE_DIR / "settlement_log.csv",
        STORAGE_DIR / "team_name_map.json",
        STORAGE_DIR / "pinnacle_league_structure.json",
        STORAGE_DIR / "league_tiers.json",
    ]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    backed_up = 0
    for f in critical_files:
        if f.exists():
            import shutil
            dest = backup_dir / f"{f.stem}_{timestamp}{f.suffix}"
            shutil.copy2(f, dest)
            backed_up += 1

    # 保留最近 30 个备份
    all_backups = sorted(backup_dir.glob("*"))
    for old in all_backups[:-30]:
        old.unlink()

    logger.info("数据备份: %d 个文件 → %s (%d 个历史备份)",
                backed_up, backup_dir, len(list(backup_dir.glob("*"))))


# =====================================================================
# 6. 实时贝叶斯更新 (每次结算后调用)
# =====================================================================
def bayesian_update_settlement(league, sub_market, odds, outcome, stake=0):
    """一次结算后立即更新该 (league, bin) 的贝叶斯后验.

    供 auto_settle.py 在每个投注结算后调用.
    """
    if outcome == 'void': return
    is_win = 1 if outcome == 'won' else 0

    # 加载已有
    bayesian_file = EVOLVE_DIR / "bayesian_weights.json"
    data = {}
    if bayesian_file.exists():
        try: data = json.loads(bayesian_file.read_text())
        except: pass

    # 找赔率bin
    from config.weight_matrix_v5 import ODDS_BINS
    def _bi(o):
        for i, t in enumerate(ODDS_BINS):
            if o <= t: return i
        return 29

    bi = _bi(odds)
    key = f"{league}|{bi}|{sub_market}"
    entry = data.get(key, [0, 0])
    data[key] = [entry[0] + is_win, entry[1] + 1]

    # 精简：最多保留500条
    if len(data) > 500:
        oldest = min(data.keys(), key=lambda k: data[k][1])
        del data[oldest]

    with open(bayesian_file, 'w') as f:
        json.dump(data, f, ensure_ascii=False)


def get_settlement_posterior(league, bin_idx, sub_market="1x2") -> Optional[float]:
    """获取该(league,bin)的结算后验胜率. None=无数据."""
    bayesian_file = EVOLVE_DIR / "bayesian_weights.json"
    if not bayesian_file.exists(): return None
    try:
        data = json.loads(bayesian_file.read_text())
        key = f"{league}|{bin_idx}|{sub_market}"
        entry = data.get(key)
        if entry and entry[1] >= 3:
            return round(entry[0] / entry[1], 4)
    except: pass
    return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-7s | %(name)s | %(message)s')

    if "--weekly" in sys.argv:
        evolve_weekly()
        backup_critical_data()
    elif "--monthly" in sys.argv:
        evolve_monthly()
        backup_critical_data()
    elif "--feedback" in sys.argv:
        fb = analyze_settlement_feedback()
        if fb:
            for k, v in sorted(fb.items()):
                print(f"{v['flag']} {k}: Pin={v['pinnacle_wr']:.1%} → 我们={v['our_wr']:.1%} (n={v['our_bets']})")
        else:
            print("无足够结算数据")
    elif "--audit" in sys.argv:
        health = audit_v4_health()
        print(json.dumps(health, indent=2, ensure_ascii=False))
    elif "--backup" in sys.argv:
        backup_critical_data()
    else:
        evolve_daily()
