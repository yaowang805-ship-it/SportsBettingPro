#!/usr/bin/env python3
"""
Build non-football sport mappings for BB→Pinnacle matching.

Two-phase approach:
  Phase 1: League keyword mappings (BB Chinese→Pinnacle English)
  Phase 2: Team name mappings (BB Chinese→Pinnacle English) for team sports

This script is idempotent — safe to run multiple times.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
LEAGUE_KW_PATH = ROOT / "data/storage/league_keywords.json"
TEAM_MAP_PATH = ROOT / "data/storage/team_name_map.json"

# ─────────────────────────────────────────────
# Phase 1: League keyword mappings
# ─────────────────────────────────────────────

NEW_LEAGUE_KEYWORDS = {
    # ═══════════ BASEBALL ═══════════
    # Already mostly mapped, adding missing variants:
    "美国职业棒球小联盟三A国际联盟": "MLB",  # Triple-A → MLB (best available)

    # ═══════════ BASKETBALL ═══════════
    # NBL1 regional sub-leagues → Australia - NBL1 (umbrella)
    "澳大利亚东部女子篮球联赛": "Australia - NBL1 Women",
    "澳大利亚北部女子篮球联赛": "Australia - NBL1 Women",
    "澳大利亚中部女子篮球联赛": "Australia - NBL1 Women",
    "澳大利亚西部女子篮球联赛": "Australia - NBL1 Women",
    "澳大利亚南部女子篮球联赛": "Australia - NBL1 Women",
    "澳大利亚南部篮球联赛": "Australia - NBL1",
    "澳大利亚北部篮球联赛": "Australia - NBL1",
    "澳大利亚中部篮球联赛": "Australia - NBL1",
    # Paulista → Brazil U20
    "巴西圣保罗州篮球锦标赛": "Brazil - Paulista FPB U20",
    # Euroleague → no direct Pinnacle equivalent, try Club Friendlies as fallback
    "欧洲篮球联赛": "World - Club Friendlies",

    # ═══════════ ICE HOCKEY ═══════════
    # NZIHL not covered by Pinnacle — no mapping possible

    # ═══════════ MMA ═══════════
    # ONE Championship not covered by Pinnacle — no mapping possible
    "ONE冠军赛": "UFC",  # Best-effort fallback (Pinnacle only has UFC)

    # ═══════════ TENNIS ═══════════
    # ITF doubles — map to singles league (Pinnacle doesn't separate ITF doubles)
    "ITF - M15 天津 男子双打": "ITF Men",
    "ITF - W15 天津 女子双打": "ITF Women",
    "ITF - M25 库尔舒姆利斯卡班亚 男子双打": "ITF Men Kursumlijska Banja - Final",
    "ITF - M25 库尔索姆利斯卡班亚 男子单打": "ITF Men Kursumlijska Banja - Final",
    "ITF - M15 波普拉德 男子双打": "ITF Men",
    "ITF - M25 罗汉普顿 男子双打": "ITF Men Aldershot - Final",
    "ITF - M15 库尔泰亚德阿尔杰什 男子双打": "ITF Men Pitesti - Final",
    "ITF - M15 美因河畔法兰克福 男子双打": "ITF Men Frankfurt - Qualifiers",
    "ITF - M15 别尔斯科 比亚瓦 男子双打": "ITF Women Bielsko Biala - Final",
    "ITF - W15 哈米林纳 女子双打": "ITF Women",
    "ITF - W15 库尔索姆利斯卡班亚 女子双打": "ITF Women Kursumlijska Banja - Final",
    "ITF - W35 罗汉普顿 女子双打": "ITF Women Aldershot - Final",
    "ITF - W100 兰迪斯维尔 女子双打": "ITF Women",
    "ITF - W75 莱比锡 女子双打": "ITF Women Leipzig W75 - Final",
    "ITF - W75 科克赛德 女子双打": "ITF Women Koksijde W75 - Final",
    "ITF - W15 阿斯塔纳 女子双打": "ITF Women Astana - Final",
    "ITF - M15 别尔斯科 比亚瓦 男子单打": "ITF Women Bielsko Biala - Final",
    # ATP/WTA doubles → map to singles (will miss doubles-specific odds but get 1X2)
    "ATP - 蒙特利尔公开赛 - 双打": "ATP Montreal",
    "WTA - 多伦多公开赛 - 双打": "WTA Toronto",
    "ATP挑战赛 - 哈根公开赛 - 双打": "ATP Challenger Hagen",
    "ATP挑战赛 - 马佐夫舍格罗济斯克公开赛 - 双打": "ATP Challenger Grodzisk Mazowiecki",
    "ATP挑战赛 - 伊斯坦布尔 2 公开赛 - 双打": "ATP Challenger Istanbul",
    "ATP挑战赛 - 普罗夫迪夫 2 公开赛 - 双打": "ATP Challenger Plovdiv",
    "ATP挑战赛 - 莱克星顿公开赛 - 双打": "ATP Challenger Lexington",
    "WTA - 华沙公开赛 - 双打": "WTA 125K Warsaw",
    "WTA - 特尔古穆列什公开赛 - 双打": "WTA 125k Targu Mures - Doubles",

    # ═══════════ AMERICAN FOOTBALL ═══════════
    # Already well-mapped. NFL Pre Season → NFL fallback
    "CFL加拿大美式足球": "Canadian Football",
}


# ─────────────────────────────────────────────
# Phase 2: Team name mappings (CN → EN)
# ─────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

TODAY = _now_iso()

MLB_TEAMS = {
    # American League East
    "纽约洋基": "New York Yankees",
    "波士顿红袜": "Boston Red Sox",
    "多伦多蓝鸟": "Toronto Blue Jays",
    "坦帕湾光芒": "Tampa Bay Rays",
    "巴尔的摩金莺": "Baltimore Orioles",
    # American League Central
    "芝加哥白袜": "Chicago White Sox",
    "克利夫兰守护者": "Cleveland Guardians",
    "明尼苏达双城": "Minnesota Twins",
    "底特律老虎": "Detroit Tigers",
    "堪萨斯市皇家": "Kansas City Royals",
    # American League West
    "休斯敦太空人": "Houston Astros",
    "西雅图水手": "Seattle Mariners",
    "德州游骑兵": "Texas Rangers",
    "洛杉矶天使": "Los Angeles Angels",
    "运动家": "Athletics",
    "奥克兰运动家": "Oakland Athletics",
    # National League East
    "亚特兰大勇士": "Atlanta Braves",
    "纽约大都会": "New York Mets",
    "费城费城人": "Philadelphia Phillies",
    "迈阿密马林鱼": "Miami Marlins",
    "华盛顿国民": "Washington Nationals",
    # National League Central
    "圣路易斯红雀": "St. Louis Cardinals",
    "密尔沃基酿酒人": "Milwaukee Brewers",
    "芝加哥小熊": "Chicago Cubs",
    "辛辛那提红人": "Cincinnati Reds",
    "匹兹堡海盗": "Pittsburgh Pirates",
    # National League West
    "洛杉矶道奇": "Los Angeles Dodgers",
    "圣地亚哥教士": "San Diego Padres",
    "旧金山巨人": "San Francisco Giants",
    "亚利桑那响尾蛇": "Arizona Diamondbacks",
    "科罗拉多洛矶": "Colorado Rockies",
}

NPB_TEAMS = {
    "北海道日本火腿斗士": "Hokkaido Nippon-Ham Fighters",
    "东北乐天金鹰": "Tohoku Rakuten Golden Eagles",
    "东京读卖巨人": "Yomiuri Giants",
    "东京养乐多燕子": "Tokyo Yakult Swallows",
    "横滨湾星": "Yokohama DeNA Baystars",
    "广岛东洋鲤鱼": "Hiroshima Toyo Carp",
    "阪神老虎": "Hanshin Tigers",
    "名古屋中日龙": "Chunichi Dragons",
    "千叶罗德海洋": "Chiba Lotte Marines",
    "欧力士野牛": "Orix Buffaloes",
    "埼玉西武狮": "Saitama Seibu Lions",
    "福冈软件银行鹰": "Fukuoka SoftBank Hawks",
}

KBO_TEAMS = {
    "起亚虎": "Kia Tigers",
    "三星狮": "Samsung Lions",
    "LG双子": "LG Twins",
    "斗山熊": "Doosan Bears",
    "KT巫师": "KT Wiz",
    "SSG登陆者": "SSG Landers",
    "乐天巨人": "Lotte Giants",
    "韩华鹰": "Hanwha Eagles",
    "NC恐龙": "NC Dinos",
    "培正英雄": "Kiwoom Heroes",
}

CPBL_TEAMS = {
    "台钢雄鹰": "TSG Hawks",
    "中信兄弟": "CTBC Brothers",
    "乐天桃猿": "Rakuten Monkeys",
    "味全龙": "Wei Chuan Dragons",
    "富邦悍将": "Fubon Guardians",
    "统一7ELEVEn狮": "Uni-President 7-Eleven Lions",
    "统一狮": "Uni-President Lions",
}

NBA_TEAMS = {
    "亚特兰大老鹰": "Atlanta Hawks",
    "波士顿凯尔特人": "Boston Celtics",
    "布鲁克林篮网": "Brooklyn Nets",
    "夏洛特黄蜂": "Charlotte Hornets",
    "芝加哥公牛": "Chicago Bulls",
    "克利夫兰骑士": "Cleveland Cavaliers",
    "达拉斯独行侠": "Dallas Mavericks",
    "达拉斯小牛": "Dallas Mavericks",
    "丹佛掘金": "Denver Nuggets",
    "底特律活塞": "Detroit Pistons",
    "金州勇士": "Golden State Warriors",
    "休斯敦火箭": "Houston Rockets",
    "印第安纳步行者": "Indiana Pacers",
    "洛杉矶快船": "Los Angeles Clippers",
    "洛杉矶湖人": "Los Angeles Lakers",
    "孟菲斯灰熊": "Memphis Grizzlies",
    "迈阿密热火": "Miami Heat",
    "密尔沃基雄鹿": "Milwaukee Bucks",
    "明尼苏达森林狼": "Minnesota Timberwolves",
    "新奥尔良鹈鹕": "New Orleans Pelicans",
    "纽约尼克斯": "New York Knicks",
    "俄克拉荷马城雷霆": "Oklahoma City Thunder",
    "奥兰多魔术": "Orlando Magic",
    "费城76人": "Philadelphia 76ers",
    "菲尼克斯太阳": "Phoenix Suns",
    "波特兰开拓者": "Portland Trail Blazers",
    "萨克拉门托国王": "Sacramento Kings",
    "圣安东尼奥马刺": "San Antonio Spurs",
    "多伦多猛龙": "Toronto Raptors",
    "犹他爵士": "Utah Jazz",
    "华盛顿奇才": "Washington Wizards",
}

WNBA_TEAMS = {
    "拉斯维加斯王牌 (女)": "Las Vegas Aces",
    "拉斯维加斯王牌": "Las Vegas Aces",
    "纽约自由人 (女)": "New York Liberty",
    "纽约自由人": "New York Liberty",
    "康涅狄格阳光 (女)": "Connecticut Sun",
    "康涅狄格阳光": "Connecticut Sun",
    "明尼苏达山猫 (女)": "Minnesota Lynx",
    "明尼苏达山猫": "Minnesota Lynx",
    "西雅图风暴 (女)": "Seattle Storm",
    "西雅图风暴": "Seattle Storm",
    "菲尼克斯水星 (女)": "Phoenix Mercury",
    "凤凰城水星 (女)": "Phoenix Mercury",
    "菲尼克斯水星": "Phoenix Mercury",
    "凤凰城水星": "Phoenix Mercury",
    "芝加哥天空 (女)": "Chicago Sky",
    "芝加哥天空": "Chicago Sky",
    "印第安纳狂热 (女)": "Indiana Fever",
    "印第安纳狂热": "Indiana Fever",
    "亚特兰大梦想 (女)": "Atlanta Dream",
    "亚特兰大梦想": "Atlanta Dream",
    "洛杉矶火花 (女)": "Los Angeles Sparks",
    "洛杉矶火花": "Los Angeles Sparks",
    "华盛顿神秘人 (女)": "Washington Mystics",
    "华盛顿神秘人": "Washington Mystics",
    "达拉斯飞翼 (女)": "Dallas Wings",
    "达拉斯飞翼": "Dallas Wings",
    "金州瓦尔基里 (女)": "Golden State Valkyries",
    "金州瓦尔基里": "Golden State Valkyries",
    "波特兰火焰 (女)": "Portland Fire",
    "波特兰火焰": "Portland Fire",
}

NFL_TEAMS = {
    "洛杉矶闪电": "Los Angeles Chargers",
    "亚利桑那红雀": "Arizona Cardinals",
    "纽约巨人": "New York Giants",
    "达拉斯牛仔": "Dallas Cowboys",
    "堪萨斯城酋长": "Kansas City Chiefs",
    "旧金山49人": "San Francisco 49ers",
    "费城老鹰": "Philadelphia Eagles",
    "巴尔的摩乌鸦": "Baltimore Ravens",
    "辛辛那提猛虎": "Cincinnati Bengals",
    "布法罗比尔": "Buffalo Bills",
    "迈阿密海豚": "Miami Dolphins",
    "纽约喷气机": "New York Jets",
    "新英格兰爱国者": "New England Patriots",
    "匹兹堡钢人": "Pittsburgh Steelers",
    "克利夫兰布朗": "Cleveland Browns",
    "印第安纳波利斯小马": "Indianapolis Colts",
    "杰克逊维尔美洲虎": "Jacksonville Jaguars",
    "田纳西泰坦": "Tennessee Titans",
    "休斯敦德州人": "Houston Texans",
    "丹佛野马": "Denver Broncos",
    "拉斯维加斯突袭者": "Las Vegas Raiders",
    "芝加哥熊": "Chicago Bears",
    "底特律雄狮": "Detroit Lions",
    "绿湾包装工": "Green Bay Packers",
    "明尼苏达维京人": "Minnesota Vikings",
    "亚特兰大猎鹰": "Atlanta Falcons",
    "卡罗莱纳黑豹": "Carolina Panthers",
    "新奥尔良圣徒": "New Orleans Saints",
    "坦帕湾海盗": "Tampa Bay Buccaneers",
    "洛杉矶公羊": "Los Angeles Rams",
    "西雅图海鹰": "Seattle Seahawks",
    "华盛顿指挥官": "Washington Commanders",
}

NHL_TEAMS = {
    "多伦多枫叶": "Toronto Maple Leafs",
    "蒙特利尔加拿大人": "Montreal Canadiens",
    "波士顿棕熊": "Boston Bruins",
    "纽约游骑兵": "New York Rangers",
    "坦帕湾闪电": "Tampa Bay Lightning",
    "佛罗里达美洲豹": "Florida Panthers",
    "底特律红翼": "Detroit Red Wings",
    "渥太华参议员": "Ottawa Senators",
    "布法罗军刀": "Buffalo Sabres",
    "卡罗莱纳飓风": "Carolina Hurricanes",
    "新泽西魔鬼": "New Jersey Devils",
    "纽约岛人": "New York Islanders",
    "华盛顿首都": "Washington Capitals",
    "匹兹堡企鹅": "Pittsburgh Penguins",
    "费城飞人": "Philadelphia Flyers",
    "哥伦布蓝衣": "Columbus Blue Jackets",
    "科罗拉多雪崩": "Colorado Avalanche",
    "达拉斯星": "Dallas Stars",
    "明尼苏达荒野": "Minnesota Wild",
    "温尼伯喷气机": "Winnipeg Jets",
    "纳什维尔掠夺者": "Nashville Predators",
    "圣路易斯蓝调": "St. Louis Blues",
    "芝加哥黑鹰": "Chicago Blackhawks",
    "埃德蒙顿油人": "Edmonton Oilers",
    "洛杉矶国王": "Los Angeles Kings",
    "维加斯黄金骑士": "Vegas Golden Knights",
    "卡尔加里火焰": "Calgary Flames",
    "温哥华加人": "Vancouver Canucks",
    "西雅图海怪": "Seattle Kraken",
    "圣何塞鲨鱼": "San Jose Sharks",
    "安纳海姆小鸭": "Anaheim Ducks",
    "犹他冰球": "Utah Hockey Club",
}

# Australian NBL1 / Big V team mappings (from existing matched data)
NBL1_TEAMS = {
    # Big V Women
    "贝拉林风暴 (女)": "Bellarine Storm",
    "麦金农美洲狮 (女)": "McKinnon Cougars",
    "贝拉林风暴": "Bellarine Storm",
    # Chile LNB
    "瓦尔迪维亚": "CD Valdivia",
    "蒙特港": "CEB Puerto Montt",
    "普恩特阿尔托": "CD Puente Alto",
    "塔尔卡西班牙人": "CD Espanol De Talca",
    "科洛科洛": "Colo Colo",
    "康塞普西翁大学": "Universidad de Concepcion",
    # Taiwan Club Friendlies
    "国立虎尾科技大学": "National Formosa University",
    "台湾啤酒": "Taiwan Beer",
}

# CFL Teams
CFL_TEAMS = {
    "多伦多淘金人": "Toronto Argonauts",
    "卡尔加里牛仔": "Calgary Stampeders",
    "蒙特利尔云雀": "Montreal Alouettes",
    "埃德蒙顿爱斯基摩人": "Edmonton Elks",
    "温尼伯蓝色轰炸机": "Winnipeg Blue Bombers",
    "萨斯喀彻温驯马师": "Saskatchewan Roughriders",
    "汉密尔顿虎猫": "Hamilton Tiger-Cats",
    "不列颠哥伦比亚狮子": "BC Lions",
    "渥太华红黑": "Ottawa Redblacks",
}

# Sports → team dict mapping
SPORT_TEAM_MAPS = {
    "baseball": [MLB_TEAMS, NPB_TEAMS, KBO_TEAMS, CPBL_TEAMS],
    "basketball": [NBA_TEAMS, WNBA_TEAMS, NBL1_TEAMS],
    "american_football": [NFL_TEAMS, CFL_TEAMS],
    "ice_hockey": [NHL_TEAMS],
}


def update_league_keywords():
    """Add missing league keyword mappings. Preserves existing entries."""
    existing = {}
    if LEAGUE_KW_PATH.exists():
        existing = json.loads(LEAGUE_KW_PATH.read_text())

    added = 0
    for bb_league, pin_league in NEW_LEAGUE_KEYWORDS.items():
        if bb_league not in existing:
            existing[bb_league] = pin_league
            added += 1
            print(f"  + [{bb_league}] → {pin_league}")

    LEAGUE_KW_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    print(f"\n✅ league_keywords.json: {added} new, {len(existing)} total")
    return added


def update_team_names():
    """Add team name mappings for non-football team sports.

    Format: simple dict {chinese_name: english_name}, with optional _meta key.
    """
    existing = {}
    if TEAM_MAP_PATH.exists():
        existing = json.loads(TEAM_MAP_PATH.read_text())

    # Collect all new mappings (CN → EN)
    new_mappings = {}
    for sport, team_dicts in SPORT_TEAM_MAPS.items():
        for team_dict in team_dicts:
            for cn, en in team_dict.items():
                if cn not in existing and cn not in new_mappings:
                    new_mappings[cn] = en

    # Apply to existing dict
    for cn, en in new_mappings.items():
        existing[cn] = en

    # Update _meta if present
    if "_meta" in existing:
        meta = existing["_meta"]
        for cn in new_mappings:
            if cn not in meta:
                meta[cn] = {
                    "sport": "unknown",
                    "n": 0,
                    "first": TODAY,
                    "last": TODAY,
                    "source": "manual",
                }

    for cn, en in list(new_mappings.items())[:20]:
        print(f"  + {cn} → {en}")
    if len(new_mappings) > 20:
        print(f"  ... and {len(new_mappings) - 20} more")

    TEAM_MAP_PATH.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    print(f"\n✅ team_name_map.json: {len(new_mappings)} new, {len(existing)} total")
    return len(new_mappings)


def main():
    print("=" * 60)
    print("Building non-football sport mappings")
    print("=" * 60)

    print("\n📋 Phase 1: League keywords...")
    kw_added = update_league_keywords()

    print("\n👥 Phase 2: Team name mappings...")
    tm_added = update_team_names()

    print(f"\n{'=' * 60}")
    print(f"Summary: +{kw_added} league keywords, +{tm_added} team names")
    if kw_added == 0 and tm_added == 0:
        print("No new mappings needed — everything already present.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
