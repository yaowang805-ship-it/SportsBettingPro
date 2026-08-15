"""批量下载 OddsPortal 在用盘口(HC/OU/DC/DNB/BTTS/OE)收盘价 — 后台长任务。

遍历 data/oddsportal/ 已下载的 212 联赛, 对每个联赛:
  1. Playwright 抓结果列表页拿 matchId
  2. HTTP 调 betting-exchanges 接口逐场抓各盘口平均收盘价

⚠️ betting-exchanges 返回的是 Betfair 交易所价(bookie 44), 不是庄家均值,
   校准时需统一口径(见记忆 oddsportal-api-decrypt)。

用法: .venv312/bin/python data/pinnacle_historical/op_market_batch.py [--sports football,basketball] [--max-per-league 50]
"""
import sys, csv, time, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data" / "pinnacle_historical"))
sys.path.insert(0, str(ROOT / ".venv312" / "lib" / "python3.12" / "site-packages"))

from op_market_scraper import get_finished_matches, extract_market

OUT_DIR = ROOT / "data" / "oddsportal_markets"
LOG = ROOT / "data" / "logs" / "op_market_batch_download.log"

# 各运动盘口: 足球有 DC/DNB/BTTS/OE (bt 4/6/13/10), 其他运动只有 1X2/OU/HC (bt 1/2/5)
MARKETS_BY_SPORT = {
    "football": [1, 2, 5, 4, 6, 13, 10],  # 1X2/OU/HC/DC/DNB/BTTS/OE
    "basketball": [1, 2, 5],
    "baseball": [1, 2, 5],
    "ice-hockey": [1, 2, 5],
    "tennis": [1, 2, 5],
    "american-football": [1, 2, 5],
}


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def slug_to_path(slug, sport):
    """从 oddsharvester 映射拿 URL, 提取 path (如 /football/france/ligue-1)。"""
    try:
        from oddsharvester.utils.sport_league_constants import SPORTS_LEAGUES_URLS_MAPPING as M
        for s, lm in M.items():
            if getattr(s, "value", str(s)) == sport:
                url = lm.get(slug)
                if url:
                    path = url.replace("https://www.oddsportal.com", "").replace("http://www.oddsportal.com", "")
                    return path.rstrip("/")  # 去尾斜杠, 避免 "-{season}" 前多斜杠
        return None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sports", default="football")
    ap.add_argument("--max-per-league", type=int, default=30)
    args = ap.parse_args()
    sports = args.sports.split(",")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 收集任务 (只处理已下载 1X2 的联赛)
    tasks = []
    for sport in sports:
        d = ROOT / "data" / "oddsportal" / sport
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.csv")):
            slug = p.stem.split("__")[0]
            season = p.stem.split("__")[1] if "__" in p.stem else "2024-2025"
            path = slug_to_path(slug, sport)
            if not path:
                continue
            out = OUT_DIR / sport / f"{slug}__{season}_markets.csv"
            if out.exists() and out.stat().st_size > 200:
                continue  # 断点续传
            tasks.append((sport, slug, path, season, out))

    log(f"待下载市场任务: {len(tasks)} 个联赛")

    done = fail = 0
    for i, (sport, slug, path, season, out) in enumerate(tasks):
        out.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        try:
            matches = get_finished_matches(sport, path, season, max_matches=args.max_per_league)
            markets = MARKETS_BY_SPORT.get(sport, [1, 2, 5])
            all_rows = []
            for m in matches:
                for bt in markets:
                    try:
                        for r in extract_market(m["match_id"], bt, scope=2):
                            r.update({"match_id": m["match_id"], "home": m["home"], "away": m["away"],
                                      "home_score": m["home_score"], "away_score": m["away_score"], "period": "ft"})
                            all_rows.append(r)
                    except Exception:
                        pass
                    time.sleep(0.3)  # 限速
            cols = ["match_id", "home", "away", "home_score", "away_score",
                    "market", "line", "side", "period", "avg_odds", "n_bookies"]
            with open(out, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in all_rows:
                    w.writerow({c: r.get(c, "") for c in cols})
            done += 1
            log(f"[ok] {sport}/{slug}: {len(matches)} 场 {len(all_rows)} 行 ({time.time()-t0:.0f}s)")
        except Exception as e:
            fail += 1
            log(f"[err] {sport}/{slug}: {type(e).__name__}: {e}")
        # 每 5 个联赛休息一下, 防风控
        if (i + 1) % 5 == 0:
            time.sleep(3)

    log(f"✅ 市场下载结束: {done} 成功, {fail} 失败")


if __name__ == "__main__":
    main()
