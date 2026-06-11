"""Dashboard 队名中文化 — 复用已有映射，处理短名/长名兼容。"""
from src.core.team_names import NBA_CN, FOOTBALL_MAP, cn_team

# NBA 短名 → 中文（power_ratings 可能只用城市名）
_NBA_SHORT_CN = {
    "Atlanta": "老鹰",
    "Boston": "凯尔特人",
    "Brooklyn": "篮网",
    "Charlotte": "黄蜂",
    "Chicago": "公牛",
    "Cleveland": "骑士",
    "Dallas": "独行侠",
    "Denver": "掘金",
    "Detroit": "活塞",
    "Golden State": "勇士",
    "Houston": "火箭",
    "Indiana": "步行者",
    "L.A. Clippers": "快船",
    "L.A. Lakers": "湖人",
    "LA Clippers": "快船",
    "Memphis": "灰熊",
    "Miami": "热火",
    "Milwaukee": "雄鹿",
    "Minnesota": "森林狼",
    "New Orleans": "鹈鹕",
    "New York": "尼克斯",
    "Oklahoma City": "雷霆",
    "Orlando": "魔术",
    "Philadelphia": "76人",
    "Phoenix": "太阳",
    "Portland": "开拓者",
    "Sacramento": "国王",
    "San Antonio": "马刺",
    "Toronto": "猛龙",
    "Utah": "爵士",
    "Washington": "奇才",
}

# 特征数据名 → 中文名（football power_ratings 使用特征数据名）
_FEATURE_TO_CN = {}
for api_name, (feat_name, cn_name) in FOOTBALL_MAP.items():
    _FEATURE_TO_CN[feat_name] = cn_name
    # 也存一份 feat_name 的各种常见变体
    if "'" in feat_name:
        _FEATURE_TO_CN[feat_name.replace("'", "’")] = cn_name
# 补充一些 power_ratings 中特有的全称格式
_EXTRA_FB = {
    "1. FC Heidenheim 1846": "海登海姆",
    "1. FC Köln": "科隆",
    "1. FC Union Berlin": "柏林联合",
    "1. FSV Mainz 05": "美因茨",
    "AC Pisa 1909": "比萨",
    "ACF Fiorentina": "佛罗伦萨",
    "AFC Bournemouth": "伯恩茅斯",
    "AJ Auxerre": "欧塞尔",
    "AS Monaco FC": "摩纳哥",
    "Angers SCO": "昂热",
    "Arsenal FC": "阿森纳",
    "Aston Villa FC": "阿斯顿维拉",
    "Bologna FC 1909": "博洛尼亚",
    "Brighton & Hove Albion FC": "布莱顿",
    "Burnley FC": "伯恩利",
    "CA Osasuna": "奥萨苏纳",
    "Cagliari Calcio": "卡利亚里",
    "Chelsea FC": "切尔西",
    "Club Atlético de Madrid": "马德里竞技",
    "Como 1907": "科莫",
    "Crystal Palace FC": "水晶宫",
    "Deportivo Alavés": "阿拉维斯",
    "Everton FC": "埃弗顿",
    "FC Augsburg": "奥格斯堡",
    "FC Barcelona": "巴塞罗那",
    "FC Bayern München": "拜仁慕尼黑",
    "FC Internazionale Milano": "国际米兰",
    "FC Lorient": "洛里昂",
    "FC Metz": "梅斯",
    "FC Nantes": "南特",
    "FC St. Pauli 1910": "圣保利",
    "Hamburger SV": "汉堡",
    "Le Havre AC": "勒阿弗尔",
    "Leicester City FC": "莱斯特城",
    "Liverpool FC": "利物浦",
    "Manchester City FC": "曼城",
    "Manchester United FC": "曼联",
    "Newcastle United FC": "纽卡斯尔联",
    "Nottingham Forest FC": "诺丁汉森林",
    "Olympique Lyonnais": "里昂",
    "Olympique de Marseille": "马赛",
    "OGC Nice": "尼斯",
    "Paris Saint-Germain FC": "巴黎圣日耳曼",
    "RC Lens": "朗斯",
    "RC Strasbourg Alsace": "斯特拉斯堡",
    "RCD Espanyol de Barcelona": "西班牙人",
    "RCD Mallorca": "马略卡",
    "RUGBY": "RUGBY",
    "Rayo Vallecano de Madrid": "巴列卡诺",
    "Real Betis Balompié": "皇家贝蒂斯",
    "Real Madrid CF": "皇家马德里",
    "Real Oviedo": "奥维耶多",
    "Real Sociedad de Fútbol": "皇家社会",
    "SC Freiburg": "弗赖堡",
    "SC Paderborn 07": "帕德博恩",
    "Southampton FC": "南安普顿",
    "Stade Brestois 29": "布雷斯特",
    "Stade Rennais FC 1901": "雷恩",
    "TSG 1899 Hoffenheim": "霍芬海姆",
    "Tottenham Hotspur FC": "热刺",
    "Toulouse FC": "图卢兹",
    "US Cremonese": "克雷莫纳",
    "US Lecce": "莱切",
    "US Sassuolo Calcio": "萨索洛",
    "Valencia CF": "巴伦西亚",
    "Venezia FC": "威尼斯",
    "VfB Stuttgart": "斯图加特",
    "VfL Bochum": "波鸿",
    "VfL Wolfsburg": "沃尔夫斯堡",
    "West Ham United FC": "西汉姆联",
    "Wolverhampton Wanderers FC": "狼队",
    "fc lorient": "洛里昂",
    "brentford fc": "布伦特福德",
    "atalanta bc": "亚特兰大",
    "lille osc": "里尔",
    "ogc nice": "尼斯",
    "fc st. pauli 1910": "圣保利",
    "hamburger sv": "汉堡",
    "sc freiburg": "弗赖堡",
    "sc paderborn 07": "帕德博恩",
    "sv werder bremen": "云达不莱梅",
    "tsg 1899 hoffenheim": "霍芬海姆",
    "vfb stuttgart": "斯图加特",
    "vfl bochum": "波鸿",
    "vfl wolfsburg": "沃尔夫斯堡",
    "1. fc köln": "科隆",
    "1. fc union berlin": "柏林联合",
    "1. fsv mainz 05": "美因茨",
    "1. fc heidenheim 1846": "海登海姆",
    "bayer 04 leverkusen": "勒沃库森",
    "borussia dortmund": "多特蒙德",
    "borussia mönchengladbach": "门兴",
    "eintracht frankfurt": "法兰克福",
    "fc bayern münchen": "拜仁慕尼黑",
    "fc augsburg": "奥格斯堡",
    "rb leipzig": "莱比锡红牛",
    "werder bremen": "云达不莱梅",
    "southampton fc": "南安普顿",
    "afc bournemouth": "伯恩茅斯",
    "aston villa fc": "阿斯顿维拉",
    "brighton & hove albion fc": "布莱顿",
    "burnley fc": "伯恩利",
    "chelsea fc": "切尔西",
    "crystal palace fc": "水晶宫",
    "everton fc": "埃弗顿",
    "fulham fc": "富勒姆",
    "leicester city fc": "莱斯特城",
    "liverpool fc": "利物浦",
    "manchester city fc": "曼城",
    "manchester united fc": "曼联",
    "newcastle united fc": "纽卡斯尔联",
    "nottingham forest fc": "诺丁汉森林",
    "tottenham hotspur fc": "热刺",
    "west ham united fc": "西汉姆联",
    "wolverhampton wanderers fc": "狼队",
    "arsenal fc": "阿森纳",
    "paris saint-germain fc": "巴黎圣日耳曼",
    "racing club de lens": "朗斯",
    "rc strasbourg alsace": "斯特拉斯堡",
    "stade brestois 29": "布雷斯特",
    "stade de reims": "兰斯",
    "stade rennais fc 1901": "雷恩",
    "toulouse fc": "图卢兹",
    "aj auxerre": "欧塞尔",
    "angers sco": "昂热",
    "as monaco fc": "摩纳哥",
    "clermont foot": "克莱蒙",
    "fc metz": "梅斯",
    "fc nantes": "南特",
    "le havre ac": "勒阿弗尔",
    "montpellier": "蒙彼利埃",
    "olympique lyonnais": "里昂",
    "olympique de marseille": "马赛",
    "paris fc": "巴黎FC",
}
_FEATURE_TO_CN.update(_EXTRA_FB)

# 项目名中文化
SPORT_CN = {
    "basketball": "🏀 篮球",
    "football": "⚽ 足球",
    "basketball_nba": "🏀 NBA",
    "nba": "🏀 NBA",
    "soccer_epl": "⚽ 英超",
    "soccer_spain_la_liga": "⚽ 西甲",
    "soccer_germany_bundesliga": "⚽ 德甲",
    "soccer_italy_serie_a": "⚽ 意甲",
    "soccer_france_ligue_one": "⚽ 法甲",
}


def nba_cn(team_name: str) -> str:
    """NBA 球队英文名 → 中文名（兼容短名和全名）。"""
    return NBA_CN.get(team_name, _NBA_SHORT_CN.get(team_name, team_name))


def fb_cn(team_name: str) -> str:
    """足球球队英文名 → 中文名（兼容特征名、odds API 名、短名）。"""
    # 尝试特征名映射
    lowered = team_name.lower().strip()
    if lowered in _FEATURE_TO_CN:
        return _FEATURE_TO_CN[lowered]
    if team_name in _FEATURE_TO_CN:
        return _FEATURE_TO_CN[team_name]
    # 尝试 odds API 名映射
    cn = cn_team(team_name, sport="football")
    if cn and cn != team_name:
        return cn
    return team_name


def team_cn(team_name: str, sport: str = "nba") -> str:
    """统一入口：球队英文名 → 中文名。"""
    if sport in ("nba", "basketball"):
        return nba_cn(team_name)
    return fb_cn(team_name)


def sport_cn(sport_key: str) -> str:
    """项目英文名 → 中文显示名。"""
    return SPORT_CN.get(sport_key, sport_key)
