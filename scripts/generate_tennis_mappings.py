"""自动生成网球中文→英文队名映射。

1. 从 BB extracted JSON 读取所有网球比赛
2. 从 Pinnacle API 读取所有网球比赛
3. 按联赛匹配（LEAGUE_KEYWORDS）
4. 同联赛内按时间接近度 + 拼音模糊匹配自动生成映射
5. 输出 TEAM_NAME_MAP 格式的条目
"""
import json, sys, re, requests
from pathlib import Path
from difflib import SequenceMatcher

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PIN_SESSION = requests.Session()
PIN_SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
})

# ── LEAGUE_KEYWORDS 反向: Pinnacle name → BB league name(s) ──
# 从 bb_vs_pinnacle.py 的 LEAGUE_KEYWORDS 反向推导
PIN_TO_BB = {}
BB_TO_PIN = {
    "ATP - 博斯塔德公开赛": "ATP Bastad",
    "ATP - 大满贯温布尔登网球公开赛": "ATP Wimbledon",
    "ATP - 格施塔德公开赛": "ATP Gstaad",
    "ATP - 乌马格公开赛": "ATP Umag",
    "ATP挑战赛 - 格兰比公开赛": "ATP Challenger Granby",
    "ATP挑战赛 - 波哥大公开赛": "ATP Challenger Bogota",
    "ATP挑战赛 - 科尔德农斯公开赛": "ATP Challenger Cordenons",
    "ATP挑战赛 - 林肯公开赛": "ATP Challenger Lincoln",
    "ATP挑战赛 - 本斯霍滕公开赛": "ATP Challenger Bunschoten",
    "ATP挑战赛 - 波索布兰科公开赛": "ATP Challenger Pozoblanco",
    "WTA - 罗马公开赛": "WTA 125K Rome",
    "WTA - 雅西公开赛": "WTA Iasi",
    "WTA - 基茨比厄尔公开赛": "WTA 125K Kitzbuhel",
    "WTA - 伊斯坦布尔 2 公开赛": "WTA 125K Istanbul",
    "WTA - 伊斯坦堡 2 公开赛": "WTA 125K Istanbul",
    "WTA - 雅典公开赛": "WTA Athens",
    "WTA - 纽波特公开赛": "WTA 125K Newport",
    "世界网球 - M25": "ITF M25",
    "世界网球 - M15": "ITF M15",
    "世界网球 - W75": "ITF W75",
    "世界网球 - W50": "ITF W50",
    "世界网球 - W35": "ITF W35",
    "世界网球 - W15": "ITF W15",
    "世界網球 - M25": "ITF M25",
}
# Build reverse map
for bb_name, pin_prefix in BB_TO_PIN.items():
    PIN_TO_BB.setdefault(pin_prefix, []).append(bb_name)


def fetch_pin_tennis():
    """Fetch all tennis matchups from Pinnacle."""
    r = PIN_SESSION.get(
        "https://guest.api.arcadia.pinnacle.com/0.1/sports/33/matchups",
        timeout=30,
    )
    return r.json()


def extract_pin_players(matchups):
    """Extract (league_name, home_player, away_player, league_id) from Pinnacle matchups."""
    results = []
    for mu in matchups:
        league = mu.get("league", {})
        league_name = league.get("name", "")
        league_id = league.get("id")
        participants = mu.get("participants", [])
        if len(participants) >= 2:
            home = participants[0].get("name", "")
            away = participants[1].get("name", "")
            results.append((league_name, league_id, home, away, mu.get("startTime", "")))
    return results


def pin_matches_bb_league(pin_league_name, bb_league_name):
    """Check if a Pinnacle league name matches a BB league keyword."""
    pin_lower = pin_league_name.lower()
    for bb_name, pin_prefix in BB_TO_PIN.items():
        if bb_name == bb_league_name or bb_name in bb_league_name:
            # Check if pin_league_name starts with or contains the mapped prefix
            prefix_lower = pin_prefix.lower()
            if pin_lower.startswith(prefix_lower) or prefix_lower in pin_lower:
                return True
        # For ITF: BB "世界网球 - M15 乌斯拉尔 男子單打" contains "M15"
        # Pinnacle "ITF Men Uslar - R1" -> check if "M15" in some pattern
    return False


def _pinyin_key(name):
    """Convert Chinese name with dots to pinyin key."""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return name.lower()

    def _is_cjk(s):
        return any('一' <= c <= '鿿' for c in s)

    raw = name.split("(")[0].strip()  # strip (女) suffix etc.
    if not _is_cjk(raw):
        return ""
    syllables = raw.split(".")
    py = " ".join("".join(lazy_pinyin(s)).lower() for s in syllables)
    return py


def fuzzy_match_tennis_names(bb_home, bb_away, pin_entries):
    """Given BB Chinese names, find best Pinnacle match."""
    bb_home_py = _pinyin_key(bb_home)
    bb_away_py = _pinyin_key(bb_away)
    if not bb_home_py and not bb_away_py:
        return None, 0.0

    best_entry = None
    best_score = 0.0

    for pin_home, pin_away in pin_entries:
        scores = []
        if bb_home_py:
            scores.append(SequenceMatcher(None, bb_home_py, pin_home.lower()).ratio())
        if bb_away_py:
            scores.append(SequenceMatcher(None, bb_away_py, pin_away.lower()).ratio())
        avg = sum(scores) / len(scores) if scores else 0
        if avg > best_score:
            best_score = avg
            best_entry = (pin_home, pin_away)

    if best_score >= 0.40:
        return best_entry, best_score
    return None, 0.0


def main():
    # 1. Read BB extracted data
    bb_file = ROOT / "data" / "storage" / "bb_odds_extracted.json"
    bb_data = json.loads(bb_file.read_text())
    bb_matches = bb_data.get("matches", [])
    tennis_matches = [m for m in bb_matches if m.get("sport") == "tennis"]
    print(f"BB tennis matches: {len(tennis_matches)}")

    # Group by league
    bb_by_league = {}
    for m in tennis_matches:
        league = m.get("league", "?")
        bb_by_league.setdefault(league, []).append(m)

    # 2. Fetch Pinnacle tennis matchups
    print("\nFetching Pinnacle tennis matchups...")
    pin_data = fetch_pin_tennis()
    pin_entries = extract_pin_players(pin_data)
    print(f"Pinnacle tennis matchups: {len(pin_entries)}")

    # Group Pinnacle by normalized league name prefix
    pin_by_prefix = {}
    for league_name, lid, home, away, start in pin_entries:
        # Get base name (strip " - R1", " - R16", " - Doubles" etc.)
        base = re.sub(r'\s*-\s*(R\d+|Doubles|Qualifiers).*', '', league_name).strip()
        pin_by_prefix.setdefault(base, []).append((home, away, league_name, lid, start))

    print(f"Pinnacle unique base leagues: {len(pin_by_prefix)}")
    for base, entries in sorted(pin_by_prefix.items()):
        print(f"  {base}: {len(entries)} matches")

    # 3. For each BB tennis league, try to find Pinnacle counterpart
    mappings = {}  # bb_chinese_name -> pin_english_name
    league_stats = {}

    for bb_league, matches in sorted(bb_by_league.items()):
        # Determine which BB keyword matches
        matched_pin_base = None
        for bb_key, pin_prefix in sorted(BB_TO_PIN.items(), key=lambda x: -len(x[0])):
            if bb_key in bb_league:
                matched_pin_base = pin_prefix
                break

        if not matched_pin_base:
            # Try partial match: "世界网球 - M15 乌斯拉尔 男子單打" -> look for "M15"
            for bb_key, pin_prefix in sorted(BB_TO_PIN.items(), key=lambda x: -len(x[0])):
                if bb_key in bb_league or bb_key.replace("世界网球 - ", "世界網球 - ") in bb_league:
                    matched_pin_base = pin_prefix
                    break

        if not matched_pin_base:
            continue

        # Find matching Pinnacle leagues
        matching_pin_bases = set()
        for pin_base in pin_by_prefix:
            pin_base_lower = pin_base.lower()
            # Check if Pinnacle base name contains our mapped prefix
            if matched_pin_base and matched_pin_base.lower() in pin_base_lower:
                matching_pin_bases.add(pin_base)
            # For ITF leagues: also match by level + location
            # e.g. BB "世界网球 - M15 乌斯拉尔 男子單打" -> Pinnacle "ITF Men Uslar - R1"
            if "世界网球" in bb_league or "世界網球" in bb_league:
                bb_parts = bb_league.replace("世界网球 - ", "").replace("世界網球 - ", "").split()
                if len(bb_parts) >= 2:
                    level = bb_parts[0]  # M15, W35, etc.
                    location = bb_parts[1]  # 乌斯拉尔
                    if level.lower() in pin_base_lower:
                        matching_pin_bases.add(pin_base)

        if not matching_pin_bases:
            continue

        # Collect all Pinnacle entries for matching bases
        pin_for_league = []
        for base in matching_pin_bases:
            for home, away, league_name, lid, start in pin_by_prefix[base]:
                pin_for_league.append((home, away, league_name, lid, start))

        if not pin_for_league:
            continue

        league_stats[bb_league] = {
            "bb_count": len(matches),
            "pin_base": matched_pin_base,
            "pin_matched_bases": matching_pin_bases,
            "pin_count": len(pin_for_league),
        }

        # For each BB match, find best Pinnacle match by time proximity + name similarity
        for m in matches:
            bb_home = m.get("home", "")
            bb_away = m.get("away", "")
            bb_time = m.get("bt", 0)

            # Check if already in TEAM_NAME_MAP
            # (We'll check existing map later)

            # Current best approach: try pinyin matching
            pin_home_list = [p[0] for p in pin_for_league]
            pin_away_list = [p[1] for p in pin_for_league]

            best, score = fuzzy_match_tennis_names(
                bb_home, bb_away,
                list(zip(pin_home_list, pin_away_list))
            )

            if best and score >= 0.50:
                pin_home_en, pin_away_en = best
                if bb_home not in mappings and score >= 0.60:
                    mappings[bb_home] = (pin_home_en, score)
                if bb_away not in mappings and score >= 0.60:
                    mappings[bb_away] = (pin_away_en, score)

    # Print summary
    print(f"\n\n=== 联赛匹配统计 ===")
    for league, stats in sorted(league_stats.items(), key=lambda x: -x[1]["bb_count"]):
        print(f"\n{league}")
        print(f"  BB: {stats['bb_count']}场 | Pinnacle: {stats['pin_count']}场 ({stats['pin_matched_bases']})")

    print(f"\n\n=== 自动生成的 TEAM_NAME_MAP 条目 ({len(mappings)}) ===")
    # Sort by confidence
    sorted_mappings = sorted(mappings.items(), key=lambda x: -x[1][1])
    for cn_name, (en_name, score) in sorted_mappings:
        # Skip doubles pairs (contain "/") — individual lookup only
        if "/" in cn_name:
            continue
        # Skip entries with colons or semicolons (format issues)
        if ":" in cn_name or ";" in cn_name:
            continue
        print(f'    "{cn_name}": "{en_name}",  # score={score:.2f}')

    print(f"\n\n# 按条目插入 bb_vs_pinnacle.py 的 TEAM_NAME_MAP")
    print(f"# Total: {len(mappings)} entries")


if __name__ == "__main__":
    main()
