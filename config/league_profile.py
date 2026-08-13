"""联赛概率特征 — 从 football-data.co.uk 全量 Pinnacle 收盘数据提取。

用途: 比价时用联赛特定的先验修正 DC 平局概率、大小球进球率等。

数据源: data/storage/league_profile.json (17 个主流联赛)
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "storage"
PROFILE_FILE = DATA_DIR / "league_profile.json"

_profile_cache = None


def _load_profile() -> dict:
    global _profile_cache
    if _profile_cache is None:
        try:
            data = json.loads(PROFILE_FILE.read_text())
            _profile_cache = data.get("leagues", {})
        except Exception:
            _profile_cache = {}
    return _profile_cache


def get_league_profile(league: str) -> dict:
    """获取联赛特征, 无数据返回空 dict。支持中英文名模糊匹配。"""
    profile = _load_profile()
    if league in profile:
        return profile[league]
    # 模糊匹配
    for name, data in profile.items():
        if name in league or league in name:
            return data
    return {}


def get_draw_rate(league: str, default: float = 0.2678) -> float:
    """联赛平局率 (默认 26.78% = 全量平均)。"""
    return get_league_profile(league).get("draw_rate", default)


def get_over25_rate(league: str, default: float = 0.52) -> float:
    """联赛大2.5率。"""
    return get_league_profile(league).get("over25_rate", default)


def get_avg_goals(league: str, default: float = 2.7) -> float:
    """联赛场均进球。"""
    return get_league_profile(league).get("avg_goals", default)


def get_pin_margin(league: str, default: float = 0.05) -> float:
    """联赛 Pinnacle 抽水 (越低越准)。"""
    return get_league_profile(league).get("pin_margin", default)


# ═══════════════════════════════════════════════════════════════
# 风控敏感联赛判定 — 软书限额 sharp 玩家的典型特征
# ═══════════════════════════════════════════════════════════════
# sharp 玩家爱去利基市场(低级别/女子/冷门联赛), 软书风控最敏感
# 主流联赛(五大联赛/欧冠/NBA等)是"看起来正常"的投注

# 安全联赛白名单 (主流, sharp 玩家少去, 投注不易被标记)
SAFE_LEAGUES = {
    "英格兰超级联赛", "西班牙甲级联赛", "德国甲级联赛", "意大利甲级联赛", "法国甲级联赛",
    "荷兰甲级联赛", "葡萄牙超级联赛", "比利时甲级联赛", "土耳其超级联赛", "俄罗斯超级联赛",
    "英格兰冠军联赛", "英格兰甲级联赛", "西班牙乙级联赛", "德国乙级联赛", "意大利乙级联赛",
    "欧洲冠军联赛", "欧足联欧洲联赛", "欧足联欧洲协会联赛",
    "NBA", "WNBA", "NFL", "MLB", "NHL", "UFC", "美国职业大联盟",
}

# 风控敏感关键词 (sharp 特征, 软书最易标记)
SENSITIVE_KEYWORDS = [
    "女子", "女篮", "女足", "后备", "青年", "U19", "U21", "U23", "二队",
    "丙级", "丁级", "地区", "州", "挑战赛", "ITF", "友谊赛", "社区盾",
]


def is_sensitive_league(league: str, sport: str = "football") -> bool:
    """判断联赛是否为风控敏感联赛 (sharp 特征, 易被软书限额)。

    返回 True = 敏感(需谨慎), False = 主流(安全)。
    """
    if not league:
        return False
    # 1. 白名单子串匹配 → 安全 (NBA 美国职业篮球联赛 含 NBA)
    for safe in SAFE_LEAGUES:
        if safe in league or league in safe:
            return False
    # 2. 敏感关键词 → 敏感
    for kw in SENSITIVE_KEYWORDS:
        if kw in league:
            return True
    # 3. 主流运动关键词 → 安全 (篮球/棒球/美足/冰球)
    for kw in ("NBA", "WNBA", "NFL", "MLB", "NHL", "UFC"):
        if kw in league:
            return False
    # 4. 含"甲级/超级/冠军"等主流联赛特征 → 偏安全
    if any(kw in league for kw in ("甲级联赛", "超级联赛", "冠军联赛", "超级杯")):
        return False
    # 5. 其余默认敏感 (保守)
    return True

