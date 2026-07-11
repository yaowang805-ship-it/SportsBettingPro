#!/usr/bin/env python3
"""BB体育 赔率快速录入与匹配 — 交互式命令行工具。

用法:
    python3 scripts/bb_correlation.py              # 显示最近比赛
    python3 scripts/bb_correlation.py --record     # 交互式录入 BB体育 赔率
    python3 scripts/bb_correlation.py --analyze    # 分析最佳匹配
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fetchers.bsd_fetcher import fetch_upcoming_events, fetch_event_odds

DATA_FILE = ROOT / "data" / "bb_correlation_data.json"

ANALYSIS_BOOKMAKERS = [
    "pinnacle", "bet365", "william-hill", "1xbet", "bwin",
    "betway", "unibet", "marathon", "888sport", "betano",
    "sportingbet", "betvictor", "ladbrokes", "betsson",
    "interwetten", "novibet", "coral", "betfair",
]


def collect_matches():
    """采集所有比赛。"""
    events = fetch_upcoming_events(hours_ahead=72)
    matches = []
    for ev in events:
        raw = fetch_event_odds(ev["id"])
        markets = raw.get("markets", {})
        h2h = markets.get("1x2")
        if not h2h:
            continue
        bms = {}
        for side_key, side_name in [("HOME", "home"), ("AWAY", "away"), ("DRAW", "draw")]:
            side_data = h2h.get(side_key, {}).get("bookmakers", {})
            for slug in ANALYSIS_BOOKMAKERS:
                odds_data = side_data.get(slug, {}).get("decimal_odds")
                if odds_data:
                    bms.setdefault(slug, {})[side_name] = float(odds_data)
        if bms:
            matches.append({
                "home": ev["home_team"],
                "away": ev["away_team"],
                "league": ev.get("league_name", ""),
                "bookmakers": bms,
            })
    return matches


def list_matches(matches):
    """列出比赛供用户对照录入。"""
    print(f"\n共 {len(matches)} 场有赔率的比赛")
    print()
    print(f"{'#':>2} {'主队':<25} {'客队':<25} {'联赛':<20} {'Pinnacle主/平/客':<25}")
    print("-" * 100)
    for i, m in enumerate(matches, 1):
        p = m["bookmakers"].get("pinnacle", {})
        p_str = f"{p.get('home',0):.2f}/{p.get('draw',0):.2f}/{p.get('away',0):.2f}" if p else "—"
        print(f"{i:>2}  {m['home']:<25} {m['away']:<25} {m['league'][:18]:<20} {p_str:<25}")


def interactive_record(matches):
    """交互式录入 BB体育 赔率。"""
    records = []

    # 先加载已有记录
    if DATA_FILE.exists():
        try:
            records = json.loads(DATA_FILE.read_text())
            print(f"\n已有 {len(records)} 条录入记录\n")
        except:
            records = []

    existing = {(r["home"], r["away"]): r for r in records}

    print("\n====== BB体育 赔率录入 ======")
    print("输入 '-' 跳过, 输入 'q' 结束\n")

    for i, m in enumerate(matches, 1):
        key = (m["home"], m["away"])
        if key in existing:
            bb = existing[key].get("bb", {})
            print(f"[{i}/{len(matches)}] ✅ 已录: {m['home']} vs {m['away']} "
                  f"(主{bb.get('home',0)} 平{bb.get('draw',0)} 客{bb.get('away',0)})")
            continue

        p = m["bookmakers"].get("pinnacle", {})
        p_str = f"Pinnacle: {p.get('home',0):.2f} / {p.get('draw',0):.2f} / {p.get('away',0):.2f}"

        print(f"\n[{i}/{len(matches)}] {m['home']} vs {m['away']} [{m['league']}]")
        print(f"  {p_str}")

        inp = input("  BB体育赔率 (主/平/客, 逗号分隔): ").strip()
        if inp.lower() == 'q':
            break
        if inp == '-':
            continue

        parts = inp.replace("，", ",").split(",")
        if len(parts) != 3:
            print("  格式错误，跳过")
            continue

        try:
            bb_h, bb_d, bb_a = float(parts[0]), float(parts[1]), float(parts[2])
            records.append({
                "home": m["home"],
                "away": m["away"],
                "league": m["league"],
                "bb": {"home": bb_h, "draw": bb_d, "away": bb_a},
            })
            print(f"  ✅ 已记录: {bb_h} / {bb_d} / {bb_a}")
        except ValueError:
            print("  数字格式错误，跳过")

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(f"\n已保存 {len(records)} 条记录到 {DATA_FILE}")


def analyze(matches):
    """分析哪家博彩公司最接近 BB体育。"""
    if not DATA_FILE.exists():
        print("❌ 没有 BB体育 数据，请先运行 --record 录入")
        return

    records = json.loads(DATA_FILE.read_text())
    if len(records) < 3:
        print(f"❌ 只有 {len(records)} 条数据，至少需要 3 条才有意义")
        return

    print(f"\n分析 {len(records)} 条 BB体育 赔率记录...\n")

    analysis = {}
    for slug in ANALYSIS_BOOKMAKERS:
        errors = []
        count = 0
        for rec in records:
            m = next((x for x in matches if x["home"] == rec["home"] and x["away"] == rec["away"]), None)
            if not m:
                continue
            bm = m["bookmakers"].get(slug)
            if not bm:
                continue
            bb = rec["bb"]
            sides = 0
            for side in ["home", "draw", "away"]:
                if side in bm and side in bb and bb[side] > 0 and bm[side] > 0:
                    errors.append(abs(bm[side] - bb[side]))
                    sides += 1
                    count += 1
            if sides > 0:
                analysis.setdefault(slug, {"total_err": 0, "count": 0, "max_err": 0, "matches": set()})
                analysis[slug]["total_err"] += sum(errors[-sides:])
                analysis[slug]["count"] += sides
                analysis[slug]["max_err"] = max(analysis[slug]["max_err"], max(errors[-sides:]))
                analysis[slug]["matches"].add(f"{rec['home']} vs {rec['away']}")

    results = [(slug, v) for slug, v in analysis.items() if v["count"] > 0]
    results.sort(key=lambda x: x[1]["total_err"] / x[1]["count"])

    print(f"{'排名':>3} {'博彩公司':<18} {'MAE':<8} {'最大误差':<8} {'样本数':<6} {'覆盖比赛':<6}")
    print("-" * 70)
    for i, (slug, v) in enumerate(results, 1):
        mae = v["total_err"] / v["count"]
        print(f"{i:>3} {slug:<18} {mae:.4f}  {v['max_err']:.4f}     {v['count']:<6} {len(v['matches'])}")

    best = results[0]
    best_mae = best[1]["total_err"] / best[1]["count"]
    print(f"\n🏆 最佳匹配: {best[0]}  (MAE={best_mae:.4f})")

    # 显示对比样本
    print(f"\n  样本对比 (BB体育 vs {best[0]}):")
    for rec in records:
        m = next((x for x in matches if x["home"] == rec["home"] and x["away"] == rec["away"]), None)
        if not m:
            continue
        bm = m["bookmakers"].get(best[0])
        if not bm:
            continue
        bb = rec["bb"]
        diffs = []
        for side, label in [("home", "主"), ("draw", "平"), ("away", "客")]:
            if side in bm and side in bb and bb[side] > 0:
                d = bm[side] - bb[side]
                diffs.append(f"{label}{bm[side]:.2f}vs{bb[side]:.2f}({d:+.2f})")
        print(f"    {rec['home']:25s} vs {rec['away']:20s}  {' | '.join(diffs)}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="BB体育 赔率匹配工具")
    parser.add_argument("--record", action="store_true", help="交互式录入 BB体育 赔率")
    parser.add_argument("--analyze", action="store_true", help="分析最佳匹配")
    args = parser.parse_args()

    print("正在采集赛事数据...")
    matches = collect_matches()

    if args.record:
        list_matches(matches)
        interactive_record(matches)
        # 录完后自动分析
        if DATA_FILE.exists():
            recs = json.loads(DATA_FILE.read_text())
            if len(recs) >= 3:
                print("\n--- 自动分析 ---")
                analyze(matches)
    elif args.analyze:
        analyze(matches)
    else:
        list_matches(matches)


if __name__ == "__main__":
    main()
