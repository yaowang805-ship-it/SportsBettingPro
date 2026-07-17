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

# Pinnacle 是标准 REST API（无 Cloudflare），直接用 requests 即可。
# 之前用 cloudscraper 会在 Python 3.14 的 chunked transfer encoding bug 下
# 触发 IncompleteRead，被误判为 403。
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://www.pinnacle.com/",
    "Origin": "https://www.pinnacle.com",
})

# SOCKS5 代理支持（Shadowrocket 本地代理，用于绕过 Cloudflare）
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
    "欧足联欧洲协会联赛": "football",
    "澳大利亚杯": "football",
    "厄瓜多尔甲级联赛": "football",
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
    "tennis":     {"ml": ["主胜","客胜"], "hc_home":"让盘主胜", "hc_away":"让盘客胜", "over":"大分", "under":"小分"},
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
        if sport == "soccer":
            return "football"
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
    "厄瓜多尔甲级联赛": "Ecuador - Serie A",
    "哈萨克斯坦超级联赛": "Kazakhstan - Premier League",
    "巴拉圭乙级联赛": "Paraguay - Division Intermedia",
    "俄罗斯甲级联赛": "Russia - First League",
    "澳门甲级联赛": "Macau - Elite League",
    "白俄罗斯超级联赛": "Belarus - Premier League",
    # 冰岛甲级联赛在 Pinnacle 无对应联赛，删除防止 false positive
    # "冰岛甲级联赛": "Iceland - Premier League",  # ❌ 这是冰岛超级联赛，完全不同的联赛
    "爱沙尼亚甲级联赛": "Estonia - Meistriliiga",
    # 早盘联赛映射
    "欧洲冠军联赛-资格赛": "UEFA - Champions League Qualifiers",
    "英格兰超级联赛": "England - Premier League",
    "英格兰冠军联赛": "England - Championship",
    "英格兰甲级联赛": "England - League One",
    "英格兰乙级联赛": "England - League Two",
    "英格兰联赛杯": "England - EFL Cup",
    "英格兰社区盾": "England - Community Shield",
    "英格兰足球议会全国联赛": "England - National League",
    "西班牙甲级联赛": "Spain - La Liga",
    "德国甲级联赛": "Germany - Bundesliga",
    "德国乙级联赛": "Germany - Bundesliga 2",
    "德国超级杯": "Germany - Super Cup",
    "意大利甲级联赛": "Italy - Serie A",
    "法国甲级联赛": "France - Ligue 1",
    "法国超级杯": "France - Super Cup",
    "荷兰甲级联赛": "Netherlands - Eredivisie",
    "巴西甲级联赛": "Brazil - Serie A",
    "巴西乙级联赛": "Brazil - Serie B",
    "巴西杯": "Brazil - Copa do Brasil",
    "墨西哥超级联赛": "Mexico - Liga MX",
    "美国职业大联盟": "USA - Major League Soccer",
    "阿根廷甲级联赛": "Argentina - Liga Pro",
    "阿根廷全国联赛": "Argentina - Primera B Nacional",
    "阿根廷杯": "Argentina - Cup",
    "比利时甲级联赛": "Belgium - Pro League",
    "比利时超级杯": "Belgium - Super Cup",
    "瑞士超级联赛": "Switzerland - Super League",
    "挪威超级联赛": "Norway - Eliteserien",
    "挪威甲级联赛": "Norway - OBOS-ligaen",
    "丹麦超级联赛": "Denmark - Superliga",
    "丹麦甲级联赛": "Denmark - 1st Division",
    "瑞典超甲级联赛": "Sweden - Superettan",
    "波兰甲级联赛": "Poland - Ekstraklasa",
    "波兰超级杯": "Poland - Super Cup",
    "捷克甲级联赛": "Czech Republic - First League",
    "奥地利甲级联赛": "Austria - Bundesliga",
    "苏格兰超级联赛": "Scotland - Premiership",
    "乌克兰超级联赛": "Ukraine - Premier League",
    "俄罗斯超级联赛": "Russia - Premier League",
    "俄罗斯杯": "Russia - Cup",
    "俄罗斯超级杯": "Russia - Super Cup",
    "罗马尼亚甲级联赛": "Romania - Liga 1",
    "保加利亚甲级联赛": "Bulgaria - First League",
    "斯洛文尼亚甲级联赛": "Slovenia - Prva Liga",
    "塞尔维亚超级联赛": "Serbia - Super Liga",
    "匈牙利甲级联赛": "Hungary - OTP Bank Liga",
    "希腊超级联赛": "Greece - Super League",
    "土耳其超级联赛": "Turkey - Super League",
    "秘鲁甲级联赛": "Peru - Liga 1",
    "智利甲级联赛": "Chile - Primera Division",
    "哥伦比亚甲级联赛": "Colombia - Liga BetPlay",
    "韩国K1联赛": "Korea Republic - K League",
    "韩国乙级联赛": "Korea Republic - K League 2",
    "韩国足协杯": "South Korea - FA Cup",
    "美国冠军联赛": "USA - USL Championship",
    "美国国家女子联赛": "USA - National Womens Soccer League",
    "加拿大超级联赛": "Canada - Premier League",
    "欧足联欧洲联赛-资格赛": "UEFA - Europa League Qualifiers",
    "欧足联欧洲会议联赛-资格赛": "UEFA - Conference League Qualifiers",
    "欧足联欧洲协会联赛-资格赛": "UEFA - Conference League Qualifiers",
    "2026世界杯 (在加拿大墨西哥&美国)": "FIFA - World Cup",
    # 南美足球
    "南美俱乐部杯": "CONMEBOL - Copa Sudamericana",
    "南美解放者杯": "CONMEBOL - Copa Libertadores",
    # 冰岛
    "冰岛超级联赛": "Iceland - Premier League",
    # 美国/加拿大
    "加拿大超级联赛": "Canada - Premier League",
    "NFL 美国职业美式足球": "NFL",
    "美国职业美式足球": "NFL",
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
    "新西兰 - NBL": "New Zealand - NBL",
    "新西兰全国篮球联赛": "New Zealand - NBL",
    "澳大利亚杯": "Australia - Cup",
    "澳大利亚北部篮球联赛": "Australia - NBL1",
    "澳大利亚南部篮球联赛": "Australia - NBL1",
    "澳大利亚东部篮球联赛": "Australia - NBL1",
    "澳大利亚西部篮球联赛": "Australia - NBL1",
    "澳大利亚中部篮球联赛": "Australia - NBL1",
    "澳大利亚北部女子篮球联赛": "Australia - NBL1 Women",
    "澳大利亚南部女子篮球联赛": "Australia - NBL1 Women",
    "澳大利亚东部女子篮球联赛": "Australia - NBL1 Women",
    "澳大利亚西部女子篮球联赛": "Australia - NBL1 Women",
    "智利全国篮球联赛": "Chile - LNB",
    "波多黎各国家篮球联赛": "Puerto Rico - Superior Nacional",
    "黎巴嫩篮球甲级联赛": "Lebanon - Lebanese Basketball League",
    "巴西LBF女子篮球联赛": "Brazil - LBF Women",
    "卢旺达全国篮球联赛": "Rwanda - National League",
    "乌干达篮球联赛": "Uganda NBL",
    "乌干达女子篮球联赛": "Uganda NBL Women",
    "乌拉圭女子篮球联赛": "Uruguay - Liga Femenina",
    "乌拉圭METRO联赛": "Uruguay - Metro League",
    "危地马拉大都会篮球联赛": "Guatemala - LMM",
    "多米尼加共和国LNB": "Dominican Republic - LNB",
    "越南职业篮球联赛": "Vietnam - VBA",
    "马里篮球甲级联赛": "Mali - Premere Division",
    # Baseball
    "MLB": "MLB",
    "美国职业棒球大联盟": "MLB",
    "日本职业棒球": "Nippon Professional Baseball",
    "日本职业包装": "Nippon Professional Baseball",  # SPA渲染错误（棒球→包装）
    "韩国职业棒球": "Korea Professional Baseball",
    "韩国棒球": "Korea Professional Baseball",
    "中华职业棒球大联盟": "Chinese Taipei - Professional League",
    "墨西哥棒球联盟": "Mexican League",
    # Tennis
    "ATP - 博斯塔德公开赛": "ATP Bastad",
    "ATP - 大满贯温布尔登网球公开赛": "ATP Wimbledon",
    "ATP - 格施塔德公开赛": "ATP Gstaad",
    "ATP - 乌马格公开赛": "ATP Umag",
    "ATP挑战赛 - 格兰比公开赛": "ATP Challenger Granby",
    "ATP挑战赛 - 林肯公开赛": "ATP Challenger Lincoln",
    "ATP挑战赛 - 波索布兰科公开赛": "ATP Challenger Pozoblanco",
    "ATP 挑战者 - 波索布兰科公开双打": "ATP Challenger Pozoblanco",
    "ATP挑战赛 - 科尔德农斯公开赛": "ATP Challenger Cordenons",
    "ATP挑战赛 - 本斯霍滕公开赛": "ATP Challenger Bunschoten",
    "ATP挑战赛 - 科尔代农斯公开赛": "ATP Challenger Cordenons",
    "WTA - 罗马公开赛": "WTA 125K Rome",
    "WTA - 雅西公开赛": "WTA Iasi",
    "WTA - 基茨比厄尔公开赛": "WTA 125K Kitzbuhel",
    "WTA - 伊斯坦布尔 2 公开赛": "WTA 125K Istanbul",
    "WTA - 伊斯坦堡 2 公开赛": "WTA 125K Istanbul",
    "WTA - 雅典公开赛": "WTA Athens",
    "WTA - 孔特雷克塞维尔公开赛": "WTA 125K Contrexeville - Final",
    "WTA - 孔特雷克塞维尔公开赛 - 双打": "WTA 125K Contrexeville - Doubles",
    "WTA - 纽波特公开赛": "WTA 125K Newport",
    # 戴维斯杯
    "戴维斯杯": "Davis Cup",
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
    # 球会友谊赛 — 更多球队
    "华沙莱吉亚": "Legia Warsaw",
    "拉多米亚克": "Radomiak Radom",
    "韦恩威斯巴登": "Wehen Wiesbaden",
    "冈山绿雉": "Fagiano Okayama",
    "哥罗纳": "Korona Kielce",
    "斯塔泽舒夫": "Stal Stalowa Wola",
    "兰德斯": "Randers",
    "诺茨郡": "Notts County",
    "葡萄牙体育": "Sporting CP",
    "凯尔特人": "Celtic",
    "拜斯迪卡": "Dukla Banska Bystrica",
    "斯拉文贝鲁波": "Slaven Belupo",
    "广岛三箭": "Sanfrecce Hiroshima",
    "特拉维夫夏普尔": "Hapoel Tel Aviv",
    # 韩国K1联赛
    "仁川联": "Incheon United",
    "全北现代": "Jeonbuk Motors",
    "济州联队": "Jeju SK",
    "浦项制铁": "Pohang Steelers",
    "蔚山现代": "Ulsan HD",
    "大田韩亚市民": "Daejeon Citizen",
    "江原FC": "Gangwon FC",
    "金泉尚武": "Gimcheon Sangmu",
    "安养FC": "Anyang",
    "光州FC": "Gwangju FC",
    "富川FC 1995": "Bucheon FC 1995",
    "首尔FC": "FC Seoul",
    "大邱FC": "Daegu FC",
    "水原FC": "Suwon FC",
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
    "东北乐天金鹰": "Tohoku Rakuten Golden Eagles",  # BB界面简体
    "中日龙": "Chunichi Dragons",
    "名古屋中日龙": "Chunichi Dragons",  # BB含城市名
    "北海道日本火腿斗士": "Hokkaido Nippon-Ham Fighters",
    "埼玉西武狮": "Saitama Seibu Lions",
    "广岛东洋鲤鱼": "Hiroshima Toyo Carp",
    "横滨海湾之星": "Yokohama Bay Stars",
    "横滨湾星": "Yokohama Bay Stars",  # BB简称
    "欧力士野牛": "Orix Buffaloes",
    "福冈软银鹰": "Fukuoka SoftBank Hawks",
    "读卖巨人": "Yomiuri Giants",
    "东京读卖巨人": "Yomiuri Giants",  # BB含城市名
    "阪神虎": "Hanshin Tigers",
    "阪神老虎": "Hanshin Tigers",  # BB界面变体
    "东京益力多燕子": "Tokyo Yakult Swallows",
    "东京养乐多燕子": "Tokyo Yakult Swallows",  # BB译名变体
    "乐天桃猿": "Rakuten Monkeys",
    "味全龙": "Wei Chuan Dragons",
    "统一7-ELEVen狮子": "Uni-President 7-ELEVEN Lions",
    "中信兄弟": "CTBC Brothers",
    "富邦悍将": "Fubon Guardians",
    # WNBA
    "康涅狄格阳光 (女)": "Connecticut Sun",
    "波特兰火焰 (女)": "Portland Fire",
    "多伦多节奏 (女)": "Toronto Tempo",
    "华盛顿神秘人 (女)": "Washington Mystics",
    # FIBA U20 country names
    "爱沙尼亚 U20": "Estonia",
    "卢森堡 U20": "Luxembourg",
    "乌克兰 U20": "Ukraine",
    "瑞士 U20": "Switzerland",
    "北马其顿 U20": "North Macedonia",
    "瑞典 U20": "Sweden",
    "波黑 U20": "Bosnia And Herzegovina",
    "塞浦路斯 U20": "Cyprus",
    "保加利亚 U20": "Bulgaria",
    "丹麦 U20": "Denmark",
    "葡萄牙 U20": "Portugal",
    "冰岛 U20": "Iceland",
    "荷兰 U20": "Netherlands",
    "匈牙利 U20": "Hungary",
    "爱尔兰 U20": "Ireland",
    "阿塞拜疆 U20": "Azerbaijan",
    "科索沃 U20": "Kosovo",
    "亚美尼亚 U20": "Armenia",

    # Tennis — ATP / WTA / Challenger 球员
    "雨果.德利恩": "Hugo Dellien",
    "伊奥尼斯.西拉斯": "Ioannis Xilas",
    "安娜.邦达尔": "Anna Bondar",
    "塔马拉.齐丹谢克": "Tamara Zidansek",
    "莉莉.塔格": "Lilli Tagger",
    "萨拉.贝莱克": "Sara Bejlek",
    "斯特凡诺.特拉瓦利亚": "Stefano Travaglia",
    "马里亚诺.纳沃内": "Mariano Navone",
    "斯特凡诺.纳波里塔诺": "Stefano Napolitano",
    "亚历克斯.巴雷纳": "Alex Barrena",
    "胡安.卡洛斯.普拉多.安杰洛": "Juan Carlos Prado Angelo",
    "达米尔.德祖赫": "Damir Dzumhur",
    "哈丽特.达特": "Harriet Dart",
    "玛莉亚.沙卡里": "Maria Sakkari",
    "加布里埃拉.克努森": "Gabriela Knutson",
    "兰拉娜.塔拉鲁迪": "Lanlana Tararudee",
    "维罗妮卡.埃尔亚韦茨": "Veronika Erjavec",
    "莫纳.巴特尔": "Mona Barthel",
    "玛丽亚.卢尔德.卡尔": "Maria Lourdes Carle",
    "巴博拉.帕利科娃": "Barbora Palicova",
    "巴勃罗.卡雷尼奥.布斯塔": "Pablo Carreno Busta",
    "卡米洛.乌戈.卡拉贝利": "Camilo Ugo Carabelli",
    "弗拉维欧.科博利": "Flavio Cobolli",
    "罗曼.安德烈斯.布尔鲁查加": "Roman Andres Burruchaga",
    "瓦伦丁.瓦切洛特": "Valentin Vacherot",
    "费德里科.邦迪奥利": "Federico Bondioli",
    "吉多.伊万.胡斯托": "Guido Ivan Justo",
    "费德里科.奥古斯丁.戈麦斯": "Federico Agustin Gomez",
    "马特奥.阿纳尔迪": "Matteo Arnaldi",
    "努诺.博尔赫斯": "Nuno Borges",
    "格里戈尔.季米特洛夫": "Grigor Dimitrov",

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
    # Baseball — KBO (韩国棒球)
    "三星狮": "Samsung Lions",
    "乐天巨人": "Lotte Giants",
    "韩华鹰": "Hanwha Eagles",
    "培证英雄": "Kiwoom Heroes",
    "起亚老虎": "Kia Tigers",
    "斗山熊": "Doosan Bears",
    "LG双子": "LG Twins",
    "NC恐龙": "NC Dinos",
    "SSG登陆者": "SSG Landers",
    "KT巫师": "KT Wiz",
    # Basketball — NBA
    "亚特兰大老鹰": "Atlanta Hawks",
    "波士顿凯尔特人": "Boston Celtics",
    "布鲁克林篮网": "Brooklyn Nets",
    "夏洛特黄蜂": "Charlotte Hornets",
    "芝加哥公牛": "Chicago Bulls",
    "克利夫兰骑士": "Cleveland Cavaliers",
    "达拉斯独行侠": "Dallas Mavericks",
    "丹佛掘金": "Denver Nuggets",
    "底特律活塞": "Detroit Pistons",
    "金州勇士": "Golden State Warriors",
    "休士顿火箭": "Houston Rockets",
    "印第安纳步行者": "Indiana Pacers",
    "洛杉矶快船": "Los Angeles Clippers",
    "洛杉矶湖人": "Los Angeles Lakers",
    "孟菲斯灰熊": "Memphis Grizzlies",
    "迈阿密热火": "Miami Heat",
    "密尔沃基雄鹿": "Milwaukee Bucks",
    "明尼苏达森林狼": "Minnesota Timberwolves",
    "新奥尔良鹈鹕队": "New Orleans Pelicans",
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
    # WNBA
    "芝加哥天空 (女)": "Chicago Sky",
    "康涅狄格阳光 (女)": "Connecticut Sun",
    "达拉斯飞翼 (女)": "Dallas Wings",
    "印第安纳狂热 (女)": "Indiana Fever",
    "拉斯维加斯王牌 (女)": "Las Vegas Aces",
    "洛杉矶火花 (女)": "Los Angeles Sparks",
    "明尼苏达山猫 (女)": "Minnesota Lynx",
    "纽约自由 (女)": "New York Liberty",
    "凤凰城水星 (女)": "Phoenix Mercury",
    "西雅图风暴 (女)": "Seattle Storm",
    "华盛顿神秘人 (女)": "Washington Mystics",
    # WNBA — 2026 expansion
    "波特兰火焰 (女)": "Portland Fire",
    "多伦多节奏 (女)": "Toronto Tempo",
    "金州瓦尔基里 (女)": "Golden State Valkyries",
    # FIBA U20 — 欧洲国家
    "阿尔巴尼亚 U20": "Albania",
    "比利时 U20": "Belgium",
    "波黑 U20": "Bosnia And Herzegovina",
    "保加利亚 U20": "Bulgaria",
    "克罗地亚 U20": "Croatia",
    "塞浦路斯 U20": "Cyprus",
    "捷克 U20": "Czech Republic",
    "丹麦 U20": "Denmark",
    "英格兰 U20": "England",
    "爱沙尼亚 U20": "Estonia",
    "芬兰 U20": "Finland",
    "法国 U20": "France",
    "格鲁吉亚 U20": "Georgia",
    "德国 U20": "Germany",
    "希腊 U20": "Greece",
    "匈牙利 U20": "Hungary",
    "冰岛 U20": "Iceland",
    "爱尔兰 U20": "Ireland",
    "以色列 U20": "Israel",
    "意大利 U20": "Italy",
    "科索沃 U20": "Kosovo",
    "拉脱维亚 U20": "Latvia",
    "立陶宛 U20": "Lithuania",
    "卢森堡 U20": "Luxembourg",
    "荷兰 U20": "Netherlands",
    "北马其顿 U20": "North Macedonia",
    "波兰 U20": "Poland",
    "葡萄牙 U20": "Portugal",
    "罗马尼亚 U20": "Romania",
    "塞尔维亚 U20": "Serbia",
    "斯洛伐克 U20": "Slovakia",
    "斯洛文尼亚 U20": "Slovenia",
    "西班牙 U20": "Spain",
    "瑞典 U20": "Sweden",
    "瑞士 U20": "Switzerland",
    "土耳其 U20": "Turkey",
    "乌克兰 U20": "Ukraine",
    # FIBA U20 non-European
    "加拿大 U20": "Canada",
    "美国 U20": "USA",
    "澳大利亚 U20": "Australia",
    "新西兰 U20": "New Zealand",
    # Basketball — 菲律宾PBA
    "汇众光纤": "Converge FiberXers",
    "泰丰吉普": "Terrafirma Dyip",
    "马拉古闪电": "Meralco Bolts",
    "凤凰燃料大师": "Phoenix Fuel Masters",
    # Basketball — 新西兰NBL
    "塔拉纳基": "Taranaki Mountainairs",
    "尼尔森巨人": "Nelson Giants",
    "奥克兰大蜥蜴": "Auckland Tuatara",
    "奥塔哥掘金": "Otago Nuggets",
    "霍克湾雄鹰": "Hawke's Bay Hawks",
    # Basketball — 澳大利亚NBL1 (北部+南部)
    "凯恩斯马林鱼": "Cairns Marlins",
    "罗克汉普顿火箭": "Rockhampton Rockets",
    "凯恩斯海豚": "Cairns Dolphins",
    "罗克汉普顿旋风": "Rockhampton Cyclones",
    "埃尔特姆野猫": "Eltham Wildcats",
    "基尔塞斯眼镜蛇": "Kilsyth Cobras",
    "桑德林剑齿虎": "Sandringham Sabres",
    "北方力量": "Northern Tasmania",
    "本迪高勇士": "Bendigo Braves",
    "凯勒闪电": "Keilor Thunder",
    "诺克斯袭击者": "Knox Raiders",
    "电光": "Hobart Chargers",
    "凯恩斯海豚 (女)": "Cairns Dolphins",
    "罗克汉普顿旋风 (女)": "Rockhampton Cyclones",
    "埃尔特姆野猫 (女)": "Eltham Wildcats",
    "基尔塞斯眼镜蛇 (女)": "Kilsyth Cobras",
    "桑德林剑齿虎 (女)": "Sandringham Sabres",
    "北方力量篮球队 (女)": "Northern Tasmania",
    "本迪高勇士 (女)": "Bendigo Braves",
    "凯勒闪电 (女)": "Keilor Thunder",
    "诺克斯袭击者 (女)": "Knox Raiders",
    "电光 (女)": "Hobart Chargers",
    # Basketball — 智利
    "塔尔卡西班牙人": "CD Espanol De Talca",
    "普恩特阿尔托": "CD Puente Alto",
    "瓦尔迪维亚": "CD Valdivia",
    "奥索尔诺西班牙人": "CD Espanol Osorno",
    "奎利普尔": "CD Colegio Los Leones",
    "科洛科洛": "CSD Colo Colo",
    # Basketball — 波多黎各
    "卡罗莱纳巨人": "Gigantes de Carolina",
    "巴亚蒙牛仔": "Vaqueros de Bayamon",
    "桑特罗斯德": "Aguada Santeros",
    "阿雷西博上尉": "Capitanes de Arecibo",
    "庞塞雄狮": "Leones de Ponce",
    "圣日尔曼": "Atleticos de San German",
    # Basketball — 卢旺达
    "EAU卢旺达": "East African University Rwanda",
    "老虎BBC": "Tigers",
    "卢旺达能源集团": "Rwanda Energy Group",
    "卢旺达爱国军": "APR BBC",
    # Basketball — 黎巴嫩
    "萨格瑟": "Sagesse",
    "朱尼耶中央俱乐部": "Central Club",
    # Basketball — 多米尼加
    "旧金山印第安人": "Indios de San Francisco de Macoris",
    "圣地亚哥地铁": "Metros de Santiago",
    # Basketball — 香港
    "康仁福建": "Wellman Fukien",
    "香港东方": "Hong Kong Eastern Long Lions",
    # Basketball — 乌拉圭METRO
    "拉雷博尔赫斯": "IA Larre Borges",
    "拉戈马尔": "Lagomar CC",
    "阿尔巴特罗斯": "Albatros",
    "蒙得维的亚BBC": "Montevideo B.B.C",
    # Basketball — 乌拉圭女子
    "拉戈马尔 (女)": "Lagomar",
    "耶鲁 (女)": "Yale",
    "阿瓜达竞技 (女)": "Aguada",
    "防卫者体育 (女)": "Defensor Sporting",
    # Basketball — 危地马拉
    "哈尔科内斯": "Halcones",
    "郊狼": "Coyotes",
    # Basketball — 越南
    "芽庄海豚": "Nha Trang Dolphins",
    "河内水牛": "Hanoi Buffaloes",
    # Basketball — Uganda
    "KCCA豹": "KCCA Panthers",
    "城市油工": "City Oilers",
    # World Cup 足球
    "英格兰": "England",
    "阿根廷": "Argentina",
    "巴西": "Brazil",
    "法国": "France",
    "德国": "Germany",
    "葡萄牙": "Portugal",
    "西班牙": "Spain",
    "荷兰": "Netherlands",
    "比利时": "Belgium",
    "克罗地亚": "Croatia",
    # Baseball — MLB 补充
    "亚利桑那响尾蛇": "Arizona Diamondbacks",
    "亚特兰大勇士": "Atlanta Braves",
    "巴尔的摩金莺": "Baltimore Orioles",
    "波士顿红袜": "Boston Red Sox",
    "芝加哥小熊": "Chicago Cubs",
    "芝加哥白袜": "Chicago White Sox",
    "辛辛那提红人": "Cincinnati Reds",
    "克利夫兰守护者": "Cleveland Guardians",
    "科罗拉多洛基山": "Colorado Rockies",
    "底特律老虎": "Detroit Tigers",
    "休士顿太空人": "Houston Astros",
    "堪萨斯城皇家": "Kansas City Royals",
    "洛杉矶天使": "Los Angeles Angels",
    "洛杉矶道奇": "Los Angeles Dodgers",
    "迈阿密马林鱼": "Miami Marlins",
    "密尔沃基酿酒人": "Milwaukee Brewers",
    "明尼苏达双城": "Minnesota Twins",
    "纽约洋基": "New York Yankees",
    "纽约大都会": "New York Mets",
    "奥克兰运动家": "Oakland Athletics",
    "费城费城人": "Philadelphia Phillies",
    "匹兹堡海盗": "Pittsburgh Pirates",
    "圣迭戈教士": "San Diego Padres",
    "旧金山巨人": "San Francisco Giants",
    "西雅图水手": "Seattle Mariners",
    "圣路易斯红雀": "St. Louis Cardinals",
    "坦帕湾光芒": "Tampa Bay Rays",
    "德州游骑兵": "Texas Rangers",
    "多伦多蓝鸟": "Toronto Blue Jays",
    "华盛顿国民": "Washington Nationals",
    # Baseball — 墨西哥联盟(部分)
    "金塔纳罗奥老虎": "Tigres de Quintana Roo",
    "墨西哥红魔鬼": "Diablos Rojos del Mexico",
    "克雷塔罗阴谋者": "Conspiradores de Queretaro",
    "莱昂刺客": "Bravos de Leon",
    "尤卡坦狮王": "Leones de Yucatan",
    "坎佩切海盗": "Piratas de Campeche",
    "塔巴斯科奥尔梅克": "Olmecas de Tabasco",
    "普埃布拉鹦鹉": "Pericos de Puebla",
    "瓦哈卡战士": "Guerreros de Oaxaca",
    "提华纳公牛": "Toros de Tijuana",
    "拉雷多双城": "Tecolotes de Los Dos Laredos",
    "里耶罗斯": "Rieleros de Aguascalientes",
    "萨拉佩": "Charros de Jalisco",
    "韦拉克鲁斯老鹰": "El Aguila de Veracruz",
    "蒙特雷苏丹": "Sultanes de Monterrey",
    "杜兰戈将军": "Generales de Durango",
    # 英格兰冠军联赛 / 英格兰联赛杯
    "西汉姆联": "West Ham United",
    "朴茨茅斯": "Portsmouth",
    "卡迪夫城": "Cardiff City",
    "斯文登": "Swindon Town",
    "诺维奇": "Norwich City",
    "西布罗姆维奇": "West Bromwich Albion",
    "女王公园巡游者": "Queens Park Rangers",
    "布里斯托尔城": "Bristol City",
    "米尔沃尔": "Millwall",
    "伯明翰城": "Birmingham City",
    "索利赫尔": "Solihull Moors",
    "米尔顿凯恩斯": "Milton Keynes Dons",
    "牛津联队": "Oxford United",
    "博尔顿": "Bolton Wanderers",
    "布莱克本流浪者": "Blackburn Rovers",
    "普雷斯顿": "Preston North End",
    "斯托克城": "Stoke City",
    "谢周三": "Sheffield Wednesday",
    "卢顿": "Luton Town",
    "伯恩利": "Burnley",
    "米德尔斯堡": "Middlesbrough",
    "德比郡": "Derby County",
    "斯旺西": "Swansea City",
    "沃特福德": "Watford",
    "雷丁": "Reading",
    "哈德斯菲尔德": "Huddersfield Town",
    "罗瑟汉姆": "Rotherham United",
    "韦康比流浪者": "Wycombe Wanderers",
    "彼得堡联": "Peterborough United",
    "巴恩斯利": "Barnsley",
    "什鲁斯伯里": "Shrewsbury Town",
    "伯顿": "Burton Albion",
    "埃克塞特城": "Exeter City",
    "北安普顿": "Northampton Town",
    "奥尔德肖特": "Aldershot Town",
    "查尔顿": "Charlton Athletic",
    "维冈竞技": "Wigan Athletic",
    "克劳利镇": "Crawley Town",
    # 丹麦超 / 挪威超
    "奥胡斯": "Aarhus",
    "霍森斯": "Horsens",
    "锡尔克堡": "Silkeborg",
    "桑德菲杰": "Sandefjord",
    "博多格林特": "Bodo Glimt",
    "费德列斯达": "Fredrikstad",
    "代格福什": "Degerfors",
    # 巴西甲/乙
    "福塔雷萨": "Fortaleza",
    "诺瓦里桑蒂诺": "Gremio Novorizontino",
    # 美国
    "拉斯维加斯光": "Las Vegas Lights",
    # 澳大利亚杯
    "布伦瑞克尤文图斯": "Brunswick Juventus FC",
    # 美国职业大联盟 (MLS)
    "休斯敦迪纳摩": "Houston Dynamo",
    "华盛顿联": "D.C. United",
    "洛杉矶银河": "Los Angeles Galaxy",
    "洛杉矶FC": "Los Angeles FC",
    "纳什维尔": "Nashville SC",
    "亚特兰大联": "Atlanta United",
    "圣何塞地震": "San Jose Earthquakes",
    "奥兰多城": "Orlando City",
    # 日本棒球（BB界面变体）
    "福冈软件银行鹰": "Fukuoka SoftBank Hawks",
    "统一狮": "Uni-President 7-ELEVEN Lions",
    # Tennis players — 中文音译 → Pinnacle 英文名
    "维多利亚.莫尔瓦约娃 (斯洛伐克)": "Viktoria Morvayova",
    "萨拉.贝莱克 (捷克)": "Sara Bejlek",
}


def api_get(path, retry=True):
    """调用 Pinnacle API，带详细错误诊断。"""
    url = f"{API_BASE}{path}"
    for attempt in range(MAX_RETRIES if retry else 1):
        try:
            # 先试 SOCKS5 代理（Shadowrocket），失败回退直连
            socks_err = None
            try:
                resp = SESSION.get(url, timeout=30, proxies={"https": PROXY, "http": PROXY})
            except Exception as e:
                socks_err = e
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
                # 最后一次失败：打印诊断
                _diagnose_pinnacle_error(url, resp.status_code, socks_err)
                return None
            return resp.json()
        except requests.exceptions.SSLError as e:
            print(f"  ❌ SSL 握手失败 ({type(e).__name__})")
            print(f"    原因: {e}")
            print(f"    建议: 检查系统时间是否正确，或更新 SSL 证书")
            return None
        except requests.exceptions.ConnectionError as e:
            print(f"  ❌ 连接失败: {e}")
            print(f"    建议: 检查网络连接 / Shadowrocket 是否开启")
            return None
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                print(f"  ⏳ timeout, retry in {RETRY_DELAY:.0f}s...")
                time.sleep(RETRY_DELAY)
                continue
            print(f"  ❌ Pinnacle API 超时（多次重试后）")
            print(f"    建议: 检查网络延迟 / 切换 VPN 节点")
            return None
        except requests.exceptions.ChunkedEncodingError as e:
            # Python 3.14 http.client bug: chunked transfer 解析失败
            print(f"  ❌ ChunkedEncodingError: {e}")
            print(f"    原因: Python 3.14 http.client chunked transfer bug")
            print(f"    自动重试中...")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
                continue
            return None
        except Exception as e:
            print(f"  ❌ Pinnacle API 请求异常 ({type(e).__name__}): {e}")
            return None
    return None


def _diagnose_pinnacle_error(url, status_code, socks_error=None):
    """Pinnacle API 非 200 响应诊断。"""
    print(f"\n  ❌ Pinnacle API 返回 {status_code}")
    if status_code == 403:
        print(f"    原因: 403 Forbidden — 非真实封锁，通常是以下情况之一：")
        print(f"      1. Python 3.14 http.client chunked transfer bug 误报")
        print(f"      2. SOCKS5 代理（localhost:1082）无此 API 访问权限")
        print(f"    解决: 已改用 requests.Session()（去掉 cloudscraper），应已修复")
        print(f"         如仍出现，尝试关闭 Shadowrocket 后重试")
    elif status_code == 401:
        print(f"    原因: 401 Unauthorized — 请求缺少/无效认证")
        print(f"    解决: Pinnacle API 是公开的，不需要认证。可能是代理问题")
        print(f"         尝试关闭 Shadowrocket 后重试")
    elif status_code == 429:
        print(f"    原因: 429 Rate Limited — 请求过快")
        print(f"    解决: 等待 1 分钟后重试")
    elif status_code == 503:
        print(f"    原因: 503 Service Unavailable — Pinnacle 服务暂时不可用")
        print(f"    解决: 等待几分钟后重试")
    else:
        print(f"    原因: 未知")
        if socks_error:
            print(f"    SOCKS5 代理错误: {socks_error}")


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

    Primary source: structured odds_ft.ml from DOM extractor (reliable).
    Fallback: positional odds_values (backward compat with text extractor).

    Returns (odds_list, is_valid).
    """
    n = 3 if sport not in TWO_WAY_SPORTS else 2

    # Primary: structured odds_ft.ml from DOM extractor
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        if "ml" in odds_ft:
            ft_ml = odds_ft["ml"]
            if isinstance(ft_ml, list) and len(ft_ml) >= n:
                bb_1x2 = [v for v in ft_ml if 1.01 <= v <= 51.0]
                if len(bb_1x2) >= n:
                    return bb_1x2, True
            # DOM says no ML or wrong count → don't fall through
            return [], False
        else:
            # odds_ft exists (even if empty) but no "ml" key → DOM merge didn't find
            # this match. Positional odds_values can misread HC/OU odds as ML,
            # so bail out rather than returning wrong data.
            return [], False

    # Not a dict — legacy data, try positional fallback
    odds = bb_match.get("odds_values", [])
    full_text = bb_match.get("full_text", "")

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

    Primary source: structured odds_ft.handicap from DOM extractor.
    Fallback: positional reading from odds_values + full_text.

    Uses 主/客 labels in full_text to correctly assign home/away lines.
    """
    # Primary: structured odds_ft.handicap from DOM extractor
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        ft_hc = odds_ft.get("handicap")
        if isinstance(ft_hc, dict) and ft_hc.get("home_odds") and ft_hc.get("away_odds"):
            home_line = ft_hc.get("home_line")
            away_line = ft_hc.get("away_line")
            if home_line is not None or away_line is not None:
                return {
                    "home_odds": ft_hc["home_odds"],
                    "away_odds": ft_hc["away_odds"],
                    "home_line": home_line,
                    "away_line": away_line,
                    "home_line_str": ft_hc.get("home_line_str", ""),
                    "away_line_str": ft_hc.get("away_line_str", ""),
                }
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

    Primary source: structured odds_ft.total from DOM extractor.
    Fallback: positional reading from odds_values + full_text.

    注意：网球可能有多个让盘口线(alternate handicaps)占用 odds[2:N-2]，
    所以 O/U 不能固定在 odds[4:6]，必须用最后2个赔率。
    """
    # Primary: structured odds_ft.total from DOM extractor
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        ft_ou = odds_ft.get("total")
        if isinstance(ft_ou, dict) and ft_ou.get("over_odds") and ft_ou.get("under_odds"):
            line = ft_ou.get("line")
            if line is not None:
                return {
                    "over_odds": ft_ou["over_odds"],
                    "under_odds": ft_ou["under_odds"],
                    "line": line,
                }
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
    Returns (home_p, away_p, is_alternate) — is_alternate=True 表示用了备用盘口线而非主线
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
        return None, None, False
    if target_line is None:
        return candidates[0][0], candidates[0][1], False

    # 找线值最接近的候选项，但要求偏差 ≤ 0.5
    # 铁律：BB 有什么线就比什么线，线不对就不比
    best = candidates[0]
    best_diff = abs(target_line - candidates[0][0].get("points", 0))
    for home_p, away_p in candidates[1:]:
        diff = abs(target_line - home_p.get("points", 0))
        if diff < best_diff:
            best_diff = diff
            best = (home_p, away_p)

    # 偏差超过 0.5 就认为线不匹配，丢弃
    if best_diff > 0.5:
        return None, None, False

    is_alternate = best is not candidates[0]
    return best[0], best[1], is_alternate


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
    if target_line is None:
        return candidates[0]

    best = candidates[0]
    best_diff = abs(target_line - candidates[0][0].get("points", 0))
    for over_p, under_p in candidates[1:]:
        diff = abs(target_line - over_p.get("points", 0))
        if diff < best_diff:
            best_diff = diff
            best = (over_p, under_p)

    if best_diff > 0.5:
        return None, None
    return best


def _pinyin_match_names(bb_home: str, bb_away: str, pin_list: list) -> tuple:
    """Fallback: pinyin-based fuzzy matching for CJK names (e.g. tennis players).

    Uses pypinyin to convert Chinese names to pinyin, then difflib
    to compare against Pinnacle English names.  Only kicks in when
    the BB names contain CJK characters and TEAM_NAME_MAP has no entry.
    Returns (match, score) or (None, 0.0).
    """
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None, 0.0
    from difflib import SequenceMatcher as _SM

    def _is_cjk(s):
        return any('一' <= c <= '鿿' for c in s)

    def _pinyin_key(name):
        # Strip country suffix "(xxx)" and whitespace
        raw = name.rsplit("(", 1)[0].strip()
        if not _is_cjk(raw):
            return ""
        # Convert each dot-separated syllable to pinyin
        syllables = raw.split(".")
        py = " ".join("".join(lazy_pinyin(s)).lower() for s in syllables)
        return py

    bb_home_py = _pinyin_key(bb_home)
    bb_away_py = _pinyin_key(bb_away)
    if not bb_home_py and not bb_away_py:
        return None, 0.0

    best_match = None
    best_score = 0.0

    for pin in pin_list:
        pin_home_l = pin.get("home", "").lower()
        pin_away_l = pin.get("away", "").lower()
        scores = []
        if bb_home_py:
            scores.append(_SM(None, bb_home_py, pin_home_l).ratio())
        if bb_away_py:
            scores.append(_SM(None, bb_away_py, pin_away_l).ratio())
        avg = sum(scores) / len(scores) if scores else 0
        if avg > best_score:
            best_score = avg
            best_match = pin

    # Lower threshold for pinyin matching — pronunciation varies
    if best_score >= 0.50:
        return best_match, best_score
    return None, 0.0


def find_pin_match_by_name(bb_home, bb_away, pin_list):
    """Find Pinnacle match by team name mapping.

    Phase 1: TEAM_NAME_MAP (exact Chinese→English).
    Phase 2: pinyin-based fuzzy matching (for tennis etc.).
    Returns (match, score) or (None, 0).
    """
    bb_home_en = TEAM_NAME_MAP.get(bb_home, "").lower()
    bb_away_en = TEAM_NAME_MAP.get(bb_away, "").lower()

    if not bb_home_en and not bb_away_en:
        return _pinyin_match_names(bb_home, bb_away, pin_list)

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
    """Convert BB match time to epoch seconds (UTC).

    支持两种格式:
    1. API 数据: bt 字段 (Unix 毫秒时间戳)
    2. DOM 提取: period("07/15") + time("03:00") (GMT+8)
    """
    # API 数据: bt 是毫秒时间戳
    bt = bb_match.get("bt")
    if bt:
        try:
            return int(int(bt) / 1000)
        except (ValueError, TypeError):
            pass

    # DOM 提取: period + time (GMT+8)
    period = bb_match.get("period", "")
    btime = bb_match.get("time", "")
    if not period or not btime:
        return None
    try:
        dt_str = f"2026-{period[:2]}-{period[3:5]}T{btime[:2]}:{btime[3:5]}:00"
        dt_naive = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
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


def _odds_similarity(bb_1x2, pin_1x2, min_odds=3, sport="football"):
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
        # 网球赔率经常严重倾斜（如 1.10 vs 6.50），ratio spread 必然 > 0.15，
        # 这个 ×0.8 惩罚对网球不合理，会导致 valid 匹配 score 被压低。
        if sport != "tennis":
            avg_ratio *= 0.8
    return avg_ratio


def _make_bb_key(bb):
    return f"{bb.get('home','')}|{bb.get('away','')}|{bb.get('league','')}"


def _compute_combined_score(bb, bb_1x2, bb_epoch, pin, pin_ml, sport="football"):
    """Combined score = odds_similarity × time_factor (0-1)."""
    min_odds = 2 if sport in TWO_WAY_SPORTS else 3
    odds_score = _odds_similarity(bb_1x2, pin_ml, min_odds, sport)
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
            bb_1x2 = []  # No ML market — still include for HC/OU comparison
        bb_data[_make_bb_key(bb)] = {
            "match": bb, "bb_1x2": bb_1x2, "epoch": _bb_to_epoch(bb),
            "sport": sport,
        }

    # Phase 1: Name-based — for each Pin match, find best BB match
    # Group BB data by league
    bb_by_league = {}
    for bb_key, bd in bb_data.items():
        league = bd["match"].get("league", "")
        bb_by_league.setdefault(league, []).append((bb_key, bd))

    for bb_league, bb_entries in bb_by_league.items():
        pin_list = pin_matches_by_league.get(bb_league, [])
        # For each Pin match, find the best available BB match
        for pin in pin_list:
            pin_id = pin.get("matchup_id", id(pin))
            if pin_id in used_pin_ids:
                continue
            best_bb_key = None
            best_bd = None
            best_name_score = 0.0
            for bb_key, bd in bb_entries:
                if bb_key in used_bb_keys:
                    continue
                _, name_score = find_pin_match_by_name(
                    bd["match"].get("home", ""), bd["match"].get("away", ""), [pin],
                )
                if name_score > best_name_score:
                    best_name_score = name_score
                    best_bb_key = bb_key
                    best_bd = bd
            if best_bd and best_name_score >= 0.50:
                sport = best_bd["sport"]
                bb_ml = best_bd.get("bb_1x2", [])
                min_odds = 2 if sport in TWO_WAY_SPORTS else 3
                pin_ml = []
                if len(bb_ml) >= min_odds:
                    # BB has ML — require Pin to have ML too
                    pin_ml = get_pin_ml_sorted(pin, sport)
                    if len(pin_ml) < min_odds:
                        continue
                # BB has no ML → still match for HC/OU comparison
                used_pin_ids.add(pin_id)
                used_bb_keys.add(best_bb_key)
                name_matched.append({
                    "bb": best_bd["match"], "pin": pin, "league": bb_league,
                    "match_score": 1.0, "team_score": best_name_score,
                    "match_type": "name",
                    "bb_1x2": bb_ml, "pin_1x2": pin_ml,
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
                # 网球的时间匹配：降低门限以覆盖更多 ITF 赛事
                # ITF 赔率差异大 + 时间经常微调，纯时间和赔率匹配很难高分
                min_threshold = 0.55 if sport == "tennis" else 0.70
                if combined >= min_threshold:
                    pairs.append((combined, bb_key, bd["match"], pin,
                                  bd["bb_1x2"], pin_ml, pin_id, sport))
                elif sport == "tennis" and combined > 0.4 and not bd['match'].get('league','').startswith('世界'):
                    bb_t = bd['match'].get('bt','')
                    pin_t = pin.get('start_time','')
                    bb_odds = bd.get('bb_1x2',[])
                    pin_odds_list = pin_ml
                    print(f"  [网球 Phase 2] {bd['match'].get('home','')} vs {bd['match'].get('away','')}")
                    print(f"    combined={combined:.3f} (阈值={min_threshold})")
                    print(f"    BB时间={bb_t} Pin时间={pin_t}")
                    print(f"    BB赔率={bb_odds} Pin赔率={pin_odds_list}")

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


def _find_itf_league_ids(bb_league_name, all_sport_matchups):
    """Handle 世界网球 (ITF) leagues with location-based matching.

    BB format: "世界网球 - M15 乌斯拉尔 男子单打"
    Pinnacle format: "ITF Men Uslar - R1"

    Uses hardcoded Chinese→English location mapping, falling back
    to pinyin fuzzy matching for unmapped locations.
    """
    # Hardcoded Chinese ITF location → English name mapping
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


def find_pinnacle_league_ids(bb_league_name, all_sport_matchups):
    """Find ALL Pinnacle league IDs matching a BB体育 league name.

    网球等赛事在 Pinnacle 可能拆分为多个子联赛（Qualifiers、R1等），
    返回所有匹配的联赛 ID + 同前缀的子联赛。

    策略：
    1. LEAGUE_KEYWORDS 精确映射
    1.5. ITF 世界网球特殊处理（位置拼音匹配）
    2. 对已精确映射的联赛，找 Pinnacle 上同前缀名的子联赛（如 "ATP Bastad"
       → "ATP Bastad - Qualifiers"、"ATP Bastad - R1"）
       限制：只对单赛事名（不含" - "在原映射名中）做子联赛扩展
    3. 未精确映射的联赛用英文关键词做可控模糊匹配
    """
    bb_lower = bb_league_name.lower().strip()
    matched_ids = set()

    # Phase 1: LEAGUE_KEYWORDS 精确映射
    matched_pin_names = []  # Pinnacle 联赛名列表
    # 收集所有匹配的关键词候选
    exact_candidate = None     # bb_name == bb_league_name (精确匹配)
    reverse_candidates = []    # bb_league_name in bb_name (联赛名在关键词内，可靠)
    direct_candidates = []     # bb_name in bb_league_name (关键词在联赛名内，可能有CJK子串碰撞)

    for bb_name, pin_name in LEAGUE_KEYWORDS.items():
        in_direct = bb_name in bb_league_name
        in_reverse = bb_league_name in bb_name

        if in_direct and in_reverse:
            # 双向子串=精确匹配，最高优先级，立即使用
            exact_candidate = (bb_name, pin_name)
            break
        elif in_reverse:
            # 联赛名在关键词内（如关键词"白俄罗斯超级联赛"包含联赛名"俄罗斯超级联赛"）
            # 这种匹配非常可靠，无假阳性
            reverse_candidates.append((bb_name, pin_name))
        elif in_direct:
            # 关键词在联赛名内（如"俄罗斯超级联赛"是"白俄罗斯超级联赛"的子串）
            # 可能有CJK子串碰撞，需要用最长关键词消歧
            direct_candidates.append((bb_name, pin_name, len(bb_name)))

    # 尝试精确匹配
    if exact_candidate:
        bb_name, pin_name = exact_candidate
        pin_names = [pin_name] if isinstance(pin_name, str) else pin_name
        for pn in pin_names:
            lid = _find_best_league(pn, all_sport_matchups)
            if lid:
                matched_ids.add(lid)
                matched_pin_names.append(pn)

    # 无精确匹配时，尝试反向匹配（可靠）
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

    # 仍无匹配时，尝试正向匹配（最长关键词优先）
    if not matched_ids and direct_candidates:
        direct_candidates.sort(key=lambda c: -c[2])  # 按关键词长度降序
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

    # Phase 1.5: ITF 世界网球 → 位置拼音匹配
    if not matched_ids and ("世界网球" in bb_league_name or "世界網球" in bb_league_name):
        itf_ids = _find_itf_league_ids(bb_league_name, all_sport_matchups)
        if itf_ids:
            return sorted(itf_ids)

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
            # 单关键词只匹配主流联赛缩写，防止 "atp" 匹配到所有 ATP 联赛
            elif len(overlap) == 1:
                single_word = list(overlap)[0]
                if single_word in ("nba", "nfl", "mlb", "wnba", "ncaa"):
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
        if diff > 0.01:
            tag = "HT" if is_ht else ""
            return False, f"{tag}让球线不一致: BB={bb_line} vs Pinnacle={ref}"
    elif market_type == "ou":
        if diff > 0.01:
            # 网球特殊：如果 diff>5 说明跨市场比较（games vs sets）
            if sport == "tennis" and diff > 5:
                return False, f"大小盘线不匹配: BB={bb_line} vs Pinnacle={ref}，可能用了错误市场"
            tag = "HT" if is_ht else ""
            return False, f"{tag}大小盘线不一致: BB={bb_line} vs Pinnacle={ref}"

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
    """启动时检测 Pinnacle API 连通性（先试直连，再试 SOCKS5 代理）。"""
    test_url = f"{API_BASE}/sports/29/matchups"
    SESSION.proxies = {}

    # 测试 1: 直连
    try:
        resp = SESSION.get(test_url, timeout=15)
        if resp.status_code == 200:
            print(f"  ✅ Pinnacle API 连通正常（直连）")
            return True
        print(f"  ⚠️  直连返回 {resp.status_code}，尝试 SOCKS5...")
    except requests.exceptions.SSLError as e:
        print(f"  ❌ Pinnacle API SSL 失败: {e}")
        print(f"     → 检查系统时间 / 更新 CA 证书")
    except requests.exceptions.ConnectionError as e:
        print(f"  ❌ Pinnacle API 直连失败: {e}")
        print(f"     → 检查网络连接")
    except Exception as e:
        print(f"  ❌ Pinnacle API 直连异常 ({type(e).__name__}): {e}")

    # 测试 2: SOCKS5 代理
    try:
        resp = SESSION.get(test_url, timeout=15, proxies={"https": PROXY, "http": PROXY})
        if resp.status_code == 200:
            print(f"  ✅ Pinnacle API 连通正常 (SOCKS5)")
            return True
        print(f"  ❌ Pinnacle API (SOCKS5) 返回 {resp.status_code}")
    except Exception as e:
        print(f"  ❌ Pinnacle API (SOCKS5) 失败: {e}")

    print(f"\n  💡 诊断: Pinnacle API 不可用")
    print(f"    两种可能:")
    print(f"    1. Python 3.14 http.client chunked bug — 重试几次可恢复")
    print(f"    2. 网络/代理问题 — 检查 Shadowrocket 是否开启")
    return False


_EXTRACTION_META_FILE = DATA_DIR / "extraction_consistency_meta.json"


def _check_extraction_consistency(n_matches: int):
    """检查 BB 提取量是否稳定。如果比上次下降 >30%，打印醒目警告。"""
    prev = None
    if _EXTRACTION_META_FILE.exists():
        try:
            prev = json.loads(_EXTRACTION_META_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    if prev:
        prev_count = prev.get("bb_matches_total", 0)
        if prev_count > 0:
            drop = (prev_count - n_matches) / prev_count
            if drop > 0.30:
                tag = "⚠️" * 5
                print(f"\n{tag} 提取量异常下降!")
                print(f"  BB 比赛数: {prev_count} → {n_matches} ({drop*100:.0f}%)")
                print(f"  检查 bb_api_fetcher.py 是否正常返回数据\n")

    _EXTRACTION_META_FILE.write_text(json.dumps({
        "bb_matches_total": n_matches,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }))


def _preflight_check():
    """连通性预检：同时检查 BB API 和 Pinnacle API，返回是否全部正常。"""
    print("\n" + "=" * 60)
    print("🔌 连通性预检")
    print("=" * 60)

    # BB API
    bb_ok = True
    bb_path = DATA_DIR / "bb_odds_extracted.json"
    if bb_path.exists():
        age = time.time() - bb_path.stat().st_mtime
        print(f"\n📡 BB体育 数据文件: {bb_path.name}")
        print(f"   文件存在, 更新于 {age/60:.0f} 分钟前")
    else:
        print(f"\n📡 BB体育: 数据文件不存在 → 需要先运行 bb_api_fetcher")
        bb_ok = False

    # Pinnacle API
    pin_ok = _check_pinnacle()

    print()
    if bb_ok and pin_ok:
        print(f"  ✅ 全部连通正常")
    else:
        if not bb_ok:
            print(f"  ❌ BB API 异常")
        if not pin_ok:
            print(f"  ❌ Pinnacle API 异常")
    print("=" * 60)
    return bb_ok and pin_ok


def main():
    print("=" * 60)
    print("BB体育 vs Pinnacle 完整赔率对比 v2")
    print("=" * 60)

    # 0. 如果传入了 --check 参数，只跑预检
    if "--check" in sys.argv:
        _preflight_check()
        return

    # 1. Load BB体育数据
    bb_matches = load_bb_odds()
    # 过滤掉冠军/优胜者盘口（非比赛）
    bb_matches = [m for m in bb_matches if m.get("league", "") not in OUTRIGHT_LEAGUES]
    # 过滤掉已开赛的比赛（bt 是毫秒时间戳）
    _now_ts = int(time.time() * 1000)
    _before = len(bb_matches)
    bb_matches = [m for m in bb_matches if not m.get("bt") or int(m["bt"]) > _now_ts]
    _filtered = _before - len(bb_matches)
    if _filtered:
        print(f"  🕐 已过滤 {_filtered} 场已开赛的比赛")
    print(f"\nBB体育: {len(bb_matches)} 场比赛 (排除冠军盘口+已开赛后)")

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
        info = all_pin_leagues.get(pin_id, {})

        # 跳过父级联赛（0 场比赛）：如 ATP Gstaad (ID 197277) 不直接包含比赛，
        # 其子联赛（R16、QF、Doubles 等）已在去重列表中单独包含。
        if info.get("matchup_count", 0) == 0:
            print(f"  跳过 [{info.get('name', pin_id)}] (ID={pin_id}) — 父级联赛无直接比赛")
            continue

        if call_idx > 0:
            delay = round(random.uniform(1.8, 4.2), 1)
            print(f"  ⏳ 等待 {delay:.1f}s 避免限流...")
            time.sleep(delay)

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
        bb_bt = bb.get("bt")
        if bb_bt:
            try:
                bb_epoch = int(int(bb_bt) / 1000)
                bb_dt = datetime.fromtimestamp(bb_epoch, tz=timezone.utc)
                bb_bj = bb_dt.astimezone(timezone(timedelta(hours=8)))
                bb_start = bb_bj.strftime("%m/%d %H:%M")
            except (ValueError, TypeError, OSError):
                bb_start = ""
        else:
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
            "draw_no_bet": [],
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
            if sport == "tennis":
                # BB tennis handicap1 is game handicap (always ±1.5 games).
                # Pinnacle's games_spread returns match-specific game handicap
                # lines (±2.0, ±4.0, ±5.5) that don't match BB's fixed ±1.5.
                # The odds from games_spread are from a different market (set
                # handicap / 盘分让分), producing false +EV.  Skip HC for tennis.
                home_sp = away_sp = None
            else:
                home_sp, away_sp, sp_is_alt = get_pin_spread(pin, target_line=bb_hl)
            if home_sp and away_sp and home_sp.get("price_decimal") and away_sp.get("price_decimal"):
                pin_home_odds = home_sp["price_decimal"]
                pin_away_odds = away_sp["price_decimal"]
                bb_home_odds = bb_hc["home_odds"]
                bb_away_odds = bb_hc["away_odds"]

                if sp_is_alt:
                    main_spreads = pin.get("spread", [])
                    if main_spreads:
                        mp = main_spreads[0].get("prices", [])
                        mp_line = next((p.get("points","?") for p in mp if p.get("designation")=="home"), "?")
                        mp_odds = next((p.get("price_decimal","?") for p in mp if p.get("designation")=="home"), "?")
                        entry["flags"].append(f"备用盘口: Pin主线={mp_line}@{mp_odds}")

                # 校准：检查让球线是否对得上
                pin_hc_line = home_sp.get("points")
                bb_hc_line_val = bb_hc.get("home_line") or bb_hc.get("away_line")
                cal_ok, cal_msg = _calibrate_market_line(sport, "hc", bb_hc_line_val, pin_hc_line, None)
                if cal_ok:
                    # 二次校验：同时检查 home 和 away 两条线的一致性
                    # 防止因盘口线波动导致单侧校验通过但整体错配
                    bb_hl = bb_hc.get("home_line")
                    bb_al = bb_hc.get("away_line")
                    pin_hl = home_sp.get("points")
                    pin_al = away_sp.get("points")
                    home_ok = (bb_hl is not None and pin_hl is not None
                               and abs(bb_hl - pin_hl) <= 0.01)
                    away_ok = (bb_al is not None and pin_al is not None
                               and abs(bb_al - pin_al) <= 0.01)
                    if (bb_hl is not None or bb_al is not None) and not (home_ok or away_ok):
                        cal_ok = False
                        cal_msg = f"让球线错配: BB=[{bb_hl},{bb_al}] vs Pin=[{pin_hl},{pin_al}]"
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
                n_ht_ml = min(len(pin_ht_ml), len(ht_labels["ml"]))  # cap to available labels
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
                home_sp, away_sp, _ = get_pin_spread(pin, target_line=bb_hl, source=pin.get("ht_spread", []))
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
            # 安全校验：BB 双重机会赔率是否与主1X2相同（侧边栏点击失败时会出现）
            dc_first3 = [round(float(x), 2) for x in bb_dc[:3]]
            ml_first3 = [round(x, 2) for x in bb_ml[:3]]
            if dc_first3 == ml_first3:
                # 侧边栏提取失败，odds_dc 只是主视图赔率的复制
                bb_dc = []
        if len(bb_dc) >= 3 and n_ml == 3:
            h, d, a = pin_ml
            if all(x and x > 0 for x in [h, d, a]):
                imp = 1/h + 1/d + 1/a
                p_h, p_d, p_a = (1/h)/imp, (1/d)/imp, (1/a)/imp
                dc_fair = [1/(p_h+p_d), 1/(p_d+p_a), 1/(p_h+p_a)]
                dc_labels = ["双重机会-主/和局", "双重机会-和局/客", "双重机会-主/客"]
                # DC 赔率必须低于对应的两个独立1X2赔率（覆盖两个赛果，概率更高）
                dc_pair_indices = [(0,1), (1,2), (0,2)]
                for i in range(3):
                    bb_dc_val = float(bb_dc[i]) if isinstance(bb_dc[i], str) else bb_dc[i]
                    if not (bb_dc_val and dc_fair[i] > 0):
                        continue
                    # 安全校验：DC赔率必须低于两个组成赛果的1X2赔率
                    idx1, idx2 = dc_pair_indices[i]
                    if bb_ml[idx1] and bb_ml[idx2] and bb_dc_val >= min(bb_ml[idx1], bb_ml[idx2]):
                        continue
                    ev = (bb_dc_val - dc_fair[i]) / dc_fair[i] * 100
                    if ev > 1:
                        entry["double_chance"].append({
                            "designation": dc_labels[i],
                            "bb_odds": bb_dc_val,
                            "fair_price": round(dc_fair[i], 4),
                            "ev_pct": round(ev, 2),
                            "_market": "dc",
                        })

        # --- 平局退款 (Draw No Bet) FT：从 Pinnacle 1X2 推导公平价 ---
        bb_dnb = bb.get("odds_dnb", [])
        if len(bb_dnb) >= 2 and n_ml == 3:
            # 安全校验：DNB赔率必须小于对应独赢赔率（退款盘更安全→赔率更低）
            bb_dnb_h = float(bb_dnb[0]) if isinstance(bb_dnb[0], str) else bb_dnb[0]
            bb_dnb_a = float(bb_dnb[1]) if isinstance(bb_dnb[1], str) else bb_dnb[1]
            if bb_dnb_h >= bb_ml[0] * 0.99 or bb_dnb_a >= bb_ml[-1] * 0.99:
                bb_dnb = []
        if len(bb_dnb) >= 2 and n_ml == 3:
            h, d, a = pin_ml
            if all(x and x > 0 for x in [h, d, a]):
                imp = 1/h + 1/d + 1/a
                p_h, p_d, p_a = (1/h)/imp, (1/d)/imp, (1/a)/imp
                dnb_fair = [1/(p_h/(p_h+p_d)), 1/(p_a/(p_a+p_d))]
                dnb_labels = ["平局退款-主", "平局退款-客"]
                for i in range(2):
                    bb_dnb_val = float(bb_dnb[i]) if isinstance(bb_dnb[i], str) else bb_dnb[i]
                    if bb_dnb_val and dnb_fair[i] > 0:
                        ev = (bb_dnb_val - dnb_fair[i]) / dnb_fair[i] * 100
                        # DNB EV > 15% 通常是提取错误（侧边栏导航错位），直接丢弃
                        if 1 < ev <= 15:
                            entry["draw_no_bet"].append({
                                "designation": dnb_labels[i],
                                "bb_odds": bb_dnb_val,
                                "fair_price": round(dnb_fair[i], 4),
                                "ev_pct": round(ev, 2),
                                "_market": "dnb",
                            })

        # --- 上半场平局退款 (HT DNB)：从 Pinnacle HT 1X2 推导公平价 ---
        if len(bb_dnb) >= 4 and n_ml == 3:
            # 安全校验：HT DNB赔率必须小于HT独赢赔率
            bb_ht_ml = bb.get("odds_ht", {}).get("ml", [])
            if len(bb_ht_ml) >= 2:
                ht_h = float(bb_dnb[2]) if isinstance(bb_dnb[2], str) else bb_dnb[2]
                ht_a = float(bb_dnb[3]) if isinstance(bb_dnb[3], str) else bb_dnb[3]
                if ht_h >= bb_ht_ml[0] * 0.99 or ht_a >= bb_ht_ml[-1] * 0.99:
                    bb_dnb = bb_dnb[:2]  # 保留FT DNB，清除HT DNB
            if len(bb_dnb) >= 4:  # HT DNB 有效时才继续
                pin_ht_ml = get_pin_ml_sorted_from_source(pin.get("ht_moneyline", []), sport)
                if len(pin_ht_ml) == 3:
                    hh, dd, aa = pin_ht_ml
                    if all(x and x > 0 for x in [hh, dd, aa]):
                        imp = 1/hh + 1/dd + 1/aa
                        p_h, p_d, p_a = (1/hh)/imp, (1/dd)/imp, (1/aa)/imp
                        dnb_fair = [1/(p_h/(p_h+p_d)), 1/(p_a/(p_a+p_d))]
                        dnb_labels = ["上半场平局退款-主", "上半场平局退款-客"]
                        for i in range(2):
                            bb_dnb_val = float(bb_dnb[2+i]) if isinstance(bb_dnb[2+i], str) else bb_dnb[2+i]
                            if bb_dnb_val and dnb_fair[i] > 0:
                                ev = (bb_dnb_val - dnb_fair[i]) / dnb_fair[i] * 100
                                if 1 < ev <= 15:
                                    entry["draw_no_bet"].append({
                                        "designation": dnb_labels[i],
                                        "bb_odds": bb_dnb_val,
                                        "fair_price": round(dnb_fair[i], 4),
                                        "ev_pct": round(ev, 2),
                                        "_market": "ht_dnb",
                                    })

        # 同一市场只保留溢价最高的选项（FT + HT + DC + DNB + HT_DNB 各自保留）
        for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
            if entry[mk]:
                ft_entries = [x for x in entry[mk] if x.get("_market") in (None, "", "main")]
                ht_entries = [x for x in entry[mk] if x.get("_market") == "ht"]
                dc_entries = [x for x in entry[mk] if x.get("_market") == "dc"]
                best = []
                if ft_entries:
                    best.append(max(ft_entries, key=lambda x: x["ev_pct"]))
                if ht_entries:
                    best.append(max(ht_entries, key=lambda x: x["ev_pct"]))
                if dc_entries:
                    best.append(max(dc_entries, key=lambda x: x["ev_pct"]))
                dnb_entries = [x for x in entry[mk] if x.get("_market") == "dnb"]
                ht_dnb_entries = [x for x in entry[mk] if x.get("_market") == "ht_dnb"]
                if dnb_entries:
                    best.append(max(dnb_entries, key=lambda x: x["ev_pct"]))
                if ht_dnb_entries:
                    best.append(max(ht_dnb_entries, key=lambda x: x["ev_pct"]))
                entry[mk] = best

        if entry["opportunities"] or entry["handicap"] or entry["over_under"] or entry["double_chance"] or entry["draw_no_bet"]:
            # 可疑 EV / 低置信度警告
            for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
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
    total_dnb = sum(len(o.get("draw_no_bet", [])) for o in opportunities)
    total_all = total_opps_1x2 + total_hc + total_ou + total_dc + total_dnb

    print(f"\n{'='*60}")
    print(f"匹配: {len(matched)} | +EV 独赢: {total_opps_1x2} | 让球: {total_hc} | 大小: {total_ou} | 双重机会: {total_dc} | 平局退款: {total_dnb} | 总计: {total_all}")
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
        for o in entry.get("draw_no_bet", []):
            print(f"    ✅ +EV {o['ev_pct']}%: {o['designation']} (BB={o['bb_odds']} Fair={o['fair_price']})")

    # 提取量一致性检查：如果 BB 比赛数比上次骤降 >30%，打印醒目警告
    _check_extraction_consistency(len(bb_matches))

    # Save
    timestamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    # Sport → name mapping for output
    sport_name_map = {"football":"足球","basketball":"篮球","tennis":"网球","baseball":"棒球","american_football":"美式足球"}
    # Per-sport breakdown for consistency tracking
    sport_counts = {}
    sport_opp_counts = {}
    for entry in opportunities:
        s = entry.get("sport", "unknown")
        sport_counts[s] = sport_counts.get(s, 0) + 1
        n_opps = (len(entry.get("opportunities", [])) + len(entry.get("handicap", []))
                   + len(entry.get("over_under", [])) + len(entry.get("double_chance", []))
                   + len(entry.get("draw_no_bet", [])))
        sport_opp_counts[s] = sport_opp_counts.get(s, 0) + n_opps

    output = {
        "version": "2.0",
        "parameters": {
            "phase2_threshold_default": 0.70,
            "phase2_threshold_tennis": 0.75,
            "ev_cap_pct": 15,
            "min_ev_pct": 1,
        },
        "timestamp": timestamp,
        "bb_matches_total": len(bb_matches),
        "pinnacle_leagues_found": len(matched_leagues),
        "matched_matches": len(matched),
        "matches_with_ev": len(opportunities),
        "per_sport_matched": {k: v for k, v in sorted(sport_counts.items())},
        "per_sport_opportunities": {k: v for k, v in sorted(sport_opp_counts.items())},
        "opportunities_1x2": total_opps_1x2,
        "opportunities_handicap": total_hc,
        "opportunities_over_under": total_ou,
        "opportunities_double_chance": total_dc,
        "opportunities_draw_no_bet": total_dnb,
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
