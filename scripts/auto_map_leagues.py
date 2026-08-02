#!/usr/bin/env python3
"""联赛自动映射工具 — 用模糊匹配发现 BB→Pinnacle 联赛映射。

用法:
  python3 scripts/auto_map_leagues.py           # 扫描并建议新映射
  python3 scripts/auto_map_leagues.py --apply   # 自动应用高置信度映射
"""

import json, sys, re
from pathlib import Path
from difflib import SequenceMatcher
from collections import Counter

PROJECT = Path(__file__).resolve().parent.parent
DATA = PROJECT / "data" / "storage"

def load_bb_leagues():
    """从 BB 数据提取所有联赛名。"""
    bb_file = DATA / "bb_odds_extracted.json"
    if not bb_file.exists():
        return {}
    bb = json.loads(bb_file.read_text())
    return Counter(m.get("league", "") for m in bb.get("matches", []))

def load_pin_league_names():
    """从 Pinnacle 结构提取所有英文联赛名。"""
    struct_file = DATA / "pinnacle_league_structure.json"
    if not struct_file.exists():
        return set()
    struct = json.loads(struct_file.read_text())
    names = set()
    for sport_id, sport_data in struct.items():
        if sport_id.startswith("_"): continue
        if not isinstance(sport_data, dict): continue
        for lid, info in sport_data.items():
            if isinstance(info, dict) and "name" in info:
                names.add(info["name"])
    return names

def load_keywords():
    kw_file = DATA / "league_keywords.json"
    if kw_file.exists():
        return json.loads(kw_file.read_text())
    return {}

def save_keywords(kw):
    DATA.joinpath("league_keywords.json").write_text(json.dumps(kw, ensure_ascii=False, indent=2))

def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def main():
    bb_leagues = load_bb_leagues()
    pin_names = load_pin_league_names()
    keywords = load_keywords()

    # 已在关键词中的跳过
    unmatched = {lg: n for lg, n in bb_leagues.most_common()
                 if lg not in keywords and lg != "?" and n >= 1}

    # 排除已匹配的
    comp_file = DATA / "bb_vs_pinnacle_comparison.json"
    if comp_file.exists():
        comp = json.loads(comp_file.read_text())
        matched = {d.get("league", "") for d in comp.get("details", [])}
        unmatched = {lg: n for lg, n in unmatched.items() if lg not in matched}

    # 排除乒乓球
    unmatched = {lg: n for lg, n in unmatched.items() if "TT" not in lg and "乒乓球" not in lg}

    print(f"待映射联赛: {len(unmatched)} 个, {sum(unmatched.values())} 场比赛\n")

    suggestions = []
    for bb_name, count in sorted(unmatched.items(), key=lambda x: -x[1]):
        # 提取有意义的词
        en_words = set(w.lower() for w in re.findall(r'[A-Za-z]{2,}', bb_name))
        cn_tokens = re.findall(r'[一-鿿]{2,}', bb_name)

        best_score = 0
        best_pin = ""

        for pin_name in pin_names:
            score = similarity(bb_name, pin_name)

            # 英文词匹配加分
            pin_words = set(pin_name.lower().split())
            en_overlap = en_words & pin_words
            if en_overlap:
                score += 0.1 * len(en_overlap)

            # 数子匹配加分 (如 U20, 2026)
            bb_nums = set(re.findall(r'\d+', bb_name))
            pin_nums = set(re.findall(r'\d+', pin_name))
            if bb_nums & pin_nums:
                score += 0.1

            if score > best_score:
                best_score = score
                best_pin = pin_name

        if best_score >= 0.5:  # 阈值
            suggestions.append((bb_name, count, best_pin, best_score))

    # 按置信度分组
    high = [(n, c, p, s) for n, c, p, s in suggestions if s >= 0.8]
    medium = [(n, c, p, s) for n, c, p, s in suggestions if 0.6 <= s < 0.8]
    low = [(n, c, p, s) for n, c, p, s in suggestions if s < 0.6]

    print(f"🟢 高置信度 (≥0.8): {len(high)} 个")
    for n, c, p, s in high[:20]:
        print(f"  {n} ({c}场) → {p} ({s:.0%})")

    print(f"\n🟡 中置信度 (0.6-0.8): {len(medium)} 个")
    for n, c, p, s in medium[:10]:
        print(f"  {n} ({c}场) → {p} ({s:.0%})")

    apply = "--apply" in sys.argv
    added = 0
    for n, c, p, s in high:
        if n not in keywords:
            keywords[n] = p
            added += 1
    if apply and added:
        save_keywords(keywords)
        print(f"\n✅ 已应用 {added} 个高置信度映射")
    elif not apply and high:
        print(f"\n💡 {len(high)} 个高置信度映射待应用 (--apply)")
    elif apply:
        print(f"\n无新增映射")

if __name__ == "__main__":
    main()
