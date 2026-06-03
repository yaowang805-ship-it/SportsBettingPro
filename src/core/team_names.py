"""团队名称映射 — odds API → 特征数据 → 中文显示名称。

特征数据名使用 fb_features.csv（含市场价值等完整特征）中的名称。
"""
from typing import Tuple

# 足球联赛中文名
LEAGUE_CN = {
    "soccer_epl": "英超",
    "soccer_spain_la_liga": "西甲",
    "soccer_germany_bundesliga": "德甲",
    "soccer_italy_serie_a": "意甲",
    "soccer_france_ligue_one": "法甲",
    "basketball_nba": "NBA",
}

# odds API 名 → (特征数据名, 中文名)
# 特征数据名用于在 fb_features.csv 中查找球队历史数据
# 中文名用于推送通知显示
FOOTBALL_MAP: dict[str, Tuple[str, str]] = {
    # ===== 英超 =====
    "Arsenal": ("arsenal fc", "阿森纳"),
    "Aston Villa": ("aston villa fc", "阿斯顿维拉"),
    "Bournemouth": ("afc bournemouth", "伯恩茅斯"),
    "Brentford": ("brentford fc", "布伦特福德"),
    "Brighton and Hove Albion": ("brighton & hove albion fc", "布莱顿"),
    "Burnley": ("burnley fc", "伯恩利"),
    "Chelsea": ("chelsea fc", "切尔西"),
    "Crystal Palace": ("crystal palace fc", "水晶宫"),
    "Everton": ("everton fc", "埃弗顿"),
    "Fulham": ("fulham fc", "富勒姆"),
    "Leeds United": ("leeds united fc", "利兹联"),
    "Leicester City": ("leicester city fc", "莱斯特城"),
    "Liverpool": ("liverpool fc", "利物浦"),
    "Manchester City": ("manchester city fc", "曼城"),
    "Manchester United": ("manchester united fc", "曼联"),
    "Newcastle United": ("newcastle united fc", "纽卡斯尔联"),
    "Nottingham Forest": ("nottingham forest fc", "诺丁汉森林"),
    "Southampton": ("southampton fc", "南安普顿"),
    "Sunderland": ("sunderland afc", "桑德兰"),
    "Tottenham Hotspur": ("tottenham hotspur fc", "热刺"),
    "West Ham United": ("west ham united fc", "西汉姆联"),
    "Wolverhampton Wanderers": ("wolverhampton wanderers fc", "狼队"),
    # ===== 西甲 =====
    "Alavés": ("deportivo alavés", "阿拉维斯"),
    "Athletic Bilbao": ("athletic club", "毕尔巴鄂竞技"),
    "Atlético Madrid": ("club atlético de madrid", "马德里竞技"),
    "Barcelona": ("fc barcelona", "巴塞罗那"),
    "CA Osasuna": ("ca osasuna", "奥萨苏纳"),
    "Celta Vigo": ("rc celta de vigo", "塞尔塔"),
    "Elche CF": ("elche cf", "埃尔切"),
    "Espanyol": ("rcd espanyol de barcelona", "西班牙人"),
    "Getafe": ("getafe cf", "赫塔费"),
    "Girona": ("girona fc", "赫罗纳"),
    "Levante": ("levante ud", "莱万特"),
    "Mallorca": ("rcd mallorca", "马略卡"),
    "Oviedo": ("real oviedo", "奥维耶多"),
    "Rayo Vallecano": ("rayo vallecano de madrid", "巴列卡诺"),
    "Real Betis": ("real betis balompié", "皇家贝蒂斯"),
    "Real Madrid": ("real madrid cf", "皇家马德里"),
    "Real Sociedad": ("real sociedad de fútbol", "皇家社会"),
    "Sevilla": ("sevilla fc", "塞维利亚"),
    "Valencia": ("valencia cf", "巴伦西亚"),
    "Villarreal": ("villarreal cf", "比利亚雷亚尔"),
    # ===== 德甲 =====
    "FC Augsburg": ("fc augsburg", "奥格斯堡"),
    "FC Bayern Munich": ("fc bayern münchen", "拜仁慕尼黑"),
    "FC Heidenheim": ("1. fc heidenheim 1846", "海登海姆"),
    "FC St. Pauli": ("fc st. pauli 1910", "圣保利"),
    "FSV Mainz 05": ("1. fsv mainz 05", "美因茨"),
    "Hamburger SV": ("hamburger sv", "汉堡"),
    "RB Leipzig": ("rb leipzig", "莱比锡红牛"),
    "SC Freiburg": ("sc freiburg", "弗赖堡"),
    "SC Paderborn": ("sc paderborn 07", "帕德博恩"),
    "SV Werder Bremen": ("sv werder bremen", "云达不莱梅"),
    "TSG 1899 Hoffenheim": ("tsg 1899 hoffenheim", "霍芬海姆"),
    "VfB Stuttgart": ("vfb stuttgart", "斯图加特"),
    "VfL Bochum": ("vfl bochum", "波鸿"),
    "VfL Wolfsburg": ("vfl wolfsburg", "沃尔夫斯堡"),
    "1. FC Köln": ("1. fc köln", "科隆"),
    "1. FC Union Berlin": ("1. fc union berlin", "柏林联合"),
    "Bayer 04 Leverkusen": ("bayer 04 leverkusen", "勒沃库森"),
    "Borussia Dortmund": ("borussia dortmund", "多特蒙德"),
    "Borussia Mönchengladbach": ("borussia mönchengladbach", "门兴"),
    "Eintracht Frankfurt": ("eintracht frankfurt", "法兰克福"),
    # 兼容新旧命名
    "Bayern Munich": ("fc bayern münchen", "拜仁慕尼黑"),
    "Mainz 05": ("1. fsv mainz 05", "美因茨"),
    "Union Berlin": ("1. fc union berlin", "柏林联合"),
    "Werder Bremen": ("sv werder bremen", "云达不莱梅"),
    "St. Pauli": ("fc st. pauli 1910", "圣保利"),
    "FC Köln": ("1. fc köln", "科隆"),
    # ===== 意甲 =====
    "AC Milan": ("ac milan", "AC米兰"),
    "AS Roma": ("as roma", "罗马"),
    "Atalanta BC": ("atalanta bc", "亚特兰大"),
    "Bologna": ("bologna fc 1909", "博洛尼亚"),
    "Cagliari": ("cagliari calcio", "卡利亚里"),
    "Como": ("como 1907", "科莫"),
    "Cremonese": ("us cremonese", "克雷莫纳"),
    "Empoli": ("empoli fc", "恩波利"),
    "Fiorentina": ("acf fiorentina", "佛罗伦萨"),
    "Frosinone": ("frosinone", "弗罗西诺内"),
    "Genoa": ("genoa cfc", "热那亚"),
    "Hellas Verona": ("hellas verona fc", "维罗纳"),
    "Inter Milan": ("fc internazionale milano", "国际米兰"),
    "Juventus": ("juventus fc", "尤文图斯"),
    "Lazio": ("ss lazio", "拉齐奥"),
    "Lecce": ("us lecce", "莱切"),
    "Monza": ("ac monza", "蒙扎"),
    "Napoli": ("ssc napoli", "那不勒斯"),
    "Parma": ("parma calcio 1913", "帕尔马"),
    "Pisa": ("ac pisa 1909", "比萨"),
    "Sassuolo": ("us sassuolo calcio", "萨索洛"),
    "Torino": ("torino fc", "都灵"),
    "Udinese": ("udinese calcio", "乌迪内斯"),
    "Venezia": ("venezia fc", "威尼斯"),
    # ===== 法甲 =====
    "AJ Auxerre": ("aj auxerre", "欧塞尔"),
    "Angers SCO": ("angers sco", "昂热"),
    "AS Monaco": ("as monaco fc", "摩纳哥"),
    "Clermont Foot": ("clermont foot", "克莱蒙"),
    "FC Lorient": ("fc lorient", "洛里昂"),
    "FC Metz": ("fc metz", "梅斯"),
    "FC Nantes": ("fc nantes", "南特"),
    "Le Havre AC": ("le havre ac", "勒阿弗尔"),
    "Lille OSC": ("lille osc", "里尔"),
    "Montpellier HSC": ("montpellier", "蒙彼利埃"),
    "OGC Nice": ("ogc nice", "尼斯"),
    "Olympique Lyonnais": ("olympique lyonnais", "里昂"),
    "Olympique de Marseille": ("olympique de marseille", "马赛"),
    "Paris FC": ("paris fc", "巴黎FC"),
    "Paris Saint-Germain": ("paris saint-germain fc", "巴黎圣日耳曼"),
    "RC Lens": ("racing club de lens", "朗斯"),
    "RC Strasbourg Alsace": ("rc strasbourg alsace", "斯特拉斯堡"),
    "Stade Brestois 29": ("stade brestois 29", "布雷斯特"),
    "Stade de Reims": ("stade de reims", "兰斯"),
    "Stade Rennais FC": ("stade rennais fc 1901", "雷恩"),
    "Toulouse FC": ("toulouse fc", "图卢兹"),
    # 法甲兼容名
    "PSG": ("paris saint-germain fc", "巴黎圣日耳曼"),
    "Monaco": ("as monaco fc", "摩纳哥"),
    "Lyon": ("olympique lyonnais", "里昂"),
    "Marseille": ("olympique de marseille", "马赛"),
    "Lens": ("racing club de lens", "朗斯"),
    "Rennes": ("stade rennais fc 1901", "雷恩"),
    "Strasbourg": ("rc strasbourg alsace", "斯特拉斯堡"),
    "Nantes": ("fc nantes", "南特"),
    "Toulouse": ("toulouse fc", "图卢兹"),
    "Lorient": ("fc lorient", "洛里昂"),
    "Metz": ("fc metz", "梅斯"),
    "Auxerre": ("aj auxerre", "欧塞尔"),
    "Angers": ("angers sco", "昂热"),
    "Brest": ("stade brestois 29", "布雷斯特"),
    "Reims": ("stade de reims", "兰斯"),
    "Clermont": ("clermont foot", "克莱蒙"),
    "Le Havre": ("le havre ac", "勒阿弗尔"),
    "Nice": ("ogc nice", "尼斯"),
    "Saint Etienne": ("as saint-étienne", "圣埃蒂安"),
    "St Etienne": ("as saint-étienne", "圣埃蒂安"),
    # 西甲兼容名
    "Alaves": ("deportivo alavés", "阿拉维斯"),
    "Osasuna": ("ca osasuna", "奥萨苏纳"),
    "Vallecano": ("rayo vallecano de madrid", "巴列卡诺"),
    "Betis": ("real betis balompié", "皇家贝蒂斯"),
    "Sociedad": ("real sociedad de fútbol", "皇家社会"),
    "Athletic Club": ("athletic club", "毕尔巴鄂竞技"),
    "Atletico Madrid": ("club atlético de madrid", "马德里竞技"),
    "Deportivo Alavés": ("deportivo alavés", "阿拉维斯"),
    # ===== 国际队 =====
    "Albania": ("albania", "阿尔巴尼亚"),
    "Algeria": ("algeria", "阿尔及利亚"),
    "Andorra": ("andorra", "安道尔"),
    "British Virgin Islands": ("british virgin islands", "英属维尔京群岛"),
    "Burundi": ("burundi", "布隆迪"),
    "Cyprus": ("cyprus", "塞浦路斯"),
    "Czechia": ("czechia", "捷克"),
    "Côte d'Ivoire": ("côte d'ivoire", "科特迪瓦"),
    "DR Congo": ("dr congo", "刚果民主共和国"),
    "Denmark": ("denmark", "丹麦"),
    "Dominican Republic": ("dominican republic", "多米尼加共和国"),
    "El Salvador": ("el salvador", "萨尔瓦多"),
    "Equatorial Guinea": ("equatorial guinea", "赤道几内亚"),
    "France": ("france", "法国"),
    "Gibraltar": ("gibraltar", "直布罗陀"),
    "Greece": ("greece", "希腊"),
    "Guam": ("guam", "关岛"),
    "Guatemala": ("guatemala", "危地马拉"),
    "Guinea": ("guinea", "几内亚"),
    "Iraq": ("iraq", "伊拉克"),
    "Israel": ("israel", "以色列"),
    "Italy": ("italy", "意大利"),
    "Liechtenstein": ("liechtenstein", "列支敦士登"),
    "Luxembourg": ("luxembourg", "卢森堡"),
    "Maldives": ("maldives", "马尔代夫"),
    "Mexico": ("mexico", "墨西哥"),
    "Netherlands": ("netherlands", "荷兰"),
    "Nigeria": ("nigeria", "尼日利亚"),
    "Northern Ireland": ("northern ireland", "北爱尔兰"),
    "Pakistan": ("pakistan", "巴基斯坦"),
    "Panama": ("panama", "巴拿马"),
    "Philippines": ("philippines", "菲律宾"),
    "Poland": ("poland", "波兰"),
    "Serbia": ("serbia", "塞尔维亚"),
    "Slovenia": ("slovenia", "斯洛文尼亚"),
    "South Korea": ("south korea", "韩国"),
    "Spain": ("spain", "西班牙"),
    "Sweden": ("sweden", "瑞典"),
    # ===== MLS / USL =====
    "Birmingham Legion FC": ("birmingham legion fc", "伯明翰军团"),
    "Louisville City FC": ("louisville city fc", "路易维尔城"),
    # ===== 摩洛哥联赛 =====
    "Ittihad Tanger": ("ittihad tanger", "伊蒂哈德丹吉尔"),
    "RS Berkane": ("rs berkane", "贝尔卡内"),
    "Raja Club Athletic": ("raja club athletic", "拉贾卡萨布兰卡"),
    "Renaissance Zemamra": ("renaissance zemamra", "泽马马拉"),
    "Union Sportive Yacoub El Mansour": ("union sportive yacoub el mansour", "雅各布曼苏尔"),
    "Wydad Casablanca": ("wydad casablanca", "维达德卡萨布兰卡"),
}

# NBA 球队映射 — odds API 名→中文名
# 特征数据名与 odds API 名一致（小写后可直接匹配）
NBA_CN = {
    "Atlanta Hawks": "老鹰",
    "Boston Celtics": "凯尔特人",
    "Brooklyn Nets": "篮网",
    "Charlotte Hornets": "黄蜂",
    "Chicago Bulls": "公牛",
    "Cleveland Cavaliers": "骑士",
    "Dallas Mavericks": "独行侠",
    "Denver Nuggets": "掘金",
    "Detroit Pistons": "活塞",
    "Golden State Warriors": "勇士",
    "Houston Rockets": "火箭",
    "Indiana Pacers": "步行者",
    "LA Clippers": "快船",
    "Los Angeles Clippers": "快船",
    "Los Angeles Lakers": "湖人",
    "Memphis Grizzlies": "灰熊",
    "Miami Heat": "热火",
    "Milwaukee Bucks": "雄鹿",
    "Minnesota Timberwolves": "森林狼",
    "New Orleans Pelicans": "鹈鹕",
    "New York Knicks": "尼克斯",
    "Oklahoma City Thunder": "雷霆",
    "Orlando Magic": "魔术",
    "Philadelphia 76ers": "76人",
    "Phoenix Suns": "太阳",
    "Portland Trail Blazers": "开拓者",
    "Sacramento Kings": "国王",
    "San Antonio Spurs": "马刺",
    "Toronto Raptors": "猛龙",
    "Utah Jazz": "爵士",
    "Washington Wizards": "奇才",
}


def lookup_football(odds_name: str) -> Tuple[str, str]:
    """查询足球球队映射。

    Args:
        odds_name: odds API 返回的球队名

    Returns:
        (特征数据名, 中文名) 元组，若无映射则返回 (原词小写, 原词)
    """
    mapped = FOOTBALL_MAP.get(odds_name)
    if mapped:
        return mapped
    return (odds_name.lower(), odds_name)


def cn_team(odds_name: str, sport: str = "nba") -> str:
    """取球队中文名。

    Args:
        odds_name: odds API 返回的球队名
        sport: 'nba' 或 'football'

    Returns:
        中文名，若无映射则返回原词
    """
    if sport == "nba":
        return NBA_CN.get(odds_name, odds_name)
    _, cn = lookup_football(odds_name)
    return cn


# 中文 → 特征名反向映射（用于预测日志 edge 特征）
_CN_TO_FEAT = None
_CN_NBA_TO_EN = None
_CN_TO_ODDS_NAME = None  # Chinese → Odds API English name (for settlement)


def _build_cn_mappings():
    """构建中文名 → 特征名 / 英文名的反向映射。"""
    global _CN_TO_FEAT, _CN_NBA_TO_EN, _CN_TO_ODDS_NAME
    if _CN_TO_FEAT is not None:
        return

    # 足球：中文 → 特征名, 中文 → Odds API 名
    _CN_TO_FEAT = {}
    _CN_TO_ODDS_NAME = {}
    for odds_name, (feat, cn) in FOOTBALL_MAP.items():
        _CN_TO_FEAT[cn.lower()] = feat.lower()
        _CN_TO_ODDS_NAME[cn.lower()] = odds_name.lower()

    # NBA：中文 → 英文名 (小写)
    _CN_NBA_TO_EN = {}
    for en, cn in NBA_CN.items():
        _CN_NBA_TO_EN[cn.lower()] = en.lower()
        _CN_TO_ODDS_NAME[cn.lower()] = en.lower()


def cn_to_feature_name(cn_name: str, sport: str = "nba") -> str:
    """中文球队名 → 特征数据中的球队名。

    Args:
        cn_name: 中文球队名
        sport: 'nba' 或 'football'

    Returns:
        特征数据名（小写），无映射时返回原词小写
    """
    _build_cn_mappings()
    key = cn_name.strip().lower()
    if sport == "nba":
        return _CN_NBA_TO_EN.get(key, key)
    return _CN_TO_FEAT.get(key, key)


def feat_name(odds_name: str) -> str:
    """取特征数据中的球队名（用于查找 fb_features.csv）。

    Args:
        odds_name: odds API 返回的球队名

    Returns:
        特征数据中的球队名（小写）
    """
    fn, _ = lookup_football(odds_name)
    return fn


def cn_to_odds_name(cn_name: str) -> str:
    """中文球队名 → Odds API 英文球队名。

    用于结算匹配：将 prediction_log 中的中文名转换为 Odds API 使用的英文名。

    Args:
        cn_name: 中文球队名

    Returns:
        Odds API 英文球队名（小写），无映射时返回原词小写
    """
    _build_cn_mappings()
    key = cn_name.strip().lower()
    return _CN_TO_ODDS_NAME.get(key, key)
