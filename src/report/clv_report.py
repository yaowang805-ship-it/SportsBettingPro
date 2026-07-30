"""CLV 日报 — 统计收盘线价值并通过钉钉推送。

用法: python3 -m src.report.clv_report [--no-push]
"""
import csv, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

RESULTS_FILE = DATA_DIR / "clv_results.csv"
BJ_TZ = timezone(timedelta(hours=8))

SPORT_CN = {
    "football": "⚽足球", "basketball": "🏀篮球", "tennis": "🎾网球",
    "baseball": "⚾棒球", "american_football": "🏈美式足球",
    "mma": "🥊MMA", "boxing": "👊拳击", "ice_hockey": "🏒冰球",
}
SUB_CN = {
    "1x2": "独赢", "hc": "让球", "ou": "大小球", "dc": "双重机会",
    "ht": "上半场", "btts": "双边进球", "dnb": "平局退款",
    "htft": "半全场", "oe": "单/双",
}


def load_results(days: int = 1):
    """加载最近 N 天的 CLV 结果。"""
    if not RESULTS_FILE.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = []
    with open(RESULTS_FILE, newline='') as f:
        for r in csv.DictReader(f):
            if r.get("collect_time", "") >= cutoff:
                for field in ("true_clv_pct", "clv_delta", "bb_odds", "push_ev_pct",
                              "close_fair_price", "push_fair_price", "close_pin_odds",
                              "minutes_before_match"):
                    try:
                        r[field] = float(r[field])
                    except (ValueError, KeyError):
                        r[field] = 0.0
                rows.append(r)
    return rows


def build_report(days: int = 1):
    """生成 CLV 统计报告文本。"""
    rows = load_results(days)
    if not rows:
        return "今日无 CLV 数据"

    today = datetime.now(BJ_TZ).strftime("%m/%d")
    avg_clv = sum(r["true_clv_pct"] for r in rows) / len(rows)
    avg_delta = sum(r["clv_delta"] for r in rows) / len(rows)
    positive = sum(1 for r in rows if r["true_clv_pct"] > 0)
    negative = sum(1 for r in rows if r["true_clv_pct"] < 0)
    pos_rate = positive / len(rows) * 100

    lines = [
        f"## 📊 CLV 日报 ({today})",
        "",
        f"**采集 {len(rows)} 条** | 平均 CLV: {avg_clv:+.1f}% | CLV>0率: {pos_rate:.0f}%",
        f"赔率变动: {avg_delta:+.1f}% (+ = 朝有利方向, - = 朝不利方向)",
        "",
    ]

    # --- 按运动 ---
    by_sport = defaultdict(list)
    for r in rows:
        by_sport[r.get("sport", "?")].append(r)
    lines.append("### 按运动")
    lines.append("| 运动 | 条数 | 平均CLV | CLV>0率 | 赔率变动 |")
    lines.append("|---|---|---|---|---|")
    for sport in sorted(by_sport, key=lambda s: len(by_sport[s]), reverse=True):
        recs = by_sport[sport]
        avg = sum(r["true_clv_pct"] for r in recs) / len(recs)
        pos = sum(1 for r in recs if r["true_clv_pct"] > 0) / len(recs) * 100
        delta = sum(r["clv_delta"] for r in recs) / len(recs)
        cn = SPORT_CN.get(sport, sport)
        lines.append(f"| {cn} | {len(recs)} | {avg:+.1f}% | {pos:.0f}% | {delta:+.1f}% |")

    # --- 按盘口 ---
    by_sub = defaultdict(list)
    for r in rows:
        by_sub[r.get("sub_market", "?")].append(r)
    lines.append("")
    lines.append("### 按盘口")
    lines.append("| 盘口 | 条数 | 平均CLV | CLV>0率 | 赔率变动 |")
    lines.append("|---|---|---|---|---|")
    for sub in sorted(by_sub, key=lambda s: len(by_sub[s]), reverse=True):
        recs = by_sub[sub]
        avg = sum(r["true_clv_pct"] for r in recs) / len(recs)
        pos = sum(1 for r in recs if r["true_clv_pct"] > 0) / len(recs) * 100
        delta = sum(r["clv_delta"] for r in recs) / len(recs)
        name = SUB_CN.get(sub, sub)
        lines.append(f"| {name} | {len(recs)} | {avg:+.1f}% | {pos:.0f}% | {delta:+.1f}% |")

    # --- 按 Tier ---
    by_tier = defaultdict(list)
    for r in rows:
        t = r.get("tier", "?")
        by_tier[t].append(r)
    lines.append("")
    lines.append("### 按层级")
    lines.append("| Tier | 条数 | 平均CLV | CLV>0率 | 赔率变动 |")
    lines.append("|---|---|---|---|---|")
    tier_names = {"1": "T1 最可靠", "2": "T2 主流", "3": "T3 低级别"}
    for t in sorted(by_tier):
        recs = by_tier[t]
        avg = sum(r["true_clv_pct"] for r in recs) / len(recs)
        pos = sum(1 for r in recs if r["true_clv_pct"] > 0) / len(recs) * 100
        delta = sum(r["clv_delta"] for r in recs) / len(recs)
        name = tier_names.get(str(t), f"T{t}")
        lines.append(f"| {name} | {len(recs)} | {avg:+.1f}% | {pos:.0f}% | {delta:+.1f}% |")

    # --- 显著变化 ---
    large_pos = [r for r in rows if r["clv_delta"] > 10]
    large_neg = [r for r in rows if r["clv_delta"] < -10]
    if large_pos or large_neg:
        lines.append("")
        lines.append("### ⚠️ 显著变化 (|CLV变动| > 10%)")
        if large_pos:
            lines.append(f"**🟢 有利 ({len(large_pos)}条):**")
            for r in sorted(large_pos, key=lambda x: -x["clv_delta"])[:5]:
                lines.append(f"- {r['home']} vs {r['away']} | {r['designation']} | CLV {r['true_clv_pct']:+.1f}% (变动 {r['clv_delta']:+.1f}%)")
        if large_neg:
            lines.append(f"**🔴 不利 ({len(large_neg)}条):**")
            for r in sorted(large_neg, key=lambda x: x["clv_delta"])[:5]:
                lines.append(f"- {r['home']} vs {r['away']} | {r['designation']} | CLV {r['true_clv_pct']:+.1f}% (变动 {r['clv_delta']:+.1f}%)")

    # --- 采集时间分布 ---
    by_min = defaultdict(list)
    for r in rows:
        m = r.get("minutes_before_match", 0)
        if m < 10:
            by_min["<10min"].append(r)
        elif m < 30:
            by_min["10-30min"].append(r)
        elif m < 60:
            by_min["30-60min"].append(r)
        else:
            by_min[">60min"].append(r)
    if len(by_min) > 1:
        lines.append("")
        lines.append("### 采集时间分布")
        lines.append("| 赛前 | 条数 | 平均CLV |")
        lines.append("|---|---|---|")
        for bucket in ["<10min", "10-30min", "30-60min", ">60min"]:
            if bucket in by_min:
                recs = by_min[bucket]
                avg = sum(r["true_clv_pct"] for r in recs) / len(recs)
                lines.append(f"| {bucket} | {len(recs)} | {avg:+.1f}% |")

    lines.append("")
    lines.append("---")
    lines.append("💡 CLV = (推送时BB赔率 - 收盘Pinnacle公平价) / 收盘Pinnacle公平价")

    return "\n".join(lines)


def push_report():
    """生成并推送到钉钉。"""
    from config.settings import send_dingtalk

    body = build_report(days=1)
    today = datetime.now(BJ_TZ).strftime("%m/%d")
    title = f"📊 CLV 日报 {today}"

    ok = send_dingtalk(title, body)
    if ok:
        logger.info("CLV 日报推送成功")
    else:
        logger.error("CLV 日报推送失败")


def main():
    if "--no-push" not in sys.argv:
        push_report()
    else:
        print(build_report(days=1))


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
