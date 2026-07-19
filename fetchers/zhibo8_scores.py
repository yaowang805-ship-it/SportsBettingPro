"""直播吧 (zhibo8.com) 比分 API — 通过 qiumibao 后端获取比赛结果。

覆盖范围有限，主要是中国联赛 + 部分国际热门联赛。
适用于 ESPN 不覆盖的小联赛结算。
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import json
import re
import time

import requests

from config.logging_config import get_logger

logger = get_logger(__name__)

# qiumibao API 端点
_API_BASE = "https://bifen4pc.qiumibao.com"
_LIST_URL = f"{_API_BASE}/json/v2/list.htm"
_DETAIL_URL = f"{_API_BASE}/json/{{date}}/v2/{{id}}.htm"
_NEWS_BASE = "https://news.zhibo8.com"

# qiumibao code → (sport, 联赛名) 映射
# 这些 code 是 qiumibao 内部联赛编码
CODE_LEAGUE_MAP: Dict[str, Tuple[str, str]] = {
    "112": ("football", "中超"),
    "118": ("football", "中甲"),
    "121": ("football", "中乙"),
    "122": ("football", "中冠"),
    "85":  ("football", "女超"),
    "132": ("football", "足协杯"),
    "722": ("basketball", "CBA"),
    "723": ("basketball", "CBA"),  # 可能是 CBA 另一阶段
    # sport codes (不具体到联赛)
    "2":   ("football", None),     # 足球综合
    "3":   ("basketball", None),   # 篮球综合
    "4":   ("other", None),        # 综合
    "5":   ("esports", None),      # 电竞
    "7":   ("basketball", None),   # 篮球综合
}

# BB 联赛 → qiumibao code 映射
# 当 ESPN 不覆盖时尝试用 qiumibao
BB_TO_ZHIBO8_CODE: Dict[str, str] = {
    "中超": "112",
    "Chinese Super League": "112",
    "中国足球超级联赛": "112",
}

# 新闻页面文章 → 队伍名提取
_HOST_RE = re.compile(r'var p_host\s*=\s*[\'\"]([^\'\"]+)[\'\"]')
_GUEST_RE = re.compile(r'var p_guest\s*=\s*[\'\"]([^\'\"]+)[\'\"]')


def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    """通用 JSON 抓取."""
    try:
        resp = requests.get(url, timeout=timeout,
                           headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                                    "Referer": "https://zhibo8.com"})
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        logger.debug("qiumibao 请求失败 %s: %s", url.split("?")[0], e)
        return None


def _fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    """通用 HTML 抓取."""
    try:
        resp = requests.get(url, timeout=timeout,
                           headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                                    "Accept": "text/html,application/xhtml+xml"})
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception as e:
        logger.debug("页面请求失败 %s: %s", url, e)
        return None


def get_completed_matches(date: str = "") -> List[dict]:
    """从 qiumibao API 获取指定日期的已完成比赛。

    Args:
        date: "2026-07-15" 格式，默认今天

    Returns:
        [{id, code, state, start_time, left_score, right_score, left_id, right_id, ...}]
    """
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    data = _fetch_json(f"{_LIST_URL}?date={date}")
    if not data:
        return []

    matches = data.get("list", [])
    completed = []
    for m in matches:
        if m.get("state") != "3":  # 3 = 完赛
            continue
        try:
            left_score = int(m.get("left", {}).get("score", ""))
            right_score = int(m.get("right", {}).get("score", ""))
        except (ValueError, TypeError):
            continue

        completed.append({
            "id": m["id"],
            "code": m.get("code", ""),
            "start_time": m.get("start_time", 0),
            "left_score": left_score,
            "right_score": right_score,
            "left_id": m.get("left", {}).get("id", ""),
            "right_id": m.get("right", {}).get("id", ""),
            "half_score": m.get("half_score", ""),
        })

    return completed


def _extract_team_names_from_news(match_id: str) -> Optional[Tuple[str, str]]:
    """从 zhibo8 新闻页面提取队伍名（仅限主力赛事）。"""
    # 尝试足球和篮球两种 URL 模式
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    for date in (today, yesterday):
        for sport in ("zuqiu", "nba"):
            url = f"{_NEWS_BASE}/{sport}/{date}/match{match_id}date2026vnative.htm"
            html = _fetch_html(url, timeout=8)
            if html:
                host = _HOST_RE.search(html)
                guest = _GUEST_RE.search(html)
                if host and guest:
                    return (host.group(1), guest.group(1))

    # 也尝试旧的 match{v}.htm 模式
    url = f"https://zhibo8.com/zhibo/other/2026/match{match_id}v.htm"
    html = _fetch_html(url, timeout=5)
    if html:
        host = _HOST_RE.search(html)
        guest = _GUEST_RE.search(html)
        if host and guest:
            return (host.group(1), guest.group(1))

    return None


def fetch_zhibo8_results(days_back: int = 3) -> Dict[str, list]:
    """获取 zhibo8 上所有已完成比赛结果。

    Returns:
        {league_name: [{home_team, away_team, home_score, away_score, ...}]}
        联赛名为中文（如"中超"），team_name 可能为空字符串（无法获取时）
    """
    now = datetime.now(timezone.utc)
    all_results: Dict[str, list] = {}

    seen_ids: set = set()
    for offset in range(days_back):
        date = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        matches = get_completed_matches(date)
        if not matches:
            continue

        for m in matches:
            if m["id"] in seen_ids:
                continue
            seen_ids.add(m["id"])

            code = m["code"]
            league_info = CODE_LEAGUE_MAP.get(code)
            league_name = league_info[1] if league_info else f"code_{code}"

            # 尝试获取队伍名（从新闻页面）
            team_names = _extract_team_names_from_news(m["id"])
            if team_names:
                home_team, away_team = team_names
            else:
                home_team = f"team_{m['left_id']}"
                away_team = f"team_{m['right_id']}"

            result = {
                "home_team": home_team,
                "away_team": away_team,
                "home_score": m["left_score"],
                "away_score": m["right_score"],
                "completed": True,
                "scores": [
                    {"name": home_team, "score": str(m["left_score"])},
                    {"name": away_team, "score": str(m["right_score"])},
                ],
                "start_time": m["start_time"],
                "source": "zhibo8",
            }

            if league_name not in all_results:
                all_results[league_name] = []
            all_results[league_name].append(result)

    return all_results


def fetch_league_results(league: str, days_back: int = 3) -> list:
    """获取指定联赛的已完成比赛结果（通过 league 名查找）。

    仅在 league 在 BB_TO_ZHIBO8_CODE 中有明确映射时返回数据，
    避免误匹配。
    """
    code = BB_TO_ZHIBO8_CODE.get(league)
    if not code:
        return []

    all_results = fetch_zhibo8_results(days_back)
    league_name = CODE_LEAGUE_MAP.get(code, (None, None))[1]
    if league_name and league_name in all_results:
        return all_results[league_name]
    for lname, matches in all_results.items():
        if f"code_{code}" == lname:
            return matches
    return []


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()

    results = fetch_zhibo8_results(days_back=3)
    for league, matches in sorted(results.items()):
        print(f"\n{league}: {len(matches)} 场")
        for m in matches[:3]:
            print(f"  {m['home_team']} {m['home_score']}-{m['away_score']} {m['away_team']}")
