"""OddsPortal 历史赔率抓取器 (Playwright) — 从结果列表页直接提取平均赔率。

替代 oddsharvester: 它的事件行选择器 + 比赛链接结构都因 OddsPortal 改版失效。
本脚本直接从 results 列表页提取 date/team/score/average-odds，无需逐场进比赛页。

用法:
    .venv312/bin/python data/pinnacle_historical/op_scraper.py \
        --sport basketball --league euroleague --season 2024-2025 \
        -m 1x2 --out data/oddsportal/basketball/euroleague__2024-2025.csv

也支持批量: 省略 --league 时下载该运动全部联赛。
"""
import sys, re, csv, time, argparse
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv312" / "lib" / "python3.12" / "site-packages"))

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
BASE = "https://www.oddsportal.com"

MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
          "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}

def parse_date(header):
    m = re.match(r"(\d{1,2}) (\w{3}) (\d{4})(?: - (.+))?", header.strip())
    if not m:
        return "", ""
    d, mon, y, tag = m.group(1), m.group(2), m.group(3), (m.group(4) or "").lower()
    dt = f"{y}-{MONTHS.get(mon,1):02d}-{int(d):02d}"
    if "play" in tag or "final" in tag or "semi" in tag or "quarter" in tag:
        mt = "play_off"
    elif "pre" in tag:
        mt = "pre_season"
    else:
        mt = "regular"
    return dt, mt


def scrape_league(sport, league_url, season, market="1x2", out_path=None):
    """抓取单个联赛某赛季。league_url 形如 /basketball/europe/euroleague (无协议)。"""
    league_url = league_url.rstrip("/")  # 已是完整 URL (含协议)
    # 赛季附加: oddsharvester 的规则是 {league_url}-{season}
    if season and season.lower() != "current":
        url = f"{league_url}-{season}/results/"
    else:
        url = f"{league_url}/results/"

    games, seen = [], set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROME,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.set_default_timeout(60000)  # 慢页容错, 防 Locator 30s 超时

        page_no = 1
        while True:
            target = url if page_no == 1 else f"{url}#/page/{page_no}"
            # 重试一次: 偶发 ERR_CONNECTION_CLOSED / 慢加载
            ok = False
            for attempt in range(2):
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=60000)
                    ok = True
                    break
                except Exception as e:
                    if attempt == 1:
                        print(f"  [warn] page {page_no} goto fail: {e}", flush=True)
                    else:
                        page.wait_for_timeout(4000)
            if not ok:
                break
            page.wait_for_timeout(6000)
            # 滚动到底加载懒加载内容
            for _ in range(6):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)

            # 按文档顺序遍历 date-header + game-row
            nodes = page.locator("[data-testid='date-header'], [data-testid='game-row']")
            n = nodes.count()
            batch = 0
            cur_date = cur_mt = ""
            for i in range(n):
                el = nodes.nth(i)
                tid = el.get_attribute("data-testid")
                if tid == "date-header":
                    d, mt = parse_date(el.inner_text())
                    if d:
                        cur_date, cur_mt = d, mt
                    continue
                # game-row
                try:
                    host = el.locator("[data-testid='game-host']").inner_text().strip()
                    guest = el.locator("[data-testid='game-guest']").inner_text().strip()
                    odds = el.locator("[data-testid='odd-container-default'], "
                                      "[data-testid='odd-container-winning']").all_inner_texts()
                    odds = [o.strip() for o in odds if o.strip()]
                    scores = el.locator("[data-testid='event-participants'] span.font-bold").all_inner_texts()
                    scores = [s.strip() for s in scores if s.strip().isdigit()]
                except Exception:
                    continue
                if not host or not guest:
                    continue
                hs = scores[0] if len(scores) >= 1 else ""
                gs = scores[1] if len(scores) >= 2 else ""
                # 3-way(足球: 主/平/客) vs 2-way(篮球/网球等: 主/客)
                if len(odds) >= 3:
                    ho, do, go = odds[0], odds[1], odds[2]
                else:
                    ho = odds[0] if odds else ""
                    go = odds[1] if len(odds) >= 2 else ""
                    do = ""
                key = (cur_date, host, guest, ho, do, go)
                if key in seen:
                    continue
                seen.add(key)
                games.append({"date": cur_date, "match_type": cur_mt,
                              "home_team": host, "away_team": guest,
                              "home_score": hs, "away_score": gs,
                              "home_odds": ho, "draw_odds": do, "away_odds": go})
                batch += 1
            print(f"  page {page_no}: +{batch} (累计 {len(games)})", flush=True)
            if batch == 0:
                break
            page_no += 1
            if page_no > 60:
                break
        browser.close()

    if out_path and games:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cols = ["date", "match_type", "home_team", "away_team",
                "home_score", "away_score", "home_odds", "draw_odds", "away_odds"]
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for g in games:
                w.writerow(g)
    return games


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", required=True)
    ap.add_argument("--league", default=None)
    ap.add_argument("--season", default="2024-2025")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from oddsharvester.utils.sport_league_constants import SPORTS_LEAGUES_URLS_MAPPING
    from oddsharvester.utils.sport_market_constants import Sport
    leagues = {}
    for s, lm in SPORTS_LEAGUES_URLS_MAPPING.items():
        if getattr(s, "value", str(s)) == args.sport:
            leagues = lm
            break
    if not leagues:
        print(f"未知运动: {args.sport}")
        return

    targets = {args.league: leagues[args.league]} if args.league else leagues
    out_dir = ROOT / "data" / "oddsportal" / args.sport
    for lg, lg_url in targets.items():
        out = Path(args.out) if args.out else out_dir / f"{lg}__{args.season}.csv"
        if out.exists() and out.stat().st_size > 100:
            print(f"[skip] {lg} 已存在", flush=True)
            continue
        print(f"[start] {args.sport}/{lg} {args.season}", flush=True)
        t0 = time.time()
        games = scrape_league(args.sport, lg_url, args.season, out_path=out)
        print(f"[done] {lg}: {len(games)} 场 ({time.time()-t0:.0f}s)", flush=True)

if __name__ == "__main__":
    main()
