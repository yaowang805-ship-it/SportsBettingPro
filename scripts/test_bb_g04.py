#!/usr/bin/env python3
"""测试：在真实浏览器里新建 push WS + 发 subscribe，收 G04 赔率推送。

关键(2026-09-27): tap 读不到 G04 的根因是浏览器网页只订阅了「比赛信息」(chId)/「联赛信息」(L04)，
没主动发 subscribe 订阅「赔率」(G04)。这里用 page.evaluate 在浏览器里新建 WebSocket
(走真实 Chrome 153 网络栈 = 真实 TLS 指纹)，发 subscribe 收 G04。

用法: .venv312/bin/python scripts/test_bb_g04.py [--seconds 60]
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

PUSH_DOMAIN = "pushi32e54ih.h906qtx274jb.com"  # 缓存的 push 域名
USER_ID = 2150894  # BBWsPushClient 硬编码 userId


def get_live_league_ids():
    """拉滚球(type=1)联赛 ID 用于 subscribe。"""
    from src.betting.bb_auto_bet import read_token, read_domain, _session
    token = read_token()
    domain = read_domain()
    if not token:
        return []
    s = _session()
    lids = set()
    for sport_id in (1, 3, 5, 7, 13, 15):  # 6 个滚球主运动
        try:
            r = s.post(f"{domain}/v1/match/getList",
                       json={"sportId": sport_id, "type": 1, "current": 1,
                             "pageSize": 50, "isPC": True, "languageType": "CMN"},
                       headers={"Content-Type": "application/json", "user-token": token,
                                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                                               "Chrome/153.0.0.0 Safari/537.36")},
                       timeout=15, verify=False)
            d = r.json()
            if d.get("code") != 0:
                continue
            for m in (d.get("data") or {}).get("records") or []:
                # 滚球(type=1)联赛 ID 在 lg.id，不是 lid/leagueId
                lid = (m.get("lg") or {}).get("id") or m.get("lid") or m.get("leagueId")
                if lid:
                    lids.add(int(lid))
        except Exception:
            pass
    return sorted(lids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    args = ap.parse_args()

    leagues = get_live_league_ids()
    print(f"滚球联赛 ID 数: {len(leagues)}")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0]
        pg = None
        for page in ctx.pages:
            if "vv899" in page.url or "bbty" in page.url:
                pg = page
                break
        if not pg:
            print("没找到 BB 页")
            return

        # 在浏览器里新建 WS + subscribe + 收 G04
        js = f"""
        (() => {{
            if (window.__bbws) {{ try {{ window.__bbws.close(); }} catch(e){{}} }}
            window.__g04 = [];
            window.__g04_n = 0;
            window.__g04_hello = 0;
            const ws = new WebSocket('wss://{PUSH_DOMAIN}/');
            window.__bbws = ws;
            ws.onopen = () => {{
                ws.send(JSON.stringify({{cmd:'subscribe', channel:{json.dumps(leagues)}, userId:{USER_ID}}}));
            }};
            ws.onmessage = (e) => {{
                try {{
                    const d = JSON.parse(e.data);
                    if (d.cmd === 'G04') {{
                        window.__g04.push({{t: Date.now(), market: d.data && d.data.market, matchId: d.data && d.data.matchId, items: d.data && d.data.items}});
                        window.__g04_n++;
                    }} else if (d.cmd === 'hello') {{
                        window.__g04_hello++;
                    }}
                }} catch(err) {{}}
            }};
            return 'ok';
        }})()
        """
        r = pg.evaluate(js)
        print("JS 注入:", r)

        # 轮询读 G04
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            time.sleep(3)
            hello = pg.evaluate("() => window.__g04_hello")
            n = pg.evaluate("() => window.__g04_n")
            print(f"  hello(订阅确认)={hello} G04数={n}")
            if n > 0:
                samples = pg.evaluate("() => window.__g04.slice(0,3)")
                for s in samples:
                    print("    G04:", json.dumps(s, ensure_ascii=False)[:200])
                break
        browser.close()


if __name__ == "__main__":
    main()
