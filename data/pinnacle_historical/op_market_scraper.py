"""OddsPortal 逐场盘口收盘价抓取器 — HC/OU/DC/DNB/BTTS/OE/CS/HTFT 补充 op_scraper 只抓 1X2 的缺口。

用已破解的加密 (见 op_market_probe.decrypt_payload) 直接 HTTP 调接口,
无需浏览器/Playwright, 每条比赛只需 N 次轻量 GET (N=盘口数)。

盘口 → bt 映射 (来自 event-page-metadata / ajax-event-data 的 bettingTypes):
  1=1X2  2=OU(大小)  5=HC(亚洲让球)  4=DC(双重机会)
  6=DNB  13=BTTS  10=OE(单双)
  8=CS(正确比分, Correct Score)  9=HTFT(半全场, Half Time/Full Time)

⚠️ 两个不同数据源:
  - bt 1/2/5/4/6/13/10 走 `betting-exchanges`(Betfair 交易所价, 单庄家 bookie 44)
  - bt 8/9 走 `match-event`(庄家均值, 多庄家, 每个 outcome 带 mixedParameterName 名字)
    —— 交易所接口对正确比分/半全场返回空, 只有 match-event 有这两个盘口的收盘价。

用法:
  .venv312/bin/python data/pinnacle_historical/op_market_scraper.py \
      --sport football --league england/premier-league --season 2024-2025 \
      --markets 1,2,5,4,6,13,10,8,9 --out /tmp/op_markets.csv
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
BT_NAME = {1: "1x2", 2: "ou", 5: "hc", 4: "dc", 6: "dnb", 13: "btts", 10: "oe",
           8: "correct_score", 9: "htft"}

# match-event 接口 (bt=8/9 庄家均值数据源) 的域名, 直接走 backend 无 /proxy/ 前缀
ME_BASE = "https://backend.oddsportal.com"


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


def fetch_match_event(match_id: str, bt: int, scope: int = 2, retries: int = 3):
    """bt=8(正确比分)/9(半全场) 收盘价走 match-event 接口 (庄家均值, 非交易所)。

    接口: https://backend.oddsportal.com/match-event/1-1-{matchId}-{bt}-{scope}-{hash}.dat
    (URL 来自 ajax-event-data 的 requestPreMatch, "1-1" = 足球 sport/version)
    响应同样 AES 加密 (decrypt_payload 可解), 结构同 betting-exchanges 的 d 字段,
    但 oddsdata.back 每个条目是一个 named outcome (mixedParameterName), odds={bookieId:[price]}。

    末尾 hash 是前端缓存 token (event-data-shell 的 xhashf), 后端不校验, 传占位即可。
    """
    url = f"{ME_BASE}/match-event/1-1-{match_id}-{bt}-{scope}-0.dat"
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


# 半全场 OddsPortal 记法 (1=主 X=平 2=客) → 系统 home/draw/away 记法 (对齐 BB/Pinnacle)
_HTFT_NAME = {
    "1/1": "home/home", "X/1": "draw/home", "2/1": "away/home",
    "1/X": "home/draw", "X/X": "draw/draw", "2/X": "away/draw",
    "1/2": "home/away", "X/2": "draw/away", "2/2": "away/away",
}


def _extract_named_outcomes(d: dict, bt: int):
    """bt=8/9: match-event 响应里每个 back 条目是一个 named outcome。

    outcome 名字在 mixedParameterName:
      bt=8 正确比分 "0:0"/"3:1" → side 输出 "0-0"/"3-1" (对齐 BB 的 "-" 记法)
      bt=9 半全场   "1/1"/"X/2" → side 输出 "home/home"/"draw/away" (对齐 BB/Pinnacle)
    odds 是 {bookieId: [price]}(单元素列表), 聚合成每 outcome 的平均收盘价。
    """
    back = (d.get("oddsdata") or {}).get("back", {})
    if not isinstance(back, dict):
        return []
    mname = BT_NAME.get(bt, str(bt))
    rows = []
    for key, v in back.items():
        name = v.get("mixedParameterName") or ""
        if bt == 8:
            name = name.replace(":", "-")            # "3:1" → "3-1"
        else:
            name = _HTFT_NAME.get(name, name)        # "1/1" → "home/home"
        prices = []
        for bookie, val in (v.get("odds") or {}).items():
            # 正确比分/半全场长冷门线赔率可到数百(如 4:4 @401), 上限放宽到 1000
            if isinstance(val, list):
                prices += [p for p in val if p and 1.01 < p < 1000]
            elif val and 1.01 < val < 1000:
                prices.append(val)
        if not prices:
            continue
        rows.append({"market": mname, "line": v.get("handicapValue", 0),
                     "side": name, "avg_odds": round(sum(prices) / len(prices), 3),
                     "n_bookies": len(prices)})
    return rows


def extract_market(match_id: str, bt: int, scope: int = 2):
    """抓取并提取某盘口的平均收盘价。

    返回 list of rows: [{market, line, side, avg_odds, n_bookies}]。
    OU/HC 有多条线 (handicapValue), 每条线一行 over/under。
    side 用 position (0/1/2), 由调用方按盘口映射到语义。

    bt=8(正确比分)/9(半全场) 走 match-event 接口 (庄家均值, 多庄家), side 直接是比分线/半全场名;
    其余 bt 走 betting-exchanges (Betfair 交易所价)。
    """
    if bt in (8, 9):
        d = fetch_match_event(match_id, bt, scope)
        if not d:
            return []
        return _extract_named_outcomes(d, bt)

    d = fetch_exchange(match_id, bt, scope)
    if not d:
        return []
    back = (d.get("oddsdata") or {}).get("back", {})
    if not isinstance(back, dict):
        return []  # 该盘口无交易所数据时 back 是空 list, 不是 dict
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
    ap.add_argument("--markets", default="1,2,5,4,6,13,10,8,9")
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
