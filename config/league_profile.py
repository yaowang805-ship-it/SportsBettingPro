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
