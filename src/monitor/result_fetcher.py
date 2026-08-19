"""赛果抓取器 — 从多个数据源提取已结束比赛的比分。

数据源优先级:
1. ESPN API (site.api.espn.com) — 覆盖最广
2. football-data.org — 足球专用，含半场比分
3. 直播吧 (zhibo8) — 国内源，备份

匹配策略: Pinnacle 英文名精确匹配 → BB 中文名模糊匹配 → 时间窗口辅助
"""
import json, re, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
from typing import Optional

import requests

ROOT = Path(__file__).resolve().parent.parent.parent

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

# ESPN 运动 → slug 映射
ESPN_SPORT_SLUGS = {
    "football": "soccer",
    "basketball": "basketball",
    "baseball": "baseball",
    "american_football": "football",  # NFL/NCAA
    "ice_hockey": "hockey",
    "tennis": "tennis",
    "mma": "mma",
    "boxing": "boxing",
}

# football-data.org API (如有 key)
FOOTBALL_DATA_API_KEY = ""  # 免费 key: https://www.football-data.org/
FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"

# 直播吧 team name mapping (Chinese → English keywords)
ZBS_MAP = DATA_DIR / "team_name_map.json"

# 队名标准化 (复用已有映射)
def _load_team_map():
    """加载队名映射表。"""
    if ZBS_MAP.exists():
        try:
            raw = json.loads(ZBS_MAP.read_text())
            if "_meta" in raw:
                del raw["_meta"]
            return raw  # {cn: en}
        except (json.JSONDecodeError, OSError):
            pass
    return {}

_team_map = None

def _get_team_map():
    global _team_map
    if _team_map is None:
        _team_map = _load_team_map()
    return _team_map


def _normalize(name: str) -> str:
    """标准化队名用于匹配。"""
    if not name:
        return ""
    return re.sub(r'[^a-z0-9]', '', name.lower().strip())


def _team_matches(name1: str, name2: str) -> bool:
    """两个队名是否指向同一支队伍。"""
    n1 = _normalize(name1)
    n2 = _normalize(name2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    if n1 in n2 or n2 in n1:
        return True
    return False


def _cn_to_en(cn_name: str) -> Optional[str]:
    """中文队名 → 英文队名 (通过映射表)。"""
    team_map = _get_team_map()
    # 精确匹配
    if cn_name in team_map:
        return team_map[cn_name]
    # 去除空格后匹配
    cn_clean = cn_name.replace(" ", "").replace("　", "")
    for k, v in team_map.items():
        if k.replace(" ", "") == cn_clean:
            return v
    return None


def fetch_results(league_name: str, days_back: int = 3) -> list:
    """使用多源抓取器获取已结束比赛的结果 (ESPN + football-data.org + 直播吧等)。

    Returns:
        [{home_team, away_team, home_score, away_score, source, completed}, ...]
    """
    try:
        from fetchers.multi_source_scores import get_completed_scores
        scores = get_completed_scores(league_name, days_back=days_back)
        return scores
    except Exception as e:
        logger.warning("多源赛果抓取失败 [%s]: %s", league_name, e)
        return []


def fetch_football_data_results(league_code: str, days_back: int = 3) -> list:
    """从 football-data.org 获取足球比赛结果 (需要 API key)。

    league_code 如 'PL' (英超), 'PD' (西甲) 等。
    """
    if not FOOTBALL_DATA_API_KEY:
        return []

    results = []
    headers = {"X-Auth-Token": FOOTBALL_DATA_API_KEY}
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.now().strftime("%Y-%m-%d")

    try:
        url = f"{FOOTBALL_DATA_BASE}/competitions/{league_code}/matches"
        resp = requests.get(url, headers=headers,
                           params={"dateFrom": date_from, "dateTo": date_to, "status": "FINISHED"},
                           timeout=15)
        if resp.status_code != 200:
            return results
        data = resp.json()
        for match in data.get("matches", []):
            if match.get("status") != "FINISHED":
                continue
            home = match.get("homeTeam", {}).get("name", "")
            away = match.get("awayTeam", {}).get("name", "")
            score = match.get("score", {}).get("fullTime", {})
            home_score = score.get("home", 0) or 0
            away_score = score.get("away", 0) or 0

            results.append({
                "home": home,
                "away": away,
                "home_score": home_score,
                "away_score": away_score,
                "winner": match.get("winner", "").lower().replace("_team", ""),
                "status": "FINAL",
                "date": match.get("utcDate", ""),
                "league": match.get("competition", {}).get("name", ""),
                "source": "football_data",
            })
    except Exception as e:
        logger.debug("football-data.org 查询失败: %s", e)

    return results


def find_match_result(bet: dict, results: list) -> Optional[dict]:
    """在赛果列表中寻找与投注匹配的结果。

    匹配优先级:
    1. Pinnacle 英文名精确匹配 (home_pin == result.home AND away_pin == result.away)
    2. BB 中文名模糊匹配
    3. 联赛名 + 比赛日期辅助验证

    Returns: 匹配的 result dict, 或 None
    """
    home_pin = bet.get("home_pin", "").strip()
    away_pin = bet.get("away_pin", "").strip()
    home_bb = bet.get("home", "").strip()
    away_bb = bet.get("away", "").strip()
    match_epoch = bet.get("match_epoch", 0)

    best_result = None
    best_score = 0

    for r in results:
        score = 0
        r_home = r.get("home_team", "").strip()
        r_away = r.get("away_team", "").strip()
        completed = r.get("completed", False)
        if not completed:
            continue

        # 1. Pinnacle 名精确匹配 (分值最高)
        if home_pin and away_pin:
            if _team_matches(home_pin, r_home) and _team_matches(away_pin, r_away):
                score += 10
            elif _team_matches(home_pin, r_away) and _team_matches(away_pin, r_home):
                # 主客反转也接受 (不同数据源可能有不同取向)
                score += 8

        # 2. BB 中文名模糊匹配
        if home_bb and away_bb:
            if _team_matches(home_bb, r_home) and _team_matches(away_bb, r_away):
                score += 6
            elif _team_matches(home_bb, r_away) and _team_matches(away_bb, r_home):
                score += 4

        # 3. 中→英映射匹配
        home_en = _cn_to_en(home_bb)
        away_en = _cn_to_en(away_bb)
        if home_en and away_en:
            if _team_matches(home_en, r_home) and _team_matches(away_en, r_away):
                score += 5

        # 4. 联赛名辅助 (同一天的同一个联赛)
        r_league = r.get("league", "")
        bet_league = bet.get("league", "")
        if bet_league and r_league:
            # 子串匹配
            if bet_league.lower() in r_league.lower() or r_league.lower() in bet_league.lower():
                score += 1

        # 5. 时间窗口 (±12h)
        if match_epoch:
            try:
                r_date = r.get("date", "")
                if r_date:
                    r_epoch = datetime.fromisoformat(r_date.replace("Z", "+00:00")).timestamp()
                    if abs(r_epoch - match_epoch) < 12 * 3600:
                        score += 1
            except (ValueError, TypeError):
                pass

        if score > best_score:
            best_score = score
            best_result = r

    # 降低门槛: 多源抓取不返回联赛/时间信息, 队名匹配即可
    if best_score >= 2:
        return best_result
    return None


def determine_result(bet: dict, match_result: dict) -> tuple:
    """根据比赛结果判定投注输赢。

    支持的盘口类型:
    - 1x2 (独赢): home/draw/away
    - hc (让球): 含亚洲让球线
    - ou (大小): over/under
    - dc (双重机会): home_draw/home_away/draw_away
    - ht (上半场): 上半场独赢/让球
    - btts (双方进球): yes/no

    Returns: (result, home_score, away_score, profit_multiplier)
        result: won/lost/void/half_won/half_lost
        profit_multiplier: 盈亏倍数 (1=全赢, 0.5=半赢, -0.5=半输, -1=全输, 0=void)
    """
    home_score = match_result.get("home_score", 0)
    away_score = match_result.get("away_score", 0)
    sub_market = bet.get("sub_market", "1x2")
    designation = bet.get("designation", "")

    # V5.10: 半场盘口改用真实半场比分判定。
    # 以前这里对 ht/ht_hc/ht_ou/ht_dc 一律 return "void"(注释写"ESPN 不提供半场比分"),
    # 结果 133 笔 void 里 60 笔(45%)是这么白丢的, 涉及 ¥7,838 下注额。
    # 现在 BB getMatchDetail 会给 ht_home_score/ht_away_score(且经 HT+2H==FT 自校验),
    # 有半场比分时就把它当成"该场比分"递归走全场那套判定逻辑, 口径完全一致。
    # 仍然拿不到半场比分时才退回 void —— 绝不用全场比分近似(会判反盈亏)。
    if sub_market.startswith("ht"):
        ht_h = match_result.get("ht_home_score")
        ht_a = match_result.get("ht_away_score")
        if ht_h is not None and ht_a is not None:
            _base = {"ht": "1x2", "ht_hc": "hc", "ht_ou": "ou",
                     "ht_dc": "dc", "ht_dnb": "dnb"}.get(sub_market)
            if _base:
                _bet = dict(bet)
                _bet["sub_market"] = _base
                _mr = dict(match_result)
                _mr["home_score"], _mr["away_score"] = ht_h, ht_a
                _mr.pop("ht_home_score", None)   # 防递归
                _mr.pop("ht_away_score", None)
                res, _h, _a, mult = determine_result(_bet, _mr)
                # 比分回报半场分, 让结算记录如实反映判定依据
                return res, ht_h, ht_a, mult

    # 判断比赛结果方向
    if home_score > away_score:
        outcome = "home"
    elif away_score > home_score:
        outcome = "away"
    else:
        outcome = "draw"

    total_goals = home_score + away_score
    goal_diff = home_score - away_score  # 正=主胜, 负=客胜

    # ── 1x2 独赢 ──
    if sub_market == "1x2":
        if "主" in designation and "客" not in designation and "和" not in designation:
            target = "home"
        elif "客" in designation:
            target = "away"
        elif "和" in designation:
            target = "draw"
        else:
            target = designation.lower()

        if target == outcome:
            return "won", home_score, away_score, 1.0
        else:
            return "lost", home_score, away_score, -1.0

    # ── 让球 (含亚洲盘) ──
    if sub_market == "hc":
        # 解析让球线
        line_str = designation
        line = _parse_handicap_line(line_str)
        # 确定投注方向
        if "主" in line_str:
            adjusted_diff = goal_diff + line  # 主队受让
        elif "客" in line_str:
            adjusted_diff = -goal_diff + line  # 客队受让
        else:
            return "void", home_score, away_score, 0

        if adjusted_diff > 0.25:
            return "won", home_score, away_score, 1.0
        elif adjusted_diff == 0.25:
            return "half_won", home_score, away_score, 0.5  # 赢半
        elif adjusted_diff == -0.25:
            return "half_lost", home_score, away_score, -0.5  # 输半
        elif adjusted_diff == 0:
            return "void", home_score, away_score, 0  # 走水
        else:
            return "lost", home_score, away_score, -1.0

    # ── 大小球 ──
    if sub_market == "ou":
        line_str = designation
        line = _parse_ou_line(line_str)
        if "大" in line_str:
            diff = total_goals - line
        elif "小" in line_str:
            diff = line - total_goals
        else:
            return "void", home_score, away_score, 0

        if diff > 0.25:
            return "won", home_score, away_score, 1.0
        elif diff == 0.25:
            return "half_won", home_score, away_score, 0.5
        elif diff == -0.25:
            return "half_lost", home_score, away_score, -0.5
        elif diff == 0:
            return "void", home_score, away_score, 0
        else:
            return "lost", home_score, away_score, -1.0

    # ── 双重机会 ──
    if sub_market == "dc":
        target = designation.lower()
        if "和局/主" in target or "主/和" in target:
            if outcome in ("home", "draw"):
                return "won", home_score, away_score, 1.0
        elif "和局/客" in target or "客/和" in target:
            if outcome in ("away", "draw"):
                return "won", home_score, away_score, 1.0
        elif "主/客" in target:
            if outcome in ("home", "away"):
                return "won", home_score, away_score, 1.0
        else:
            return "void", home_score, away_score, 0
        return "lost", home_score, away_score, -1.0

    # ── 上半场 (独赢部分) ──
    if sub_market == "ht":
        # 上半场比分数据源不可用(ESPN 不提供), 用全场比分近似会判反盈亏 → 保守 void
        return "void", home_score, away_score, 0

    # ── 双方进球 ──
    if sub_market == "btts":
        both_scored = home_score > 0 and away_score > 0
        if "是" in designation.lower() or "yes" in designation.lower():
            return "won" if both_scored else "lost", home_score, away_score, (1.0 if both_scored else -1.0)
        elif "否" in designation.lower() or "no" in designation.lower():
            return "won" if not both_scored else "lost", home_score, away_score, (1.0 if not both_scored else -1.0)
        return "void", home_score, away_score, 0

    # ── 平局退款 (draw no bet) ──
    if sub_market == "dnb":
        if "客" in designation:
            if outcome == "away":
                return "won", home_score, away_score, 1.0
            elif outcome == "draw":
                return "void", home_score, away_score, 0  # 平局退款
            else:
                return "lost", home_score, away_score, -1.0
        elif "主" in designation:
            if outcome == "home":
                return "won", home_score, away_score, 1.0
            elif outcome == "draw":
                return "void", home_score, away_score, 0  # 平局退款
            else:
                return "lost", home_score, away_score, -1.0
        else:
            return "void", home_score, away_score, 0

    # ── 半场让球/大小/双重机会/退款 (需半场比分, 当前数据源无 → 保守 void, 避免用全场比分误判) ──
    if sub_market in ("ht_hc", "ht_ou", "ht_dc", "ht_dnb"):
        return "void", home_score, away_score, 0

    # ── 默认: 无法判定 → void ──
    return "void", home_score, away_score, 0


def _parse_handicap_line(line_str: str) -> float:
    """解析亚洲让球线，如 '-0.5', '+0.5/1', '0', '-0/0.5'。

    折中线符号规则: '-0.5/1' = -0.5 和 -1 各半 = -0.75 (符号由首字符决定)。
    旧实现用 re.findall 拆数字, '-0.5/1' 会把第二个数 '1' 当正数 → 得 +0.25, 判反盈亏。
    """
    if not line_str:
        return 0
    # 提取带符号的盘口线 token (如 '让分主胜(-0.5/1)' → '-0.5/1')
    m = re.search(r'[-+]?\d+(?:\.\d+)?(?:/[-+]?\d+(?:\.\d+)?)?', str(line_str))
    if not m:
        return 0
    token = m.group(0)
    sign = -1 if token.startswith('-') else 1
    token = token.lstrip('+-')
    try:
        if '/' in token:
            parts = [float(p) for p in token.split('/')]
            return sign * sum(parts) / len(parts)
        return sign * float(token)
    except (ValueError, TypeError):
        return 0


def _parse_ou_line(line_str: str) -> float:
    """解析大小球线，如 '大球(2.5)', '小球(3.0/3.5)'。"""
    return _parse_handicap_line(line_str)


def settle_pending_bets(dry_run: bool = False) -> dict:
    """批量结算所有待处理的投注。

    1. 获取所有 pending 且比赛已结束的投注
    2. 按联赛分组，从数据源获取赛果
    3. 逐条匹配并判定结果

    Returns: {settled: N, failed: N, details: [...]}
    """
    from src.monitor.bet_tracker import get_unsettled_bets, settle_bet

    bets = get_unsettled_bets(hours_after_match=1.0)  # 比赛结束后1小时开始结算
    if not bets:
        logger.info("无待结算投注")
        return {"settled": 0, "failed": 0, "details": []}

    logger.info("开始结算: %d 笔待处理", len(bets))

    # 按运动+联赛分组
    by_sport_league = defaultdict(list)
    for b in bets:
        sport = b.get("sport", "football")
        league = b.get("league", "")
        key = f"{sport}|{league}"
        by_sport_league[key].append(b)

    settled = 0
    failed = 0
    details = []

    for key, group_bets in by_sport_league.items():
        sport, league = key.split("|", 1)
        logger.info("  查询 [%s] %s: %d 笔", sport, league, len(group_bets))

        # 获取赛果 (多源: ESPN + football-data.org + 直播吧)
        results = fetch_results(league, days_back=3)

        if not results:
            logger.info("    无赛果数据，跳过 %d 笔", len(group_bets))
            failed += len(group_bets)
            continue

        for bet in group_bets:
            match_result = find_match_result(bet, results)
            if not match_result:
                logger.debug("    未匹配: %s vs %s", bet.get("home", "?"), bet.get("away", "?"))
                failed += 1
                continue

            result, home_score, away_score, multiplier = determine_result(bet, match_result)

            if dry_run:
                details.append({
                    "push_id": bet["push_id"][:60],
                    "home": bet.get("home", "?"),
                    "away": bet.get("away", "?"),
                    "result": result,
                    "score": f"{home_score}-{away_score}",
                    "would_settle": True,
                })
                settled += 1
            else:
                ok = settle_bet(
                    bet["push_id"], result,
                    home_score=home_score, away_score=away_score,
                    source=match_result.get("source", "espn")
                )
                if ok:
                    settled += 1
                else:
                    failed += 1

    summary = {"settled": settled, "failed": failed, "details": details}
    logger.info("结算完成: %d 笔成功, %d 笔失败", settled, failed)
    return summary
