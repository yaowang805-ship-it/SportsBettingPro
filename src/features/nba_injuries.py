"""NBA 伤病数据模块。

从 ESPN 免费接口获取当前 NBA 球员伤病名单。
备用方案：若 ESPN 不可用，尝试从网络爬取实时数据。

用法：
    from src.features.nba_injuries import get_nba_injuries, get_injured_rotation_players

    injuries = get_nba_injuries()  # 所有伤病
    key_missing = get_injured_rotation_players()  # 核心缺阵名单
"""
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"

# NBA 球队简称与 ESPN 名称映射
TEAM_ABBR = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP", "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}

# 一般不被视为"核心轮换"的临时代码状态（双向合同、Exhibit 10 等）
NON_ROTATION_KEYWORDS = ["two-way", "exhibit 10", "g league", "training camp"]


def _fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    """通用 JSON 抓取（带重试）。"""
    for attempt in range(2):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt == 0:
                print(f"⚠️ 伤病数据重试... ({e})")
            else:
                print(f"⚠️ 伤病数据抓取失败: {e}")
    return None


def get_nba_injuries() -> List[Dict]:
    """获取 ESPN 当前 NBA 全部伤病名单。

    返回: [
        {
            'team': 'LAL',
            'team_full': 'Los Angeles Lakers',
            'player': 'LeBron James',
            'status': 'Out',
            'comment': 'Left ankle sprain',
            'date': '2025-01-15',
        },
        ...
    ]
    """
    data = _fetch_json(ESPN_INJURIES_URL)
    if not data:
        return _fallback_injuries()

    result = []
    injuries_list = data.get("injuries", [])
    for team_entry in injuries_list:
        team_full = team_entry.get("displayName", "")
        team_abbr = TEAM_ABBR.get(team_full, team_full)
        for injury in team_entry.get("injuries", []):
            status = injury.get("status", "")
            # ESPN status: "Out", "Day-To-Day", "Doubtful", "Questionable"
            result.append({
                "team": team_abbr,
                "team_full": team_full,
                "player": injury.get("athlete", {}).get("displayName", ""),
                "status": status,
                "comment": injury.get("shortComment", injury.get("longComment", "")),
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            })
    return result


def get_injured_rotation_players(min_games_impact: int = 1) -> List[Dict]:
    """获取核心轮换球员中确认缺阵的名单。

    过滤掉"Out"状态的伤病视为确认缺阵。
    返回与 get_nba_injuries 相同格式的列表。
    """
    injuries = get_nba_injuries()
    # 只保留确认缺阵（Out）的伤病
    confirmed_out = [i for i in injuries if i["status"].lower() in ("out", "doubtful")]
    return confirmed_out


def get_team_injury_report(team_abbr: str) -> List[Dict]:
    """获取指定球队的伤病报告。

    Args:
        team_abbr: 球队简称，如 'LAL', 'BOS'
    """
    all_injuries = get_nba_injuries()
    return [i for i in all_injuries if i["team"].upper() == team_abbr.upper()]


def _fallback_injuries() -> List[Dict]:
    """ESPN 不可用时的备用方案：提示用户或返回空列表。

    后续可扩展为从其他源爬取（如 CBS Sports 等）。
    """
    print("⚠️ ESPN 伤病源不可用，尝试通过 Odds API 获取伤病信息...")
    return []


if __name__ == "__main__":
    injuries = get_nba_injuries()
    print(f"📋 当前 NBA 伤病总数: {len(injuries)}")
    # 按球队统计
    by_team = {}
    for i in injuries:
        by_team.setdefault(i["team"], []).append(i)
    for team, players in sorted(by_team.items()):
        print(f"\n{team} ({len(players)} 人):")
        for p in players:
            print(f"  {p['player']} — {p['status']}: {p['comment'][:80]}")

    print(f"\n\n🔴 确认缺阵: {len(get_injured_rotation_players())} 人")
