"""BB体育 vs Pinnacle 赔率对比（改进版）
1. 从 Pinnacle API 获取所有联赛 + 赔率
2. 从 BB体育 提取赔率（只取前3个为1X2赔率）
3. 寻找重叠比赛并计算 +EV

关键改进：
- 只取 BB 前 3 个赔率作为 1X2（跳过没有 1X2 的比赛）
- 通过 full_text 检测 1X2 是否可用
- 队名映射辅助校验
- 提高匹配阈值减少误报
"""
import json, sys, time, math, re, random
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import DATA_DIR

import requests

API_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
SESSION = requests.Session()
SESSION.trust_env = False  # Python 3.14+ 避免自动读取系统代理配置
SESSION.headers.update({"Accept": "application/json"})

# SOCKS5 代理支持（Shadowrocket 本地代理，用于绕过 Cloudflare）
# 直连也支持（代理不可用时自动回退）
PROXY = "socks5://localhost:1082"

# 重试参数
MAX_RETRIES = 3
RETRY_DELAY = 2.0  # 初始延迟（秒）

# 运动 ID 映射
SPORT_IDS = {29: "足球", 4: "篮球", 33: "网球", 3: "棒球", 15: "美式足球"}
TWO_WAY_SPORTS = {"basketball", "tennis", "baseball", "american_football"}

# 明显是冠军/优胜者盘口的联赛（非比赛）
OUTRIGHT_LEAGUES = {"年度最佳教练", "年度最佳辅导"}

# BB体育联赛关键词 → 运动类型
BB_SPORT_KEYWORDS = {
    # Football
    "欧洲冠军联赛": "football", "欧洲足联欧洲联赛": "football",
    "超级联赛": "football", "西班牙甲级联赛": "football",
    "德国甲级联赛": "football", "世界杯": "football",
    "球会友谊赛": "football", "苏格兰联赛杯": "football",
    "芬兰": "football", "瑞典超级联赛": "football",
    "超级挪威联赛": "football", "乌拉圭甲级联赛": "football",
    "哈萨克斯坦超级联赛": "football", "巴拉圭": "football",
    "俄罗斯甲级联赛": "football", "澳门甲级联赛": "football",
    "白俄罗斯超级联赛": "football", "冰岛甲级联赛": "football",
    "爱沙尼亚甲级联赛": "football",
    # Basketball
    "NBA": "basketball", "美国职业篮球": "basketball",
    "欧洲篮球联赛": "basketball", "CBA": "basketball",
    "韩国篮球": "basketball", "日本篮球": "basketball",
    "菲律宾篮球": "basketball", "篮球": "basketball",
    "年度最佳": "american_football", "美式足球": "american_football",
    "NFL": "american_football",
    # Tennis
    "ATP": "tennis", "WTA": "tennis", "网球": "tennis",
    # Baseball
    "MLB": "baseball", "日本职业棒球": "baseball",
    "韩国棒球": "baseball", "中华职业棒球": "baseball",
    "棒球": "baseball",
}

# 各运动的市场标签
MARKET_LABELS = {
    "football":  {"ml": ["主胜","和局","客胜"], "hc_home":"让球主胜", "hc_away":"让球客胜", "over":"大球", "under":"小球"},
    "basketball": {"ml": ["主胜","客胜"], "hc_home":"让分主胜", "hc_away":"让分客胜", "over":"大分", "under":"小分"},
    "tennis":     {"ml": ["主胜","客胜"], "hc_home":"让局主胜", "hc_away":"让局客胜", "over":"大分", "under":"小分"},
    "baseball":   {"ml": ["主胜","客胜"], "hc_home":"让分主胜", "hc_away":"让分客胜", "over":"大分", "under":"小分"},
    "american_football": {"ml": ["主胜","客胜"], "hc_home":"让分主胜", "hc_away":"让分客胜", "over":"大分", "under":"小分"},
}

def detect_sport(bb_match):
    """从 BB 比赛数据中检测运动类型。
    优先使用联赛关键词匹配（比 BB 的 sport 字段更可靠，因为
    提取阶段可能会把网球比赛标记为棒球），回退到 BB sport 字段。"""
    league = bb_match.get("league", "")
    for kw, s in BB_SPORT_KEYWORDS.items():
        if kw in league:
            return s
    sport = bb_match.get("sport", "")
    if sport:
        return sport
    return "football"  # 默认

# BB体育中文联赛名 → Pinnacle 联赛名（关键词匹配）
LEAGUE_KEYWORDS = {
    "苏格兰联赛杯": "Scotland - League Cup",
    "球会友谊赛": "Club Friendlies",
    "芬兰超级联赛": "Finland - Veikkausliiga",
    "芬兰甲级联赛": ["Finland - Ykkosliiga", "Finland - Ykkonen"],
    "芬兰乙级联赛": "Finland - Kakkonen",
    "瑞典超级联赛": "Sweden - Allsvenskan",
    "超级挪威联赛": "Norway - Eliteserien",
    "乌拉圭甲级联赛": "Uruguay - Primera Division",
    "哈萨克斯坦超级联赛": "Kazakhstan - Premier League",
    "巴拉圭乙级联赛": "Paraguay - Division Intermedia",
    "俄罗斯甲级联赛": "Russia - First League",
    "澳门甲级联赛": "Macau - Elite League",
    "白俄罗斯超级联赛": "Belarus - Premier League",
    "冰岛甲级联赛": "Iceland - Premier League",
    "爱沙尼亚甲级联赛": "Estonia - Meistriliiga",
    # 早盘联赛映射
    "欧洲冠军联赛-资格赛": "UEFA - Champions League Qualifiers",
    "英格兰超级联赛": "England - Premier League",
    "西班牙甲级联赛": "Spain - La Liga",
    "德国甲级联赛": "Germany - Bundesliga",
    "欧足联欧洲联赛-资格赛": "UEFA - Europa League Qualifiers",
    "2026世界杯 (在加拿大墨西哥&美国)": "FIFA - World Cup",
    # US / AU sports
    "NBA夏季联赛": "NBA",
    "美国职业篮球联赛": "NBA",
    "WNBA 美国职业女子篮球联赛": "WNBA",
    "FIBA欧洲篮球A级锦标赛": "European U20 Championship Division A",
    "FIBA欧洲篮球B级锦标赛": "European U20 Championship Division B",
    "FIBA欧洲女子篮球锦标赛A级": "European U20 Championship Division A Women",
    "FIBA欧洲女子篮球锦标赛B级": "European U20 Championship Division B Women",
    "菲律宾PBA总督杯": "Philippines - PBA Governors Cup",
    "澳洲篮球": "NBL",
    "篮球 - NCAA": "NCAA",
    "新西兰 - NBL": "Uganda NBL",
    "澳大利亚": "Australia",
    "女子大V": "Australia",
    "女子 NBL 1": "Australia",
    "智利全国篮球联赛": "Chile - LNB",
    "波多黎各国家篮球联赛": "Puerto Rico - Superior Nacional",
    "黎巴嫩篮球甲级联赛": "Lebanon - Lebanese Basketball League",
    "巴西LBF女子篮球联赛": "Brazil - LBF Women",
    "卢旺达全国篮球联赛": "Rwanda - National League",
    "乌干达篮球联赛": "Uganda NBL",
    "乌干达女子篮球联赛": "Uganda NBL Women",
    "乌拉圭女子篮球联赛": "Uruguay - Liga Femenina",
    "马里篮球甲级联赛": "Mali - Premere Division",
    # Baseball
    "MLB": "MLB",
    "美国职业棒球大联盟": "MLB",
    "日本职业棒球": "Nippon Professional Baseball",
    "韩国棒球": "Korea Professional Baseball",
    # Tennis
    "ATP - 博斯塔德公开赛": "ATP Bastad",
    "ATP - 大满贯温布尔登网球公开赛": "ATP Wimbledon",
    "ATP - 格施塔德公开赛": "ATP Gstaad",
    "ATP - 乌马格公开赛": "ATP Umag",
    "ATP挑战赛 - 格兰比公开赛": "ATP Challenger Granby",
    "ATP挑战赛 - 波哥大公开赛": "ATP Challenger Bogota",
    "ATP挑战赛 - 科尔德农斯公开赛": "ATP Challenger Cordenons",
    "WTA - 罗马公开赛": "WTA 125K Rome",
    "WTA - 雅西公开赛": "WTA Iasi",
    "WTA - 基茨比厄尔公开赛": "WTA 125K Kitzbuhel",
    "WTA - 伊斯坦布尔 2 公开赛": "WTA 125K Istanbul",
    "WTA - 雅典公开赛": "WTA Athens",
    "WTA - 孔特雷克塞维尔公开赛": "WTA 125K Contrexeville - Final",
    "WTA - 孔特雷克塞维尔公开赛 - 双打": "WTA 125K Contrexeville - Doubles",
    "WTA - 纽波特公开赛": "WTA 125K Newport",
}

# 常见球队中英名称映射（用于匹配验证）
TEAM_NAME_MAP = {
    # 球会友谊赛 — 英国球队
    "阿克灵顿斯坦利": "Accrington Stanley",
    "布莱克本": "Blackburn",
    "切斯特菲尔德": "Chesterfield",
    "谢菲尔德联队": "Sheffield United",
    "沃特福德": "Watford",
    "博瑞汉姆": "Boreham Wood",
    "沃尔索尔": "Walsall",
    "雷克斯汉姆": "Wrexham",
    "伯顿": "Burton",
    "艾尔佛莱顿": "Alfreton",
    "林肯城": "Lincoln City",
    "波士顿联队": "Boston United",
    "约克城": "York City",
    "巴恩斯利": "Barnsley",
    "利明顿": "Leamington",
    "特温特": "Twente",
    "PAOK沙朗历基": "PAOK",
    "布拉格斯巴达": "Sparta Prague",
    "大阪钢巴": "Gamba Osaka",
    "标准列日": "Standard Liege",
    "洛桑体育队": "Lausanne",
    "纳沙泰尔": "Xamax",
    "布隆德比": "Brondby",
    "欧登塞": "Odense",
    "法兰波垒斯": "Francs Borains",
    "红牛布拉干蒂诺": "Bragantino",
    "巴拉纳竞技": "Athletico Paranaense",
    "莱比锡火车站": "RB Leipzig",
    "柏林赫塔": "Hertha Berlin",
    "法尔肯堡": "Falkenberg",
    "兰斯科罗纳": "Landskrona",
    "克拉罗夫": "Klarov",  # unclear, might be a czech team
    # 苏格兰联赛杯
    "邓迪": "Dundee",
    "艾尔德里联": "Airdrieonians",
    "东基尔布莱德": "East Kilbride",
    "邓弗姆林": "Dunfermline",
    "因弗内斯": "Inverness",
    "东法夫郡": "East Fife",
    "爱丁堡": "Edinburgh City",
    "福尔柯克": "Falkirk",
    "女王公园": "Queen's Park",
    "南方女王FC": "Queen of the South",
    "凯尔蒂赫斯": "Kelty Hearts",
    "斯巴顿斯": "Spartans",
    "阿布罗斯": "Arbroath",
    "邓巴顿": "Dumbarton",
    "圣米伦": "St Mirren",
    "彼得·黑德": "Peterhead",
    "汉密尔顿学院": "Hamilton Academical",
    "斯青威尔": "Stenhousemuir",
    "艾尔联": "Ayr United",
    "帕尔蒂克": "Partick Thistle",
    "摩顿": "Greenock Morton",
    "罗斯郡": "Ross County",
    "安南竞技": "Annan Athletic",
    "拉茨流浪者": "Raith Rovers",
    "埃尔金城": "Elgin City",
    "斯坦豪斯摩尔": "Stenhousemuir",
    "福弗尔竞技": "Forfar Athletic",
    "布若亚": "Brora Rangers",
    "林利斯哥玫瑰": "Linlithgow Rose",
    "布里金城": "Brechin City",
    "埃尔金城": "Elgin City",
    # 瑞典超
    "米亚尔比": "Mjallby",
    "AIK索尔纳": "AIK Solna",
    # 挪威超
    "奥勒松": "Aalesund",
    "莫尔德": "Molde",
    # 芬兰超
    "TPS土尔库": "TPS",
    "奥卢": "Oulu",
    "格尼斯坦": "Gnistan",
    "玛丽港": "Mariehamn",
    # 芬兰甲
    "哈卡": "Haka",
    "EIF埃克纳斯": "Ekenas",
    "华小学院": "JJK",
    "MP米克力": "MP",
    # 芬兰乙
    "中新地": "Mypa",  # might be different
    "詹兹": "Jazz",
    "VJS万塔": "VJS",
    "奥陆": "Oulu",
    "KPV科格拉": "KPV",
    "韦斯屈莱": "Jyvaskyla",
    "阿卡提米亚": "Academia",
    # 乌拉圭甲
    "尤文图德德拉": "Juventud de Las Piedras",
    "蒙得维的亚城图尔克": "Montevideo City Torque",
    # 哈萨克超
    "捷迪苏": "Zhetysu",
    "阿勒泰瑟美": "Altay",
    # 巴拉圭乙
    "诺维布雷": "Novibet",
    "桑坦尼体育会": "Sportivo Santani",
    # 俄罗斯甲
    "乌里扬诺夫斯克": "Ulyanovsk",
    "叶尼塞": "Yenisey",
    # 澳门甲
    "嘉华": "Ka Wa",
    "千叶罗德海洋": "Chiba Lotte Marines",
    # 白俄罗斯超
    "巴拉诺维治": "Baranovichi",
    "若基诺鱼雷": "Torpedo Zhodino",
    # 冰岛甲
    "胡萨维克": "Husavik",
    "阿费查尔丁": "Fjardabyggd",
    # 爱沙尼亚甲
    "诺米联": "Nomme United",
    "库雷撒勒": "Kuressaare",
    # 补充未映射
    "RAAL 拿路维亚": "RAAL La Louviere",
    "TPV坦佩雷": "TPV",
    "洛克伦特姆斯": "Lokeren-Temse",
    # 欧洲冠军联赛-资格赛
    "沙姆洛克流浪": "Shamrock Rovers",
    "佛罗里亚纳": "Floriana",
    "库奥皮奥": "KuPS",
    "华达": "Vardar",
    "伊比利亚 1999": "Iberia 1999",
    "塔林弗洛拉": "Flora Tallinn",
    "新圣徒": "The New Saints",
    "萨巴赫": "Sabah FK",
    "索菲亚列夫斯基": "Levski Sofia",
    "巴尼亚卢卡战士": "Borac Banja Luka",
    # 欧足联欧洲联赛-资格赛
    "费伦茨瓦罗斯": "Ferencvaros",
    "伏伊伏丁那": "Vojvodina",
    "克卢日大学": "Universitatea Cluj",
    "基辅迪纳摩": "Dynamo Kyiv",
    "斯利纳": "Zilina",
    "哈伊杜克斯普利特": "Hajduk Split",
    "德利城": "Derry City",
    "索菲亚中央陆军": "CSKA Sofia",
    # 英格兰超级联赛
    "富勒姆": "Fulham",
    "切尔西": "Chelsea",
    "伊普斯维奇": "Ipswich Town",
    "桑德兰": "Sunderland",
    "埃弗顿": "Everton",
    "水晶宫": "Crystal Palace",
    "曼彻斯特城": "Manchester City",
    "伯恩茅斯": "Bournemouth",
    "诺丁汉森林": "Nottingham Forest",
    "利兹联": "Leeds United",
    "纽卡斯尔联": "Newcastle United",
    "利物浦": "Liverpool",
    "布伦特福德": "Brentford",
    "托特纳姆热刺": "Tottenham Hotspur",
    "布莱顿": "Brighton and Hove Albion",
    "阿斯顿维拉": "Aston Villa",
    "赫尔城": "Hull City",
    "曼彻斯特联": "Manchester United",
    "阿森纳": "Arsenal",
    "考文垂": "Coventry City",
    # 德国甲级联赛
    "莱比锡红牛": "RB Leipzig",
    "门兴格拉德巴赫": "Borussia Monchengladbach",
    "艾禾斯堡": "Elversberg",
    "勒沃库森": "Bayer Leverkusen",
    "柏林联合": "Union Berlin",
    "法兰克福": "Eintracht Frankfurt",
    "科隆": "FC Koln",
    "霍芬海姆": "Hoffenheim",
    "多特蒙德": "Borussia Dortmund",
    "汉堡": "Hamburger SV",
    "奥格斯堡": "Augsburg",
    "沙尔克 04": "Schalke 04",
    "拜仁慕尼黑": "Bayern Munich",
    "斯图加特": "Stuttgart",
    # 西班牙甲级联赛
    "西班牙人": "Espanyol",
    "莱万特": "Levante",
    "阿拉维斯": "Alaves",
    "赫塔菲": "Getafe",
    "拉科鲁尼亚": "Deportivo La Coruna",
    "埃尔切": "Elche",
    "巴塞罗那": "Barcelona",
    "毕尔巴鄂竞技": "Athletic Bilbao",
    "瓦伦西亚": "Valencia",
    "皇家贝蒂斯": "Real Betis",
    "桑坦德竞技": "Racing Santander",
    "比利亚雷亚尔": "Villarreal",
    "皇家马德里": "Real Madrid",
    "皇家社会": "Real Sociedad",
    # 补充映射
    "伊斯卡尔德斯": "Inter Club d'Escaldes",
    "林肯红魔鬼": "Lincoln Red Imps",
    "弗赖堡": "Freiburg",
    "云达不莱梅": "Werder Bremen",
    # Soccer 补充
    "巴黎圣日尔曼": "Paris Saint-Germain",
    "曼城": "Manchester City",
    # Baseball / NPB
    "东北乐天金鹫": "Tohoku Rakuten Golden Eagles",
    "中日龙": "Chunichi Dragons",
    "北海道日本火腿斗士": "Hokkaido Nippon-Ham Fighters",
    "埼玉西武狮": "Saitama Seibu Lions",
    "广岛东洋鲤鱼": "Hiroshima Toyo Carp",
    "横滨海湾之星": "Yokohama DeNA BayStars",
    "欧力士野牛": "Orix Buffaloes",
    "福冈软银鹰": "Fukuoka SoftBank Hawks",
    "读卖巨人": "Yomiuri Giants",
    "阪神虎": "Hanshin Tigers",
    "东京益力多燕子": "Tokyo Yakult Swallows",
    "乐天桃猿": "Rakuten Monkeys",
    "味全龙": "Wei Chuan Dragons",
    "统一7-ELEVen狮子": "Uni-President 7-ELEVEN Lions",
    "中信兄弟": "CTBC Brothers",
    "富邦悍将": "Fubon Guardians",
    "台钢雄鹰": "TSG Hawks",
    # Baseball / MLB
    "华盛顿国民": "Washington Nationals",
    "圣迭戈教士": "San Diego Padres",
    "多伦多蓝鸟": "Toronto Blue Jays",
    "纽约扬基": "New York Yankees",
    "芝加哥小熊": "Chicago Cubs",
    "辛辛那提红人": "Cincinnati Reds",
    "科罗拉多洛基山": "Colorado Rockies",
    "洛杉矶天使": "Los Angeles Angels",
    # Basketball
    "奥克兰大蜥蜴": "Auckland Tuatara",
    "奥塔哥掘金": "Otago Nuggets",
}


def api_get(path, retry=True):
    url = f"{API_BASE}{path}"
    for attempt in range(MAX_RETRIES if retry else 1):
        try:
            resp = SESSION.get(url, timeout=30)
            if resp.status_code == 429:
                wait = RETRY_DELAY * (2 ** attempt)
                print(f"  ⏳ 429 rate limited, retry in {wait:.0f}s...")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                    continue
                return None
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                print(f"  ⏳ timeout, retry in {RETRY_DELAY:.0f}s...")
                time.sleep(RETRY_DELAY)
                continue
            return None
        except Exception:
            return None
    return None


def us_to_decimal(us_price):
    if us_price is None:
        return None
    if us_price > 0:
        return round(1 + us_price / 100, 4)
    else:
        return round(1 - 100 / us_price, 4)


def load_bb_odds():
    path = DATA_DIR / "bb_odds_extracted.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text()).get("matches", [])
    # 去重：相同 (home, away, league) 只保留第一个
    seen = set()
    unique = []
    for m in data:
        key = (m.get("home", ""), m.get("away", ""), m.get("league", ""))
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def extract_bb_1x2(bb_match, sport="football"):
    """Extract 1X2 odds from BB match.

    3-way (足球): odds[0:3] = [home, draw, away]
    2-way (篮球/网球/棒球): odds[0:2] = [home, away]

    Returns (odds_list, is_valid).
    """
    odds = bb_match.get("odds_values", [])
    full_text = bb_match.get("full_text", "")

    if sport in TWO_WAY_SPORTS:
        n = 2
    else:
        n = 3

    if len(odds) < n:
        return [], False

    if sport not in TWO_WAY_SPORTS:
        # Football 3-way: check if 1X2 is available (no "-" after "和")
        ft_compact = " ".join(full_text.split())
        he_idx = ft_compact.find("和")
        if he_idx >= 0:
            after_he = ft_compact[he_idx:he_idx+30]
            if "-" in after_he.split()[1:4]:
                return [], False

    bb_1x2 = []
    for o in odds[:n]:
        try:
            val = float(o)
            if 1.01 <= val <= 51.0:
                bb_1x2.append(val)
        except (ValueError, TypeError):
            pass

    if len(bb_1x2) < n:
        return [], False

    return bb_1x2, True


def parse_asian_line(line_str):
    """Convert Chinese Asian handicap notation to decimal line.
    Examples: '-0/0.5' → -0.25, '+0.5/1' → +0.75, '-1' → -1.0, '大2.5' → 2.5
    """
    if not line_str:
        return None
    s = line_str.strip()

    if s.startswith('大') or s.startswith('小'):
        try:
            return float(s[1:])
        except ValueError:
            return None

    sign = 1.0
    rest = s
    if s.startswith('+'):
        sign = 1.0
        rest = s[1:]
    elif s.startswith('-'):
        sign = -1.0
        rest = s[1:]

    if '/' in rest:
        parts = rest.split('/')
        try:
            low = float(parts[0])
            high = float(parts[1])
            return sign * (low + high) / 2.0
        except (ValueError, IndexError):
            return None

    try:
        return sign * float(rest)
    except ValueError:
        return None


def extract_bb_handicap(bb_match, sport="football"):
    """Extract handicap odds and line from BB match.

    3-way (足球): handicap odds at odds[3:5], lines found in full_text
    2-way (篮球/网球/棒球): handicap odds at odds[2:4]

    Uses 主/客 labels in full_text to correctly assign home/away lines.
    """
    odds = bb_match.get("odds_values", [])
    idx = 3 if sport not in TWO_WAY_SPORTS else 2
    if len(odds) < idx + 2:
        return None

    home_odds = float(odds[idx])
    away_odds = float(odds[idx + 1])

    text = bb_match.get("full_text", "")
    tokens = [t.strip() for t in text.split('\n') if t.strip()]

    home_line_str = ""
    away_line_str = ""

    # Phase 1: 寻找 "主" + 盘口线 / "客" + 盘口线 配对
    for i, t in enumerate(tokens):
        if t == '主' and i + 1 < len(tokens):
            if re.match(r'^[+-]', tokens[i + 1]):
                home_line_str = tokens[i + 1]
        elif t == '客' and i + 1 < len(tokens):
            if re.match(r'^[+-]', tokens[i + 1]):
                away_line_str = tokens[i + 1]

    # Phase 2: 回退 — 按顺序取前两条盘口线
    if not home_line_str and not away_line_str:
        lines_found = []
        for t in tokens:
            if re.match(r'^[+-]\d+(?:\.\d+)?(?:\/\d+(?:\.\d+)?)?$', t):
                lines_found.append(t)
        if len(lines_found) >= 2:
            home_line_str = lines_found[0]
            away_line_str = lines_found[1]

    if not home_line_str and not away_line_str:
        return None

    home_line_val = parse_asian_line(home_line_str) if home_line_str else None
    away_line_val = parse_asian_line(away_line_str) if away_line_str else round(-(home_line_val or 0), 2)

    return {
        "home_odds": home_odds,
        "away_odds": away_odds,
        "home_line": home_line_val,
        "away_line": away_line_val,
        "home_line_str": home_line_str,
        "away_line_str": away_line_str,
    }


def extract_bb_ou(bb_match, sport="football"):
    """Extract over/under odds and line from BB match.

    3-way (足球): O/U odds at odds[5:7]
    2-way (篮球/网球/棒球): O/U odds at odds[-2:] (最后2个赔率)

    注意：网球可能有多个让盘口线(alternate handicaps)占用 odds[2:N-2]，
    所以 O/U 不能固定在 odds[4:6]，必须用最后2个赔率。
    """
    odds = bb_match.get("odds_values", [])
    if sport in TWO_WAY_SPORTS:
        if len(odds) < 6:
            return None  # 只有 ml+hc，没有大小盘
        idx = len(odds) - 2  # 大小盘永远是最后的2个赔率
    else:
        idx = 5  # 足球：3 ml + 2 hc
    if len(odds) < idx + 2:
        return None

    over_odds = float(odds[idx])
    under_odds = float(odds[idx + 1])

    text = bb_match.get("full_text", "")
    tokens = [t.strip() for t in text.split('\n') if t.strip()]

    over_line_val = None
    under_line_val = None
    for t in tokens:
        if t.startswith('大'):
            ov = parse_asian_line(t)
            if ov is not None:
                over_line_val = ov
        elif t.startswith('小'):
            uv = parse_asian_line(t)
            if uv is not None:
                under_line_val = uv

    if over_line_val is not None and under_line_val is not None:
        return {
            "over_odds": over_odds,
            "under_odds": under_odds,
            "line": over_line_val,
        }
    return None


def sort_ml_prices(prices):
    """Sort moneyline prices to [home, draw, away] order by designation."""
    order = {"home": 0, "draw": 1, "away": 2}
    sorted_p = sorted(prices, key=lambda p: order.get(p.get("designation", ""), 99))
    return sorted_p


def get_league_matchups_and_markets(league_id):
    """Get matchups and markets for a specific league"""
    matchups = api_get(f"/leagues/{league_id}/matchups")
    if not matchups:
        return []

    mu_map = {m["id"]: m for m in matchups}

    markets = api_get(f"/leagues/{league_id}/markets/straight")
    if not markets:
        return []

    mm = {}
    for m in markets:
        mid = m.get("matchupId")
        if mid not in mm:
            mm[mid] = []
        mm[mid].append(m)

    result = []
    for mid, mkt_list in mm.items():
        mu = mu_map.get(mid)
        if not mu:
            continue

        league = mu.get("league", {})
        participants = mu.get("participants", [])

        home, away = "", ""
        for p in participants:
            if p.get("alignment") == "home":
                home = p.get("name", "")
            elif p.get("alignment") == "away":
                away = p.get("name", "")

        if not home and not away:
            continue

        moneyline, spread, total = [], [], []
        ht_moneyline, ht_spread, ht_total = [], [], []
        team_total = []
        for mkt in mkt_list:
            mtype = mkt.get("type", "")
            per = mkt.get("period", 0)
            prices = [{
                "designation": p.get("designation", ""),
                "price_decimal": us_to_decimal(p.get("price")),
                "points": p.get("points"),  # handicap line / total line
            } for p in mkt.get("prices", [])]

            entry = {"period": per, "prices": prices}
            if mtype == "moneyline":
                entry["prices_sorted"] = sort_ml_prices(prices)
                if per == 0:
                    moneyline.append(entry)
                elif per == 1:
                    ht_moneyline.append(entry)
            elif mtype == "spread":
                if per == 0:
                    spread.append(entry)
                elif per == 1:
                    ht_spread.append(entry)
            elif mtype == "total":
                if per == 0:
                    total.append(entry)
                elif per == 1:
                    ht_total.append(entry)
            elif mtype == "team_total":
                team_total.append(entry)

        result.append({
            "matchup_id": mid,
            "league_name": league.get("name", ""),
            "league_group": league.get("group", ""),
            "home": home,
            "away": away,
            "start_time": mu.get("startTime", ""),
            "moneyline": moneyline,
            "spread": spread,
            "total": total,
            "ht_moneyline": ht_moneyline,
            "ht_spread": ht_spread,
            "ht_total": ht_total,
            "team_total": team_total,
        })

    # 对网球：把 Games 条目（局数让分/大小）合并到常规条目
    games_map = {}
    for r in result:
        h, a = r["home"], r["away"]
        if h.endswith(" (Games)") or a.endswith(" (Games)"):
            base_h = h.replace(" (Games)", "")
            base_a = a.replace(" (Games)", "")
            games_map[(base_h, base_a)] = {"spread": r["spread"], "total": r["total"]}
    for r in result:
        h, a = r["home"], r["away"]
        if not h.endswith(" (Games)") and not a.endswith(" (Games)"):
            g = games_map.get((h, a))
            if g:
                r["games_spread"] = g["spread"]
                r["games_total"] = g["total"]

    return result


def team_name_score(bb_home, bb_away, pin_home, pin_away):
    """Score how well BB team names (Chinese) match Pinnacle team names (English).
    Uses TEAM_NAME_MAP for known translations and fuzzy matching.
    Returns score 0.0-1.0.
    """
    def lookup_cn(name):
        return TEAM_NAME_MAP.get(name, name.lower())

    bb_home_en = lookup_cn(bb_home)
    bb_away_en = lookup_cn(bb_away)
    # Normalize: lowercase for comparison
    bb_home_en_l = bb_home_en.lower() if bb_home_en else ""
    bb_away_en_l = bb_away_en.lower() if bb_away_en else ""
    pin_home_l = pin_home.lower()
    pin_away_l = pin_away.lower()

    # If the name wasn't in the map, we can't verify it
    bb_home_mapped = bb_home_en != bb_home.lower()
    bb_away_mapped = bb_away_en != bb_away.lower()

    def name_match(bb_en_l, pin_l):
        """Check if BB English name matches Pinnacle name, case-insensitive."""
        if not bb_en_l or not pin_l:
            return False
        # Exact match
        if bb_en_l == pin_l:
            return True
        # BB name is a substring of Pinnacle name (e.g., "Inverness" in "inverness ct")
        if bb_en_l in pin_l or pin_l in bb_en_l:
            return True
        # Pinnacle name is in BB name (e.g., "east fife" in "east fife...")
        return False

    home_match = name_match(bb_home_en_l, pin_home_l) if bb_home_mapped else False
    away_match = name_match(bb_away_en_l, pin_away_l) if bb_away_mapped else False

    if home_match and away_match:
        return 1.0
    elif home_match or away_match:
        return 0.6
    else:
        return 0.0


def get_pin_ml_sorted(pin_match, sport="football"):
    """Get Pinnacle moneyline odds sorted for the given sport.
    3-way (足球): returns [home, draw, away]; 2-way: returns [home, away].
    """
    min_req = 2 if sport in TWO_WAY_SPORTS else 3
    for ml in pin_match.get("moneyline", []):
        if ml["period"] == 0:
            prices = ml.get("prices_sorted", ml.get("prices", []))
            odds = []
            for p in prices:
                if p.get("price_decimal") and 1.01 <= p["price_decimal"] <= 51.0:
                    odds.append(p["price_decimal"])
            if len(odds) >= min_req:
                return odds[:min_req]
    return []


def get_pin_ml_sorted_from_source(ml_source, sport="football"):
    """Get Pinnacle moneyline odds from a market source list (any period).
    3-way: [home, draw, away]; 2-way: [home, away].
    """
    min_req = 2 if sport in TWO_WAY_SPORTS else 3
    for ml in ml_source:
        prices = ml.get("prices_sorted", ml.get("prices", []))
        odds = []
        for p in prices:
            if p.get("price_decimal") and 1.01 <= p["price_decimal"] <= 51.0:
                odds.append(p["price_decimal"])
        if len(odds) >= min_req:
            return odds[:min_req]
    return []


def get_pin_spread(pin_match, target_line=None, source=None):
    """Get Pinnacle spread (handicap).

    source: 直接传入 spread 列表（如 ht_spread），不传则用 pin_match["spread"] period=0
    """
    candidates = []
    entries = source if source is not None else pin_match.get("spread", [])
    for sp in entries:
        if source is None and sp.get("period", 0) != 0:
            continue
        prices = sp.get("prices", [])
        home_p = None
        away_p = None
        for p in prices:
            if p.get("designation") == "home":
                home_p = p
            elif p.get("designation") == "away":
                away_p = p
        if home_p and away_p:
            candidates.append((home_p, away_p))

    if not candidates:
        return None, None
    if target_line is None or len(candidates) == 1:
        return candidates[0]

    best = candidates[0]
    best_diff = abs(target_line - candidates[0][0].get("points", 0))
    for home_p, away_p in candidates[1:]:
        diff = abs(target_line - home_p.get("points", 0))
        if diff < best_diff:
            best_diff = diff
            best = (home_p, away_p)
    return best


def get_pin_total(pin_match, target_line=None, source=None):
    """Get Pinnacle total (over/under).

    source: 直接传入 total 列表（如 ht_total），不传则用 pin_match["total"] period=0
    """
    candidates = []
    entries = source if source is not None else pin_match.get("total", [])
    for t in entries:
        if source is None and t.get("period", 0) != 0:
            continue
        prices = t.get("prices", [])
        over_p = None
        under_p = None
        for p in prices:
            if p.get("designation") == "over":
                over_p = p
            elif p.get("designation") == "under":
                under_p = p
        if over_p and under_p:
            candidates.append((over_p, under_p))

    if not candidates:
        return None, None
    if target_line is None or len(candidates) == 1:
        return candidates[0]

    best = candidates[0]
    best_diff = abs(target_line - candidates[0][0].get("points", 0))
    for over_p, under_p in candidates[1:]:
        diff = abs(target_line - over_p.get("points", 0))
        if diff < best_diff:
            best_diff = diff
            best = (over_p, under_p)
    return best


def find_pin_match_by_name(bb_home, bb_away, pin_list):
    """Find Pinnacle match by team name mapping.

    Uses TEAM_NAME_MAP to translate BB Chinese names to English,
    then finds the best matching Pinnacle match.
    Returns (match, score) or (None, 0).
    """
    bb_home_en = TEAM_NAME_MAP.get(bb_home, "").lower()
    bb_away_en = TEAM_NAME_MAP.get(bb_away, "").lower()

    if not bb_home_en and not bb_away_en:
        return None, 0.0

    best_match = None
    best_score = 0.0

    for pin in pin_list:
        pin_home_l = pin.get("home", "").lower()
        pin_away_l = pin.get("away", "").lower()

        score_parts = []

        if bb_home_en:
            if bb_home_en == pin_home_l:
                score_parts.append(1.0)
            elif bb_home_en in pin_home_l or pin_home_l in bb_home_en:
                score_parts.append(0.9)
            else:
                score_parts.append(0.0)

        if bb_away_en:
            if bb_away_en == pin_away_l:
                score_parts.append(1.0)
            elif bb_away_en in pin_away_l or pin_away_l in bb_away_en:
                score_parts.append(0.9)
            else:
                score_parts.append(0.0)

        avg = sum(score_parts) / len(score_parts) if score_parts else 0

        if avg > best_score:
            best_score = avg
            best_match = pin

    if best_score >= 0.8:
        return best_match, best_score
    return None, 0.0


def _bb_to_epoch(bb_match):
    """Convert BB match period+time (GMT+8) to epoch seconds (UTC)."""
    period = bb_match.get("period", "")  # "07/15"
    btime = bb_match.get("time", "")    # "03:00"
    if not period or not btime:
        return None
    try:
        dt_str = f"2026-{period[:2]}-{period[3:5]}T{btime[:2]}:{btime[3:5]}:00"
        dt_naive = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        # GMT+8 → UTC: subtract 8 hours
        dt_utc = dt_naive - timedelta(hours=8)
        return int(dt_utc.replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, IndexError):
        return None


def _pin_to_epoch(pin_match):
    """Convert Pinnacle matchup start_time (UTC) to epoch seconds."""
    start = pin_match.get("start_time", "")
    if not start or "T" not in start:
        return None
    try:
        # Handle "2026-07-14T19:00:00Z" by replacing Z with +00:00
        start_clean = start.replace("Z", "+00:00")
        dt = datetime.fromisoformat(start_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, IndexError):
        return None


def _odds_similarity(bb_1x2, pin_1x2, min_odds=3):
    """Compute odds similarity score (0-1)."""
    if len(bb_1x2) < min_odds or len(pin_1x2) < min_odds:
        return 0.0
    ratios = []
    for i in range(min_odds):
        bb_o = bb_1x2[i]
        pin_o = pin_1x2[i]
        if pin_o > 0:
            ratios.append(min(bb_o, pin_o) / max(bb_o, pin_o))
        else:
            ratios.append(0)

    avg_ratio = sum(ratios) / len(ratios) if ratios else 0
    if len(ratios) >= min_odds and max(ratios) - min(ratios) > 0.15:
        avg_ratio *= 0.8
    return avg_ratio


def _make_bb_key(bb):
    return f"{bb.get('home','')}|{bb.get('away','')}|{bb.get('league','')}"


def _compute_combined_score(bb, bb_1x2, bb_epoch, pin, pin_ml, sport="football"):
    """Combined score = odds_similarity × time_factor (0-1)."""
    min_odds = 2 if sport in TWO_WAY_SPORTS else 3
    odds_score = _odds_similarity(bb_1x2, pin_ml, min_odds)
    time_factor = 1.0
    if bb_epoch:
        pin_epoch = _pin_to_epoch(pin)
        if pin_epoch is not None:
            diff = abs(bb_epoch - pin_epoch)
            if diff < 600:           # < 10 min — same time
                time_factor = 1.0
            elif diff < 1800:         # 10-30 min
                time_factor = 0.97
            elif diff < 3600:         # 30-60 min
                time_factor = 0.93
            elif diff < 7200:         # 1-2 hr
                time_factor = 0.88
            elif diff < 14400:        # 2-4 hr
                time_factor = 0.50
            else:
                time_factor = 0.20
    return odds_score * time_factor


def find_matches_by_odds(bb_matches, pin_matches_by_league):
    """Match BB体育 games to Pinnacle games.

    Two-phase:
    1. Name-based (preferred) — uses TEAM_NAME_MAP
    2. Global greedy: build all (BB, Pin) pairs per league, sort by
       combined (time × odds) score, greedily assign.  Prevents the
       common swap-error when two matches share a kickoff time.
    """
    name_matched = []
    matched = []
    used_pin_ids = set()
    used_bb_keys = set()

    # Pre-compute BB data（检测运动类型）
    bb_data = {}
    for bb in bb_matches:
        sport = detect_sport(bb)
        bb_1x2, valid = extract_bb_1x2(bb, sport)
        min_odds = 2 if sport in TWO_WAY_SPORTS else 3
        if not valid or len(bb_1x2) < min_odds:
            continue
        bb_data[_make_bb_key(bb)] = {
            "match": bb, "bb_1x2": bb_1x2, "epoch": _bb_to_epoch(bb),
            "sport": sport,
        }

    # Phase 1: Name-based
    for bb_key, bd in bb_data.items():
        bb = bd["match"]
        sport = bd["sport"]
        bb_league = bb.get("league", "")
        pin_list = pin_matches_by_league.get(bb_league, [])
        pin_match, name_score = find_pin_match_by_name(
            bb.get("home", ""), bb.get("away", ""), pin_list,
        )
        if pin_match and name_score >= 0.9:
            pin_ml = get_pin_ml_sorted(pin_match, sport)
            min_odds = 2 if sport in TWO_WAY_SPORTS else 3
            if len(pin_ml) >= min_odds:
                mid = pin_match.get("matchup_id", id(pin_match))
                if mid not in used_pin_ids:
                    used_pin_ids.add(mid)
                    used_bb_keys.add(bb_key)
                    name_matched.append({
                        "bb": bb, "pin": pin_match, "league": bb_league,
                        "match_score": 1.0, "team_score": name_score,
                        "match_type": "name",
                        "bb_1x2": bd["bb_1x2"], "pin_1x2": pin_ml,
                        "sport": sport,
                    })

    # Phase 2: Global greedy per-league
    for bb_league, pin_list in pin_matches_by_league.items():
        pairs = []
        for bb_key, bd in bb_data.items():
            if bb_key in used_bb_keys:
                continue
            if bd["match"].get("league", "") != bb_league:
                continue
            sport = bd["sport"]
            min_odds = 2 if sport in TWO_WAY_SPORTS else 3
            for pin in pin_list:
                pin_id = pin.get("matchup_id", id(pin))
                if pin_id in used_pin_ids:
                    continue
                pin_ml = get_pin_ml_sorted(pin, sport)
                if len(pin_ml) < min_odds:
                    continue
                combined = _compute_combined_score(
                    bd["match"], bd["bb_1x2"], bd["epoch"], pin, pin_ml, sport,
                )
                if combined >= 0.70:
                    pairs.append((combined, bb_key, bd["match"], pin,
                                  bd["bb_1x2"], pin_ml, pin_id, sport))

        pairs.sort(key=lambda x: -x[0])
        for combined, bb_key, bb, pin, bb_1x2, pin_ml, pin_id, sport in pairs:
            if bb_key in used_bb_keys or pin_id in used_pin_ids:
                continue
            used_bb_keys.add(bb_key)
            used_pin_ids.add(pin_id)
            matched.append({
                "bb": bb, "pin": pin, "league": bb_league,
                "match_score": round(combined, 3), "match_type": "time",
                "bb_1x2": bb_1x2, "pin_1x2": pin_ml, "sport": sport,
            })

    return name_matched + matched


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
    """匹配 Pinnacle 联赛名，优先返回精确匹配。"""
    needle = pin_name.lower().strip()
    matched = []
    for lid, info in all_sport_matchups.items():
        if _match_pin_name(needle, info["name"]):
            matched.append(lid)
    if not matched:
        return None
    # 精确匹配优先（防止 "Division A" 前缀匹配到 "Division A Women"）
    for lid in matched:
        if all_sport_matchups[lid]["name"].lower() == needle:
            return lid
    return matched[0]


def find_pinnacle_league_id(bb_league_name, all_sport_matchups):
    """Find Pinnacle league ID that matches a BB体育 league name (single best match)"""
    ids = find_pinnacle_league_ids(bb_league_name, all_sport_matchups)
    return ids[0] if ids else None


def find_pinnacle_league_ids(bb_league_name, all_sport_matchups):
    """Find ALL Pinnacle league IDs matching a BB体育 league name.

    网球等赛事在 Pinnacle 可能拆分为多个子联赛（Qualifiers、R1等），
    返回所有匹配的联赛 ID + 同前缀的子联赛。

    策略：
    1. LEAGUE_KEYWORDS 精确映射
    2. 对已精确映射的联赛，找 Pinnacle 上同前缀名的子联赛（如 "ATP Bastad"
       → "ATP Bastad - Qualifiers"、"ATP Bastad - R1"）
       限制：只对单赛事名（不含" - "在原映射名中）做子联赛扩展
    3. 未精确映射的联赛用英文关键词做可控模糊匹配
    """
    bb_lower = bb_league_name.lower().strip()
    matched_ids = set()

    # Phase 1: LEAGUE_KEYWORDS 精确映射
    matched_pin_names = []  # Pinnacle 联赛名列表
    for bb_name, pin_name in LEAGUE_KEYWORDS.items():
        if bb_name in bb_league_name or bb_league_name in bb_name:
            pin_names = [pin_name] if isinstance(pin_name, str) else pin_name
            for pn in pin_names:
                lid = _find_best_league(pn, all_sport_matchups)
                if lid:
                    matched_ids.add(lid)
                    matched_pin_names.append(pn)
            break
        if bb_lower == bb_name.lower():
            pin_names = [pin_name] if isinstance(pin_name, str) else pin_name
            for pn in pin_names:
                lid = _find_best_league(pn, all_sport_matchups)
                if lid:
                    matched_ids.add(lid)
                    matched_pin_names.append(pn)
            break

    if matched_ids:
        # Phase 1.5: 子联赛扩展 — 只对不含" - "的短名（如 "ATP Bastad"）
        # 找所有同前缀的 Pinnacle 联赛（如 "ATP Bastad - Qualifiers"）
        # 但不对 "Russia - First League" 这种结构扩展
        for pn in matched_pin_names:
            if " - " in pn:
                continue  # 已经是多段名称，不做子联赛扩展
            for lid, info in all_sport_matchups.items():
                if lid in matched_ids:
                    continue
                if info["name"].lower().startswith(pn.lower()):
                    matched_ids.add(lid)

        return sorted(matched_ids)

    # Phase 2: 只有未精确映射的联赛才做英文关键词模糊匹配
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
            elif len(overlap) == 1:
                single_word = list(overlap)[0]
                if single_word not in ("cup", "league", "championship", "championships",
                                       "premier", "division", "super", "open", "tour"):
                    matched_ids.add(lid)

    return sorted(matched_ids) if matched_ids else []


def verify_match(bb_match, pin_match):
    """Verify a match by checking if team names correspond.
    Returns (verified: bool, note: str)."""
    bb_home = bb_match.get("home", "")
    bb_away = bb_match.get("away", "")
    pin_home = pin_match.get("home", "")
    pin_away = pin_match.get("away", "")

    ts = team_name_score(bb_home, bb_away, pin_home, pin_away)

    if ts >= 1.0:
        return True, "队名完全匹配"
    elif ts >= 0.6:
        return True, "队名部分匹配"
    else:
        return False, "队名无法验证（无中文→英文映射）"


def _calibrate_market_line(sport, market_type, bb_line, pin_line, pin_points, is_ht=False):
    """检查 BB 盘口线与 Pinnacle 盘口线是否一致，防止市场错配。

    market_type: "hc"(让球) 或 "ou"(大小)
    is_ht: HT(半场)市场 — 线必须完全一致，不允许近似匹配
    返回 (ok, msg)，ok=False 表示线不匹配，该机会应被过滤掉。
    """
    if bb_line is None or (pin_line is None and pin_points is None):
        return True, ""
    ref = pin_line if pin_line is not None else pin_points
    try:
        ref = float(ref)
    except (TypeError, ValueError):
        return True, ""

    diff = abs(bb_line - ref)

    if market_type == "hc":
        # HT 让球线必须精确一致（通常是0或-0.25）
        if is_ht and diff > 0.01:
            return False, f"HT让球线不一致: BB={bb_line} vs Pinnacle={ref}"
        # FT 让球线偏差超过 0.5 → 匹配错了让球线
        if diff > 0.5:
            return False, f"让球线不一致: BB={bb_line} vs Pinnacle={ref}"
    elif market_type == "ou":
        # 网球特殊：OU 线(20.5) vs sets total(2.5)
        if sport == "tennis" and diff > 5:
            return False, f"大小盘线不匹配: BB={bb_line} vs Pinnacle={ref}，可能用了错误市场"
        # HT 大小盘：线必须精确一致（BB 0.5 vs Pin 0.75 = 无效对比）
        if is_ht and diff > 0.01:
            return False, f"HT大小盘线不一致: BB={bb_line} vs Pinnacle={ref}"
        # FT 常规大小盘线偏差超过 1.0 → 可疑
        if diff > 1.0:
            return False, f"大小盘线不一致: BB={bb_line} vs Pinnacle={ref}"

    return True, ""


def _warn_suspicious(ev_pct, match_score, verified):
    """返回高 EV / 低置信度警告标记，None 表示无警告。"""
    if ev_pct > 20:
        return "⚠️ 溢价异常高(>20%)，可能是匹配错误，请核对球队"
    if ev_pct > 15:
        return "⚠️ 溢价偏高(>15%)，建议核对赔率"
    if ev_pct > 10 and match_score < 0.85:
        return f"⚠️ 匹配度偏低({match_score})，请确认球队是否正确"
    if not verified and match_score < 0.75:
        return "⚠️ 匹配度偏低，请核对球队是否正确"
    return None


def _check_pinnacle():
    """启动时检测 Pinnacle API 连通性。"""
    test_url = f"{API_BASE}/sports/29/matchups"
    SESSION.proxies = {}
    try:
        resp = SESSION.get(test_url, timeout=15)
        if resp.status_code == 200:
            print(f"  ✅ Pinnacle API 连通正常")
            return True
        print(f"  ❌ Pinnacle API 返回 {resp.status_code}")
    except requests.exceptions.Timeout:
        print("  ❌ Pinnacle API 超时")
    except requests.exceptions.ConnectionError:
        print("  ❌ Pinnacle API 连接失败")
    except Exception as e:
        print(f"  ❌ Pinnacle API 异常: {e}")
    return False


def main():
    print("=" * 60)
    print("BB体育 vs Pinnacle 完整赔率对比 v2")
    print("=" * 60)

    # 1. Load BB体育 data
    bb_matches = load_bb_odds()
    # 过滤掉冠军/优胜者盘口（非比赛）
    bb_matches = [m for m in bb_matches if m.get("league", "") not in OUTRIGHT_LEAGUES]
    print(f"\nBB体育: {len(bb_matches)} 场比赛 (排除冠军盘口后)")

    # Count how many have valid 1X2/moneyline
    valid_1x2 = 0
    valid_2way = 0
    for m in bb_matches:
        sport = detect_sport(m)
        _, valid = extract_bb_1x2(m, sport)
        if valid:
            if sport in TWO_WAY_SPORTS:
                valid_2way += 1
            else:
                valid_1x2 += 1
    print(f"  有独赢赔率: {valid_1x2} 场足球 + {valid_2way} 场其他 = {valid_1x2 + valid_2way}")

    # 0. Pinnacle 连通性检测
    if not _check_pinnacle():
        print("\n⚠️ Pinnacle API 不可用，中止。解决办法：")
        print("  1. 确认 Shadowrocket 已开启")
        print("  2. 确认 SOCKS5 代理在 localhost:1082 运行")
        print("  3. 切换代理节点后重试")
        sys.exit(1)

    # 2. Get all matchups from Pinnacle for relevant sports
    all_pin_leagues = {}
    for sid, sname in SPORT_IDS.items():
        matchups = api_get(f"/sports/{sid}/matchups") or []
        for mu in matchups:
            league = mu.get("league", {})
            lid = league.get("id")
            if lid:
                if lid not in all_pin_leagues:
                    all_pin_leagues[lid] = {
                        "name": league.get("name", ""),
                        "group": league.get("group", ""),
                        "sport": sname,
                        "sport_id": sid,
                        "matchup_count": 0,
                    }
                all_pin_leagues[lid]["matchup_count"] += 1

    print(f"Pinnacle 联赛总数: {len(all_pin_leagues)}")

    # 3. Map BB体育 leagues to Pinnacle league IDs
    bb_leagues = {}
    for m in bb_matches:
        league = m.get("league", "?")
        if league not in bb_leagues:
            bb_leagues[league] = 0
        bb_leagues[league] += 1

    print(f"\nBB体育联赛分布 ({len(bb_leagues)}):")
    for league, count in sorted(bb_leagues.items(), key=lambda x: -x[1]):
        pin_ids = find_pinnacle_league_ids(league, all_pin_leagues)
        status = f" → Pinnacle ID={pin_ids}" if pin_ids else " → ❌ 未匹配"
        print(f"  {league}: {count}场{status}")

    # 4. Get Pinnacle odds for matched leagues
    matched_leagues = {}
    for league in bb_leagues:
        pin_ids = find_pinnacle_league_ids(league, all_pin_leagues)
        if pin_ids:
            matched_leagues[league] = pin_ids

    if not matched_leagues:
        print("\n⚠️ 没有找到匹配的 Pinnacle 联赛")
        return

    # 5. Fetch markets for each matched league — 去重，每个 Pinnacle ID 只调用一次
    all_unique_pin_ids = set()
    for pin_ids in matched_leagues.values():
        all_unique_pin_ids.update(pin_ids)
    print(f"\n  Pinnacle 联赛去重后: {len(all_unique_pin_ids)} 个 (来自 {len(matched_leagues)} 个 BB 联赛)")

    all_pin_matches = []
    for call_idx, pin_id in enumerate(sorted(all_unique_pin_ids)):
        if call_idx > 0:
            delay = round(random.uniform(1.8, 4.2), 1)
            print(f"  ⏳ 等待 {delay:.1f}s 避免限流...")
            time.sleep(delay)

        info = all_pin_leagues.get(pin_id, {})
        print(f"\n获取 [{info.get('name', pin_id)}] (ID={pin_id}) 赔率...")
        matches = get_league_matchups_and_markets(pin_id)
        print(f"  → {len(matches)} 场比赛")
        all_pin_matches.extend(matches)

    # 6. Group Pinnacle matches by BB league name for matching
    pin_by_bb_league = {}
    for bb_league in matched_leagues:
        pin_ids = matched_leagues[bb_league]
        pin_league_names = set()
        for pid in pin_ids:
            name = all_pin_leagues.get(pid, {}).get("name", "")
            if name:
                pin_league_names.add(name)
        pin_by_bb_league[bb_league] = [
            m for m in all_pin_matches if m["league_name"] in pin_league_names
        ]

    # 7. Find overlapping matches by odds pattern matching
    matched = find_matches_by_odds(bb_matches, pin_by_bb_league)

    name_matches = [m for m in matched if m.get("match_type") == "name"]
    other_matches = [m for m in matched if m.get("match_type") != "name"]

    print(f"\n\n匹配比赛: {len(matched)} 场")
    print(f"  队名: {len(name_matches)} | 时间+赔率: {len(other_matches)}")

    # 验证匹配的比赛：检查球队名是否一致
    verified_count = 0
    for m in matched:
        verified, note = verify_match(m["bb"], m["pin"])
        m["verified"] = verified
        m["verify_note"] = note
        if verified:
            verified_count += 1
    print(f"  队名验证: {verified_count}/{len(matched)} 可确认球队一致")

    # 校准计数器
    cal_blocked_hc = 0
    cal_blocked_ou = 0

    # For +EV calculation
    valid_matches = matched

    if not matched:
        print("\n⚠️ 联赛匹配成功但没有找到相同比赛")
        return

    # 8. Compare all markets (1X2, Handicap, O/U)
    opportunities = []
    for m in valid_matches:
        bb = m["bb"]
        pin = m["pin"]
        sport = m.get("sport", "football")
        mlabels = MARKET_LABELS.get(sport, MARKET_LABELS["football"])
        bb_ml = m.get("bb_1x2", [])
        pin_ml = m.get("pin_1x2", [])
        n_ml = len(bb_ml)  # 3 for football, 2 for others

        # 开赛时间（北京时间）
        bb_period = bb.get("period", "")
        bb_time = bb.get("time", "")
        bb_start = f"{bb_period} {bb_time}".strip()
        pin_start_raw = pin.get("start_time", "")
        # Convert Pinnacle UTC to epoch for display
        pin_epoch = _pin_to_epoch(pin)

        entry = {
            "league": m["league"],
            "match_type": m.get("match_type", "?"),
            "home_bb": bb.get("home", "?"),
            "away_bb": bb.get("away", "?"),
            "home_pin": pin.get("home", "?"),
            "away_pin": pin.get("away", "?"),
            "match_score": m["match_score"],
            "sport": sport,
            "flags": [],
            "start_time_bb": bb_start,
            "start_time_pin": pin_start_raw,
            "start_time_pin_epoch": pin_epoch,
            "_bb_view": bb.get("_bb_view", "main"),
            "opportunities": [],
            "handicap": [],
            "over_under": [],
            "double_chance": [],
        }

        pin_ml_source = pin.get("moneyline", [])
        pin_hc_source = pin.get("spread", [])
        pin_ou_source = pin.get("total", [])

        # Sanity check: flag if moneyline odds differ by > 3x
        for i in range(n_ml):
            if bb_ml[i] and pin_ml[i]:
                ratio = max(bb_ml[i], pin_ml[i]) / min(bb_ml[i], pin_ml[i])
                if ratio > 3.0:
                    entry["flags"].append(f"{mlabels['ml'][i]}差异{ratio:.1f}x")
                    break

        # --- 独赢 (Moneyline) 带去抽水 ---
        total_implied_ml = sum(1.0 / p for p in pin_ml if p and p > 0)
        for i in range(n_ml):
            bb_o = bb_ml[i]
            pin_o = pin_ml[i]
            if pin_o and pin_o > 0:
                fair_price = round(pin_o * total_implied_ml, 4) if total_implied_ml > 0 else round(pin_o, 2)
                ev = (bb_o - fair_price) / fair_price * 100 if fair_price > 0 else 0
                if ev > 1:
                    entry["opportunities"].append({
                        "designation": mlabels["ml"][i],
                        "bb_odds": bb_o,
                        "pin_odds": pin_o,
                        "fair_price": fair_price,
                        "ev_pct": round(ev, 2),
                    })

        # --- 让球/让分 (Handicap/Spread) ---
        bb_hc = extract_bb_handicap(bb, sport)
        if bb_hc:
            bb_hl = bb_hc.get("home_line") or bb_hc.get("away_line")
            if sport == "tennis" and bb_hl is not None and abs(bb_hl) > 10:
                gs = pin.get("games_spread")
                home_sp, away_sp = get_pin_spread({"spread": gs}) if gs else (None, None)
            else:
                home_sp, away_sp = get_pin_spread(pin, target_line=bb_hl)
            if home_sp and away_sp and home_sp.get("price_decimal") and away_sp.get("price_decimal"):
                pin_home_odds = home_sp["price_decimal"]
                pin_away_odds = away_sp["price_decimal"]
                bb_home_odds = bb_hc["home_odds"]
                bb_away_odds = bb_hc["away_odds"]

                # 校准：检查让球线是否对得上
                pin_hc_line = home_sp.get("points")
                bb_hc_line_val = bb_hc.get("home_line") or bb_hc.get("away_line")
                cal_ok, cal_msg = _calibrate_market_line(sport, "hc", bb_hc_line_val, pin_hc_line, None)
                if not cal_ok:
                    if cal_msg not in entry["flags"]:
                        entry["flags"].append(cal_msg)
                    home_sp = away_sp = None
                    cal_blocked_hc += 1

                if not home_sp or not away_sp:
                    continue
                # 通过盘口线（points）对齐：BB 的哪条线匹配 Pinnacle 的主/客
                bb_hl = bb_hc.get("home_line")
                bb_al = bb_hc.get("away_line")
                pin_hl = home_sp.get("points")
                pin_al = away_sp.get("points")
                swapped = False
                if bb_hl is not None and bb_al is not None and pin_hl is not None and pin_al is not None:
                    home_diff = abs(bb_hl - pin_hl)
                    away_diff = abs(bb_al - pin_al)
                    cross_home = abs(bb_al - pin_hl)
                    cross_away = abs(bb_hl - pin_al)
                    # 如果交叉匹配比直接匹配更好 → 交换
                    if cross_home + cross_away < home_diff + away_diff - 0.01:
                        swapped = True

                if swapped:
                    # BB 的主客与 Pinnacle 相反，交换对比
                    bb_hc_odds_for_pin_home = bb_away_odds
                    bb_hc_odds_for_pin_away = bb_home_odds
                    hc_home_desig = bb_hc.get("away_line_str", "")
                    hc_away_desig = bb_hc.get("home_line_str", "")
                else:
                    bb_hc_odds_for_pin_home = bb_home_odds
                    bb_hc_odds_for_pin_away = bb_away_odds
                    hc_home_desig = bb_hc.get("home_line_str", "")
                    hc_away_desig = bb_hc.get("away_line_str", "")

                # 去抽水公平价
                total_implied_hc = 1.0 / pin_home_odds + 1.0 / pin_away_odds
                pin_home_fair = round(pin_home_odds * total_implied_hc, 4)
                pin_away_fair = round(pin_away_odds * total_implied_hc, 4)

                # EV = (BB - 公平价) / 公平价
                ev_h = (bb_hc_odds_for_pin_home - pin_home_fair) / pin_home_fair * 100
                ev_a = (bb_hc_odds_for_pin_away - pin_away_fair) / pin_away_fair * 100

                if ev_h > 1:
                    entry["handicap"].append({
                        "designation": mlabels["hc_home"],
                        "line": hc_home_desig,
                        "bb_odds": bb_hc_odds_for_pin_home,
                        "pin_odds": pin_home_odds,
                        "fair_price": pin_home_fair,
                        "ev_pct": round(ev_h, 2),
                    })
                if ev_a > 1:
                    entry["handicap"].append({
                        "designation": mlabels["hc_away"],
                        "line": hc_away_desig,
                        "bb_odds": bb_hc_odds_for_pin_away,
                        "pin_odds": pin_away_odds,
                        "fair_price": pin_away_fair,
                        "ev_pct": round(ev_a, 2),
                    })

        # --- 大小 (Over/Under) 带去抽水 ---
        bb_ou = extract_bb_ou(bb, sport)
        if bb_ou:
            # 网球：BB 大小线 > 10 表示局数大小，用 games_total
            bb_line = bb_ou.get("line")
            if sport == "tennis" and bb_line is not None and bb_line > 10:
                gt = pin.get("games_total")
                over_p, under_p = get_pin_total({"total": gt}) if gt else (None, None)
            else:
                # 找线值最接近的 Pinnacle 大小盘（可能有多个大小线）
                over_p, under_p = get_pin_total(pin, target_line=bb_line)
            if over_p and under_p:
                total_implied_ou = 1.0 / over_p["price_decimal"] + 1.0 / under_p["price_decimal"]
                over_fair = round(over_p["price_decimal"] * total_implied_ou, 4)
                under_fair = round(under_p["price_decimal"] * total_implied_ou, 4)

                # 校准：检查大小盘线是否对得上
                pin_ou_line = over_p.get("points")
                cal_ok, cal_msg = _calibrate_market_line(sport, "ou", bb_ou["line"], pin_ou_line, None)
                if not cal_ok:
                    if cal_msg not in entry["flags"]:
                        entry["flags"].append(cal_msg)
                    # 校准失败：跳过整个大小盘
                    over_p = under_p = None
                    cal_blocked_ou += 1
                if not over_p or not under_p:
                    # 校准失败：跳过大小盘 EV 计算，保留独赢/让球结果
                    pass
                else:
                    if over_p.get("price_decimal") and over_p["price_decimal"] > 0:
                        ev_o = (bb_ou["over_odds"] - over_fair) / over_fair * 100
                        if ev_o > 1:
                            entry["over_under"].append({
                                "designation": mlabels["over"],
                                "line": str(bb_ou["line"]),
                                "bb_odds": bb_ou["over_odds"],
                                "pin_odds": over_p["price_decimal"],
                                "fair_price": over_fair,
                                "ev_pct": round(ev_o, 2),
                            })
                    if under_p.get("price_decimal") and under_p["price_decimal"] > 0:
                        ev_u = (bb_ou["under_odds"] - under_fair) / under_fair * 100
                        if ev_u > 1:
                            entry["over_under"].append({
                                "designation": mlabels["under"],
                                "line": str(bb_ou["line"]),
                                "bb_odds": bb_ou["under_odds"],
                                "pin_odds": under_p["price_decimal"],
                                "fair_price": under_fair,
                                "ev_pct": round(ev_u, 2),
                            })

        # --- 上半场 (HT) 对比：从 DOM odds_ht 读，与 Pinnacle period=1 对比 ---
        bb_ht = bb.get("odds_ht", {})
        if bb_ht and bb_ht.get("ml"):
            ht_labels = {
                "ml": ["上半场主胜", "上半场和局", "上半场客胜"] if sport == "football" else ["上半场主胜", "上半场客胜"],
                "hc_home": "上半场让球主胜", "hc_away": "上半场让球客胜",
                "over": "上半场大球", "under": "上半场小球",
            }
            # HT 独赢
            pin_ht_ml = get_pin_ml_sorted_from_source(pin.get("ht_moneyline", []), sport)
            if pin_ht_ml and len(pin_ht_ml) >= 2:
                n_ht_ml = len(pin_ht_ml)
                bb_ht_ml = bb_ht["ml"]
                if len(bb_ht_ml) >= n_ht_ml:
                    total_implied_ht_ml = sum(1.0 / p for p in pin_ht_ml if p and p > 0)
                    for i in range(n_ht_ml):
                        bb_o = bb_ht_ml[i]
                        pin_o = pin_ht_ml[i]
                        if pin_o and pin_o > 0:
                            fair_price = round(pin_o * total_implied_ht_ml, 4) if total_implied_ht_ml > 0 else round(pin_o, 2)
                            ev = (bb_o - fair_price) / fair_price * 100 if fair_price > 0 else 0
                            if ev > 1:
                                entry["opportunities"].append({
                                    "designation": ht_labels["ml"][i],
                                    "bb_odds": bb_o,
                                    "pin_odds": pin_o,
                                    "fair_price": fair_price,
                                    "ev_pct": round(ev, 2),
                                    "_market": "ht",
                                })

            # HT 让球
            bb_ht_hc = bb_ht.get("handicap")
            if bb_ht_hc:
                bb_hl = bb_ht_hc.get("home_line") or bb_ht_hc.get("away_line")
                home_sp, away_sp = get_pin_spread(pin, target_line=bb_hl, source=pin.get("ht_spread", []))
                if home_sp and away_sp and home_sp.get("price_decimal") and away_sp.get("price_decimal"):
                    pin_home_odds = home_sp["price_decimal"]
                    pin_away_odds = away_sp["price_decimal"]
                    # 校准：HT 让球线必须精确一致
                    pin_hc_line = home_sp.get("points")
                    bb_hc_line_val = bb_ht_hc.get("home_line")
                    cal_ok, _ = _calibrate_market_line(sport, "hc", bb_hc_line_val, pin_hc_line, None, is_ht=True)
                    if cal_ok:
                        total_implied = 1.0 / pin_home_odds + 1.0 / pin_away_odds
                        home_fair = round(pin_home_odds * total_implied, 4)
                        away_fair = round(pin_away_odds * total_implied, 4)
                        ev_h = (bb_ht_hc["home_odds"] - home_fair) / home_fair * 100 if home_fair > 0 else 0
                        ev_a = (bb_ht_hc["away_odds"] - away_fair) / away_fair * 100 if away_fair > 0 else 0
                        if ev_h > 1:
                            entry["handicap"].append({
                                "designation": ht_labels["hc_home"],
                                "line": bb_ht_hc.get("home_line_str", ""),
                                "bb_odds": bb_ht_hc["home_odds"],
                                "pin_odds": pin_home_odds,
                                "fair_price": home_fair,
                                "ev_pct": round(ev_h, 2),
                                "_market": "ht",
                            })
                        if ev_a > 1:
                            entry["handicap"].append({
                                "designation": ht_labels["hc_away"],
                                "line": bb_ht_hc.get("away_line_str", ""),
                                "bb_odds": bb_ht_hc["away_odds"],
                                "pin_odds": pin_away_odds,
                                "fair_price": away_fair,
                                "ev_pct": round(ev_a, 2),
                                "_market": "ht",
                            })

            # HT 大小
            bb_ht_ou = bb_ht.get("total")
            if bb_ht_ou:
                bb_line = bb_ht_ou.get("line")
                over_p, under_p = get_pin_total(pin, target_line=bb_line, source=pin.get("ht_total", []))
                if over_p and under_p:
                    pin_ou_line = over_p.get("points")
                    cal_ok, _ = _calibrate_market_line(sport, "ou", bb_ht_ou["line"], pin_ou_line, None, is_ht=True)
                    if cal_ok:
                        total_implied = 1.0 / over_p["price_decimal"] + 1.0 / under_p["price_decimal"]
                        over_fair = round(over_p["price_decimal"] * total_implied, 4)
                        under_fair = round(under_p["price_decimal"] * total_implied, 4)
                        if over_p.get("price_decimal") and over_p["price_decimal"] > 0:
                            ev_o = (bb_ht_ou["over_odds"] - over_fair) / over_fair * 100
                            if ev_o > 1:
                                entry["over_under"].append({
                                    "designation": ht_labels["over"],
                                    "line": str(bb_ht_ou["line"]),
                                    "bb_odds": bb_ht_ou["over_odds"],
                                    "pin_odds": over_p["price_decimal"],
                                    "fair_price": over_fair,
                                    "ev_pct": round(ev_o, 2),
                                    "_market": "ht",
                                })
                        if under_p.get("price_decimal") and under_p["price_decimal"] > 0:
                            ev_u = (bb_ht_ou["under_odds"] - under_fair) / under_fair * 100
                            if ev_u > 1:
                                entry["over_under"].append({
                                    "designation": ht_labels["under"],
                                    "line": str(bb_ht_ou["line"]),
                                    "bb_odds": bb_ht_ou["under_odds"],
                                    "pin_odds": under_p["price_decimal"],
                                    "fair_price": under_fair,
                                    "ev_pct": round(ev_u, 2),
                                    "_market": "ht",
                                })

        # --- 双重机会 (Double Chance) FT：从 Pinnacle 1X2 推导公平价 ---
        bb_dc = bb.get("odds_dc", [])
        if len(bb_dc) >= 3 and n_ml == 3:
            h, d, a = pin_ml
            if all(x and x > 0 for x in [h, d, a]):
                imp = 1/h + 1/d + 1/a
                p_h, p_d, p_a = (1/h)/imp, (1/d)/imp, (1/a)/imp
                dc_fair = [1/(p_h+p_d), 1/(p_d+p_a), 1/(p_h+p_a)]
                dc_labels = ["双重机会-主/和局", "双重机会-和局/客", "双重机会-主/客"]
                for i in range(3):
                    bb_dc_val = float(bb_dc[i]) if isinstance(bb_dc[i], str) else bb_dc[i]
                    if bb_dc_val and dc_fair[i] > 0:
                        ev = (bb_dc_val - dc_fair[i]) / dc_fair[i] * 100
                        if ev > 1:
                            entry["double_chance"].append({
                                "designation": dc_labels[i],
                                "bb_odds": bb_dc_val,
                                "fair_price": round(dc_fair[i], 4),
                                "ev_pct": round(ev, 2),
                                "_market": "dc",
                            })

        # --- 上半场双重机会 (HT DC)：从 Pinnacle HT 1X2 推导公平价 ---
        if len(bb_dc) >= 6 and n_ml == 3:
            pin_ht_ml = get_pin_ml_sorted_from_source(pin.get("ht_moneyline", []), sport)
            if len(pin_ht_ml) == 3:
                hh, dd, aa = pin_ht_ml
                if all(x and x > 0 for x in [hh, dd, aa]):
                    imp = 1/hh + 1/dd + 1/aa
                    p_h, p_d, p_a = (1/hh)/imp, (1/dd)/imp, (1/aa)/imp
                    dc_fair = [1/(p_h+p_d), 1/(p_d+p_a), 1/(p_h+p_a)]
                    dc_labels = ["上半场双重机会-主/和局", "上半场双重机会-和局/客", "上半场双重机会-主/客"]
                    for i in range(3):
                        bb_dc_val = float(bb_dc[3+i]) if isinstance(bb_dc[3+i], str) else bb_dc[3+i]
                        if bb_dc_val and dc_fair[i] > 0:
                            ev = (bb_dc_val - dc_fair[i]) / dc_fair[i] * 100
                            if ev > 1:
                                entry["double_chance"].append({
                                    "designation": dc_labels[i],
                                    "bb_odds": bb_dc_val,
                                    "fair_price": round(dc_fair[i], 4),
                                    "ev_pct": round(ev, 2),
                                    "_market": "ht_dc",
                                })

        # 同一市场只保留溢价最高的选项（FT + HT + DC + HT_DC 各自保留）
        for mk in ("opportunities", "handicap", "over_under", "double_chance"):
            if entry[mk]:
                ft_entries = [x for x in entry[mk] if x.get("_market") in (None, "", "main")]
                ht_entries = [x for x in entry[mk] if x.get("_market") == "ht"]
                dc_entries = [x for x in entry[mk] if x.get("_market") == "dc"]
                ht_dc_entries = [x for x in entry[mk] if x.get("_market") == "ht_dc"]
                best = []
                if ft_entries:
                    best.append(max(ft_entries, key=lambda x: x["ev_pct"]))
                if ht_entries:
                    best.append(max(ht_entries, key=lambda x: x["ev_pct"]))
                if dc_entries:
                    best.append(max(dc_entries, key=lambda x: x["ev_pct"]))
                if ht_dc_entries:
                    best.append(max(ht_dc_entries, key=lambda x: x["ev_pct"]))
                entry[mk] = best

        if entry["opportunities"] or entry["handicap"] or entry["over_under"] or entry["double_chance"]:
            # 可疑 EV / 低置信度警告
            for mk in ("opportunities", "handicap", "over_under", "double_chance"):
                for o in entry.get(mk, []):
                    w = _warn_suspicious(o["ev_pct"], entry["match_score"], m.get("verified", False))
                    if w:
                        o["_warn"] = w
                        if w not in entry["flags"]:
                            entry["flags"].append(w)
            # 低匹配度 + 不可验证 → 标记
            ms = entry["match_score"]
            if not m.get("verified", False) and ms < 0.85:
                entry["flags"].append(f"球队待确认(匹配度{ms})")
            opportunities.append(entry)

    total_opps_1x2 = sum(len(o["opportunities"]) for o in opportunities)
    total_hc = sum(len(o.get("handicap", [])) for o in opportunities)
    total_ou = sum(len(o.get("over_under", [])) for o in opportunities)
    total_dc = sum(len(o.get("double_chance", [])) for o in opportunities)
    total_all = total_opps_1x2 + total_hc + total_ou + total_dc

    print(f"\n{'='*60}")
    print(f"匹配: {len(matched)} | +EV 独赢: {total_opps_1x2} | 让球: {total_hc} | 大小: {total_ou} | 双重机会: {total_dc} | 总计: {total_all}")
    print(f"{'='*60}")
    # 校准报告
    if cal_blocked_hc or cal_blocked_ou:
        print(f"\n  🔒 校准拦截: 让球{cal_blocked_hc}个 | 大小{cal_blocked_ou}个 (盘口线不匹配)")
    else:
        print("\n  ✅ 校准全部通过 (所有让球/大小盘口线一致)")
    print()
    for entry in opportunities:
        flag_txt = ""
        sport_tag = {"football":"⚽","basketball":"🏀","tennis":"🎾","baseball":"⚾","american_football":"🏈"}.get(entry.get("sport", ""), "")
        if entry.get("flags"):
            flag_txt = " ⚠️ " + ", ".join(entry["flags"])
        print(f"\n  [{entry['league']}]{flag_txt}")
        print(f"  BB: {entry['home_bb']} vs {entry['away_bb']}  [{sport_tag}]")
        print(f"  Pin: {entry['home_pin']} vs {entry['away_pin']}")
        print(f"  score={entry['match_score']} | type={entry['match_type']}")
        for o in entry["opportunities"]:
            print(f"    ✅ +EV {o['ev_pct']}%: {o['designation']} (BB={o['bb_odds']} Pin={o['pin_odds']})")
        for o in entry.get("handicap", []):
            print(f"    ✅ +EV {o['ev_pct']}%: {o['line']} {o['designation']} (BB={o['bb_odds']} Pin={o['pin_odds']})")
        for o in entry.get("over_under", []):
            print(f"    ✅ +EV {o['ev_pct']}%: {o['designation']}({o['line']}) (BB={o['bb_odds']} Pin={o['pin_odds']})")
        for o in entry.get("double_chance", []):
            print(f"    ✅ +EV {o['ev_pct']}%: {o['designation']} (BB={o['bb_odds']} Fair={o['fair_price']})")

    # Save
    timestamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    # Sport → name mapping for output
    sport_name_map = {"football":"足球","basketball":"篮球","tennis":"网球","baseball":"棒球","american_football":"美式足球"}
    output = {
        "timestamp": timestamp,
        "bb_matches_total": len(bb_matches),
        "pinnacle_leagues_found": len(matched_leagues),
        "matched_matches": len(matched),
        "matches_with_ev": len(opportunities),
        "opportunities_1x2": total_opps_1x2,
        "opportunities_handicap": total_hc,
        "opportunities_over_under": total_ou,
        "opportunities_double_chance": total_dc,
        "opportunities_total": total_all,
        "calibration_blocked_hc": cal_blocked_hc,
        "calibration_blocked_ou": cal_blocked_ou,
        "details": opportunities,
    }
    out_path = DATA_DIR / "bb_vs_pinnacle_comparison.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    print(f"\n已保存到 {out_path}")


if __name__ == "__main__":
    main()
