#!/usr/bin/env python3
"""Pinnacle 赔率获取器 — 用 Playwright 绕过 Cloudflare，获取 Pinnacle 真实赔率。

支持足球和篮球，获取 Pinnacle 的去抽水公平价（真实概率）。
结果用于与 BB体育 赔率对比，发现 +EV 机会。

用法:
    python3 fetchers/pinnacle_fetcher.py --list-sports
    python3 fetchers/pinnacle_fetcher.py --sport 3  # 篮球
    python3 fetchers/pinnacle_fetcher.py --sport 1  # 足球
"""
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR

logger = get_logger(__name__)

# Pinnacle sport IDs
SPORTS = {
    1: "football",
    3: "basketball",
}

CACHE_DIR = DATA_DIR / "pinnacle"
CACHE_TTL = 600  # 10 分钟


def _load_cache(name: str):
    path = CACHE_DIR / f"{name}.json"
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > CACHE_TTL:
        return None
    return json.loads(path.read_text())


def _save_cache(name: str, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{name}.json").write_text(json.dumps(data, indent=2))


class PinnacleFetcher:
    """Pinnacle 赔率获取器。使用 Playwright 获取会话后通过 API 拉取数据。"""

    API_BASE = "https://guest.api.arcadia.pinnacle.com/api/v2"

    def __init__(self):
        self._session_cookies = None

    def _get_session(self) -> dict:
        """用 Playwright 获取 Pinnacle 会话 cookie。"""
        if self._session_cookies:
            return self._session_cookies

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("需要安装 playwright: pip install playwright && python3 -m playwright install chromium")
            return {}

        logger.info("  获取 Pinnacle 会话...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()

            # 拦截 API 请求
            api_responses = []

            def on_response(response):
                if "guest.api.arcadia.pinnacle.com" in response.url:
                    api_responses.append({"url": response.url, "status": response.status})

            page.on("response", on_response)

            # 访问 Pinnacle
            page.goto("https://www.pinnacle.com/en/basketball/nba/matchups", wait_until="networkidle", timeout=30000)
            time.sleep(3)

            # 获取 cookie
            cookies = context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            logger.info(f"  API 请求: {len(api_responses)} 个")
            logger.info(f"  Cookie: {len(cookies)} 个")

            browser.close()

            self._session_cookies = cookie_dict
            return cookie_dict

    def _api_call(self, path: str, params: Optional[Dict] = None) -> Optional[dict]:
        """用会话 cookie 调用 Pinnacle API。"""
        cookies = self._get_session()
        if not cookies:
            return None

        import requests

        url = f"{self.API_BASE}/{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.pinnacle.com/",
            "Origin": "https://www.pinnacle.com",
        }

        # 添加 cookie
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

        try:
            resp = requests.get(url, headers=headers, cookies={c["name"]: c["value"] for c in self._get_session().items()}, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 204:
                # 可能需要刷新会话
                logger.warning("  Pinnacle API 204 — 会话过期")
                self._session_cookies = None
                return None
            else:
                logger.warning(f"  Pinnacle API {resp.status_code}: {resp.text[:100]}")
                return None
        except Exception as e:
            logger.warning(f"  Pinnacle API 错误: {e}")
            return None

    def get_leagues(self, sport_id: int) -> List[dict]:
        """获取指定运动的联赛列表。"""
        cache = _load_cache(f"leagues_{sport_id}")
        if cache:
            return cache

        data = self._api_call("leagues", {"sportId": sport_id})
        if data and isinstance(data, list):
            _save_cache(f"leagues_{sport_id}", data)
            return data
        return []

    def get_events(self, league_id: int) -> List[dict]:
        """获取指定联赛的比赛和赔率。"""
        cache = _load_cache(f"events_{league_id}")
        if cache:
            return cache

        data = self._api_call("events", {"leagueId": league_id, "isLive": 0})
        if data:
            events = data if isinstance(data, list) else data.get("events", [])
            _save_cache(f"events_{league_id}", events)
            return events
        return []

    def get_odds(self, event_id: int) -> Optional[dict]:
        """获取单场比赛的赔率。"""
        cache = _load_cache(f"odds_{event_id}")
        if cache:
            return cache

        data = self._api_call(
            "odds",
            {"eventId": event_id, "oddsFormat": "DECIMAL"},
        )
        if data:
            _save_cache(f"odds_{event_id}", data)
            return data
        return None

    def get_moneyline_odds(self, event_id: int) -> Optional[Dict[str, float]]:
        """获取独赢（Moneyline）赔率，返回 {home, away}。"""
        odds = self.get_odds(event_id)
        if not odds:
            return None

        # 找 moneyline (priceId=standard, 通常是第一个)
        markets = odds.get("markets", [])
        for m in markets:
            if m.get("type") == "MONEYLINE":
                prices = m.get("prices", [])
                result = {}
                for p in prices:
                    side = "home" if p.get("designation") == "HOME" else "away"
                    result[side] = p.get("decimal")
                if "home" in result and "away" in result:
                    return result
                break

        # 如果没有 MONEYLINE 类型，尝试第一个市场
        if markets and markets[0].get("prices"):
            prices = markets[0]["prices"]
            result = {}
            for p in prices:
                side = "home" if p.get("designation") == "HOME" else "away"
                result[side] = p.get("decimal")
            if result:
                return result

        return None

    def scan_sport(self, sport_id: int) -> List[Dict]:
        """扫描整个运动的所有联赛，返回 +EV 机会列表。"""
        sport_name = SPORTS.get(sport_id, f"sport_{sport_id}")
        logger.info(f"  扫描 {sport_name}...")

        leagues = self.get_leagues(sport_id)
        if not leagues:
            logger.info(f"  无联赛数据")
            return []

        logger.info(f"  联赛: {len(leagues)} 个")
        all_events = []
        for league in leagues:
            league_id = league.get("id")
            league_name = league.get("name", f"league_{league_id}")
            events = self.get_events(league_id)
            for e in events:
                e["league_name"] = league_name
            all_events.extend(events)

        logger.info(f"  比赛: {len(all_events)} 场")

        # 获取每场比赛的赔率
        results = []
        for e in all_events:
            event_id = e.get("id")
            home = e.get("homeName", e.get("home", ""))
            away = e.get("awayName", e.get("away", ""))
            if not home or not away:
                continue

            odds = self.get_moneyline_odds(event_id)
            if not odds:
                continue

            # 去抽水
            imp_h = 1.0 / float(odds["home"])
            imp_a = 1.0 / float(odds["away"])
            total_imp = imp_h + imp_a
            vig = total_imp - 1.0

            if vig <= 0:
                prob_h, prob_a = imp_h, imp_a
            else:
                prob_h = imp_h / total_imp
                prob_a = imp_a / total_imp

            results.append({
                "sport": sport_name,
                "league": e.get("league_name", ""),
                "home_team": home,
                "away_team": away,
                "home_odds": float(odds["home"]),
                "away_odds": float(odds["away"]),
                "prob_home": round(prob_h, 4),
                "prob_away": round(prob_a, 4),
                "fair_price_home": round(1.0 / prob_h, 2),
                "fair_price_away": round(1.0 / prob_a, 2),
                "vig_pct": round(vig * 100, 2),
            })

        return results

    def list_sports(self):
        """列出所有支持的运动。"""
        print("\nPinnacle 支持的运动:")
        for sid, sname in SPORTS.items():
            leagues = self.get_leagues(sid)
            print(f"  sportId={sid} {sname}: {len(leagues)} 个联赛")
            for league in leagues[:10]:
                print(f"    - {league.get('name', '?')} (id={league.get('id', '?')})")
            if len(leagues) > 10:
                print(f"    ... 还有 {len(leagues)-10} 个")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Pinnacle 赔率获取器")
    parser.add_argument("--list-sports", action="store_true", help="列出支持的运动")
    parser.add_argument("--sport", type=int, choices=[1, 3], help="扫描指定运动: 1=足球, 3=篮球")
    args = parser.parse_args()

    fetcher = PinnacleFetcher()

    if args.list_sports:
        fetcher.list_sports()
        return

    if args.sport:
        results = fetcher.scan_sport(args.sport)
        print(f"\n{sports_name}: {len(results)} 场比赛")
        for r in results[:10]:
            print(f"  {r['home_team']:25} vs {r['away_team']:25} | {r['home_odds']:.2f} / {r['away_odds']:.2f} (抽水 {r['vig_pct']:.1f}%)")

    print("\n用法示例:")
    print("  python3 fetchers/pinnacle_fetcher.py --list-sports")
    print("  python3 fetchers/pinnacle_fetcher.py --sport 3  # 篮球")


if __name__ == "__main__":
    main()
