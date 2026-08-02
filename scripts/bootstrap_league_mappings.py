#!/usr/bin/env python3
"""Bootstrapper: map ALL BB leagues to Pinnacle leagues using multi-strategy matching.

Strategies (in priority order):
1. Existing keyword mapping (fastest)
2. English token overlap (works for leagues with shared abbreviations)
3. Pinyin transliteration of Chinese city/location names → fuzzy match against Pinnacle
4. Manual hardcoded overrides for remaining edge cases

Output: saves updated league_keywords.json
"""

import json, re, sys
from pathlib import Path
from difflib import SequenceMatcher as SM
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BB_SPORT_KEYWORDS = {
    "足球": "football", "篮球": "basketball", "网球": "tennis",
    "棒球": "baseball", "美式足球": "american_football", "乒乓球": "pingpong",
    "拳击": "boxing", "MMA": "mma", "羽毛球": "badminton", "冰球": "ice_hockey",
    "排球": "volleyball", "斯诺克": "snooker",
}

# ---------------------------------------------------------------------------
# City name transliteration helper — maps common Chinese city names to their
# international (English) forms used in Pinnacle league names.
# ---------------------------------------------------------------------------
# Many ATP/WTA/ITF tournament cities use well-known transliterations.
# We use pypinyin as a fallback for unknown cities.
CITY_TRANSLATIONS = {
    # ATP/WTA/ITF common tournament cities
    "洛斯卡沃斯": "los cabos", "蒙特利尔": "montreal", "多伦多": "toronto",
    "华盛顿": "washington", "孟菲斯": "memphis", "波恩": "bonn",
    "哈根": "hagen", "圣马力诺": "san marino", "伊斯坦布尔": "istanbul",
    "利贝雷茨": "liberec", "普罗夫迪夫": "plovdiv", "萨姆松": "samsun",
    "温哥华": "vancouver", "莱克星顿": "lexington", "根措夫特": "gentofte",
    "奥尔德肖特": "aldershot", "皮特什蒂": "pitesti", "科沙林": "koszalin",
    "韦茨拉尔": "wetzlar", "爱德华兹维尔": "edwardsville",
    "马佐夫舍格罗济斯克": "grodzisk mazowiecki", "格罗济斯克": "grodzisk",
    "特尔古穆列什": "targu mures", "佛罗伦萨": "florence",
    "佩尔加米诺": "pergamino", "克诺克海斯特": "knokke heist",
    "黑兴根": "hechingen", "列克星敦": "lexington", "都柏林": "dublin",
    "大加那利岛": "gran canaria", "罗加什卡斯拉蒂纳": "rogaska slatina",
    "乌曼德拉": "umandra", "别尔斯科比亚瓦": "bielsko biala",
    "萨维泰帕莱": "savitaipale", "阿斯塔纳": "astana",
    "哈蒂瓦": "xativa", "圣保罗": "san paulo", "斯特拉斯堡": "strasbourg",
    "库尔索姆利斯卡班亚": "kursumlijska banja", "瓦曼特拉": "huamantla",
    "韦尔斯": "wels", "索菲亚": "sofia", "布达佩斯": "budapest",
    "布拉格": "prague", "维也纳": "vienna", "巴塞罗那": "barcelona",
    "马德里": "madrid", "罗马": "rome", "巴黎": "paris",
    "上海": "shanghai", "北京": "beijing", "东京": "tokyo",
    "悉尼": "sydney", "墨尔本": "melbourne",
}

# Pre-built manual mappings for leagues the auto-mapper gets wrong
MANUAL_LEAGUE_MAPPINGS = {
    # Tennis
    "ATP - 洛斯卡沃斯公开赛": "ATP Los Cabos - Final",
    "ATP - 蒙特利尔公开赛": "ATP Montreal - Final",
    "ATP挑战赛 - 波恩公开赛": "ATP Challenger Bonn - Final",
    "ATP挑战赛 - 哈根公开赛": "ATP Challenger Hagen - Qualifiers",
    "ATP挑战赛 - 圣马力诺公开赛": "ATP Challenger San Marino - Final",
    "ATP挑战赛 - 伊斯坦布尔 2 公开赛": "ATP Challenger Istanbul - Qualifiers",
    "ATP挑战赛 - 利贝雷茨公开赛": "ATP Challenger Liberec - Final",
    "ATP挑战赛 - 普罗夫迪夫 2 公开赛": "ATP Challenger Plovdiv - Qualifiers",
    "ATP挑战赛 - 萨姆松公开赛": "ATP Challenger Samsun - Final",
    "ATP挑战赛 - 温哥华公开赛": "ATP Challenger Vancouver - Final",
    "ATP挑战赛 - 马佐夫舍格罗济斯克公开赛": "ATP Challenger Grodzisk Mazowiecki - Qualifiers",
    "WTA - 华盛顿公开赛": "WTA Washington - Final",
    "WTA - 孟菲斯公开赛": "WTA Memphis - Final",
    "WTA - 多伦多公开赛": "WTA Toronto - Final",
    "WTA - 特尔古穆列什公开赛": "WTA 125k Targu Mures - Final",
    "ITF - M15 阿斯塔纳 男子单打": "ITF Men Astana - Final",
    "ITF - M15 哈蒂瓦 男子单打": "ITF Men Xativa - Final",
    "ITF - M15 圣保罗 男子单打": "ITF Men San Paulo - Final",
    "ITF - M15 都柏林 男子单打": "ITF Men Dublin - Final",
    "ITF - M15 韦尔斯 男子单打": "ITF Men Wels - Final",
    "ITF - M15 瓦曼特拉 男子单打": "ITF Men Huamantla - Final",
    "ITF - M25 皮特什蒂 男子单打": "ITF Men Pitesti - Final",
    "ITF - M25 科沙林 男子单打": "ITF Men Koszalin - Final",
    "ITF - M25 韦茨拉尔 男子单打": "ITF Men Wetzlar - Final",
    "ITF - M25 爱德华兹维尔 男子单打": "ITF Men Edwardsville - Final",
    "ITF - M25 奥尔德肖特 男子单打": "ITF Men Aldershot - Final",
    "ITF - M25 根措夫特 男子单打": "ITF Men Gentofte - Final",
    "ITF - W75 黑兴根 女子单打": "ITF Women Hechingen W75 - Final",
    "ITF - W75 列克星敦 女子单打": "ITF Women Lexington W75 - Final",
    "ITF - W50 克诺克海斯特 女子单打": "ITF Women Knokke Heist W50 - Final",
    "ITF - W50 都柏林 女子单打": "ITF Women Dublin W50 - Final",
    "ITF - W35 佛罗伦萨 女子单打": "ITF Women Florence W35 - Final",
    "ITF - W35 佩尔加米诺 女子单打": "ITF Women Pergamino W35 - Final",
    "ITF - W35 奥尔德肖特 女子单打": "ITF Women Aldershot W35 - Final",
    "ITF - W15 罗加什卡斯拉蒂纳 女子单打": "ITF Women Rogaska Slatina W15 - Final",
    "ITF - W15 别尔斯科比亚瓦 女子单打": "ITF Women Bielsko Biala W15 - Final",
    "ITF - W15 库尔索姆利斯卡班亚 女子单打": "ITF Women Kursumlijska Banja W15 - Final",
    "ITF - W15 阿斯塔纳 女子单打": "ITF Women Astana W15 - Final",
    "ITF - W15 萨维泰帕莱 女子单打": "ITF Women Savitaipale W15 - Final",
    "ITF - W15 乌曼德拉 女子单打": "ITF Women Umandra W15 - Final",
    "ITF - W100 大加那利岛 女子单打": "ITF Women Gran Canaria W100 - Final",
    "ITF - M15 斯特拉斯堡 男子单打": "ITF Men Strasbourg - Final",
    "ITF - M15 库尔索姆利斯卡班亚 男子单打": "ITF Men Kursumlijska Banja - Final",
    # Challenger doubles
    "ATP挑战赛 - 哈根公开赛 - 双打": "ATP Challenger Hagen - Qualifiers",
    "ATP挑战赛 - 马佐夫舍格罗济斯克公开赛 - 双打": "ATP Challenger Grodzisk Mazowiecki - Qualifiers",
    "WTA - 特尔古穆列什公开赛 - 双打": "WTA 125k Targu Mures - Doubles",
    "ITF - W75 黑兴根 女子双打": "ITF Women Hechingen W75 - Final",
    # Ice Hockey (BB: 澳洲冰球联盟 → Pin: Australia - IHL)
    "澳洲冰球联盟": "Australia - IHL",
    # Baseball
    "中华职业棒球大联盟": "Chinese Taipei - Professional League",
    # Football — important leagues
    "乌克兰超级联赛": "Ukraine - Premier League",
    "塞尔维亚超级联赛": "Serbia - Super Liga",
    "塞尔维亚甲级联赛": "Serbia - First League",
    "土耳其甲级联赛": "Turkey - 1. Lig",
    "斯洛文尼亚甲级联赛": "Slovenia - 1. SNL",
    "斯洛伐克乙级联赛": "Slovakia - 1. Liga",
    "斯洛伐克杯": "Slovakia - Cup",
    "捷克杯": "Czech Republic - Cup",
    "保加利亚超级杯": "Bulgaria - Super Cup",
    "希腊超级杯": "Greece - Super Cup",
    "黑山甲级联赛": "Montenegro - First League",
    "厄瓜多尔甲级联赛": "Ecuador - Liga Pro",
    "智利甲级联赛": "Chile - Primera Division",
    "智利女子甲级联赛": "Chile - Primera Division Women",
    "智利丁级联赛": "Chile - Tercera Division",
    "玻利维亚甲级联赛": "Bolivia - Division Profesional",
    "玻利维亚全国联赛 U19": "Bolivia - Division Profesional",
    "洪都拉斯甲级联赛": "Honduras - Liga Nacional",
    "尼加拉瓜甲级联赛": "Nicaragua - Liga Primera",
    "巴拉圭甲级联赛": "Paraguay - Primera Division",
    "巴拉圭女子锦标赛": "Paraguay - Primera Division Women",
    "白俄罗斯超级联赛": "Belarus - Premier League",
    "爱沙尼亚乙级联赛": "Estonia - Esiliiga",
    "拉脱维亚甲级联赛": "Latvia - Virsliga",
    "黎巴嫩超级联赛": "Lebanon - Premier League",
    "南非超级联赛": "South Africa - Premier League",
    "印度杜兰德杯": "India - Durand Cup",
    "哥斯达黎加乙级联赛": "Costa Rica - Liga de Ascenso",
    "威尔士超级联赛": "Wales - Cymru Premier",
    "威尔士冠军联赛北部组": "Wales - Cymru North",
    "芬兰丙级联赛": "Finland - Kakkonen",
    "波兰丙级联赛": "Poland - 2. Liga",
    "波兰女子超级联赛": "Poland - Ekstraliga Women",
    "挪威女子甲级联赛": "Norway - 1st Division Women",
    "奥地利丙级联赛西部组": "Austria - Regionalliga West",
    "奥地利女子甲级联赛": "Austria - Frauenliga",
    "乌拉圭女子联赛": "Uruguay - Primera Division Women",
    "墨西哥超级联赛 U19": "Mexico - U19",
    "墨西哥超级联赛 U21": "Mexico - U21",
    "墨西哥女子联赛 U19": "Mexico - Liga MX Femenil U19",
    "葡萄牙甲级联赛": "Portugal - Liga Portugal 2",
    "中国甲级联赛": "China - Jia League",
    "中国青少年精英联赛 U20": "China - U20 League",
    "阿根廷丙组曼特波里顿联赛": "Argentina - Primera B Metropolitana",
    "马来西亚总统杯 U20": "Malaysia - President Cup U20",
    "非洲女子国家杯 (在摩洛哥)": "Africa - Women Cup of Nations",
    "WAFUB区非洲国家杯 U20 (在科特迪瓦)": "Africa - U20 Cup of Nations",
    "东南亚现代杯": "ASEAN - Championship",
    "马拉维八强杯": "Malawi - Super League",
    "澳大利亚北领地超级联赛": "Australia - NPL Northern Territory",
    "澳大利亚新南威尔士州全国超级联赛": "Australia - NPL New South Wales",
    "澳大利亚昆士兰州超级联赛": "Australia - NPL Queensland",
    "澳大利亚昆士兰州超级联赛2": "Australia - NPL Queensland 2",
    "澳大利亚昆士兰女子超级联赛": "Australia - NPL Queensland Women",
    "巴西圣保罗州联赛U20": "Brazil - Paulista U20",
    "巴西女子足球锦标赛A2": "Brazil - Brasileiro Women A2",
    "巴西米内罗乙级联赛": "Brazil - Mineiro 2",
    "巴西卡皮克斯巴乙级联赛": "Brazil - Capixaba 2",
    "巴西国亚诺乙级联赛": "Brazil - Goiano 2",
    "巴西巴拉拿丙级联赛": "Brazil - Paranaense 3",
    "巴西利亚联赛U20-附加赛": "Brazil - Brasiliense U20",
    "巴西圣保罗州乙級联赛U23-附加赛": "Brazil - Paulista U23",
    "巴西塞阿仁斯区联赛 U20-附加赛": "Brazil - Cearense U20",
    "巴西波蒂加联赛 U20": "Brazil - Potiguar U20",
    "巴西甲级联赛": "Brazil - Serie A",
    # European national leagues
    "欧洲国家联赛 A组": "Europe - UEFA Nations League A",
    "欧洲国家联赛 B组": "Europe - UEFA Nations League B",
    # Basketball leagues that Pinnacle covers
    "欧洲篮球联赛": "Euroleague",
    "篮球俱乐部友谊赛": "World - Club Friendlies",
    "中美洲及加勒比海运动会女子篮球U20": "World - U20 Women",
    "巴西LBF女子篮球联赛": "Brazil - LBF Women",
    "加拿大精英篮球联赛": "Canada - Elite Basketball League",
    "新西兰全国篮球联赛": "New Zealand - NBL",
    "智利全国篮球联赛": "Chile - LNB",
    "智利篮球 SAESA 联赛": "Chile - LNB",
    "智利全国篮球乙级联赛": "Chile - LNB 2",
    "越南职业篮球联赛": "Vietnam - VBA",
    "韩国女子大学篮球联赛": "South Korea - WKBL",
    "菲律宾PBA总督杯": "Philippines - PBA Governors Cup",
    # Volleyball
    "FIVB国际排球联赛": "FIVB - Nations League",
    "排球女子俱乐部友谊赛": "World - Club Friendlies Women",
    "东南亚女子排球杯": "AVC - Women's Cup",
    # English League Cup qualifiers
    "英格兰联赛杯-资格赛": "England - EFL Cup",
    "澳大利亚Big V女子篮球联赛": "Australia - Big V Women",
    "澳大利亚东部篮球联赛": "Australia - NBL1 East",
    "波多黎各国家篮球联赛": "Puerto Rico - BSN",
    # CFL (Canadian Football)
    "CFL加拿大美式足球": "Canada - CFL",
    # College football
    "美国大学美式足球": "NCAA - Football",
}


def main():
    # Load Pinnacle league structure
    structure_path = ROOT / 'data/storage/pinnacle_league_structure.json'
    with open(structure_path) as f:
        structure = json.load(f)

    # Flatten Pinnacle leagues: {name → league_id}
    pin_name_to_id = {}
    pin_id_to_name = {}
    for sport_id, sport_data in structure.items():
        if not isinstance(sport_data, dict):
            continue
        for lid, linfo in sport_data.items():
            if isinstance(linfo, dict) and 'name' in linfo:
                name = linfo['name']
                pin_name_to_id[name.lower()] = lid
                pin_id_to_name[lid] = name

    # Load existing keywords
    kw_path = ROOT / 'data/storage/league_keywords.json'
    with open(kw_path) as f:
        keywords = json.load(f)

    # Load BB data to get all leagues
    bb_path = ROOT / 'data/storage/bb_odds_extracted.json'
    with open(bb_path) as f:
        bb_data = json.load(f)

    bb_leagues = set()
    for m in bb_data['matches']:
        bb_leagues.add(m.get('league', ''))

    # Stats
    already_mapped = 0
    newly_mapped = 0

    # Apply manual mappings first
    for bb_name, pin_name in MANUAL_LEAGUE_MAPPINGS.items():
        if bb_name not in keywords and bb_name in bb_leagues:
            # Verify this Pinnacle league exists
            matched = False
            for stored_name, lid in pin_name_to_id.items():
                if _match_pin_name(pin_name, stored_name):
                    keywords[bb_name] = pin_name
                    newly_mapped += 1
                    matched = True
                    break
            if not matched:
                print(f'  ⚠️ Manual mapping not found in Pinnacle: {bb_name} → {pin_name}')

    # For remaining unmapped BB leagues, try pinyin+city matching
    try:
        from pypinyin import lazy_pinyin
        HAS_PINYIN = True
    except ImportError:
        HAS_PINYIN = False
        print("  ⚠️ pypinyin not installed, skipping pinyin matching")

    if HAS_PINYIN:
        for bb_name in sorted(bb_leagues):
            if bb_name in keywords:
                already_mapped += 1
                continue

            # Skip non-sport leagues
            bb_sport = 'football'
            for kw, s in BB_SPORT_KEYWORDS.items():
                if kw in bb_name:
                    bb_sport = s
                    break

            # Only process sports Pinnacle covers
            if bb_sport in ('pingpong', 'boxing', 'mma', 'badminton', 'snooker'):
                continue

            # Try to match using Chinese location names
            match = _match_by_city(bb_name, pin_name_to_id, bb_sport)
            if match:
                keywords[bb_name] = match
                newly_mapped += 1
                print(f'  ✓ {bb_name} → {match}')
                continue

            # For tennis: try number-based matching
            if bb_sport == 'tennis':
                match = _match_tennis_by_level(bb_name, pin_name_to_id)
                if match:
                    keywords[bb_name] = match
                    newly_mapped += 1
                    print(f'  ✓ {bb_name} → {match}')
                    continue

    # Check if manual mappings are missing any
    missing_from_manual = [l for l in MANUAL_LEAGUE_MAPPINGS if l not in bb_leagues]
    if missing_from_manual:
        print(f'\n  ⚠️ {len(missing_from_manual)} manual mappings not in current BB data (stale):')
        for l in sorted(missing_from_manual)[:10]:
            print(f'    {l}')

    # Save
    with open(kw_path, 'w') as f:
        json.dump(keywords, f, ensure_ascii=False, indent=2)

    total_mapped = sum(1 for l in bb_leagues if l in keywords)
    print(f'\n  📊 League coverage: {total_mapped}/{len(bb_leagues)} ({total_mapped*100//len(bb_leagues)}%)')
    print(f'  🆕 Newly mapped: {newly_mapped}')
    print(f'  ✅ Already mapped: {already_mapped}')
    print(f'  ❌ Still unmapped: {len(bb_leagues) - total_mapped}')
    print(f'  💾 Saved to {kw_path}')


def _match_pin_name(needle, haystack):
    """Word-boundary substring match."""
    nl = needle.lower()
    hl = haystack.lower()
    idx = hl.find(nl)
    while idx != -1:
        before = idx == 0 or hl[idx - 1] in " -"
        after = idx + len(nl) >= len(hl) or hl[idx + len(nl)] in " -"
        if before and after:
            return True
        idx = hl.find(nl, idx + 1)
    return False


def _match_by_city(bb_name, pin_name_to_id, sport):
    """Match BB league name by extracting Chinese city/location names,
    transliterating to pinyin, and fuzzy-matching against Pinnacle names."""
    from pypinyin import lazy_pinyin

    # Extract city names: look for CJK strings between delimiters
    # Common patterns: "ATP挑战赛 - 波恩公开赛", "WTA - 多伦多公开赛"
    parts = re.split(r'[-－\s]+', bb_name)

    # Try pinyin conversion for the whole non-English part
    cjk_parts = []
    for p in parts:
        # Only keep CJK-dominant parts (not "ATP", "WTA", "ITF", numbers)
        if not p or p.isascii() or p.isdigit():
            continue
        py = "".join(lazy_pinyin(p)).lower()
        py = re.sub(r'[^a-z]', '', py)
        if len(py) >= 3:
            cjk_parts.append(py)

    if not cjk_parts:
        return None

    # Try each CJK part against Pinnacle league names
    best_match = None
    best_score = 0.0

    for lid, pin_name in pin_name_to_id.items():
        pin_lower = pin_name.lower()
        score = 0.0
        matches = 0

        for py_part in cjk_parts:
            if len(py_part) < 3:
                continue
            sm = SM(None, py_part, pin_lower)
            part_score = sm.ratio()
            if part_score > 0.5:
                matches += 1
                score += part_score

        if matches > 0:
            score = score / len(cjk_parts)  # average
            if score > best_score:
                best_score = score
                best_match = pin_name

    if best_score >= 0.55 and best_match:
        return best_match
    return None


def _match_tennis_by_level(bb_name, pin_name_to_id):
    """Match tennis ITF events by level number (M15, W35, etc.)"""
    # Extract level: M15, M25, W15, W35, W50, W75, W100
    level_match = re.search(r'[MW]\d{2,3}', bb_name)
    if not level_match:
        return None

    level = level_match.group(0)
    # Gender: M→Men, W→Women
    gender = 'Men' if level.startswith('M') else 'Women'

    # Search for matching Pinnacle league
    candidates = []
    for pin_name in pin_name_to_id.values():
        pin_lower = pin_name.lower()
        if gender.lower() in pin_lower and level.lower() in pin_lower:
            candidates.append(pin_name)

    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates — need city matching
    if candidates:
        from pypinyin import lazy_pinyin
        best = None
        best_score = 0.0

        # Extract city from BB name
        bb_parts = re.split(r'[-－\s]+', bb_name)
        city_pinyin = ""
        for p in bb_parts:
            if not p or p.isascii() or p.isdigit():
                continue
            city_pinyin = "".join(lazy_pinyin(p)).lower()
            city_pinyin = re.sub(r'[^a-z]', '', city_pinyin)
            if len(city_pinyin) >= 3:
                break

        if city_pinyin:
            for c in candidates:
                sm = SM(None, city_pinyin, c.lower())
                if sm.ratio() > best_score:
                    best_score = sm.ratio()
                    best = c

        if best_score >= 0.5:
            return best

    # Return first candidate as fallback
    if candidates:
        return candidates[0]
    return None


if __name__ == '__main__':
    main()
