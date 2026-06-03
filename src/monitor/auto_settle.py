"""虚拟投注自动结算 — 根据 Odds API 已完成比赛结果自动结算待处理投注。"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import requests

from config.logging_config import get_logger
from config.settings import ODDS_API_KEY
from src.dashboard.components.virtual_portfolio import (
    _load_state, _save_state, settle_bet,
)
from src.core.team_names import cn_to_odds_name
from src.fetchers.espn_scores import fetch_espn_scores, LEAGUE_ESPN_PATH

logger = get_logger(__name__)

API_BASE = "https://api.the-odds-api.com/v4/sports"

# 联赛名 → (sport key for odds API, display name)
LEAGUE_SPORT_MAP = {
    "NBA": ("basketball_nba", "NBA"),
    "英超": ("soccer_epl", "英超"),
    "西甲": ("soccer_spain_la_liga", "西甲"),
    "德甲": ("soccer_germany_bundesliga", "德甲"),
    "意甲": ("soccer_italy_serie_a", "意甲"),
    "法甲": ("soccer_france_ligue_one", "法甲"),
}

# 兜底：sport 字段 → sport key（精确匹配）
SPORT_FALLBACK = {
    "nba": "basketball_nba",
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
        })
    return odds_format


def _fetch_completed_scores(sport_key: str, days_back: int = 3) -> list:
    """获取已结束比赛比分：优先 ESPN（免费），降级到 Odds API。"""
    # 先尝试 ESPN
    league_name = None
    for lname, (spath, _) in LEAGUE_ESPN_PATH.items():
        if sport_key.replace("_", "/") == spath or \
           spath.replace("/", "_") == sport_key or \
           sport_key.endswith(spath.split("/")[-1]) or \
           spath.startswith(sport_key.replace("_", "/")):
            league_name = lname
            break
    # 直接通过映射查
    if not league_name:
        sk_to_l = {
            "basketball_nba": "NBA", "soccer_epl": "英超",
            "soccer_spain_la_liga": "西甲", "soccer_germany_bundesliga": "德甲",
            "soccer_italy_serie_a": "意甲", "soccer_france_ligue_one": "法甲",
        }
        league_name = sk_to_l.get(sport_key)

    if league_name:
        espn_data = _fetch_completed_scores_espn(league_name, days_back)
        if espn_data:
            logger.info("  ESPN %s: %s 场已完成", league_name, len(espn_data))
            return espn_data

    # 降级：Odds API
    logger.debug("ESPN 不可用，降级到 Odds API: %s", sport_key)
    for d in [days_back, 2, 1]:
        url = f"{API_BASE}/{sport_key}/scores/?apiKey={ODDS_API_KEY}&daysFrom={d}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 422:
                logger.debug("比分API daysFrom=%s 无效，降级重试", d)
            else:
                logger.warning("比分API返回 %s: %s", resp.status_code, resp.text[:100])
                return []
        except Exception as e:
            logger.warning("比分API请求失败 %s: %s", sport_key, e)
            return []
    logger.warning("比分API %s 所有 daysFrom 均失败", sport_key)
    return []


def _normalize_team(name) -> str:
    """归一化队名以便匹配。"""
    if not isinstance(name, str):
        return ""
    return name.strip().lower().replace("fc", "").replace("cf", "").strip()


def _match_bet(bet: dict, completed_games: list) -> Optional[str]:
    """尝试将投注与已完成比赛匹配，返回 'won' 或 'lost' 或 None。

    遍历多种队名候选（英文翻译、中文名、原始存储名），
    逐一尝试匹配 Odds API 返回的已完成比赛结果。
    """
    home_raw = bet.get("home_team", bet.get("home_cn", ""))
    away_raw = bet.get("away_team", bet.get("away_cn", ""))
    home_cn = bet.get("home_cn", "")
    away_cn = bet.get("away_cn", "")
    market = bet.get("market_type", bet.get("market_detail", ""))

    # 构建候选列表: 英文翻译 → 原始值 → 中文名
    home_candidates = []
    for name in [_normalize_team(home_raw), _normalize_team(home_cn)]:
        if name:
            home_candidates.append(name)
            # 尝试中文→英文翻译
            en_name = _normalize_team(cn_to_odds_name(name))
            if en_name and en_name not in home_candidates:
                home_candidates.append(en_name)
    # 去重保持顺序
    seen_h = set()
    home_cands = [c for c in home_candidates if not (c in seen_h or seen_h.add(c))]

    away_candidates = []
    for name in [_normalize_team(away_raw), _normalize_team(away_cn)]:
        if name:
            away_candidates.append(name)
            en_name = _normalize_team(cn_to_odds_name(name))
            if en_name and en_name not in away_candidates:
                away_candidates.append(en_name)
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
                # 正向匹配
                if (hc in api_home or api_home in hc) and (ac in api_away or api_away in ac):
                    found_h, found_a = api_home, api_away
                # 主客互换
                elif (hc in api_away or api_away in hc) and (ac in api_home or api_home in ac):
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

                is_home_win = home_score > away_score
                bet_on_home = "主胜" in market or "home" in market.lower()

                if is_home_win and bet_on_home:
                    return "won"
                if not is_home_win and not bet_on_home and "客胜" in market:
                    return "won"
                if home_score == away_score and "平" in market:
                    return "won"
                # 未命中
                return "lost"

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

    # 按运动分组获取比分
    sport_groups = {}
    for bet in pending:
        sport = bet.get("sport", "")
        if sport not in sport_groups:
            sport_groups[sport] = []
        sport_groups[sport].append(bet)

    for sport, bets in sport_groups.items():
        # 用第一笔投注的 league 确定 odds API sport key
        sample_bet = bets[0]
        league = sample_bet.get("league", "")
        api_key_info = LEAGUE_SPORT_MAP.get(league)
        if not api_key_info:
            # 兜底：用 sport 字段在 SPORT_FALLBACK 中查找
            sk = SPORT_FALLBACK.get(sport)
            if sk:
                api_key_info = (sk, league or sport)
            else:
                logger.warning("未知联赛: %s (sport=%s)，跳过 %s 笔", league, sport, len(bets))
                continue

        sport_key, display = api_key_info
        logger.info("获取 %s 已完成比赛...", display)
        completed = _fetch_completed_scores(sport_key)
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
                    logger.info("  ✅ %s → %s (盈亏¥%.0f)", bid[:40], result,
                                stake * (odds - 1) if result == "won" else -stake)
                settled_count += 1

    if settled_count == 0:
        logger.info("未找到可结算的投注（比赛可能仍未结束）")
    else:
        logger.info("自动结算完成: %s 笔", settled_count)

    return settled_count


def main():
    auto_settle(dry_run="--dry-run" in sys.argv)


if __name__ == "__main__":
    main()
