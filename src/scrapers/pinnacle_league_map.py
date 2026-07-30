"""Pinnacle league mapping utilities

Extracted from bb_vs_pinnacle.py for modularity.
Handles:
- League structure caching (load/save with TTL)
- Team name mapping (Chinese -> English, load/save)
- League keyword matching (Chinese BB name -> Pinnacle name)
- Auto-mapping of leagues and team names from high-confidence matches
- ITF tennis location-based league matching
- Pinnacle league ID resolution (keywords + fuzzy + sub-league expansion)
"""
import json, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import DATA_DIR
from src.scrapers.pinnacle_api import api_get

from src.scrapers.bb_data import BB_SPORT_KEYWORDS

# ---------------------------------------------------------------------------
# Module-level variables
# ---------------------------------------------------------------------------

# Pinnacle league structure (sport -> list of leagues -> ID/name), nearly static
PINNACLE_LEAGUE_FILE = DATA_DIR / "pinnacle_league_structure.json"
CACHE_TTL_DAYS = 7  # force refresh after this many days

# Team name mapping (Chinese -> English), loaded from file
TEAM_NAME_MAP_FILE = DATA_DIR / "team_name_map.json"

# League keyword mapping (Chinese BB name -> Pinnacle name), loaded from file
LEAGUE_KEYWORDS_FILE = DATA_DIR / "league_keywords.json"


# ---------------------------------------------------------------------------
# Load / save helpers
# ---------------------------------------------------------------------------

def _load_league_structure(force_refresh: bool = False):
    """Load Pinnacle league structure from cache file; return empty dict if
    the cache is older than CACHE_TTL_DAYS or *force_refresh* is True."""
    if force_refresh:
        return {}
    if PINNACLE_LEAGUE_FILE.exists():
        age_seconds = time.time() - PINNACLE_LEAGUE_FILE.stat().st_mtime
        age_days = age_seconds / 86400
        if age_days > CACHE_TTL_DAYS:
            print(f"  ⏳ 联赛结构缓存已过期（{age_days:.1f} 天 > {CACHE_TTL_DAYS} 天），重新拉取...")
            return {}
        return json.loads(PINNACLE_LEAGUE_FILE.read_text())
    return {}


def _save_league_structure(data):
    """Save Pinnacle league structure to cache file."""
    PINNACLE_LEAGUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PINNACLE_LEAGUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"  \U0001f4c1 Pinnacle 联赛结构已保存到 {PINNACLE_LEAGUE_FILE}")


def _load_team_name_map():
    """Load team name mapping (Chinese -> English) from file."""
    if TEAM_NAME_MAP_FILE.exists():
        return json.loads(TEAM_NAME_MAP_FILE.read_text())
    print("  ⚠️ team_name_map.json 不存在，返回空映射")
    return {}


def _save_team_name_map(data):
    """Save team name mapping to file."""
    TEAM_NAME_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEAM_NAME_MAP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"  \U0001f4c1 队名映射已保存 ({len(data)} 条)")


def _load_league_keywords():
    """Load league keyword mapping (Chinese BB name -> Pinnacle name) from file."""
    if LEAGUE_KEYWORDS_FILE.exists():
        return json.loads(LEAGUE_KEYWORDS_FILE.read_text())
    print("  ⚠️ league_keywords.json 不存在，返回空映射")
    return {}


def _save_league_keywords(data):
    """Save league keyword mapping to file."""
    LEAGUE_KEYWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEAGUE_KEYWORDS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"  \U0001f4c1 联赛关键词已保存 ({len(data)} 条)")


# ---------------------------------------------------------------------------
# Module-level mutable data (initialised at import time)
# ---------------------------------------------------------------------------

TEAM_NAME_MAP = _load_team_name_map()
LEAGUE_KEYWORDS = _load_league_keywords()


# 通用英文关键词：单独出现不足以确定联赛身份
_GENERIC_KEYWORD_BLACKLIST = {"open", "cup", "league", "tour", "tournament", "series", "masters", "grand", "slam", "classic", "trophy", "final", "qualifiers", "group", "premier", "super", "club", "international", "championship", "division", "liga", "serie", "primera", "segunda", "tercera", "first", "second", "third", "national"}

# ---------------------------------------------------------------------------
# Auto-discovery: new leagues
# ---------------------------------------------------------------------------

def discover_new_leagues(all_pin_leagues):
    """扫描 Pinnacle 联赛，寻找未映射的 BB 联赛并自动匹配。"""
    import re
    from difflib import SequenceMatcher as _SM
    new_mappings = {}

    # 收集BB联赛列表
    bb_leagues = set()
    try:
        bb = json.loads((DATA_DIR / 'bb_odds_extracted.json').read_text())
        for m in bb.get('matches', []):
            bb_leagues.add(m.get('league', ''))
    except: pass

    for pin_id, info in all_pin_leagues.items():
        if not isinstance(info, dict): continue
        pin_name = info.get("name", "")
        if not pin_name or info.get("matchup_count", 0) == 0: continue
        if pin_name in LEAGUE_KEYWORDS.values(): continue

        # 提取Pin联赛名的有意义英文词
        en_words = set(w.lower() for w in re.findall(r'[A-Za-z]{3,}', pin_name))
        meaningful = {w for w in en_words if w not in _GENERIC_KEYWORD_BLACKLIST}
        if not meaningful: continue

        # 在BB未映射联赛中搜索
        for bb_name in bb_leagues:
            if bb_name in LEAGUE_KEYWORDS or bb_name in new_mappings: continue
            bb_lower = bb_name.lower()
            # 全部有意义词都在BB名中 → 匹配
            if all(w in bb_lower for w in meaningful):
                # 跨运动校验
                bb_sport = None
                for kw, s in BB_SPORT_KEYWORDS.items():
                    if kw in bb_name: bb_sport = s; break
                if bb_sport is None: bb_sport = 'football'
                pin_sport_en = {'Soccer':'football','Basketball':'basketball','Tennis':'tennis',
                    'Baseball':'baseball','American Football':'american_football',
                    'Mixed Martial Arts':'mma','Boxing':'boxing','Ice Hockey':'ice_hockey'
                }.get(info.get('sport',''), '')
                if pin_sport_en and bb_sport != pin_sport_en: continue

                new_mappings[bb_name] = pin_name
                break

    if new_mappings:
        for bb_name, pin_name in new_mappings.items():
            LEAGUE_KEYWORDS[bb_name] = pin_name
        _save_league_keywords(LEAGUE_KEYWORDS)
        print(f"  🆕 自动发现 {len(new_mappings)} 个新联赛映射")
    return new_mappings


# ---------------------------------------------------------------------------
# Auto-mapping: leagues
# ---------------------------------------------------------------------------

def _auto_map_leagues(unmatched_bb_leagues, all_pin_leagues, dry_run=False):
    """Auto-map unmatched BB league names to Pinnacle league IDs.

    Multi-strategy matching (priority order):
    1. English token overlap - extract English words from BB name vs Pinnacle name
    2. Number + remaining text fuzzy - "U22" -> "Brazil - LDB U22"
    3. Pure CJK names - detect known sport keyword then filter by sport

    Matches with confidence >= 0.5 are saved to LEAGUE_KEYWORDS.
    """
    import re as _re
    from difflib import SequenceMatcher as _SM

    if not unmatched_bb_leagues:
        return {}

    # Known auto-map false-positive blacklist - prevents NZIHL -> NHL etc.
    _BANNED_AUTO_MAP = {"nzihl新西兰冰球联盟"}

    new_mappings = {}

    for bb_name in sorted(unmatched_bb_leagues):
        if bb_name.lower() in _BANNED_AUTO_MAP:
            continue
        # ITF网球: 走地点匹配(_find_itf_league_ids), 不靠auto-map
        if bb_name.startswith('ITF'):
            continue
        # Doubles matches: Pinnacle does not offer ITF doubles odds
        if "双打" in bb_name:
            continue

        bb_lower = bb_name.lower().strip()

        # Extract English tokens and standalone numbers
        bb_en_tokens = _re.findall(r'[a-z]+', bb_lower)
        bb_en_set = set(bb_en_tokens)
        bb_numbers = _re.findall(r'\d+', bb_name)
        bb_has_cjk = any('一' <= c <= '鿿' for c in bb_name)

        # Determine likely sport from BB name keywords
        bb_sport = None
        for kw, s in BB_SPORT_KEYWORDS.items():
            if kw in bb_name:
                bb_sport = s
                break
        if bb_sport is None:
            bb_sport = 'football'  # 大部分未匹配联赛是足球, 跨运动由国家名+运动校验兜底

        candidates = []

        for lid, info in all_pin_leagues.items():
            pin_name = info.get("name", "")
            pin_sport = info.get("sport", "")
            if not pin_name:
                continue
            pin_lower = pin_name.lower()
            pin_words = set(pin_lower.split())

            score = 0.0

            # Sport filter bonus: same sport = +0.1
            # Pinnacle sport 可能是中文或英文
            _pin_sport_en = {"足球":"football","篮球":"basketball","网球":"tennis","棒球":"baseball",
                              "美式足球":"american_football","乒乓球":"pingpong","拳击":"boxing",
                              "MMA":"mma","羽毛球":"badminton","冰球":"ice_hockey","排球":"volleyball",
                              "Soccer":"football","Basketball":"basketball","Tennis":"tennis",
                              "Baseball":"baseball","American Football":"american_football",
                              "Ice Hockey":"ice_hockey","Volleyball":"volleyball",
                              "Boxing":"boxing","Mixed Martial Arts":"mma",
                              "Badminton":"badminton","Table Tennis":"pingpong",
                              "Aussie Rules":"aussie_rules","Cricket":"cricket",
                              "Handball":"handball","Rugby":"rugby"}.get(pin_sport, pin_sport.lower() if pin_sport else "")
            sport_bonus = 0.1 if (bb_sport and bb_sport == _pin_sport_en) else 0.0

            # --- Method 1: English token overlap ---
            if bb_en_set:
                overlap = bb_en_set & pin_words
                meaningful = {w for w in overlap
                              if len(w) >= 3 or w in (
                                  'nba', 'nfl', 'mlb', 'wnba', 'ncaa',
                                  'nhl', 'cba', 'kbo', 'atp', 'wta', 'itf',
                                  'ldb', 'fifa', 'uefa', 'nrl', 'afl',
                              )}

                if len(meaningful) >= 2:
                    score = max(score, 0.75 + sport_bonus)
                elif len(meaningful) == 1:
                    single = list(meaningful)[0]
                    # Famous league abbrevs -> strong match
                    if single in ('nba', 'nfl', 'mlb', 'wnba', 'ncaa',
                                  'nhl', 'cba', 'kbo', 'ldb'):
                        score = max(score, 0.70 + sport_bonus)
                    elif single in ('atp', 'wta', 'itf', 'fifa', 'uefa'):
                        score = max(score, 0.65 + sport_bonus)
                    # Generic words -> weak signal
                    elif single in ('cup', 'league', 'open', 'championship',
                                    'tournament', 'series', 'tour', 'masters',
                                    'grand', 'prix', 'classic', 'trophy',
                                    'title', 'final', 'qualifiers', 'group',
                                    'premier', 'super', 'club', 'international'):
                        score = max(score, 0.15 + sport_bonus)
                    else:
                        score = max(score, 0.30 + sport_bonus)

            # --- Method 2: Number + remaining text fuzzy ---
            remaining = ' '.join(bb_en_tokens + bb_numbers).strip()
            if remaining and len(remaining) >= 2:
                sm = _SM(None, remaining.lower(), pin_lower)
                sm_score = sm.ratio()
                if sm_score > 0.35:
                    score = max(score, sm_score * 0.7 + sport_bonus)

            # --- Method 2b: Alphanumeric + CJK-stripped substring match ---
            # Catches "U22" matching "ldb u22" or "U20" matching "u20 women's"
            bb_stripped = _re.sub(r'[一-鿿]+', ' ', bb_name).strip()
            bb_stripped = _re.sub(r'\s+', ' ', bb_stripped).strip().lower()
            if bb_stripped and len(bb_stripped) >= 2:
                # Substring: does Pinnacle name contain the stripped BB text?
                if bb_stripped in pin_lower:
                    score = max(score, 0.55 + sport_bonus)
                # Fuzzy on stripped text
                sm_stripped = _SM(None, bb_stripped, pin_lower)
                if sm_stripped.ratio() > 0.35:
                    score = max(score, sm_stripped.ratio() * 0.65 + sport_bonus)

            # --- Method 3: Chinese country+division matching ---
            if bb_has_cjk and not bb_en_set and bb_sport:
                if pin_sport == bb_sport:
                    # 中→英国家名映射
                    _CN2EN_COUNTRY = {
                        '挪威': 'norway', '俄罗斯': 'russia', '乌拉圭': 'uruguay', '阿根廷': 'argentina',
                        '芬兰': 'finland', '捷克': 'czech', '印度': 'india', '乌克兰': 'ukraine',
                        '厄瓜多尔': 'ecuador', '秘鲁': 'peru', '哥伦比亚': 'colombia',
                        '巴西': 'brazil', '爱沙尼亚': 'estonia', '智利': 'chile', '墨西哥': 'mexico',
                        '瑞典': 'sweden', '巴拉圭': 'paraguay', '韩国': 'korea', '日本': 'japan',
                        '澳大利亚': 'australia', '美国': 'usa', '英格兰': 'england',
                        '德国': 'germany', '法国': 'france', '意大利': 'italy', '西班牙': 'spain',
                        '葡萄牙': 'portugal', '荷兰': 'netherlands', '比利时': 'belgium',
                        '奥地利': 'austria', '瑞士': 'switzerland', '波兰': 'poland',
                        '罗马尼亚': 'romania', '保加利亚': 'bulgaria', '丹麦': 'denmark',
                        '冰岛': 'iceland', '爱尔兰': 'ireland', '苏格兰': 'scotland',
                        '土耳其': 'turkey', '希腊': 'greece', '南非': 'south africa',
                        '塞尔维亚': 'serbia', '克罗地亚': 'croatia', '斯洛伐克': 'slovakia',
                        '匈牙利': 'hungary', '以色列': 'israel', '中国': 'china',
                        '加拿大': 'canada', '埃及': 'egypt', '摩洛哥': 'morocco',
                        '突尼斯': 'tunisia', '尼日利亚': 'nigeria', '加纳': 'ghana',
                        '伊朗': 'iran', '沙特': 'saudi', '阿联酋': 'uae', '卡塔尔': 'qatar',
                        '泰国': 'thailand', '越南': 'vietnam', '马来西亚': 'malaysia',
                        '印尼': 'indonesia', '新加坡': 'singapore',
                    }
                    # 级别匹配: 甲=1st/Premier, 乙=2nd, 丙=3rd, 丁=4th
                    _CN2EN_DIV = {
                        '超级': 'premier', '甲': '1st', '乙': '2nd', '丙': '3rd', '丁': '4th',
                        '后备': 'reserve', '女子': 'women', '青年': 'youth', 'U19': 'u19',
                        'U20': 'u20', 'U21': 'u21', 'U23': 'u23', '杯': 'cup',
                    }
                    country_score = 0
                    div_score = 0
                    for cn, en in _CN2EN_COUNTRY.items():
                        if cn in bb_name and en in pin_lower:
                            country_score = 0.50  # 提高: 国家名是强信号
                            break
                    for cn, en in _CN2EN_DIV.items():
                        if cn in bb_name and en in pin_lower:
                            div_score = max(div_score, 0.20)  # 提高: 级别也是强信号
                    if country_score > 0:
                        score = max(score, country_score + div_score + sport_bonus)

            if score >= 0.55:
                candidates.append((lid, score, pin_name))
                if '巴西发展' in bb_name:
                    print(f'    DEBUG: lid={lid}, name={pin_name}, score={score}, sport_bonus={sport_bonus}, pin_lower={pin_lower}')

        if candidates:
            candidates.sort(key=lambda x: -x[1])
            best_id, best_score, best_pin_name = candidates[0]

            # 跨运动校验: BB足球不应映射到Pinnacle篮球
            best_pin_sport = all_pin_leagues.get(best_id, {}).get('sport', '')
            _SPORT_MAP = {'Soccer':'football','Basketball':'basketball','Tennis':'tennis',
                'Baseball':'baseball','American Football':'american_football',
                'Mixed Martial Arts':'mma','Boxing':'boxing','Ice Hockey':'ice_hockey'}
            if _SPORT_MAP.get(best_pin_sport, '') not in ('', bb_sport):
                continue  # 跨运动→拒绝

            # 防误映射: 只靠通用词匹配且分低→拒绝
            meaningful_tokens = bb_en_set - _GENERIC_KEYWORD_BLACKLIST
            if not meaningful_tokens and best_score < 0.70:
                continue

            # Dedup: don't map two BB names to the same Pinnacle name
            already_mapped = False
            existing_bb = None
            for ebb, epin in LEAGUE_KEYWORDS.items():
                epins = [epin] if isinstance(epin, str) else epin
                if best_pin_name in epins:
                    already_mapped = True
                    existing_bb = ebb
                    break

            if already_mapped:
                # 只接受同联赛变体(相似度>0.8), 拒绝不同联赛→同一Pinnacle
                sm = _SM(None, bb_name.lower(), existing_bb.lower())
                if sm.ratio() > 0.8:
                    if bb_name not in LEAGUE_KEYWORDS:
                        if not dry_run:
                            LEAGUE_KEYWORDS[bb_name] = best_pin_name
                        new_mappings[bb_name] = [best_id]
                        print(f"  🔄 自动映射 (变体): [{bb_name}] → [{best_pin_name}] (score={best_score:.2f})")
            else:
                print(f"  \U0001f504 自动映射: [{bb_name}] → [{best_pin_name}] (ID={best_id}, score={best_score:.2f})")
                if not dry_run:
                    LEAGUE_KEYWORDS[bb_name] = best_pin_name
                new_mappings[bb_name] = [best_id]

    if new_mappings:
        if not dry_run:
            _save_league_keywords(LEAGUE_KEYWORDS)
            print(f"  ✅ 自动映射: {len(new_mappings)} 个联赛已保存到 league_keywords.json")
        else:
            print(f"  \U0001f4dd 自动映射发现 (dry-run): {len(new_mappings)} 个联赛")
    else:
        print(f"  ℹ️ 自动映射: 无新联赛可匹配")

    return new_mappings


# ---------------------------------------------------------------------------
# Auto-mapping: team names
# ---------------------------------------------------------------------------

def _auto_map_team_names(matched_entries):
    """Auto-extract team name mappings (Chinese -> English) from high-confidence matches.

    🔴 铁律: 只有完美的队名匹配(name, score>=0.95)才能自动学习映射.
    时间匹配(time)绝不自动学习 — 同赛事多场同时开打导致交叉错配.

    Only extracts from matches where:
    - match_type == "name" AND match_score >= 0.95
    - BB team name contains Chinese characters (not pure ASCII)
    Saves to team_name_map.json automatically.
    """
    new_pairs = 0
    skipped = 0

    for m in matched_entries:
        match_score = m.get("match_score", 0)
        match_type = m.get("match_type", "")

        # 🔴 铁律: 只有完美队名匹配才自动学习
        # 时间匹配绝不学习 (同时间多场比赛容易交叉错配)
        if match_type != "name" or match_score < 0.95:
            skipped += 1
            continue

        bb = m.get("bb", {})
        pin = m.get("pin", {})
        bb_home = bb.get("home", "").strip()
        bb_away = bb.get("away", "").strip()
        pin_home = pin.get("home", "").strip()
        pin_away = pin.get("away", "").strip()

        # 额外安全检查: 如果队名在映射表中已存在且指向不同Pin名, 跳过
        # (防止覆盖正确的手动映射)
        for bb_name, pin_name in [(bb_home, pin_home), (bb_away, pin_away)]:
            if bb_name in TEAM_NAME_MAP and TEAM_NAME_MAP[bb_name] != pin_name:
                skipped += 1
                continue

        # Map home team: only if BB name has Chinese characters
        if bb_home and pin_home and len(bb_home) >= 2:
            if not bb_home.isascii() and bb_home not in TEAM_NAME_MAP:
                TEAM_NAME_MAP[bb_home] = pin_home
                new_pairs += 1

        # Map away team
        if bb_away and pin_away and len(bb_away) >= 2:
            if not bb_away.isascii() and bb_away not in TEAM_NAME_MAP:
                TEAM_NAME_MAP[bb_away] = pin_away
                new_pairs += 1

    if new_pairs > 0:
        _save_team_name_map(TEAM_NAME_MAP)
        print(f"  \U0001f4c1 队名自动映射: 新增 {new_pairs} 条队名映射 (已保存到 team_name_map.json)")
    else:
        print(f"  \U0001f4c1 队名自动映射: 0 条新增")

    return new_pairs


# ---------------------------------------------------------------------------
# League name matching helpers
# ---------------------------------------------------------------------------

def _match_pin_name(pn, pin_name):
    """Check if pin keyword matches Pinnacle league name (word boundary)."""
    needle = pn.lower()
    haystack = pin_name.lower()
    idx = haystack.find(needle)
    while idx != -1:
        before = idx == 0 or haystack[idx - 1] in " -"
        after = idx + len(needle) >= len(haystack) or haystack[idx + len(needle)] in " -"
        if before and after:
            return True
        idx = haystack.find(needle, idx + 1)
    return False


def _find_best_league(pin_name, all_sport_matchups):
    """Match Pinnacle league name, prefer exact match."""
    needle = pin_name.lower().strip()
    matched = []
    for lid, info in all_sport_matchups.items():
        if _match_pin_name(needle, info["name"]):
            matched.append(lid)
    if not matched:
        return None
    # Exact match preferred (prevent "Division A" prefix matching "Division A Women")
    for lid in matched:
        if all_sport_matchups[lid]["name"].lower() == needle:
            return lid
    return matched[0]


def find_pinnacle_league_id(bb_league_name, all_sport_matchups):
    """Find Pinnacle league ID that matches a BB league name (single best match)."""
    ids = find_pinnacle_league_ids(bb_league_name, all_sport_matchups)
    return ids[0] if ids else None


# ---------------------------------------------------------------------------
# ITF tennis league matching (location-based)
# ---------------------------------------------------------------------------

def _find_itf_league_ids(bb_league_name, all_sport_matchups):
    """Handle ITF (World Tennis) leagues with location-based matching.

    BB format: "世界网球 - M15 乌斯拉尔 男子单打"
    Pinnacle format: "ITF Men Uslar - R1"

    Uses hardcoded Chinese->English location mapping, falling back
    to pinyin fuzzy matching for unmapped locations.

    NOTE: Pinnacle does NOT have separate doubles leagues for ITF events,
    so if the BB league name contains 双打/雙打/Doubles, return empty.
    """
    # ITF doubles: Pinnacle has no corresponding doubles leagues
    if '双打' in bb_league_name or '雙打' in bb_league_name or 'Doubles' in bb_league_name:
        return []

    # Hardcoded Chinese ITF location -> English name mapping
    # (Transliterations vary too much for reliable pinyin-only matching)
    ITF_LOCATION_MAP = {
        "乌斯拉尔": "uslar",
        "新戈里卡": "nova gorica",
        "武宁": "wuning",
        "维多利亚加斯泰斯": "vitoria-gasteiz",
        "库尔索姆利斯卡班亚": "kursumlijska banja",
        "布朗库堡": "castelo branco",
        "达姆施塔特": "darmstadt",
        "都灵": "torino",
        "六安": "luan",
        "圣保罗": "sao paulo",
        "达拉斯": "dallas",
        "奥洛穆茨": "olomouc",
        "莫纳斯提尔": "monastir",
        "路易斯维尔": "louisville",
        "希尔克雷斯特": "hillcrest",
        "克尔什科": "krsko",
        "克尔斯科": "krsko",
        "古比奥": "gubbio",
        "古比奧": "gubbio",
        "克拉姆萨赫": "kramsach",
        "甘迪亚": "gandia",
        "诺丁汉": "nottingham",
        "格兰比": "granby",
        "罗切斯特": "rochester",
        "比利亚孔斯蒂图西翁": "villa constitucion",
        "斯洛博齐亚": "slobozia",
        "于尔亚日": "uriage",
        "阿斯塔纳": "astana",
        "布里斯班": "brisbane",
        "印多尔": "indore",
        "新戈里察": "nova gorica",
        "阿姆施泰滕": "amstetten",
        "诺让苏尔马恩": "nogent-sur-marne",
        "克诺克海斯特": "knokke-heist",
        "佩尔加米诺": "pergamino",
        "黑兴根": "hechingen",
        "列克星敦": "lexington",
        "根措夫特": "gentofte",
        "博尔扎诺": "bolzano",
        "斯特拉斯堡": "strasbourg",
        "奥尔德肖特": "aldershot",
        "韦茨拉尔": "wetzlar",
        "科沙林": "koszalin",
        "皮特什蒂": "pitesti",
        "哈蒂瓦": "xativa",
        "都柏林": "dublin",
        "大加那利岛": "gran canaria",
        "别尔斯科比亚瓦": "bielsko biala",
        "罗加什卡斯拉蒂纳": "rogaska slatina",
        "萨维泰帕莱": "savitaipale",
        "巴厘": "bali",
    }

    import re as _re

    # Extract level (M15, M25, W15, W35, W50, W75)
    level_m = _re.search(r'(M\d+|W\d+)', bb_league_name)
    if not level_m:
        return []
    level = level_m.group(1)
    is_women = level.startswith('W')

    # Extract location: characters after the level code
    after_level = bb_league_name.split(level, 1)[-1].strip()
    loc_parts = []
    for ch in after_level:
        if '一' <= ch <= '鿿' or ch == '·':
            loc_parts.append(ch)
        elif loc_parts:
            break
    location_cn = ''.join(loc_parts).strip('· ')
    if not location_cn:
        return []

    # Get English location name from map, or fall back to pinyin
    location_en = ITF_LOCATION_MAP.get(location_cn, "")
    if not location_en:
        try:
            from pypinyin import lazy_pinyin
            location_en = ''.join(lazy_pinyin(location_cn)).lower().replace(' ', '')
        except ImportError:
            location_en = location_cn.lower()

    gender_prefix = "Women" if is_women else "Men"
    location_lower = location_en.lower().strip()

    matched_ids = []
    for lid, info in all_sport_matchups.items():
        name = info.get("name", "")
        if not name.startswith("ITF") or gender_prefix not in name:
            continue
        # Extract location from Pinnacle league name
        pin_after_itf = name.split("ITF", 1)[-1].strip()
        pin_after_gender = pin_after_itf.split(gender_prefix, 1)[-1].strip()
        pin_location = pin_after_gender.split("-")[0].strip().lower()

        if not pin_location:
            continue

        # Direct match: location is a substring of Pinnacle name
        if location_lower in pin_location or pin_location in location_lower:
            if lid not in matched_ids:
                matched_ids.append(lid)

    return matched_ids


# ---------------------------------------------------------------------------
# Main league ID resolver
# ---------------------------------------------------------------------------

def find_pinnacle_league_ids(bb_league_name, all_sport_matchups):
    """Find ALL Pinnacle league IDs matching a BB league name.

    Tennis etc. may be split into multiple sub-leagues on Pinnacle
    (Qualifiers, R1, etc.), so return all matching IDs + prefix-matched
    sub-leagues.

    Strategy:
    1. LEAGUE_KEYWORDS exact mapping
    1.5. ITF World Tennis special handling (location pinyin matching)
    2. For exactly-mapped leagues, find Pinnacle sub-leagues with same prefix
       (e.g. "ATP Bastad" -> "ATP Bastad - Qualifiers", "ATP Bastad - R1")
       Limited: only expand short names (no " - " in original mapped name)
    3. For unmapped leagues, use English keyword controlled fuzzy matching
    """
    bb_lower = bb_league_name.lower().strip()
    matched_ids = set()

    # Phase 1: LEAGUE_KEYWORDS exact mapping
    matched_pin_names = []  # Pinnacle league name list
    # Collect all keyword candidates
    exact_candidate = None       # bb_name == bb_league_name (exact match)
    reverse_candidates = []      # bb_league_name in bb_name (league name inside keyword, reliable)
    direct_candidates = []       # bb_name in bb_league_name (keyword inside league name, potential CJK collision)

    for bb_name, pin_name in LEAGUE_KEYWORDS.items():
        in_direct = bb_name in bb_league_name
        in_reverse = bb_league_name in bb_name

        if in_direct and in_reverse:
            # Bidirectional substring = exact match, highest priority, use immediately
            exact_candidate = (bb_name, pin_name)
            break
        elif in_reverse:
            # League name inside keyword (e.g. keyword "白俄罗斯超级联赛" contains "俄罗斯超级联赛")
            # Very reliable, no false positives
            reverse_candidates.append((bb_name, pin_name))
        elif in_direct:
            # Keyword inside league name (e.g. "俄罗斯超级联赛" is substring of "白俄罗斯超级联赛")
            # Potential CJK collision, disambiguate by longest keyword
            direct_candidates.append((bb_name, pin_name, len(bb_name)))

    # Try exact match
    if exact_candidate:
        bb_name, pin_name = exact_candidate
        pin_names = [pin_name] if isinstance(pin_name, str) else pin_name
        for pn in pin_names:
            lid = _find_best_league(pn, all_sport_matchups)
            if lid:
                matched_ids.add(lid)
                matched_pin_names.append(pn)

    # No exact match -> try reverse match (reliable)
    if not matched_ids:
        for bb_name, pin_name in reverse_candidates:
            pin_names = [pin_name] if isinstance(pin_name, str) else pin_name
            for pn in pin_names:
                lid = _find_best_league(pn, all_sport_matchups)
                if lid:
                    matched_ids.add(lid)
                    matched_pin_names.append(pn)
                    break
            if matched_ids:
                break

    # Still no match -> try forward match (longest keyword first)
    if not matched_ids and direct_candidates:
        direct_candidates.sort(key=lambda c: -c[2])  # sort by keyword length descending
        for bb_name, pin_name, _ in direct_candidates:
            pin_names = [pin_name] if isinstance(pin_name, str) else pin_name
            for pn in pin_names:
                lid = _find_best_league(pn, all_sport_matchups)
                if lid:
                    matched_ids.add(lid)
                    matched_pin_names.append(pn)
                    break
            if matched_ids:
                break

    # Phase 1.5: ITF World Tennis -> location pinyin matching
    if not matched_ids and ("世界网球" in bb_league_name or "世界網球" in bb_league_name):
        itf_ids = _find_itf_league_ids(bb_league_name, all_sport_matchups)
        if itf_ids:
            return sorted(itf_ids)

    if matched_ids:
        # Phase 1.5: Sub-league expansion - only for short names without " - "
        # (e.g. "ATP Bastad" -> "ATP Bastad - Qualifiers", "ATP Bastad - R1")
        # But do NOT expand "Russia - First League" style names
        for pn in matched_pin_names:
            if " - " in pn:
                continue  # already multi-segment, skip sub-league expansion
            for lid, info in all_sport_matchups.items():
                if lid in matched_ids:
                    continue
                if info["name"].lower().startswith(pn.lower()):
                    matched_ids.add(lid)

        return sorted(matched_ids)

    # Phase 2: Only for unmatched leagues, do English keyword fuzzy matching
    import re as _re
    bb_en_parts = _re.findall(r'[A-Za-z]{2,}', bb_lower)
    bb_en_set = set(w.lower() for w in bb_en_parts)

    if bb_en_set:
        for lid, info in all_sport_matchups.items():
            pin_name = info["name"].lower()
            pin_words = set(pin_name.split())
            overlap = bb_en_set & pin_words
            if len(overlap) >= 2:
                matched_ids.add(lid)
            # Single keyword only matches major league abbreviations
            # to prevent "atp" matching ALL ATP leagues
            elif len(overlap) == 1:
                single_word = list(overlap)[0]
                if single_word in ("nba", "nfl", "mlb", "wnba", "ncaa"):
                    matched_ids.add(lid)

    return sorted(matched_ids) if matched_ids else []
