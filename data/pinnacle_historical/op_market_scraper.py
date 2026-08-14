"""OddsPortal 逐场盘口收盘价抓取器 — HC/OU/DC/DNB/BTTS/OE 补充 op_scraper 只抓 1X2 的缺口。

用已破解的加密 (见 op_market_probe.decrypt_payload) 直接 HTTP 调 betting-exchanges 接口,
无需浏览器/Playwright, 每条比赛只需 N 次轻量 GET (N=盘口数)。

盘口 → bt 映射 (来自 event-page-metadata):
  1=1X2  2=OU(大小)  5=HC(亚洲让球)  4=DC(双重机会)
  6=DNB  13=BTTS  10=OE(单双)

用法:
  .venv312/bin/python data/pinnacle_historical/op_market_scraper.py \
      --sport football --league england/premier-league --season 2024-2025 \
      --markets 1,2,5,4,6,13,10 --out /tmp/op_markets.csv
"""
import sys, re, csv, time, json, ssl, argparse
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv312" / "lib" / "python3.12" / "site-packages"))
sys.path.insert(0, str(ROOT / "data" / "pinnacle_historical"))
from op_market_probe import decrypt_payload

BASE = "https://www.oddsportal.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
_CTX = ssl.create_default_context()

# 市场名
BT_NAME = {1: "1x2", 2: "ou", 5: "hc", 4: "dc", 6: "dnb", 13: "btts", 10: "oe"}


def fetch_exchange(match_id: str, bt: int, scope: int = 2, retries: int = 3):
    """直接 HTTP 调 betting-exchanges 接口并解密, 返回 data dict (d 字段)。"""
    url = f"{BASE}/proxy/ajax-betting-exchanges/1-{match_id}-{bt}-{scope}/"
    for attempt in range(retries):
        try:
            req = __import__("urllib.request").request.Request(url, headers={
                "User-Agent": UA, "Accept": "*/*", "Referer": BASE + "/",
            })
            body = __import__("urllib.request").request.urlopen(req, timeout=20, context=_CTX).read().decode("utf-8", "replace")
            pt = decrypt_payload(body)
            if pt:
                return json.loads(pt.decode("utf-8")).get("d", {})
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))  # 限流退避
    return None


def _avg_outcome_odds(odds: dict):
    """odds 结构: {bookieId: {outcomeId: price}} 或 {bookieId: [price1, price2]}。
    返回 {outcomeKey: (avg_price, n_bookies)}，过滤异常价 (<1.01 或 >100)。"""
    acc = defaultdict(list)
    for bookie, val in odds.items():
        if isinstance(val, dict):
            for ok, price in val.items():
                if price and 1.01 < price < 100:
                    acc[ok].append(price)
        elif isinstance(val, list):
            for i, price in enumerate(val):
                if price and 1.01 < price < 100:
                    acc[str(i)].append(price)
    return {k: (sum(v) / len(v), len(v)) for k, v in acc.items() if v}


def extract_market(match_id: str, bt: int, scope: int = 2):
    """抓取并提取某盘口的平均收盘价。

    返回 list of rows: [{market, line, side, avg_odds, n_bookies}]。
    OU/HC 有多条线 (handicapValue), 每条线一行 over/under。
    side 用 position (0/1/2), 由调用方按盘口映射到语义。
    """
    d = fetch_exchange(match_id, bt, scope)
    if not d:
        return []
    back = (d.get("oddsdata") or {}).get("back", {})
    # 各盘口的 side 语义 (position → 含义)
    SIDES = {
        1: ["home", "draw", "away"],   # 1X2
        2: ["over", "under"],          # OU
        5: ["home", "away"],           # HC
        4: ["home/draw", "home/away", "draw/away"],  # DC 3-way
        6: ["home", "away"],           # DNB
        13: ["yes", "no"],             # BTTS
        10: ["odd", "even"],           # OE
    }
    sides = SIDES.get(bt, ["0", "1", "2"])
    rows = []
    mname = BT_NAME.get(bt, str(bt))
    for key, v in back.items():
        line = v.get("handicapValue", 0)
        avg = _avg_outcome_odds(v.get("odds", {}))
        for ok in sorted(avg.keys()):
            (price, n) = avg[ok]
            pos = int(ok) if ok.isdigit() else 0
            side = sides[pos] if pos < len(sides) else ok
            rows.append({"market": mname, "line": line, "side": side,
                         "avg_odds": round(price, 3), "n_bookies": n})
    return rows


def get_finished_matches(sport: str, league_url: str, season: str, max_matches: int = 100):
    """从结果列表页抓 matchId + 赛果 + 队名。league_url 形如 /football/england/premier-league。"""
    from playwright.sync_api import sync_playwright
    CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    url = f"{BASE}{league_url}-{season}/results/" if season and season.lower() != "current" \
        else f"{BASE}{league_url}/results/"
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=CHROME,
            args=["--disable-blink-features=AutomationControlled"])
        page = b.new_context(user_agent=UA).new_page()
        page.set_default_timeout(60000)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        # 抓 H2H 链接 + 队名 + 比分
        rows = page.locator("[data-testid='game-row']")
        n = rows.count()
        for i in range(min(n, max_matches)):
            try:
                r = rows.nth(i)
                href = r.locator("a[href*='h2h']").first.get_attribute("href") or ""
                m = re.search(r"#([A-Za-z0-9]{8})", href)
                if not m:
                    continue
                host = r.locator("[data-testid='game-host']").inner_text().strip()
                guest = r.locator("[data-testid='game-guest']").inner_text().strip()
                scores = r.locator("[data-testid='event-participants'] span.font-bold").all_inner_texts()
                hs = next((s for s in scores if s.strip().isdigit()), "")
                gs = ""
                for s in scores:
                    if s.strip().isdigit():
                        if hs == "":
                            hs = s.strip()
                        else:
                            gs = s.strip()
                            break
                out.append({"match_id": m.group(1), "home": host, "away": guest,
                            "home_score": hs, "away_score": gs})
            except Exception:
                continue
        b.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="football")
    ap.add_argument("--league", required=True, help="如 england/premier-league")
    ap.add_argument("--season", default="2024-2025")
    ap.add_argument("--markets", default="1,2,5,4,6,13,10")
    ap.add_argument("--max-matches", type=int, default=50)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    bts = [int(x) for x in args.markets.split(",") if x.strip()]
    matches = get_finished_matches(args.sport, args.league, args.season, args.max_matches)
    print(f"[matches] {len(matches)} 场", flush=True)

    all_rows = []
    for i, m in enumerate(matches):
        for bt in bts:
            try:
                rows = extract_market(m["match_id"], bt)
                for r in rows:
                    r.update({"match_id": m["match_id"], "home": m["home"], "away": m["away"],
                              "home_score": m["home_score"], "away_score": m["away_score"]})
                    all_rows.append(r)
            except Exception as e:
                print(f"  [err] {m['match_id']} bt={bt}: {e}", flush=True)
            time.sleep(1.0)  # 限速
        if (i + 1) % 10 == 0:
            print(f"  ...{i + 1}/{len(matches)} 场, 累计 {len(all_rows)} 行", flush=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["match_id", "home", "away", "home_score", "away_score",
            "market", "line", "side", "avg_odds", "n_bookies"]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in all_rows:
            w.writerow({c: r.get(c, "") for c in cols})
    print(f"[done] {len(all_rows)} 行 → {out}", flush=True)


if __name__ == "__main__":
    main()
