"""虚拟投注自动结算 — 多数据源（ESPN + football-data.org + 直播吧）自动结算。"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import requests

from config.logging_config import get_logger
from src.dashboard.components.virtual_portfolio import (
    _load_state, _save_state, settle_bet,
)
from src.core.team_names import cn_to_odds_name
from fetchers.multi_source_scores import get_completed_scores
from src.risk.manager import RiskManager
from src.betting.strategy_optimizer import SettlementLogger
from src.risk.calibration import BetCalibrator

logger = get_logger(__name__)

# 联赛名 → (sport key for odds API, display name)
# 同时支持 BB API 全称中文名（如"英格兰超级联赛"）和简称（如"英超"）
LEAGUE_SPORT_MAP = {
    "NBA": ("basketball_nba", "NBA"),
    "英超": ("soccer_epl", "英超"),
    "英格兰超级联赛": ("soccer_epl", "英超"),
    "西甲": ("soccer_spain_la_liga", "西甲"),
    "西班牙甲级联赛": ("soccer_spain_la_liga", "西甲"),
    "德甲": ("soccer_germany_bundesliga", "德甲"),
    "德国甲级联赛": ("soccer_germany_bundesliga", "德甲"),
    "意甲": ("soccer_italy_serie_a", "意甲"),
    "意大利甲级联赛": ("soccer_italy_serie_a", "意甲"),
    "法甲": ("soccer_france_ligue_one", "法甲"),
    "法国甲级联赛": ("soccer_france_ligue_one", "法甲"),
    # 扩展
    "巴甲": ("soccer_brazil_campeonato", "巴甲"),
    "巴西甲级联赛": ("soccer_brazil_campeonato", "巴甲"),
    "解放者杯": ("soccer_copa_libertadores", "解放者杯"),
    "南美解放者杯": ("soccer_copa_libertadores", "解放者杯"),
    "美职联": ("soccer_usa_mls", "美职联"),
    "美国职业大联盟": ("soccer_usa_mls", "美职联"),
    "墨超": ("soccer_mexico_liga_mx", "墨超"),
    "墨西哥超级联赛": ("soccer_mexico_liga_mx", "墨超"),
    "阿甲": ("soccer_argentina_primera_division", "阿甲"),
    "阿根廷甲级联赛": ("soccer_argentina_primera_division", "阿甲"),
    "葡超": ("soccer_portugal_primeira_liga", "葡超"),
    "葡萄牙超级联赛": ("soccer_portugal_primeira_liga", "葡超"),
    "荷甲": ("soccer_netherlands_eredivisie", "荷甲"),
    "荷兰甲级联赛": ("soccer_netherlands_eredivisie", "荷甲"),
    "比甲": ("soccer_belgium_first_div", "比甲"),
    "比利时甲级联赛": ("soccer_belgium_first_div", "比甲"),
    "土超": ("soccer_turkey_super_league", "土超"),
    "土耳其超级联赛": ("soccer_turkey_super_league", "土超"),
    "苏超": ("soccer_scotland_premiership", "苏超"),
    "苏格兰超级联赛": ("soccer_scotland_premiership", "苏超"),
    "J联赛": ("soccer_japan_j_league", "J联赛"),
    "日本职业联赛": ("soccer_japan_j_league", "J联赛"),
    "澳超": ("soccer_australia_aleague", "澳超"),
    "澳洲甲级联赛": ("soccer_australia_aleague", "澳超"),
    "德乙": ("soccer_germany_bundesliga2", "德乙"),
    "德国乙级联赛": ("soccer_germany_bundesliga2", "德乙"),
    "法乙": ("soccer_france_ligue_two", "法乙"),
    "法国乙级联赛": ("soccer_france_ligue_two", "法乙"),
    "英冠": ("soccer_england_championship", "英冠"),
    "英格兰冠军联赛": ("soccer_england_championship", "英冠"),
    "欧冠": ("soccer_uefa_champions_league", "欧冠"),
    "欧洲冠军联赛": ("soccer_uefa_champions_league", "欧冠"),
    "欧洲冠军联赛-资格赛": ("soccer_uefa_champions_league", "欧冠"),
    "欧联": ("soccer_uefa_europa_league", "欧联"),
    "欧足联欧洲联赛": ("soccer_uefa_europa_league", "欧联"),
    "欧足联欧洲联赛-资格赛": ("soccer_uefa_europa_league", "欧联"),
    "欧足联欧洲协会联赛": ("soccer_uefa_conference_league", "欧协联"),
    "欧足联欧洲协会联赛-资格赛": ("soccer_uefa_conference_league", "欧协联"),
    "NFL": ("americanfootball_nfl", "NFL"),
    "EuroLeague": ("basketball_euroleague", "EuroLeague"),
    "欧洲篮球联赛": ("basketball_euroleague", "EuroLeague"),
    "世界杯": ("soccer_fifa_world_cup", "世界杯"),
    "World Cup 2026": ("soccer_fifa_world_cup", "世界杯"),
    "WNBA": ("basketball_wnba", "WNBA"),
    "西乙": ("soccer_spain_segunda_division", "西乙"),
    "西班牙乙级联赛": ("soccer_spain_segunda_division", "西乙"),
    "巴乙": ("soccer_brazil_serie_b", "巴乙"),
    "巴西乙级联赛": ("soccer_brazil_serie_b", "巴乙"),
    "英甲": (None, "英甲"),  # ESPN only, no Odds API sport key
    "英格兰甲级联赛": (None, "英甲"),
    "英乙": (None, "英乙"),
    "英格兰乙级联赛": (None, "英乙"),
    "意乙": (None, "意乙"),
    "意大利乙级联赛": (None, "意乙"),
    "中超": ("soccer_china_superleague", "中超"),
    "Chinese Super League": ("soccer_china_superleague", "中超"),
    "瑞典超": ("soccer_sweden_allsvenskan", "瑞典超"),
    "瑞典超级联赛": ("soccer_sweden_allsvenskan", "瑞典超"),
    "挪威超": ("soccer_norway_eliteserien", "挪威超"),
    "超级挪威联赛": ("soccer_norway_eliteserien", "挪威超"),
    "芬超": ("soccer_finland_veikkausliiga", "芬超"),
    "芬兰超级联赛": ("soccer_finland_veikkausliiga", "芬超"),
    "芬兰甲级联赛": ("soccer_finland_ykkosliiga", "芬甲"),
    "爱超": ("soccer_league_of_ireland", "爱超"),
    "爱尔兰超级联赛": ("soccer_league_of_ireland", "爱超"),
    "瑞典甲": ("soccer_sweden_superettan", "瑞典甲"),
    "瑞典甲级联赛": ("soccer_sweden_superettan", "瑞典甲"),
    "南美杯": ("soccer_conmebol_copa_sudamericana", "南美杯"),
    "乌拉圭甲级联赛": (None, "乌拉圭甲级联赛"),
    "乌拉圭乙级联赛": (None, "乌拉圭乙级联赛"),
    "俄罗斯甲级联赛": (None, "俄罗斯甲级联赛"),
    "俄罗斯超级联赛": (None, "俄罗斯超级联赛"),
    "巴拉圭甲级联赛": (None, "巴拉圭甲级联赛"),
    "巴拉圭乙级联赛": (None, "巴拉圭乙级联赛"),
    "哈萨克斯坦超级联赛": (None, "哈萨克斯坦超级联赛"),
    "白俄罗斯超级联赛": (None, "白俄罗斯超级联赛"),
    "冰岛甲级联赛": (None, "冰岛甲级联赛"),
    "爱沙尼亚甲级联赛": (None, "爱沙尼亚甲级联赛"),
    "苏格兰联赛杯": (None, "苏格兰联赛杯"),
    "马来西亚总统杯 U20": (None, "马来西亚总统杯 U20"),
    "韩国足协杯": (None, "韩国足协杯"),
    "澳大利亚杯": (None, "澳大利亚杯"),
    "厄瓜多尔甲级联赛": (None, "厄瓜多尔甲级联赛"),
    "罗马尼亚甲级联赛": (None, "罗马尼亚甲级联赛"),
    "阿根廷全国联赛": (None, "阿根廷全国联赛"),
    "英格兰联赛杯": (None, "英格兰联赛杯"),
    "澳门甲级联赛": (None, "澳门甲级联赛"),
    # 新增 BB API 联赛映射
    "保加利亚甲级联赛": ("soccer_bulgaria_first_league", "保甲"),
    "韩国K1联赛": ("soccer_korea_k_league", "K1联赛"),
    "罗马尼亚甲级联赛": ("soccer_romania_liga_1", "罗甲"),
    "阿根廷全国联赛": ("soccer_argentina_primera_b_nacional", "阿全国联"),
    "阿根廷甲级联赛": ("soccer_argentina_primera_division", "阿甲"),
    "挪威超级联赛": ("soccer_norway_eliteserien", "挪超"),
    "瑞士超级联赛": ("soccer_switzerland_super_league", "瑞士超"),
    "奥地利甲级联赛": ("soccer_austria_bundesliga", "奥甲"),
    "丹麦超级联赛": ("soccer_denmark_superliga", "丹超"),
    "捷克甲级联赛": ("soccer_czech_first_league", "捷甲"),
    "克罗地亚甲级联赛": ("soccer_croatia_first_league", "克甲"),
    "塞尔维亚超级联赛": ("soccer_serbia_super_league", "塞超"),
    "波兰甲级联赛": ("soccer_poland_ekstraklasa", "波甲"),
    "英格兰联赛杯": ("soccer_england_league_cup", "英联赛杯"),
    "球会友谊赛": (None, "球会友谊赛"),
    "美国冠军联赛": ("soccer_usa_usl_championship", "美冠联"),
    "澳大利亚杯": ("soccer_australia_cup", "澳洲杯"),
    "厄瓜多尔甲级联赛": ("soccer_ecuador_serie_a", "厄甲"),
    "巴西杯": ("soccer_brazil_cup", "巴西杯"),
    "南美俱乐部杯": ("soccer_conmebol_copa_sudamericana", "南美杯"),
    "南美解放者杯": ("soccer_copa_libertadores", "解放者杯"),
    # 网球 — 无 ESPN 覆盖，纯超时作废
    "ATP - 格施塔德公开赛 - 双打": (None, "ATP格施塔德双打"),
    "ATP挑战赛 - 林肯公开赛": (None, "ATP挑战赛林肯"),
    "ITF - M15 乌斯拉尔男子單打": (None, "ITF M15乌斯拉尔"),
    "ITF - M15 乌斯拉尔男子单打": (None, "ITF M15乌斯拉尔"),
    "ITF - M15 罗切斯特男子单打": (None, "ITF M15罗切斯特"),
    "ITF - M25 克拉姆萨赫 男子单打": (None, "ITF M25克拉姆萨赫"),
    "ITF - M25 布里斯班 男子单打": (None, "ITF M25布里斯班单打"),
    "ITF - M25 布里斯班 男子双打": (None, "ITF M25布里斯班双打"),
    "ITF - M25 希尔克雷斯特 男子单打": (None, "ITF M25希尔克雷斯特"),
    "ITF - M25 路易斯维尔 男子单打": (None, "ITF M25路易斯维尔"),
    "ITF - W15 库尔索姆利斯卡班亚 女子单打": (None, "ITF W15库尔索姆利斯卡"),
    "ITF - W35 圣保罗 女子双打": (None, "ITF W35圣保罗双打"),
    "ITF - W50 达姆施塔特 女子单打": (None, "ITF W50达姆施塔特"),
    "ITF - W75 奥洛穆茨 女子单打": (None, "ITF W75奥洛穆茨"),
    "ITF - M15 新戈里卡 男子单打": (None, "ITF M15新戈里卡"),
    # 篮球 — 无 ESPN 覆盖
    "新西兰全国篮球联赛": (None, "NZ NBL"),
    "菲律宾PBA总督杯": (None, "菲律宾PBA"),
    "FIBA欧洲篮球A级锦标赛 U20": (None, "FIBA U20"),
    "危地马拉大都会篮球联赛": (None, "危地马拉篮球"),
    # 澳大利亚低级别足球 — 无 ESPN 覆盖
    "澳大利亚新南威尔士甲级联赛U20": (None, "澳新南U20"),
    "澳大利亚新南威尔士州北部全国超级联赛": (None, "澳北超"),
    "澳大利亚维多利亚州全国超级联赛": (None, "澳维超"),
    "澳大利亚维多利亚州超级联赛 1": (None, "澳维甲1"),
    "澳大利亚维多利亚州超级联赛 2": (None, "澳维甲2"),
    # 其他足球联赛
    "阿根廷杯": ("soccer_argentina_cup", "阿根廷杯"),
    # 网球 — ITF 赛事别名（BB API vs 推送显示不同）
    "世界网球 - M15 乌斯拉尔 男子單打": (None, "ITF M15乌斯拉尔"),
    "世界网球 - M25 希尔克雷斯特 男子单打": (None, "ITF M25希尔克雷斯特"),
    # 棒球
    "墨西哥棒球联盟": (None, "墨西哥棒球"),
    # 冰岛联赛 — 极小众，无 ESPN 覆盖，超时作废
    "冰岛超级联赛": (None, "冰岛超"),
    "冰岛超级联赛女子": (None, "冰岛超女"),
    # 棒球 — 无 ESPN 覆盖
    "日本职业棒球": (None, "日本职棒"),
    # 足球小联赛
    "澳大利亚新南威尔士州北部全国超级联赛": (None, "澳北超"),
    "瑞典超甲级联赛": (None, "瑞典超甲"),
}

# 兜底：sport 字段 → sport key（精确匹配）
SPORT_FALLBACK = {
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "nfl": "americanfootball_nfl",
    "football": None,  # 需要由 league 决定
}


def _fetch_completed_scores_espn(league: str, days_back: int = 3) -> list:
    """从 ESPN 免费 API 获取已结束比赛的比分（无配额限制）。"""
    espn_games = fetch_espn_scores(league, days_back)
    if not espn_games:
        logger.debug("ESPN 无 %s 比分数据", league)
        return []
    # 转换为与 Odds API 兼容的格式
    odds_format = []
    for g in espn_games:
        home_score = g.get("home_score", 0)
        away_score = g.get("away_score", 0)
        odds_format.append({
            "home_team": g["home_team"],
            "away_team": g["away_team"],
            "completed": g.get("completed", True),
            "scores": [
                {"name": g["home_team"], "score": str(home_score)},
                {"name": g["away_team"], "score": str(away_score)},
            ],
            "home_corners": g.get("home_corners"),
            "away_corners": g.get("away_corners"),
        })
    return odds_format


def _sport_key_to_espn_league(sport_key: str) -> Optional[str]:
    """将 Odds API sport_key 转为 ESPN 联赛名。

    通过反向遍历 LEAGUE_SPORT_MAP 找到对应的 ESPN 联赛名。
    """
    # Method 1: 通过 LEAGUE_SPORT_MAP 反查
    for bb_league, (sk, _) in LEAGUE_SPORT_MAP.items():
        if sk == sport_key and bb_league in LEAGUE_ESPN_PATH:
            return bb_league
    # Method 2: 直接路径匹配
    for lname, (spath, _) in LEAGUE_ESPN_PATH.items():
        if sport_key.replace("_", "/") == spath:
            return lname
    # Method 3: 硬编码兜底（旧的 sport_key 命名）
    _HARDCODED = {
        "basketball_nba": "NBA", "soccer_epl": "英超",
        "soccer_spain_la_liga": "西甲", "soccer_germany_bundesliga": "德甲",
        "soccer_italy_serie_a": "意甲", "soccer_france_ligue_one": "法甲",
        "soccer_fifa_world_cup": "世界杯",
        "basketball_wnba": "WNBA",
        "soccer_spain_segunda_division": "西乙",
        "soccer_brazil_serie_b": "巴乙",
        "soccer_china_superleague": "中超",
        "soccer_sweden_allsvenskan": "瑞典超",
        "soccer_norway_eliteserien": "挪威超",
        "soccer_chile_campeonato": "智利甲",
        "soccer_finland_veikkausliiga": "芬超",
        "soccer_league_of_ireland": "爱超",
        "soccer_sweden_superettan": "瑞典甲",
        "soccer_germany_dfb_pokal": "德杯",
        "soccer_conmebol_copa_sudamericana": "南美杯",
    }
    return _HARDCODED.get(sport_key)


def _fetch_completed_scores(league_name: str, days_back: int = 3) -> list:
    """多源获取已结束比赛比分。"""
    scores = get_completed_scores(league_name, days_back)
    if scores:
        logger.info("  已完成: %s 场", len(scores))
    return scores


def _normalize_team(name) -> str:
    """归一化队名以便匹配（去除拉丁口音符号，保留CJK字符）。"""
    if not isinstance(name, str):
        return ""
    import re as _re
    import unicodedata
    # 只剥离组合变音符号（é→e, ñ→n），不影响 CJK 字符
    name = ''.join(c for c in unicodedata.normalize('NFKD', name) if not unicodedata.combining(c))
    name = name.strip().lower()
    # 只移除独立单词的 fc/cf（不破坏 LAFC、布伦瑞克城FC 等名字）
    name = _re.sub(r'\bfc\b', '', name)
    name = _re.sub(r'\bcf\b', '', name)
    return name.strip()


# 常见队名昵称/缩写映射（fuzzy 太不可控，用精确别名代替）
_TEAM_ALIASES = {
    "mancity": "manchester city",
    "man utd": "manchester united",
    "manu": "manchester united",
    "barca": "barcelona",
    "inter": "inter milan",
    "madrid": "real madrid",
    "atleti": "atletico madrid",
    "chelsea": "chelsea",
    "spurs": "tottenham hotspur",
    "arsenal": "arsenal",
    "liverpool": "liverpool",
    "juve": "juventus",
    "napoli": "napoli",
    "milan": "ac milan",
    "bayern": "bayern munich",
    "leverkusen": "bayer leverkusen",
    "dortmund": "borussia dortmund",
    "gladbach": "borussia monchengladbach",
    "freiburg": "sc freiburg",
    "wolfsburg": "vfl wolfsburg",
    "stuttgart": "vfb stuttgart",
    "leipzig": "rb leipzig",
    "frankfurt": "eintracht frankfurt",
    "union": "union berlin",
    "heidenheim": "fc heidenheim",
    "augsburg": "fc augsburg",
    "hoffenheim": "tsg hoffenheim",
    "bochum": "vfl bochum",
    "mainz": "mainz 05",
    "st pauli": "fc st pauli",
    "werder": "werder bremen",
    "koln": "fc koln",
}

# 常见通用词，不应参与子串/fuzzy匹配
_GENERIC_TEAM_TOKENS = {"fc", "cf", "sc", "ac", "osc", "hsc", "scc", "bc", "us",
                        "ssc", "tsg", "sv", "vfl", "vfb", "fsv", "as", "rc", "1"}

# 中文 → 英文队名映射（专用于结算匹配，覆盖 cn_to_odds_name 未覆盖的球队）
# 格式: 中文(小写) → 英文(小写)  — 与 cn_to_odds_name 格式一致
_CN_TO_EN_SETTLEMENT = {
    # === 乌拉圭甲级联赛 ===
    "乌拉圭民族": "nacional",
    "蒙得维的亚流浪者": "montevideo wanderers",
    "蒙得维的亚城图尔克": "ciudad de montevideo",
    "普罗格雷索": "progreso",
    "马尔多纳多": "deportivo maldonado",
    "达努比奥": "danubio",
    # === 厄瓜多尔甲级联赛 ===
    "德芬": "delfin",
    "马卡拉": "macara",
    "基多天主大学": "universidad catolica (quito)",
    "基多大学体育": "liga de quito",
    "利伯塔德洛哈": "libertad loja",
    "理工大学竞技": "tecnico universitario",
    "奥伦斯": "orense",
    "埃梅莱克": "emelec",
    "瓜亚基尔巴塞罗那": "barcelona guayaquil",
    "瓜亚基尔城": "guayaquil city",
    # === 阿根廷甲级联赛 ===
    "甘拿斯亚门多萨": "gimnasia mendoza",
    "科尔多瓦中央": "central cordoba",
    "萨斯菲尔德": "velez sarsfield",
    "科尔多瓦学院": "instituto cordoba",
    "河床": "river plate",
    "巴拉卡斯中央队": "barracas central",
    # === 阿根廷全国联赛 ===
    "科勒加勒斯": "colegiales",
    "米德兰": "midland",
    "阿马格罗": "almagro",
    "甘拿斯亚迪罗": "gimnasia jujuy",
    # === 挪威超级联赛 ===
    "汉坎": "hamkam",
    "特罗姆瑟": "tromso",
    "维京": "viking",
    "桑德菲杰": "sandefjord",
    "博多格林特": "bodoe/glimt",
    "费德列斯达": "fredrikstad",
    # === 瑞典超级联赛 ===
    "哈马比": "hammarby",
    "代格福什": "degerfors",
    "奥尔格里特": "ois",
    "尤尔戈登": "djurgardens",
    # === 美国职业大联盟 ===
    "圣何塞地震": "san jose earthquakes",
    "奥兰多城": "orlando city",
    "洛杉矶银河": "la galaxy",
    "洛杉矶FC": "lafc",
    "休斯敦迪纳摩": "houston dynamo",
    "华盛顿联": "dc united",
    # === 韩国K1联赛 ===
    "仁川联": "incheon united",
    "全北现代": "jeonbuk hyundai motors",
    "济州联队": "jeju united",
    "浦项制铁": "pohang steelers",
    # === 巴西乙级联赛 ===
    "福塔雷萨": "fortaleza",
    "诺瓦里桑蒂诺": "novorizontino",
    "雷加塔斯巴西": "recife",
    "累西腓航海": "nautico",
    # === 墨西哥超级联赛 ===
    "莱昂": "leon",
    "阿特拉斯": "atlas",
    # === 德国甲级联赛 ===
    "多特蒙德": "borussia dortmund",
    "汉堡": "hamburger sv",
    "拜仁慕尼黑": "fc bayern munich",
    "斯图加特": "vfb stuttgart",
    # === 欧足联欧洲联赛-资格赛 ===
    "克卢日大学": "university cluj",
    "基辅迪纳摩": "dynamo kyiv",
    "德利城": "derry city",
    "索菲亚中央陆军": "cska sofia",
    "费伦茨瓦罗斯": "ferencvaros",
    "伏伊伏丁那": "vojvodina",
    # === 欧洲冠军联赛-资格赛 ===
    "伊拿迪亚": "dinamo tirana",
    "佩特罗古": "petrocub",
    "艾达比辛": "elbasani",
    "克拉克斯维克": "klippan",
    # === 俄罗斯甲级联赛 ===
    "乌拉尔": "ural",
    "叶尼塞": "yenisey",
    "图拉兵工厂": "arsenal tula",
    "下卡姆斯克石油": "neftekhimik",
    "莫斯科鱼雷": "torpedo moscow",
    "乌里扬诺夫斯克": "volga ulyanovsk",
    "叶尼塞克拉斯诺亚尔斯克": "yenisey",
    # === 英格兰超级联赛 ===
    "纽卡斯尔联": "newcastle united",
    "利物浦": "liverpool",
    "赫尔城": "hull city",
    "曼彻斯特联": "manchester united",
    "诺丁汉森林": "nottingham forest",
    "利兹联": "leeds united",
    # === 西班牙甲级联赛 ===
    "塞维利亚": "sevilla",
    "巴列卡诺": "rayo vallecano",
    "拉科鲁尼亚": "deportivo la coruna",
    "埃尔切": "elche",
    "桑坦德竞技": "racing santander",
    "比利亚雷亚尔": "villarreal",
    "瓦伦西亚": "valencia",
    "皇家贝蒂斯": "real betis",
    "西班牙人": "espanyol",
    "莱万特": "levante",
    "阿拉维斯": "alaves",
    "赫塔菲": "getafe",
    "马德里竞技": "atletico madrid",
    "马拉加": "malaga",
    # === 日本职业棒球 ===
    "北海道日本火腿斗士": "hokkaido nippon-ham fighters",
    "福冈软件银行鹰": "fukuoka softbank hawks",
    "名古屋中日龙": "chunichi dragons",
    "阪神老虎": "hanshin tigers",
    # === 新西兰全国篮球联赛 ===
    "奥塔哥掘金": "otago nuggets",
    "霍克湾雄鹰": "hawke's bay hawks",
    "塔拉纳基": "taranaki airs",
    "尼尔森巨人": "nelson giants",
    # === 菲律宾PBA总督杯 ===
    "汇众光纤": "converge fiberxers",
    "泰丰吉普": "terrafirma dyip",
    "马拉古闪电": "meralco bolts",
    "凤凰燃料大师": "phoenix fuel masters",
    # === FIBA U20 ===
    "德国 U20": "germany u20",
    "拉脱维亚 U20": "latvia u20",
    "西班牙 U20": "spain u20",
    "罗马尼亚 U20": "romania u20",
    # === 澳大利亚杯 ===
    "贝尔格莱德阿德莱德": "belgrade adelaide",
    "北鹰阳光": "northern eagles",
    "马林海岸游骑兵FC": "marin coastal rangers",
    "布伦瑞克尤文图斯": "brunswick juventus",
    # === 意大利甲级联赛 ===
    "国际米兰": "inter milan",
    "蒙扎": "monza",
    # === 法国甲级联赛 ===
    "巴黎FC": "paris fc",
    "特鲁瓦": "troyes",
    # === 芬兰甲级联赛 ===
    "MP米克力": "mikkeli",
    "吉普": "jjk",
    # === 其他 ===
    "布伦瑞克城": "brunswick city",
    "梅特兰": "maitland",
    "米德兰": "midland",
    "艾达比辛": "elbasani",
    "克拉克斯维克": "klippan",
    "德国 U20": "germany u20",
    "西班牙 U20": "spain u20",
    "拉脱维亚 U20": "latvia u20",
    "罗马尼亚 U20": "romania u20",
}


def _resolve_alias(name: str) -> str:
    """通过昵称/缩写查找标准名。"""
    return _TEAM_ALIASES.get(name, name)


def _team_matches(candidate: str, api_name: str) -> bool:
    """多层队名匹配：别名 → 精确 → 分词 → 子串 → fuzzy。

    相比单纯的子串匹配，减少了误匹配风险（如 'barcelona' 匹配 'barcelona sc'）。
    """
    if not candidate or not api_name:
        return False

    # 0. 别名解析
    candidate = _resolve_alias(candidate)
    api_name = _resolve_alias(api_name)

    # 1. 精确匹配
    if candidate == api_name:
        return True

    # 2. 分词匹配：候选词的所有有意义单词都在 api 名中
    cand_words = [w for w in candidate.split() if w not in _GENERIC_TEAM_TOKENS]
    api_words = [w for w in api_name.split() if w not in _GENERIC_TEAM_TOKENS]
    if cand_words and api_words:
        if all(w in api_words for w in cand_words):
            return True
        if all(w in cand_words for w in api_words):
            return True

    # 3. 子串匹配（仅对长名，避免短名误匹配）
    if len(candidate) >= 4 and (candidate in api_name or api_name in candidate):
        return True

    # 4. Fuzzy match (rapidfuzz)，仅对有意义的长名
    if len(candidate) >= 4 and candidate not in _GENERIC_TEAM_TOKENS:
        try:
            from rapidfuzz import fuzz
            if fuzz.token_set_ratio(candidate, api_name) >= 88:
                return True
        except ImportError:
            pass

    return False


def _match_bet(bet: dict, completed_games: list) -> Optional[str]:
    """尝试将投注与已完成比赛匹配，返回 'won' 或 'lost' 或 None。

    支持 H2H（主胜/客胜/平）、大小球（大X/小X）、让球盘（handicap）三种盘口类型。
    遍历多种队名候选（英文翻译、中文名、原始存储名），
    逐一尝试匹配比赛结果。
    """
    import re

    home_raw = bet.get("home_team", bet.get("home_cn", ""))
    away_raw = bet.get("away_team", bet.get("away_cn", ""))
    home_cn = bet.get("home_cn", "")
    away_cn = bet.get("away_cn", "")
    outcome = bet.get("market_type", bet.get("market_detail", ""))
    bet_market = bet.get("market", "")

    # 判断盘口类型
    # 支持格式: "大球(2.5)" "上半场大球(1.0)" "大分(11.5)" "小球(2.25)" "大(2)" "over 2.5"
    ou_match = re.match(
        r'^(?:(上半场|下半场|全场)?[_\s]*([大小])(?:球|分)?|(over|under))'
        r'[_\s]*[\(（]?([\d.]+(?:/[\d.]+)?)[\)）]?$',
        outcome.strip(), re.IGNORECASE
    )
    is_over_under = ou_match is not None
    is_btts = outcome.strip().lower() in ("yes", "no")
    # 让球盘：market="handicap" 或 outcome 含 "让" 或 line 字段存在
    hc_line_raw = bet.get("line", "")
    is_handicap = bet_market == "handicap" or "让" in outcome or hc_line_raw

    # 构建候选列表: 中文原始名 → Odds API 英译 → 结算专用英译
    home_candidates = []
    for name in [_normalize_team(home_raw), _normalize_team(home_cn)]:
        if name:
            home_candidates.append(name)
            en_name = _normalize_team(cn_to_odds_name(name))
            if en_name and en_name not in home_candidates:
                home_candidates.append(en_name)
            en_settle = _normalize_team(_CN_TO_EN_SETTLEMENT.get(name, name))
            if en_settle and en_settle not in home_candidates and en_settle != name:
                home_candidates.append(en_settle)
    seen_h = set()
    home_cands = [c for c in home_candidates if not (c in seen_h or seen_h.add(c))]

    away_candidates = []
    for name in [_normalize_team(away_raw), _normalize_team(away_cn)]:
        if name:
            away_candidates.append(name)
            en_name = _normalize_team(cn_to_odds_name(name))
            if en_name and en_name not in away_candidates:
                away_candidates.append(en_name)
            en_settle = _normalize_team(_CN_TO_EN_SETTLEMENT.get(name, name))
            if en_settle and en_settle not in away_candidates and en_settle != name:
                away_candidates.append(en_settle)
    seen_a = set()
    away_cands = [c for c in away_candidates if not (c in seen_a or seen_a.add(c))]

    if not home_cands or not away_cands:
        return None

    for game in completed_games:
        if not isinstance(game, dict):
            continue
        api_home = _normalize_team(game.get("home_team", ""))
        api_away = _normalize_team(game.get("away_team", ""))
        completed = game.get("completed", False)
        scores = game.get("scores", [])

        if not completed or not scores:
            continue

        for hc in home_cands:
            for ac in away_cands:
                # 正向匹配（h=home, a=away）
                forward = _team_matches(hc, api_home) and _team_matches(ac, api_away)
                # 主客互换
                swapped = _team_matches(hc, api_away) and _team_matches(ac, api_home)

                if forward:
                    found_h, found_a = api_home, api_away
                elif swapped:
                    found_h, found_a = api_away, api_home
                else:
                    continue

                # 确定得分
                home_score = None
                away_score = None
                for s in scores:
                    name = _normalize_team(s.get("name", ""))
                    score = s.get("score")
                    if name in found_h or found_h in name:
                        home_score = int(score) if score is not None else None
                    elif name in found_a or found_a in name:
                        away_score = int(score) if score is not None else None

                if home_score is None or away_score is None:
                    continue

                # ── 大小球结算 ──
                if is_over_under:
                    total = home_score + away_score
                    # 解析盘口线（支持折中盘如 0/0.5 → 0.25）
                    line_str = ou_match.group(4)
                    if '/' in line_str:
                        parts = line_str.split('/')
                        try:
                            line = sum(float(p) for p in parts) / len(parts)
                        except (ValueError, TypeError):
                            line = None
                    else:
                        try:
                            line = float(line_str)
                        except (ValueError, TypeError):
                            line = None
                    if line is None:
                        continue
                    direction = (ou_match.group(2) or ou_match.group(3)).lower()
                    if direction in ('大', 'over'):
                        return "won" if total > line else "lost"
                    else:  # 小 / under
                        return "won" if total < line else "lost"

                # ── BTTS 双方进球结算 ──
                if is_btts:
                    both_scored = home_score > 0 and away_score > 0
                    if outcome.strip().lower() == "yes":
                        return "won" if both_scored else "lost"
                    else:  # "no"
                        return "won" if not both_scored else "lost"

                # ── 让球盘结算（handicap） ──
                if is_handicap:
                    hc_line = None
                    # 从 line 字段解析
                    if hc_line_raw:
                        try:
                            hc_line = float(hc_line_raw)
                        except (ValueError, TypeError):
                            pass
                    # 从 outcome 解析（如 "+3.5"、"-1.5"、或 "让球主胜(0)"、"让球客胜(+0/0.5)"）
                    if hc_line is None:
                        # 先尝试纯数字格式
                        hcm = re.match(r'^([+-]?\d+(?:\.\d+)?)$', outcome.strip())
                        if hcm:
                            hc_line = float(hcm.group(1))
                        else:
                            # 尝试从括号中提取（如 "让球主胜(0)"、"让分客胜(-3.5)"）
                            hcm = re.search(r'[\(（]([^)）]+)[\)）]', outcome.strip())
                            if hcm:
                                hc_str = hcm.group(1)
                                if '/' in hc_str:
                                    # 亚洲让球折中盘（如 +0/0.5 → 平均值 0.25）
                                    parts = hc_str.split('/')
                                    try:
                                        vals = [float(p) for p in parts]
                                        hc_line = sum(vals) / len(vals)
                                    except (ValueError, TypeError):
                                        pass
                                else:
                                    try:
                                        hc_line = float(hc_str)
                                    except (ValueError, TypeError):
                                        pass
                    if hc_line is None:
                        return None  # 解析不出盘口线，跳过
                    effective_home = home_score + hc_line
                    if effective_home > away_score:
                        return "won"  # 让球主胜
                    elif effective_home < away_score:
                        return "lost"  # 让球客胜
                    else:
                        # 走水（平局） — 返还本金
                        return "won"  # 走水算赢（本金返还，不赚不亏）

                # ── H2H 结算（主胜/客胜/平） ──
                is_home_win = home_score > away_score
                is_draw = home_score == away_score

                # 和局（"平"和"和"两种中文表达）
                if "平" in outcome or "和" in outcome or "draw" in outcome.lower():
                    return "won" if is_draw else "lost"
                if "主胜" in outcome or "home" in outcome.lower():
                    return "won" if is_home_win else "lost"
                if "客胜" in outcome or "away" in outcome.lower():
                    return "won" if (away_score > home_score) else "lost"

                # ── 双重机会（Double Chance） ──
                if "双重机会" in outcome or "double chance" in outcome.lower():
                    # "主/和局" = home/draw
                    if "主" in outcome and "和" in outcome:
                        return "won" if (is_home_win or is_draw) else "lost"
                    # "和局/客" = draw/away
                    if "和" in outcome and "客" in outcome:
                        return "won" if (is_draw or away_score > home_score) else "lost"
                    # "主/客" = home/away
                    if "主" in outcome and "客" in outcome:
                        return "won" if (is_home_win or away_score > home_score) else "lost"
                    return None  # 无法识别的双重机会组合

                # 未识别的 market_type，保守返回 None 不误判
                return None

    return None


def auto_settle(dry_run: bool = False) -> int:
    """自动结算所有已结束比赛的待处理投注。

    Args:
        dry_run: True 时只打印不实际结算

    Returns:
        结算的投注数量
    """
    state = _load_state()
    pending = state.get("pending_bets", [])
    if not pending:
        logger.info("无待结算投注")
        return 0

    logger.info("开始自动结算: %s 笔待处理", len(pending))
    settled_count = 0

    # 按 (运动, 联赛) 分组获取比分（同运动不同联赛必须分开）
    league_groups = {}
    for bet in pending:
        key = (bet.get("sport", ""), bet.get("league", ""))
        if key not in league_groups:
            league_groups[key] = []
        league_groups[key].append(bet)

    for (sport, league), bets in league_groups.items():
        # 跳过已从 LEAGUE_SPORT_MAP 确认不支持的联赛
        api_key_info = LEAGUE_SPORT_MAP.get(league)
        if not api_key_info:
            sk = SPORT_FALLBACK.get(sport)
            if sk:
                api_key_info = (sk, league or sport)
            else:
                logger.warning("未知联赛: %s (sport=%s)，跳过 %s 笔", league, sport, len(bets))
                continue

        _, display = api_key_info
        logger.info("获取 %s 已完成比赛...", display)

        # 多源获取比赛结果（ESPN → football-data.org → 直播吧）
        completed = _fetch_completed_scores(league)

        if not completed:
            logger.warning("  %s 无比分数据", display)
            continue

        logger.info("  %s 条已完成记录", len(completed))

        for bet in bets:
            bid = bet.get("id", "")
            result = _match_bet(bet, completed)
            if result:
                stake = bet.get("stake", 0)
                odds = bet.get("odds", 0)
                if dry_run:
                    logger.info("  [试运行] %s → %s (注额¥%.0f 赔率%.2f)", bid[:40], result, stake, odds)
                else:
                    settle_bet(bid, result, stake, odds)
                    if result == "won":
                        profit = stake * (odds - 1)
                    elif result == "push":
                        profit = 0.0
                    else:
                        profit = -stake
                    logger.info("  ✅ %s → %s (盈亏¥%.0f)", bid[:40], result, profit)
                    # 记录到策略优化器
                    try:
                        SettlementLogger().record(
                            bet_id=bid,
                            league=bet.get("league", ""),
                            market=bet.get("market_type", "unknown"),
                            edge_pct=bet.get("model_prob", 0) / (1.0 / max(odds, 1.01)) - 1.0 if odds > 1 else 0,
                            odds=odds,
                            stake=stake,
                            profit=profit,
                            outcome=result,
                        )
                    except Exception as e:
                        logger.warning("  记录结算日志失败: %s", e)
                    # 记录到校准器
                    try:
                        model_prob = bet.get("model_prob", 0)
                        if model_prob > 0 and odds > 1:
                            BetCalibrator().record(
                                bet_id=bid,
                                league=bet.get("league", ""),
                                market=bet.get("market_type", "unknown"),
                                edge_pct=(model_prob - 1.0/odds) / (1.0/odds) * 100 if odds > 1 else 0,
                                model_prob=model_prob,
                                odds=odds,
                                result=result,
                            )
                    except Exception as e:
                        logger.warning("  校准记录失败: %s", e)
                    # 同步到 RiskManager 冷却状态
                    try:
                        rm = RiskManager()
                        prob = bet.get("model_prob", 1.0 / odds if odds > 1 else 0.5)
                        rm.record_outcome(stake, result == "won", odds, prob,
                                          sport=bet.get("sport", ""),
                                          home_team=bet.get("home_team", bet.get("home_cn", "")),
                                          away_team=bet.get("away_team", bet.get("away_cn", "")),
                                          bet_type=bet.get("market_type", "h2h"))
                    except Exception as e:
                        logger.warning("  ⚠️ 风险状态同步失败: %s", e)
                settled_count += 1

    if settled_count == 0:
        logger.info("未找到可结算的投注（比赛可能仍未结束）")
    else:
        logger.info("自动结算完成: %s 笔", settled_count)

    # ── 超时兜底：超过 3 天的 pending 投注自动作废（返还本金，不记盈亏）──
    if not dry_run:
        timeout_count = _auto_void_timeout()
        if timeout_count:
            logger.info("超时自动作废: %s 笔", timeout_count)
            settled_count += timeout_count

    # ── 策略自进化 ──
    if not dry_run and settled_count > 0:
        try:
            from src.risk.self_learn import analyze, apply_adjustments
            _report = analyze()
            if _report.get("status") == "ok" and _report.get("recommendations"):
                _n = apply_adjustments(_report)
                if _n:
                    logger.info("策略自进化: 已调整 %d 个联赛层级", _n)
        except Exception as e:
            logger.warning("策略自进化异常: %s", e)

    return settled_count


def _auto_void_timeout(max_days: int = 7) -> int:
    """超时兜底：超过 max_days 仍未匹配到结果的投注自动作废。

    作废 = 返还本金，不记盈亏。防止投注因 API 配额/数据缺失永久卡在 pending。
    """
    state = _load_state()
    pending = state.get("pending_bets", [])
    if not pending:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_days)
    voided = 0
    remaining = []
    for bet in pending:
        created = bet.get("created_at", "")
        if not created:
            remaining.append(bet)
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            remaining.append(bet)
            continue

        if dt < cutoff:
            bid = bet.get("id", "")
            stake = bet.get("stake", 0)
            odds = bet.get("odds", 0)
            # 作废：本金返还 balance，不记盈亏
            state["balance"] += stake
            state["history"].append({
                "id": bid,
                "match": f"{stake:.0f}¥ @ {odds:.2f} (超时作废)",
                "date": now.isoformat(),
                "stake": stake,
                "odds": odds,
                "profit": 0.0,
                "status": "void",
            })
            logger.info("  ⏰ 超时作废: %s (%.0f¥, 已 %d 天)", bid[:40], stake, (now - dt).days)
            voided += 1
        else:
            remaining.append(bet)

    if voided:
        state["pending_bets"] = remaining
        _save_state(state)
        logger.info("  超时作废完成: %d 笔", voided)
    return voided


def main():
    from config.logging_config import setup_logging
    setup_logging()
    auto_settle(dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    main()
