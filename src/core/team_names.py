"""团队名称映射 — odds API → 特征数据 → 中文显示名称。

特征数据名使用 fb_features.csv（含市场价值等完整特征）中的名称。
"""
from typing import Tuple, Optional

# 足球联赛中文名
LEAGUE_CN = {
    "soccer_epl": "英超",
    "soccer_spain_la_liga": "西甲",
    "soccer_germany_bundesliga": "德甲",
    "soccer_italy_serie_a": "意甲",
    "soccer_france_ligue_one": "法甲",
    "basketball_nba": "NBA",
    # 扩展联赛
    "soccer_brazil_campeonato": "巴甲",
    "soccer_copa_libertadores": "解放者杯",
    "soccer_usa_mls": "美职联",
    "soccer_mexico_liga_mx": "墨超",
    "soccer_argentina_primera_division": "阿甲",
    "soccer_portugal_primeira_liga": "葡超",
    "soccer_netherlands_eredivisie": "荷甲",
    "soccer_belgium_first_div": "比甲",
    "soccer_turkey_super_league": "土超",
    "soccer_scotland_premiership": "苏超",
    "soccer_japan_j_league": "J联赛",
    "soccer_australia_aleague": "澳超",
    "soccer_germany_bundesliga2": "德乙",
    "soccer_france_ligue_two": "法乙",
    "soccer_england_championship": "英冠",
    "soccer_england_league_one": "英甲",
    "soccer_switzerland_super_league": "瑞士超",
    "soccer_serbia_super_liga": "塞尔超",
    "soccer_croatia_prva_liga": "克甲",
    "soccer_greece_super_league": "希超",
    "soccer_copa_sudamericana": "南美杯",
    "soccer_uefa_champions_league": "欧冠",
    "soccer_uefa_europa_league": "欧联",
    "americanfootball_nfl": "NFL",
    "basketball_euroleague": "EuroLeague",
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
    # ===== MLS =====
    "Atlanta United FC": ("atlanta united fc", "亚特兰大联"),
    "Austin FC": ("austin fc", "奥斯汀"),
    "CF Montréal": ("cf montréal", "蒙特利尔"),
    "Charlotte FC": ("charlotte fc", "夏洛特"),
    "Chicago Fire FC": ("chicago fire fc", "芝加哥火焰"),
    "Colorado Rapids": ("colorado rapids", "科罗拉多急流"),
    "Columbus Crew": ("columbus crew", "哥伦布机员"),
    "D.C. United": ("dc united", "华盛顿联"),
    "FC Cincinnati": ("fc cincinnati", "辛辛那提"),
    "FC Dallas": ("fc dallas", "达拉斯"),
    "Houston Dynamo FC": ("houston dynamo fc", "休斯顿迪纳摩"),
    "Inter Miami CF": ("inter miami cf", "迈阿密国际"),
    "LA Galaxy": ("la galaxy", "洛杉矶银河"),
    "LAFC": ("lafc", "洛杉矶FC"),
    "Minnesota United FC": ("minnesota united fc", "明尼苏达联"),
    "Nashville SC": ("nashville sc", "纳什维尔"),
    "New England Revolution": ("new england revolution", "新英格兰革命"),
    "New York City FC": ("new york city fc", "纽约城"),
    "Orlando City SC": ("orlando city sc", "奥兰多城"),
    "Philadelphia Union": ("philadelphia union", "费城联合"),
    "Portland Timbers": ("portland timbers", "波特兰伐木者"),
    "Real Salt Lake": ("real salt lake", "皇家盐湖城"),
    "Red Bull New York": ("new york red bulls", "纽约红牛"),
    "San Diego FC": ("san diego fc", "圣迭戈"),
    "San Jose Earthquakes": ("san jose earthquakes", "圣何塞地震"),
    "Seattle Sounders FC": ("seattle sounders fc", "西雅图海湾人"),
    "Sporting Kansas City": ("sporting kansas city", "堪萨斯城竞技"),
    "St. Louis CITY SC": ("st louis city sc", "圣路易斯城"),
    "Toronto FC": ("toronto fc", "多伦多"),
    "Vancouver Whitecaps": ("vancouver whitecaps", "温哥华白帽"),
    # 别名
    "Inter Miami": ("inter miami cf", "迈阿密国际"),
    "NY Red Bulls": ("new york red bulls", "纽约红牛"),
    "New York RB": ("new york red bulls", "纽约红牛"),
    "New York Red Bulls": ("new york red bulls", "纽约红牛"),
    "DC United": ("dc united", "华盛顿联"),
    "Montreal Impact": ("cf montréal", "蒙特利尔"),
    "NE Revolution": ("new england revolution", "新英格兰革命"),
    # ===== J联赛 =====
    "Albirex Niigata": ("albirex niigata", "新潟天鹅"),
    "Avispa Fukuoka": ("avispa fukuoka", "福冈黄蜂"),
    "Cerezo Osaka": ("cerezo osaka", "大阪樱花"),
    "FC Tokyo": ("fc tokyo", "东京FC"),
    "Fagiano Okayama": ("fagiano okayama", "冈山绿雉"),
    "Gamba Osaka": ("gamba osaka", "大阪钢巴"),
    "Kashima Antlers": ("kashima antlers", "鹿岛鹿角"),
    "Kashiwa Reysol": ("kashiwa reysol", "柏太阳神"),
    "Kawasaki Frontale": ("kawasaki frontale", "川崎前锋"),
    "Kyoto Sanga": ("kyoto sanga", "京都不死鸟"),
    "Machida Zelvia": ("machida zelvia", "町田泽维亚"),
    "Nagoya Grampus": ("nagoya grampus", "名古屋鲸八"),
    "Sanfrecce Hiroshima": ("sanfrecce hiroshima", "广岛三箭"),
    "Shimizu S-Pulse": ("shimizu s-pulse", "清水心跳"),
    "Shonan Bellmare": ("shonan bellmare", "湘南海洋"),
    "Tokyo Verdy 1969": ("tokyo verdy 1969", "东京绿茵"),
    "Urawa Red Diamonds": ("urawa red diamonds", "浦和红钻"),
    "Vissel Kobe": ("vissel kobe", "神户胜利船"),
    "Yokohama F. Marinos": ("yokohama f. marinos", "横滨水手"),
    "Yokohama FC": ("yokohama fc", "横滨FC"),
    # ===== 土超 =====
    "Galatasaray": ("galatasaray", "加拉塔萨雷"),
    "Fenerbahçe": ("fenerbahçe", "费内巴切"),
    "Beşiktaş": ("beşiktaş", "贝西克塔斯"),
    "Trabzonspor": ("trabzonspor", "特拉布宗体育"),
    "Başakşehir": ("başakşehir", "巴萨克赛尔"),
    # ===== 比甲 =====
    "Club Brugge": ("club brugge", "布鲁日"),
    "RSC Anderlecht": ("rsc anderlecht", "安德莱赫特"),
    "KRC Genk": ("krc genk", "亨克"),
    "KAA Gent": ("kaa gent", "根特"),
    "Royal Antwerp": ("royal antwerp", "安特卫普"),
    "Standard Liège": ("standard liège", "标准列日"),
    # ===== 阿甲 =====
    "River Plate": ("river plate", "河床"),
    "Boca Juniors": ("boca juniors", "博卡青年"),
    "Independiente": ("independiente", "独立"),
    "Racing Club": ("racing club", "竞技"),
    "San Lorenzo": ("san lorenzo", "圣洛伦索"),
    # ===== 巴甲补充 =====
    "Flamengo": ("flamengo", "弗拉门戈"),
    "Palmeiras": ("palmeiras", "帕尔梅拉斯"),
    "Santos": ("santos", "桑托斯"),
    "São Paulo": ("são paulo", "圣保罗"),
    "Corinthians": ("corinthians", "科林蒂安"),
    # ===== 巴甲扩展 (2026 赛季) =====
    "Atletico Mineiro": ("atletico mineiro", "米内罗竞技"),
    "Atletico Paranaense": ("atletico paranaense", "巴拉纳竞技"),
    "Bahia": ("bahia", "巴伊亚"),
    "Botafogo": ("botafogo", "博塔弗戈"),
    "Bragantino-SP": ("bragantino sp", "布拉甘蒂诺"),
    "Chapecoense": ("chapecoense", "沙佩科恩斯"),
    "Coritiba": ("coritiba", "科里蒂巴"),
    "Cruzeiro": ("cruzeiro", "克鲁塞罗"),
    "Fluminense": ("fluminense", "弗鲁米嫩塞"),
    "Grêmio": ("grêmio", "格雷米奥"),
    "Internacional": ("internacional", "巴西国际"),
    "Mirassol": ("mirassol", "米拉索尔"),
    "Remo": ("remo", "雷莫"),
    "Sao Paulo": ("são paulo", "圣保罗"),
    "Vasco da Gama": ("vasco da gama", "瓦斯科达伽马"),
    "Vitoria": ("vitoria", "维多利亚"),
    "Birmingham Legion FC": ("birmingham legion fc", "伯明翰军团"),
    "Louisville City FC": ("louisville city fc", "路易维尔城"),
    # ===== 摩洛哥联赛 =====
    "Ittihad Tanger": ("ittihad tanger", "伊蒂哈德丹吉尔"),
    "RS Berkane": ("rs berkane", "贝尔卡内"),
    "Raja Club Athletic": ("raja club athletic", "拉贾卡萨布兰卡"),
    "Renaissance Zemamra": ("renaissance zemamra", "泽马马拉"),
    "Union Sportive Yacoub El Mansour": ("union sportive yacoub el mansour", "雅各布曼苏尔"),
    "Wydad Casablanca": ("wydad casablanca", "维达德卡萨布兰卡"),
    # ===== 常用补充（2026-06-23）=====
    "Türkiye": ("türkiye", "土耳其"),
    "Miami FC": ("miami fc", "迈阿密FC"),
    "Orange County SC": ("orange county sc", "橙县SC"),
    "Tunisia": ("tunisia", "突尼斯"),
    "Morocco": ("morocco", "摩洛哥"),
    "Croatia": ("croatia", "克罗地亚"),
    "Portugal": ("portugal", "葡萄牙"),
    "Uzbekistan": ("uzbekistan", "乌兹别克斯坦"),
    "Japan": ("japan", "日本"),
    "Colombia": ("colombia", "哥伦比亚"),
    "Ecuador": ("ecuador", "厄瓜多尔"),
    "Germany": ("germany", "德国"),
    "Scotland": ("scotland", "苏格兰"),
    "Brazil": ("brazil", "巴西"),
    "South Africa": ("south africa", "南非"),
    "Switzerland": ("switzerland", "瑞士"),
    "Canada": ("canada", "加拿大"),
    "England": ("england", "英格兰"),
    "Ghana": ("ghana", "加纳"),
    "Paraguay": ("paraguay", "巴拉圭"),
    "Australia": ("australia", "澳大利亚"),
    "Czech Republic": ("czech republic", "捷克"),
    "Curaçao": ("curaçao", "库拉索"),
    "Qatar": ("qatar", "卡塔尔"),
    "Bosnia & Herzegovina": ("bosnia & herzegovina", "波黑"),
    "Haiti": ("haiti", "海地"),
    "Honduras": ("honduras", "洪都拉斯"),
    "Costa Rica": ("costa rica", "哥斯达黎加"),
    "Jordan": ("jordan", "约旦"),
    "Finland": ("finland", "芬兰"),
    "Iceland": ("iceland", "冰岛"),
    "North Macedonia": ("north macedonia", "北马其顿"),
    "Slovakia": ("slovakia", "斯洛伐克"),
    "Romania": ("romania", "罗马尼亚"),
    "Montenegro": ("montenegro", "黑山"),
    "Wales": ("wales", "威尔士"),
    "Faroe Islands": ("faroe islands", "法罗群岛"),
    "Kazakhstan": ("kazakhstan", "哈萨克斯坦"),
    "Bulgaria": ("bulgaria", "保加利亚"),
    "Estonia": ("estonia", "爱沙尼亚"),
    "Georgia": ("georgia", "格鲁吉亚"),
    "Belarus": ("belarus", "白俄罗斯"),
    "Armenia": ("armenia", "亚美尼亚"),
    "China": ("china", "中国"),
    # ===== 中超（2026赛季） =====
    "Cabo Verde": ("cabo verde", "佛得角"),
    "Chengdu Rongcheng": ("chengdu rongcheng", "成都蓉城"),
    "Chongqing Tonglianglong FC": ("chongqing tonglianglong", "重庆铜梁龙"),
    "Henan FC": ("henan fc", "河南"),
    "Shanghai Port": ("shanghai port", "上海海港"),
    "Shenzhen Peng City": ("shenzhen peng city", "深圳鹏城"),
    "Tianjin Jinmen Tiger": ("tianjin jinmen tiger", "天津津门虎"),
    "Dalian Yingbo FC": ("dalian yingbo", "大连英博"),
    "Shanghai Shenhua": ("shanghai shenhua", "上海申花"),
    "Wuhan Three Towns": ("wuhan three towns", "武汉三镇"),
    "Shandong Taishan": ("shandong taishan", "山东泰山"),
    "Beijing Guoan": ("beijing guoan", "北京国安"),
    "Liaoning Tieren FC": ("liaoning tieren", "辽宁铁人"),
    # ===== 瑞典超（Allsvenskan）=====
    "Kalmar FF": ("kalmar ff", "卡尔马"),
    "Örgryte IS": ("örgryte is", "厄格里特"),
    "IFK Göteborg": ("ifk göteborg", "哥德堡"),
    "AIK": ("aik", "索尔纳"),
    "AIK Stockholm": ("aik", "索尔纳"),
    "Degerfors IF": ("degerfors if", "代格福什"),
    "Malmö FF": ("malmö ff", "马尔默"),
    "Djurgårdens IF": ("djurgårdens if", "尤尔加登"),
    "Hammarby IF": ("hammarby if", "哈马比"),
    "IFK Norrköping": ("ifk norrköping", "北雪平"),
    "BK Häcken": ("bk häcken", "赫根"),
    "IF Elfsborg": ("if elfsborg", "埃尔夫斯堡"),
    "IFK Värnamo": ("ifk värnamo", "韦纳穆"),
    "Mjällby AIF": ("mjällby aif", "米亚尔比"),
    "GAIS": ("gais", "加尔斯"),
    "IF Brommapojkarna": ("if brommapojkarna", "布洛马波卡纳"),
    # ===== K联赛 =====
    "Ulsan Hyundai": ("ulsan hyundai", "蔚山现代"),
    "Jeonbuk Hyundai Motors": ("jeonbuk hyundai motors", "全北现代"),
    "FC Seoul": ("fc seoul", "首尔FC"),
    "Suwon Samsung Bluewings": ("suwon samsung bluewings", "水原三星"),
    "Pohang Steelers": ("pohang steelers", "浦项铁人"),
    "Daegu FC": ("daegu fc", "大邱FC"),
    "Incheon United": ("incheon united", "仁川联"),
    "Jeju United": ("jeju united", "济州联"),
    "Gwangju FC": ("gwangju fc", "光州FC"),
    "Gangwon FC": ("gangwon fc", "江原FC"),
    "Daejeon Hana Citizen": ("daejeon hana citizen", "大田韩亚市民"),
    "Suwon FC": ("suwon fc", "水原FC"),
    "FC Anyang": ("fc anyang", "安阳FC"),
    "Gimcheon Sangmu": ("gimcheon sangmu", "金泉尚武"),
    # ===== 瑞典超补充 =====
    "Halmstads BK": ("halmstads bk", "哈尔姆斯塔德"),
    "Västerås SK": ("västerås sk", "韦斯特罗斯"),
    # ===== 芬超 =====
    "FC Lahti": ("fc lahti", "拉赫蒂"),
    "IF Gnistan": ("if gnistan", "吉尼斯坦"),
    "HJK Helsinki": ("hjk helsinki", "赫尔辛基"),
    "SJK Seinäjoki": ("sjk seinäjoki", "塞伊奈约基"),
    "FC Inter Turku": ("fc inter turku", "图尔库国际"),
    "FC Haka": ("fc haka", "哈卡"),
    "FC Ilves": ("fc ilves", "伊尔韦斯"),
    "FC Honka": ("fc honka", "洪卡"),
    "VPS Vaasa": ("vps vaasa", "瓦萨"),
    "KuPS Kuopio": ("kups kuopio", "库普斯"),
    "AC Oulu": ("ac oulu", "奥卢"),
    # ===== 昆士兰超（NPL Queensland）=====
    "Moreton City Excelsior FC": ("moreton city excelsior fc", "莫尔顿城"),
    "Magic United TFA": ("magic united tfa", "魔术联"),
    "Brisbane Roar Youth": ("brisbane roar youth", "布里斯班狮吼青年"),
    "Rochedale Rovers": ("rochedale rovers", "罗奇代尔流浪者"),
    "Peninsula Power": ("peninsula power", "半岛力量"),
    "Wynnum Wolves FC": ("wynnum wolves fc", "温纳姆狼队"),
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

# WNBA 球队中文名
BB_CN = {
    "Atlanta Dream": "亚特兰大梦想",
    "Chicago Sky": "芝加哥天空",
    "Connecticut Sun": "康涅狄格太阳",
    "Dallas Wings": "达拉斯飞翼",
    "Indiana Fever": "印第安纳狂热",
    "Las Vegas Aces": "拉斯维加斯王牌",
    "Los Angeles Sparks": "洛杉矶火花",
    "Minnesota Lynx": "明尼苏达山猫",
    "New York Liberty": "纽约自由人",
    "Phoenix Mercury": "菲尼克斯水星",
    "Seattle Storm": "西雅图风暴",
    "Toronto Tempo": "多伦多节奏",
    "Washington Mystics": "华盛顿神秘人",
    "Golden State Valkyries": "金州女武神",
    "Portland Fire": "波特兰火焰",
}

# MLB 球队中文名
MLB_CN = {
    "Arizona Diamondbacks": "亚利桑那响尾蛇",
    "Atlanta Braves": "亚特兰大勇士",
    "Baltimore Orioles": "巴尔的摩金莺",
    "Boston Red Sox": "波士顿红袜",
    "Chicago Cubs": "芝加哥小熊",
    "Chicago White Sox": "芝加哥白袜",
    "Cincinnati Reds": "辛辛那提红人",
    "Cleveland Guardians": "克利夫兰守护者",
    "Colorado Rockies": "科罗拉多洛基",
    "Detroit Tigers": "底特律老虎",
    "Houston Astros": "休斯顿太空人",
    "Kansas City Royals": "堪萨斯城皇家",
    "Los Angeles Angels": "洛杉矶天使",
    "Los Angeles Dodgers": "洛杉矶道奇",
    "Miami Marlins": "迈阿密马林鱼",
    "Milwaukee Brewers": "密尔沃基酿酒人",
    "Minnesota Twins": "明尼苏达双城",
    "New York Mets": "纽约大都会",
    "New York Yankees": "纽约洋基",
    "Oakland Athletics": "奥克兰运动家",
    "Philadelphia Phillies": "费城费城人",
    "Pittsburgh Pirates": "匹兹堡海盗",
    "San Diego Padres": "圣迭戈教士",
    "San Francisco Giants": "旧金山巨人",
    "Seattle Mariners": "西雅图水手",
    "St. Louis Cardinals": "圣路易斯红雀",
    "Tampa Bay Rays": "坦帕湾光芒",
    "Texas Rangers": "得克萨斯游骑兵",
    "Toronto Blue Jays": "多伦多蓝鸟",
    "Washington Nationals": "华盛顿国民",
}

# NPB 日本棒球队中文名
NPB_CN = {
    "Chiba Lotte Marines": "千叶罗德海洋",
    "Chunichi Dragons": "中日龙",
    "Fukuoka SoftBank Hawks": "福冈软银鹰",
    "Hanshin Tigers": "阪神虎",
    "Hiroshima Toyo Carp": "广岛东洋鲤鱼",
    "Hokkaido Nippon-Ham Fighters": "北海道日本火腿斗士",
    "Orix Buffaloes": "欧力士野牛",
    "Saitama Seibu Lions": "埼玉西武狮",
    "Tohoku Rakuten Golden Eagles": "东北乐天金鹫",
    "Tokyo Yakult Swallows": "东京养乐多燕子",
    "Yokohama DeNA BayStars": "横滨DeNA海湾之星",
    "Yomiuri Giants": "读卖巨人",
}

# 网球球员中文名（按发现的球员逐步添加）
TENNIS_CN = {
    # ATP 温网 2026
    "Flavio Cobolli": "弗拉维奥·科博利",
    "Arthur Fery": "阿瑟·费里",
    "Taylor Fritz": "泰勒·弗里茨",
    "Alexander Zverev": "亚历山大·兹维列夫",
    "Jannik Sinner": "扬尼克·辛纳",
    "Novak Djokovic": "诺瓦克·德约科维奇",
    # WTA 温网 2026
    "Linda Noskova": "琳达·诺斯科娃",
    "Elise Mertens": "爱丽丝·梅尔滕斯",
    "Marta Kostyuk": "玛尔塔·科斯丘克",
    "Jasmine Paolini": "贾斯明·保利尼",
    "Karolina Muchova": "卡罗利娜·穆霍娃",
}


# 足球球队名标准化：预处理 odds API 名称提高命中率
_NORMALIZE_RULES = [
    (lambda n: n.replace('_', ' ').strip(), '下划线转空格'),
]


def _normalize(name: str) -> str:
    """标准化球队名以提高映射命中率。"""
    n = name.strip()
    # 下划线→空格
    n = n.replace('_', ' ')
    # 多个空格→一个
    n = ' '.join(n.split())
    return n


# 球队名变体映射（odds API 不同命名风格 → FOOTBALL_MAP 中的标准名）
_NAME_ALIASES = {
    # 德甲变体 (ö→oe, ü→ue 等)
    'FC Bayern München': 'FC Bayern Munich',
    'Mönchengladbach': 'Mönchengladbach',
    # 意甲变体
    'FC Internazionale Milano': 'Inter Milan',
    'SSC Napoli': 'Napoli',
    # 法甲变体 (空格→短横)
    'Paris Saint Germain': 'Paris Saint-Germain',
    'Saint Etienne': 'Saint-Étienne',
    # MLS/新增联赛
    'Inter Miami CF': 'Inter Miami',
    # 通用: 去掉 FC/CF 后缀
}


def _fuzzy_lookup(name: str) -> Optional[Tuple[str, str]]:
    """模糊查找：尝试多种变体。"""
    # 1. 精确匹配
    mapped = FOOTBALL_MAP.get(name)
    if mapped:
        return mapped

    # 2. 别名映射
    aliased = _NAME_ALIASES.get(name)
    if aliased:
        mapped = FOOTBALL_MAP.get(aliased)
        if mapped:
            return mapped

    # 3. 去掉常见后缀再试
    for suffix in (' FC', ' CF', ' SCC', ' BC', ' AC', ' OSC', ' HSC', ' SCC'):
        if name.endswith(suffix):
            base = name[:-len(suffix)].strip()
            mapped = FOOTBALL_MAP.get(base)
            if mapped:
                return mapped
            # title case
            mapped = FOOTBALL_MAP.get(base.title())
            if mapped:
                return mapped

    # 4. 去掉常见前缀再试
    for prefix in ('FC ', 'RC ', 'SC ', 'US ', 'AC ', 'SSC ', 'TSG ', 'SV ',
                   'VfL ', 'VfB ', '1. FC ', 'FSV '):
        if name.startswith(prefix):
            base = name[len(prefix):].strip()
            mapped = FOOTBALL_MAP.get(base)
            if mapped:
                return mapped

    # 5. 标准化短横: "Saint-Germain" ↔ "Saint Germain"
    for sep_from, sep_to in [('-', ' '), (' ', '-')]:
        alt = name.replace(sep_from, sep_to)
        if alt != name:
            mapped = FOOTBALL_MAP.get(alt)
            if mapped:
                return mapped

    # 6. 小写匹配（忽略大小写差异）
    name_lower = name.lower()
    for key in FOOTBALL_MAP:
        if name_lower == key.lower():
            return FOOTBALL_MAP[key]

    return None


# ── 世界杯国家队中文名 ──
WC_CN = {
    "Algeria": "阿尔及利亚", "Argentina": "阿根廷", "Australia": "澳大利亚",
    "Austria": "奥地利", "Belgium": "比利时", "Bosnia & Herzegovina": "波黑",
    "Brazil": "巴西", "Canada": "加拿大", "Cape Verde": "佛得角",
    "Colombia": "哥伦比亚", "Croatia": "克罗地亚", "Curaçao": "库拉索",
    "Czech Republic": "捷克", "DR Congo": "刚果金", "Ecuador": "厄瓜多尔",
    "Egypt": "埃及", "England": "英格兰", "France": "法国",
    "Germany": "德国", "Ghana": "加纳", "Haiti": "海地",
    "Iran": "伊朗", "Iraq": "伊拉克", "Ivory Coast": "科特迪瓦",
    "Japan": "日本", "Jordan": "约旦", "Mexico": "墨西哥",
    "Morocco": "摩洛哥", "Netherlands": "荷兰", "New Zealand": "新西兰",
    "Norway": "挪威", "Panama": "巴拿马", "Paraguay": "巴拉圭",
    "Portugal": "葡萄牙", "Qatar": "卡塔尔", "Saudi Arabia": "沙特",
    "Scotland": "苏格兰", "Senegal": "塞内加尔", "South Africa": "南非",
    "South Korea": "韩国", "Spain": "西班牙", "Sweden": "瑞典",
    "Switzerland": "瑞士", "Tunisia": "突尼斯", "Turkey": "土耳其",
    "USA": "美国", "Uruguay": "乌拉圭", "Uzbekistan": "乌兹别克斯坦",
    "Qingdao Hainiu": "青岛海牛", "Yunnan Yukun": "云南玉昆",
    "Cabo Verde": "佛得角", "Cape Verde": "佛得角",
    "Dalian Yingbo FC": "大连英博", "Shanghai Shenhua": "上海申花",
    "Wuhan Three Towns": "武汉三镇", "Shandong Taishan": "山东泰山",
    "Beijing Guoan": "北京国安", "Liaoning Tieren FC": "辽宁铁人",
    "Dynamo Kyiv": "基辅迪纳摩",
    "FC Universitatea Cluj": "克卢日大学",
}


def lookup_football(odds_name: str) -> Tuple[str, str]:
    """查询足球球队映射（支持模糊匹配）。

    Args:
        odds_name: odds API 返回的球队名

    Returns:
        (特征数据名, 中文名) 元组，若无映射则返回 (原词小写, 原词)
    """
    name = _normalize(odds_name)
    mapped = _fuzzy_lookup(name)
    if mapped:
        return mapped
    return (name.lower(), odds_name)


def cn_team(odds_name: str, sport: str = "nba") -> str:
    """取球队中文名。

    Args:
        odds_name: odds API 返回的球队名
        sport: 'nba', 'basketball', 'football', 'baseball', 'tennis'

    Returns:
        中文名，若无映射则返回原词
    """
    if sport in ("nba", "basketball", "wnba", "WNBA"):
        cn = BB_CN.get(odds_name)
        if cn:
            return cn
        return NBA_CN.get(odds_name, odds_name)
    if sport in ("baseball", "mlb", "MLB"):
        cn = MLB_CN.get(odds_name)
        if cn:
            return cn
        return NPB_CN.get(odds_name, odds_name)
    if sport in ("tennis",):
        return TENNIS_CN.get(odds_name, odds_name)
    _, cn = lookup_football(odds_name)
    if cn == odds_name:
        return WC_CN.get(odds_name, odds_name)
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
