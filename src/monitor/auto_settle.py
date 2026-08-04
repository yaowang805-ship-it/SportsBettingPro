"""虚拟投注自动结算 — 多数据源（ESPN + football-data.org + 直播吧）自动结算。"""
import json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR
from src.dashboard.components.virtual_portfolio import (
    _load_state, _save_state, settle_bet,
)
from src.core.team_names import cn_to_odds_name
from fetchers.multi_source_scores import get_completed_scores, get_completed_scores_by_sport
from src.risk.manager import RiskManager
from src.betting.strategy_optimizer import SettlementLogger
from src.risk.calibration import BetCalibrator

logger = get_logger(__name__)

# ── 三态结算标记 ──
# source quality levels
SOURCE_PRIMARY = "primary"      # ESPN/直播吧 — 可靠主源
SOURCE_BACKUP = "backup"        # football-data.org/Odds API/BSD/BALLDONTLIE — 备用源
SOURCE_UNRESOLVED = "unresolved"  # 所有源都无法匹配 → 待人工确认

_PRIMARY_SOURCES = {"espn", "zhibo8"}
_UNRESOLVED_FILE = DATA_DIR / "unresolved_bets.json"


def _detect_source_quality(completed_games: list) -> str:
    """检测已完成比赛数据的来源质量。"""
    if not completed_games:
        return SOURCE_UNRESOLVED
    source = (completed_games[0].get("source") or "").lower()
    return SOURCE_PRIMARY if source in _PRIMARY_SOURCES else SOURCE_BACKUP


def _save_unresolved(unresolved: list):
    """持久化未结算投注列表供日报使用。"""
    _UNRESOLVED_FILE.write_text(json.dumps(unresolved, ensure_ascii=False, indent=2, default=str))


def _load_unresolved() -> list:
    """加载未结算投注列表。"""
    if _UNRESOLVED_FILE.exists():
        try:
            return json.loads(_UNRESOLVED_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


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
    "乌拉圭甲级联赛": (None, "乌拉圭甲"),
    "乌拉圭乙级联赛": (None, "乌拉圭乙"),
    "巴拉圭甲级联赛": (None, "巴拉圭甲"),
    "巴拉圭乙级联赛": (None, "巴拉圭乙"),
    "哈萨克斯坦超级联赛": (None, "哈萨克斯坦超"),
    "白俄罗斯超级联赛": (None, "白俄罗斯超"),
    "爱沙尼亚甲级联赛": (None, "爱沙尼亚甲"),
    "澳门甲级联赛": (None, "澳门甲"),
    "马来西亚总统杯 U20": (None, "马来西亚U20"),
    "韩国足协杯": (None, "韩国足协杯"),
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
    # --- 新增：提高结算覆盖率 ---
    "智利甲级联赛": ("soccer_chile_primera_division", "智甲"),
    "智利甲": ("soccer_chile_primera_division", "智甲"),
    "波兰超级联赛": ("soccer_poland_ekstraklasa", "波超"),
    "波兰甲级联赛": ("soccer_poland_ekstraklasa", "波甲"),
    "秘鲁甲级联赛": ("soccer_peru_primera_division", "秘甲"),
    "乌兹别克斯坦超级联赛": ("soccer_uzbekistan_super_league", "乌超"),
    "哥伦比亚甲级联赛": ("soccer_colombia_primera_a", "哥甲"),
    "俄罗斯超级联赛": ("soccer_russia_premier_league", "俄超"),
    "俄罗斯甲级联赛": ("soccer_russia_fnl", "俄甲"),
    "匈牙利甲级联赛": ("soccer_hungary_nb1", "匈甲"),
    "斯洛文尼亚甲级联赛": ("soccer_slovenia_prva_liga", "斯洛甲"),
    "乌克兰超级联赛": ("soccer_ukraine_premier_league", "乌超联"),
    "墨西哥甲级联赛": ("soccer_mexico_liga_mx", "墨甲"),
    "美国MLS下级职业赛": ("soccer_usa_mls_next_pro", "MLS下级"),
    "美国冠军联赛": ("soccer_usa_usl_championship", "美冠联"),
    "MLB 美国职业棒球大联盟": ("baseball_mlb", "MLB"),
    "韩国职业棒球": ("baseball_kbo", "KBO"),
    "日本职业棒球": ("baseball_npb", "NPB"),
    "墨西哥棒球联盟": ("baseball_mexico", "墨棒"),
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
    "冰岛超级联赛": ("soccer_iceland_pepsi_deild", "冰岛超"),
    "冰岛超级联赛女子": (None, "冰岛超女"),
    # 棒球
    "日本职业棒球": ("baseball_npb", "日本职棒"),
    "MLB 美国职业棒球大联盟": ("baseball_mlb", "MLB"),
    "MLB联赛": ("baseball_mlb", "MLB"),
    # 芬兰足球
    "芬兰甲级联赛": ("soccer_finland_ykkosliiga", "芬甲"),
    # 瑞典足球
    "瑞典超甲级联赛": ("soccer_sweden_superettan", "瑞典超甲"),
    # 俄罗斯足球
    "俄罗斯超级联赛": ("soccer_russia_premier_league", "俄超"),
    "俄罗斯甲级联赛": ("soccer_russia_first_league", "俄甲"),
    "俄罗斯乙级A组联赛": ("soccer_russia_second_league_a", "俄乙A"),
    # 南美足球
    "秘鲁甲级联赛": ("soccer_peru_primeira_division", "秘鲁甲"),
    "立陶宛甲级联赛": ("soccer_lithuania_a_lyga", "立陶宛甲"),
    "印度加尔各答超级联赛": (None, "印度加尔各答超"),
    # 欧足联会议联赛（BB API 用词变体）
    "欧足联欧洲会议联赛-资格赛": ("soccer_uefa_conference_league", "欧协联"),
    "欧足联欧洲会议联赛": ("soccer_uefa_conference_league", "欧协联"),
    # WNBA 全称
    "WNBA 美国职业女子篮球联赛": ("basketball_wnba", "WNBA"),
    # 足球小联赛
    "澳大利亚新南威尔士州北部全国超级联赛": (None, "澳北超"),
    # 苏格兰联赛杯
    "苏格兰联赛杯": ("soccer_scotland_league_cup", "苏格兰联赛杯"),
    # 冰岛甲级
    "冰岛甲级联赛": (None, "冰岛甲级联赛"),
}

# 兜底：sport 字段 → sport key（精确匹配）
SPORT_FALLBACK = {
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "nfl": "americanfootball_nfl",
    "football": None,  # 需要由 league 决定
}


def _fetch_completed_scores(league_name: str, days_back: int = 1) -> list:
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
    "格鲁吉亚 U20": "georgia u20",
    "丹麦 U20": "denmark u20",
    # === WNBA ===
    "亚特兰大梦想 (女)": "atlanta dream",
    "芝加哥天空 (女)": "chicago sky",
    "达拉斯飞翼 (女)": "dallas wings",
    "洛杉矶火花 (女)": "los angeles sparks",
    # === 俄罗斯足球 ===
    "ska哈巴罗夫斯克": "ska khabarovsk",
    "索科尔萨拉托夫": "sokol saratov",
    "fk卡卢加": "fk kaluga",
    "弗拉季高加索": "alania vladikavkaz",
    "莫斯科罗迪纳": "rodina moscow",
    "莫斯科罗迪纳二队": "rodina moscow ii",
    "莫斯科斯巴达": "spartak moscow",
    "洛特伏尔加格勒": "rotor volgograd",
    # === 保加利亚足球 ===
    "弗拉察博特夫": "botev vratsa",
    "普罗夫迪夫博特夫": "botev plovdiv",
    "索菲亚列夫斯基": "levski sofia",
    "索菲亚火车头": "lokomotiv sofia",
    "索非亚斯拉维亚": "slavia sofia",
    # === 罗马尼亚足球 ===
    "布格勒斯特迅速 1923": "rapid bucharest",
    "米耶尔库雷亚丘克": "miercurea ciuc",
    "胡内多阿拉": "corvinul hunedoara",
    "舍佩斯": "sepsi osv",
    "达克斯特雷达 1904": "dac dunajska streda",
    # === 瑞典足球 ===
    "松兹瓦尔": "sundsvall",
    "诺尔比": "norrby",
    # === 丹麦足球 ===
    "维积利": "vejle",
    "ab格莱萨克瑟": "ab gladsaxe",
    # === 芬兰足球 ===
    "玛丽港": "mariehamn",
    "拉赫蒂": "lahti",
    # === 冰岛 ===
    "格林达维克 (女)": "grindavik",
    "科帕沃古": "kopavogur",
    "维斯特里": "vestri",
    # === 立陶宛 ===
    "苏杜瓦": "suduva",
    "黑格尔曼": "hegelmann",
    # === 波黑 ===
    "莫斯塔尔维列兹": "velez mostar",
    # === 韩国 ===
    "天安城": "cheonan city",
    "首尔衣恋": "seoul e land",
    # === MLS/USL ===
    "底特律城": "detroit city",
    "温哥华白帽": "vancouver whitecaps",
    "辛辛那提": "fc cincinnati",
    # === 南美足球 ===
    "博利瓦尔": "bolivar",
    "泰格雷": "tigre",
    "科其姆波": "coquimbo",
    "约森独立队": "independiente del valle",
    "穆苏克鲁纳": "mushuc runa",
    "阿利亚加": "aliaga",
    "卡塞罗斯学生队": "estudiantes caseros",
    "里奥夸尔托学生队": "estudiantes rio cuarto",
    "新芝加哥": "nueva chicago",
    "拉费尔拿": "rafaela",
    "康塞普西翁大学": "universidad de concepcion",
    "瓦斯科达伽马": "vasco da gama",
    "瓦斯科达伽马 u22": "vasco da gama u22",
    "米拉索尔": "mirassol",
    "体育生队": "sport boys",
    "洛斯香卡斯": "los chankas",
    # === 印度 ===
    "莫亨巴根二队": "mohun bagan ii",
    "加尔各答ms": "calcutta ms",
    # === MLB ===
    "千叶罗德海洋": "chiba lotte marines",
    "巴尔的摩金莺": "baltimore orioles",
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

    # 从 team_name_map.json 查找英文名（3370条中→英，远超 _CN_TO_EN_SETTLEMENT）
    def _lookup_team_map(name: str) -> str:
        if not name:
            return ""
        try:
            from config.settings import DATA_DIR as _DD
            _map_path = _DD / "team_name_map.json"
            if _map_path.exists():
                _tm = json.loads(_map_path.read_text())
                return _tm.get(name, "")
        except Exception:
            pass
        return ""

    # 构建候选列表: 中文原始名 → Odds API 英译 → 结算专用英译 → team_name_map
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
            en_tm = _normalize_team(_lookup_team_map(name))
            if en_tm and en_tm not in home_candidates and en_tm != name:
                home_candidates.append(en_tm)
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
            en_tm = _normalize_team(_lookup_team_map(name))
            if en_tm and en_tm not in away_candidates and en_tm != name:
                away_candidates.append(en_tm)
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
                    if total == line:
                        return "push"  # 大小球走水（恰好等于盘口线）
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
                        return "push"  # push = 本金返还，不赚不亏

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
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 30  # 连续失败此数后放弃本次结算
    PER_LEAGUE_TIMEOUT = 10        # 单联赛结算超时秒数（跳过慢联赛）

    # V4.5: 联赛失败缓存 — 连续3次失败 → 24h 跳过, 加速结算
    from pathlib import Path
    _failure_cache_file = DATA_DIR / "settle_failure_cache.json"
    _failure_cache = {}
    if _failure_cache_file.exists():
        try: _failure_cache = json.loads(_failure_cache_file.read_text())
        except: pass
    now_ts = time.time()
    # 清理过期 (>24h)
    _failure_cache = {k: v for k, v in _failure_cache.items() if v.get("ts", 0) > now_ts - 86400}

    # 按 (运动, 联赛) 分组获取比分
    league_groups = {}
    skipped_leagues = 0
    for bet in pending:
        key = (bet.get("sport", ""), bet.get("league", ""))
        lg_name = bet.get("league", "")
        # 跳过已证明失败的联赛
        fc = _failure_cache.get(lg_name, {})
        if fc.get("consecutive", 0) >= 3:
            skipped_leagues += 1
            continue
        if key not in league_groups:
            league_groups[key] = []
        league_groups[key].append(bet)
    if skipped_leagues:
        logger.info("跳过 %d 笔 (联赛连续失败≥3次, 24h冷却)", skipped_leagues)

    _sport_fallback_cache: dict[str, list] = {}  # sport → completed scores 缓存
    unresolved_bets = []  # 所有源都无法匹配的投注
    source_quality_log = {}  # {(sport, league): quality}

    for (sport, league), bets in league_groups.items():
        # 跳过已从 LEAGUE_SPORT_MAP 确认不支持的联赛
        api_key_info = LEAGUE_SPORT_MAP.get(league)
        looks_up_scores = True
        display = league or sport
        if not api_key_info:
            sk = SPORT_FALLBACK.get(sport)
            if sk:
                api_key_info = (sk, league or sport)
            else:
                # 级联退避：按 sport 遍历所有已知联赛获取比分（仅首次调用，后续缓存）
                if sport not in _sport_fallback_cache:
                    logger.info("  联赛映射未知，尝试 sport 级联退避: %s", sport)
                    _sport_fallback_cache[sport] = get_completed_scores_by_sport(sport) or []
                completed = _sport_fallback_cache[sport]
                if not completed:
                    logger.warning("未知联赛: %s (sport=%s)，跳过 %s 笔", league, sport, len(bets))
                    continue
                looks_up_scores = False

        if looks_up_scores:
            _, display = api_key_info
            logger.info("获取 %s 已完成比赛...", display)
            completed = _fetch_completed_scores(league)

        if not completed:
            logger.warning("  %s 无比分数据", display)
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.error("连续 %d 个联赛无数据, 结算中断(避免死循环)", consecutive_failures)
                break
            source_quality_log[(sport, league)] = SOURCE_UNRESOLVED
            for bet in bets:
                unresolved_bets.append({
                    "id": bet.get("id", ""),
                    "league": league,
                    "sport": sport,
                    "home": bet.get("home_cn", bet.get("home_team", "")),
                    "away": bet.get("away_cn", bet.get("away_team", "")),
                    "market": bet.get("market_type", ""),
                    "reason": "数据源无比分",
                })
            continue
        else:
            consecutive_failures = 0  # 成功获取则重置

        source_quality = _detect_source_quality(completed)
        source_quality_log[(sport, league)] = source_quality
        logger.info("  %s 条已完成记录 (来源: %s)", len(completed), source_quality)

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
                        # 结算反馈: 胜投确认匹配正确, 自动学习队名映射
                        _learn_team_names_from_win(bet)
                    elif result == "push":
                        profit = 0.0
                    else:
                        profit = -stake
                    logger.info("  ✅ %s → %s (盈亏¥%.0f)", bid[:40], result, profit)
                    # 记录结算追踪
                    try:
                        from src.core.settleability import record_settlement
                        record_settlement(bet.get("league", ""), success=True)
                    except ImportError:
                        pass
                    # 记录到策略优化器
                    try:
                        SettlementLogger().record(
                            bet_id=bid,
                            league=bet.get("league", ""),
                            market=bet.get("market_type", "unknown"),
                            sub_market=bet.get("sub_market", bet.get("_market", "")),
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
            else:
                # 三态：有数据但匹配失败 → 标记 unresolved
                unresolved_bets.append({
                    "id": bid,
                    "league": league,
                    "sport": sport,
                    "home": bet.get("home_cn", bet.get("home_team", "")),
                    "away": bet.get("away_cn", bet.get("away_team", "")),
                    "market": bet.get("market_type", ""),
                    "stake": bet.get("stake", 0),
                    "odds": bet.get("odds", 0),
                    "source_quality": source_quality,
                    "reason": "队名匹配失败",
                })
                logger.debug("  ⏳ %s 暂未匹配到比分 (来源: %s)", bid[:40], source_quality)

    # ── 回写推荐记录：将已结算投注的结果同步到 recommendation_log.csv ──
    if not dry_run:
        _sync_recommendation_log(state, settled_count)
    # ── 推荐记录全量结算：已投注已回写，未投注的用推荐 stakes 算结果 ──
    if not dry_run:
        rec_settled = _settle_recommendation_log()
        if rec_settled:
            settled_count += rec_settled

    if settled_count == 0:
        logger.info("未找到可结算的投注（比赛可能仍未结束）")
    else:
        logger.info("自动结算完成: %s 笔", settled_count)

    # ── 超时兜底：超过 N 天自动作废（返还本金，不记盈亏）──
    # 所有联赛 5 天, ESPN 不覆盖联赛 3 天加速
    if not dry_run:
        timeout_count = _auto_void_timeout(max_days=5)
        if timeout_count:
            logger.info("超时自动作废(5天): %s 笔", timeout_count)
            settled_count += timeout_count
        timeout_fast = _auto_void_timeout(max_days=3, skip_settleable=True)
        if timeout_fast:
            logger.info("超时自动作废(3天/非覆盖): %s 笔", timeout_fast)
            settled_count += timeout_fast

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

    # ── 三态结算标记：持久化未解决投注 ──
    if not dry_run:
        # 过滤掉 unresolved_bets 中已被 settled 的（settle_bet 移除了 pending_bets）
        state_after = _load_state()
        settled_ids = set()
        for h in state_after.get("history", []):
            settled_ids.add(h.get("id", ""))
        unresolved_bets = [u for u in unresolved_bets if u["id"] not in settled_ids]
        _save_unresolved(unresolved_bets)
        if unresolved_bets:
            logger.warning("⏳ 待人工确认: %d 笔投注在所有数据源中均未找到匹配", len(unresolved_bets))

    return settled_count


def _settle_recommendation_log() -> int:
    """全量结算推荐记录中所有未结算的比赛。

    加载 recommendation_log.csv，对每个 status != "settled" 的条目：
    1. 用 _match_bet 查比分
    2. 用推荐时的 stake 算盈亏
    3. 更新 CSV

    Returns:
        结算数量
    """
    try:
        from src.report.recommendation_tracker import load_all, _rewrite_all, _make_fingerprint_from_row
    except ImportError:
        return 0

    records = load_all()
    if not records:
        return 0

    pending = [r for r in records if r.get("status") != "settled"]
    if not pending:
        return 0

    logger.info("推荐记录全量结算: %s 笔待处理", len(pending))

    # 按 (sport, league) 分组
    league_groups = {}
    for rec in pending:
        key = (rec.get("sport", ""), rec.get("league", ""))
        league_groups.setdefault(key, []).append(rec)

    _sport_fallback_cache: dict[str, list] = {}
    settled = 0
    updated_rows = set()  # track indices that need updating

    for (sport, league), recs in league_groups.items():
        # 获取比分
        api_key_info = LEAGUE_SPORT_MAP.get(league)
        if not api_key_info:
            sk = SPORT_FALLBACK.get(sport)
            if sk:
                api_key_info = (sk, league or sport)
            else:
                if sport not in _sport_fallback_cache:
                    _sport_fallback_cache[sport] = get_completed_scores_by_sport(sport) or []
                completed = _sport_fallback_cache[sport]
                if not completed:
                    continue
        if api_key_info:
            completed = _fetch_completed_scores(league)
        if not completed:
            continue

        for rec in recs:
            # 构造 fake bet 给 _match_bet 用
            designation = rec.get("designation", "")
            market_type_str = rec.get("market_type", "")
            try:
                odds_val = float(rec.get("bb_odds", 0))
                stake_val = float(rec.get("stake", 0))
            except (ValueError, TypeError):
                continue
            if odds_val <= 0 or stake_val <= 0:
                continue

            # 映射中文标识到 _match_bet 能识别的格式
            mapped_designation = designation
            if "双边进球" in designation:
                # "双边进球-是" → "yes", "双边进球-否" → "no"
                mapped_designation = "yes" if "是" in designation else "no"
            elif "双重机会" in designation:
                # "双重机会-主/和局" → "双重机会-主/和局" 保留原样（_match_bet 已支持）
                pass

            fake_bet = {
                "home_team": rec.get("home_team", ""),
                "home_cn": rec.get("home_cn", ""),
                "away_team": rec.get("away_team", ""),
                "away_cn": rec.get("away_cn", ""),
                "market_type": mapped_designation,
                "market": market_type_str,
                "odds": odds_val,
                "stake": stake_val,
            }

            result = _match_bet(fake_bet, completed)
            if result:
                if result == "won":
                    profit = stake_val * (odds_val - 1)
                elif result == "push":
                    profit = 0.0
                else:
                    profit = -stake_val

                # Update in-place
                rec["status"] = "settled"
                rec["result"] = result
                rec["profit"] = str(round(profit, 2))
                settled += 1

                logger.info("  [推荐结算] %s vs %s | %s | stake=¥%.0f → %s (盈亏¥%.0f)",
                            rec.get("home_cn", ""), rec.get("away_cn", ""),
                            designation, stake_val, result, profit)

    # 标记超时未匹配的推荐记录为 unresolved
    _now = datetime.now(timezone.utc)
    _unresolved_cutoff = _now - timedelta(days=2)
    unresolved_count = 0
    for r in records:
        if r.get("status") in ("settled", "unresolved"):
            continue
        start_str = r.get("start_time", "")
        if not start_str:
            continue
        try:
            # 格式: "07/29 08:15" (MM/DD HH:MM)
            parts = start_str.strip().split()
            if len(parts) == 2:
                mm, dd = parts[0].split("/")
                hh, minute = parts[1].split(":")
                match_dt = datetime(_now.year, int(mm), int(dd), int(hh), int(minute), tzinfo=timezone.utc)
                if match_dt < _unresolved_cutoff:
                    r["status"] = "unresolved"
                    r["result"] = "no_data"
                    r["profit"] = "0"
                    unresolved_count += 1
        except (ValueError, IndexError):
            continue

    if unresolved_count:
        logger.info("推荐记录: %d 条超时未匹配标记为 unresolved", unresolved_count)

    if settled or unresolved_count:
        _rewrite_all(records)
        logger.info("推荐记录全量结算完成: %d 笔", settled)
    else:
        logger.info("推荐记录全量结算: 无可结算条目")

    return settled


def _learn_team_names_from_win(bet: dict):
    """结算反馈: 胜投→匹配正确→自动学习BB中文→Pinnacle英文队名映射。"""
    try:
        from config.settings import DATA_DIR
        tm_file = DATA_DIR / "team_name_map.json"
        tm = json.loads(tm_file.read_text()) if tm_file.exists() else {}
        new_pairs = 0
        for cn_key, en_key in [("home_cn", "home_team"), ("away_cn", "away_team")]:
            cn_name = bet.get(cn_key, "").strip()
            en_name = bet.get(en_key, "").strip()
            if cn_name and en_name and not cn_name.isascii() and cn_name not in tm:
                tm[cn_name] = en_name
                new_pairs += 1
        if new_pairs:
            tm_file.write_text(json.dumps(tm, ensure_ascii=False, indent=2))
            logger.info("结算反馈: 学习 %d 个队名映射", new_pairs)
    except Exception as e:
        logger.debug("队名学习失败: %s", e)


def _auto_void_timeout(max_days: int = 5, skip_settleable: bool = False) -> int:
    """超时兜底：超过 max_days 仍未匹配到结果的投注自动作废。

    作废 = 返还本金，不记盈亏。防止投注因 API 配额/数据缺失永久卡在 pending。
    skip_settleable=True: 只作废不可结算联赛的投注（3天加速通道）。
    """
    state = _load_state()
    pending = state.get("pending_bets", [])
    if not pending:
        return 0

    if skip_settleable:
        from src.core.settleability import is_league_settleable
        pending = [b for b in pending if not is_league_settleable(
            b.get("league", ""), b.get("sport", "")
        )]

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_days)
    voided = 0
    remaining = []
    for bet in pending:
        created = bet.get("created_at", "")
        if not created:
            stake = bet.get("stake", 0)
            bid = bet.get("id", "")
            logger.info("  ⏰ 超时作废(无创建时间): %s (%.0f¥)", bid[:40], stake)
            voided.append(bid)
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            stake = bet.get("stake", 0)
            bid = bet.get("id", "")
            logger.info("  ⏰ 超时作废(时间格式无效): %s (%.0f¥)", bid[:40], stake)
            voided.append(bid)
            continue

        if dt < cutoff:
            bid = bet.get("id", "")
            stake = bet.get("stake", 0)
            odds = bet.get("odds", 0)
            league = bet.get("league", "")
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
                "source": "bb_vs_pinnacle",
                "league": league,
                "sport": bet.get("sport", ""),
                "home_cn": bet.get("home_cn", ""),
                "away_cn": bet.get("away_cn", ""),
                "market_type": bet.get("market_type", bet.get("market", "")),
            })
            # 记录到结算追踪
            if league:
                try:
                    from src.core.settleability import record_void
                    record_void(league)
                except ImportError:
                    pass
            logger.info("  ⏰ 超时作废: %s (%.0f¥, 已 %d 天)", bid[:40], stake, (now - dt).days)
            voided += 1
        else:
            remaining.append(bet)

    if voided:
        state["pending_bets"] = remaining
        _save_state(state)
        logger.info("  超时作废完成: %d 笔", voided)
    return voided


def _sync_recommendation_log(state: dict, settled_count: int):
    """将已结算投注的结果同步到 recommendation_log.csv。

    通过 (home_cn, away_cn, designation) 三元组匹配推荐记录与投注结果。
    """
    try:
        from src.report.recommendation_tracker import load_all, _rewrite_all
    except ImportError:
        return

    records = load_all()
    if not records:
        return

    # 从 portfolio history 构建 结果映射
    # 格式：(home_cn, away_cn, desig) → (result, profit)
    history_results = {}
    for h in state.get("history", []):
        if h.get("status") not in ("won", "lost", "void"):
            continue
        desig = h.get("market_type", "")
        if not desig:
            # 从 bet_id 提取 designation（id 格式: bb_vs_pin_{market}_{home}_{away}_{designation})
            bid = h.get("id", "")
            parts = bid.split("_")
            if len(parts) >= 5:
                desig = parts[-1]
        if not desig:
            continue
        key = (h.get("home_cn", ""), h.get("away_cn", ""), desig)
        profit = h.get("profit", 0) or 0
        history_results[key] = (h["status"], profit)

    # 从 settled dict 补充（bid 含编码信息）
    settled = state.get("settled", {})
    if isinstance(settled, dict):
        for bid, result in settled.items():
            if isinstance(result, str) and result in ("won", "lost"):
                parts = bid.split("_")
                # 尝试从 bet_id 提取
                if len(parts) >= 5:
                    # bb_vs_pin_{market}_{home}_{away}_{designation}
                    desig = parts[-1]
                    home_encoded = parts[-3] if len(parts) >= 4 else ""
                    away_encoded = parts[-2] if len(parts) >= 4 else ""
                    key = (home_encoded, away_encoded, desig)
                    if key not in history_results:
                        history_results[key] = (result, 0)

    if not history_results:
        return

    updated = 0
    for row in records:
        key = (row.get("home_cn", ""), row.get("away_cn", ""), row.get("designation", ""))
        if key in history_results and row.get("status") != "settled":
            result, profit = history_results[key]
            row["status"] = "settled"
            row["result"] = result
            row["profit"] = str(round(float(profit), 2))
            updated += 1

    if updated:
        _rewrite_all(records)
        logger.info("推荐记录同步: 已更新 %d 条结算结果", updated)


def main():
    from config.logging_config import setup_logging
    setup_logging()
    auto_settle(dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    main()
