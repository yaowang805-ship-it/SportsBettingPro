"""虚拟投注自动结算 — 根据 ESPN 已完成比赛结果自动结算待处理投注。"""
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
from fetchers.espn_scores import fetch_espn_scores, LEAGUE_ESPN_PATH
from src.risk.manager import RiskManager

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
        league_name = sk_to_l.get(sport_key)

    if league_name:
        espn_data = _fetch_completed_scores_espn(league_name, days_back)
        if espn_data:
            logger.info("  ESPN %s: %s 场已完成", league_name, len(espn_data))
            return espn_data

    logger.warning("ESPN 无数据: %s", sport_key)
    return []


def _normalize_team(name) -> str:
    """归一化队名以便匹配。"""
    if not isinstance(name, str):
        return ""
    return name.strip().lower().replace("fc", "").replace("cf", "").strip()


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

    支持 H2H（主胜/客胜/平）和 大小球（大X/小X）两种盘口类型。
    遍历多种队名候选（英文翻译、中文名、原始存储名），
    逐一尝试匹配比赛结果。
    """
    import re

    home_raw = bet.get("home_team", bet.get("home_cn", ""))
    away_raw = bet.get("away_team", bet.get("away_cn", ""))
    home_cn = bet.get("home_cn", "")
    away_cn = bet.get("away_cn", "")
    market = bet.get("market_type", bet.get("market_detail", ""))

    # 判断盘口类型（支持 大2.5 / 小1.5 和 over_2.5 / under_1.5 两种格式）
    ou_match = re.match(r'^(?:([大小])|(over|under))[_\s]*([\d.]+)$', market.strip(), re.IGNORECASE)
    is_over_under = ou_match is not None
    is_btts = market.strip().lower() in ("yes", "no")

    # 构建候选列表: 英文翻译 → 原始值 → 中文名
    home_candidates = []
    for name in [_normalize_team(home_raw), _normalize_team(home_cn)]:
        if name:
            home_candidates.append(name)
            en_name = _normalize_team(cn_to_odds_name(name))
            if en_name and en_name not in home_candidates:
                home_candidates.append(en_name)
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
                    line = float(ou_match.group(3))
                    direction = (ou_match.group(1) or ou_match.group(2)).lower()
                    if direction in ('大', 'over'):
                        return "won" if total > line else "lost"
                    else:  # 小 / under
                        return "won" if total < line else "lost"

                # ── BTTS 双方进球结算 ──
                if is_btts:
                    both_scored = home_score > 0 and away_score > 0
                    if market.strip().lower() == "yes":
                        return "won" if both_scored else "lost"
                    else:  # "no"
                        return "won" if not both_scored else "lost"

                # ── H2H 结算（主胜/客胜/平） ──
                is_home_win = home_score > away_score
                is_draw = home_score == away_score

                if "平" in market or "draw" in market.lower():
                    return "won" if is_draw else "lost"
                if "主胜" in market or "home" in market.lower():
                    return "won" if is_home_win else "lost"
                if "客胜" in market or "away" in market.lower():
                    return "won" if (away_score > home_score) else "lost"

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

        if sport_key:
            completed = _fetch_completed_scores(sport_key)
        else:
            # 无 Odds API 映射的联赛：直接用 ESPN
            league_name = display
            from fetchers.espn_scores import LEAGUE_ESPN_PATH
            if league_name in LEAGUE_ESPN_PATH:
                from fetchers.espn_scores import fetch_espn_scores
                completed = [
                    {"home_team": g["home_team"], "away_team": g["away_team"],
                     "completed": g.get("completed", True),
                     "scores": [{"name": g["home_team"], "score": str(g["home_score"])},
                                {"name": g["away_team"], "score": str(g["away_score"])}]}
                    for g in fetch_espn_scores(league_name, days_back=3)
                ]
            else:
                completed = []

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
                    profit = stake * (odds - 1) if result == "won" else -stake
                    logger.info("  ✅ %s → %s (盈亏¥%.0f)", bid[:40], result, profit)
                    # 记录到策略优化器
                    try:
                        from src.betting.strategy_optimizer import SettlementLogger
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
                        from src.risk.calibration import BetCalibrator
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

    return settled_count


def _auto_void_timeout(max_days: int = 3) -> int:
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
