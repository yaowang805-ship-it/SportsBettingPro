"""休赛期检测 — 检查指定运动是否有进行中的比赛。

用于 daily_bb.py / daily_fb.py 在赛季外自动跳过，避免 API 空跑。
"""
import requests

from config.logging_config import get_logger
logger = get_logger(__name__)

from config.settings import ODDS_API_KEY
from fetchers.espn_scores import fetch_espn_scores, LEAGUE_ESPN_PATH


def has_upcoming_games(sport: str, league: str = None, days_back: int = 2) -> bool:
    """检查指定运动是否有比赛数据（免费 ESPN 优先）。

    Args:
        sport: "nba" / "football"
        league: ESPN 联赛名；football 时传 None 会自动检查五大联赛
        days_back: 往回看的天数

    Returns:
        True = 有比赛（赛季中），False = 无比赛（休赛期）
    """
    if sport == "football":
        # 足球：检查五大联赛 + 世界杯，任一有数据即赛季中
        for fb_league in ("英超", "西甲", "德甲", "意甲", "法甲", "世界杯"):
            try:
                games = fetch_espn_scores(fb_league, days_back=days_back)
                if games:
                    return True
            except Exception:
                continue
        # 兜底 check Odds API 足球列表（免费 endpoint）
        try:
            url = f"https://api.the-odds-api.com/v4/sports?apiKey={ODDS_API_KEY}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                for s in resp.json():
                    if s["key"].startswith("soccer_") and s.get("active", False):
                        return True
        except Exception:
            pass
        logger.info("  ℹ️ 足球当前无比赛（休赛期），跳过")
        return False

    # NBA
    if sport == "nba":
        try:
            games = fetch_espn_scores("NBA", days_back=days_back)
            if games:
                return True
        except Exception:
            pass
        # Odds API 兜底
        try:
            url = f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds/?apiKey={ODDS_API_KEY}&regions=us&markets=h2h&oddsFormat=decimal"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0:
                    return True
        except Exception:
            pass
        logger.info("  ℹ️ NBA 当前无比赛（休赛期），跳过")
        return False

    # 通用 league 参数
    if league and league in LEAGUE_ESPN_PATH:
        try:
            games = fetch_espn_scores(league, days_back=days_back)
            if games:
                return True
        except Exception:
            pass

    logger.info("  ℹ️ %s 当前无比赛，跳过", sport.upper())
    return False
