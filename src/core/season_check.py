"""休赛期检测 — 检查指定运动是否有进行中的比赛。

仅使用免费 ESPN API，不消耗 Odds API 配额。

用于 daily_bb.py / daily_fb.py / daily_nfl.py 在赛季外自动跳过。
"""
from config.logging_config import get_logger
logger = get_logger(__name__)

from fetchers.espn_scores import fetch_espn_scores


def has_upcoming_games(sport: str, days_back: int = 2) -> bool:
    """检查指定运动是否有比赛数据（免费 ESPN API）。

    Args:
        sport: "nba" / "football" / "nfl"
        days_back: 往回看的天数

    Returns:
        True = 有比赛（赛季中），False = 无比赛（休赛期）
    """
    if sport == "football":
        for fb_league in ("英超", "西甲", "德甲", "意甲", "法甲", "世界杯"):
            try:
                games = fetch_espn_scores(fb_league, days_back=days_back)
                if games:
                    return True
            except Exception:
                continue
        logger.info("  ℹ️ 足球当前无比赛（休赛期），跳过")
        return False

    if sport == "nba":
        try:
            games = fetch_espn_scores("NBA", days_back=days_back)
            if games:
                return True
        except Exception:
            pass
        logger.info("  ℹ️ NBA 当前无比赛（休赛期），跳过")
        return False

    if sport == "nfl":
        try:
            games = fetch_espn_scores("NFL", days_back=days_back)
            if games:
                return True
        except Exception:
            pass
        logger.info("  ℹ️ NFL 当前无比赛（休赛期），跳过")
        return False

    # 通用 league 参数（兜底）
    from fetchers.espn_scores import LEAGUE_ESPN_PATH
    if sport in LEAGUE_ESPN_PATH:
        try:
            games = fetch_espn_scores(sport, days_back=days_back)
            if games:
                return True
        except Exception:
            pass

    logger.info("  ℹ️ %s 当前无比赛，跳过", sport.upper())
    return False
