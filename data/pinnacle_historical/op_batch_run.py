"""全运动批量下载 OddsPortal 历史赔率 — 后台长任务。

遍历 6 个相关运动的所有联赛，逐一下载 2024-2025 赛季 1x2(胜负)平均赔率。
输出 data/oddsportal/<sport>/<league>__<season>.csv，已存在则跳过(断点续传)。
日志写 data/logs/op_batch.log。

用法: .venv312/bin/python data/pinnacle_historical/op_batch_run.py [--seasons 2024-2025]
"""
import sys, time, argparse
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data" / "pinnacle_historical"))

from op_scraper import scrape_league

RELEVANT_SPORTS = ["football", "basketball", "tennis", "baseball",
                   "american-football", "ice-hockey"]
# 每运动默认赛季: 跨年联赛用 YYYY-YYYY, 年度赛事(网球/棒球)用 YYYY
SPORT_SEASONS = {
    "football": "2024-2025",
    "basketball": "2024-2025",
    "tennis": "2025",
    "baseball": "2025",
    "american-football": "2024-2025",
    "ice-hockey": "2024-2025",
}
OUT_DIR = ROOT / "data" / "oddsportal"
LOG_FILE = ROOT / "data" / "logs" / "op_batch.log"

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="2024-2025")
    ap.add_argument("--sports", default=",".join(RELEVANT_SPORTS))
    ap.add_argument("--only", default=None, help="逗号分隔的联赛名, 只下这些")
    args = ap.parse_args()
    seasons = args.seasons.split(",")
    sports = args.sports.split(",")

    from oddsharvester.utils.sport_league_constants import SPORTS_LEAGUES_URLS_MAPPING

    tasks = []
    for sport in sports:
        for s, lm in SPORTS_LEAGUES_URLS_MAPPING.items():
            if getattr(s, "value", str(s)) == sport:
                for lg, lg_url in lm.items():
                    if args.only and lg not in args.only.split(","):
                        continue
                    for season in seasons:
                        # 年度赛事(网球/棒球)默认单年; 其余用 --seasons
                        season = SPORT_SEASONS.get(sport, season)
                        out = OUT_DIR / sport / f"{lg}__{season}.csv"
                        if out.exists() and out.stat().st_size > 100:
                            continue
                        tasks.append((sport, lg, lg_url, season, out))
                break

    log(f"待下载任务: {len(tasks)} 个")
    done = fail = 0
    for sport, lg, lg_url, season, out in tasks:
        out.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        # 年度联赛(MLS/巴甲等)用单年, 跨年用 YYYY-YYYY; 空则回退单年
        seasons_to_try = [season]
        if "-" in season:
            seasons_to_try.append(season.split("-")[0])
        games = []
        for s in seasons_to_try:
            try:
                games = scrape_league(sport, lg_url, s, out_path=out)
            except Exception as e:
                log(f"[error] {sport}/{lg} {s}: {type(e).__name__}: {e}")
                games = []
            if games:
                break
        if games:
            done += 1
            log(f"[ok] {sport}/{lg} {season}: {len(games)} 场 ({time.time()-t0:.0f}s)")
        else:
            fail += 1
            log(f"[empty] {sport}/{lg} {season}: 0 场 ({time.time()-t0:.0f}s)")
    log(f"完成: {done} 成功, {fail} 失败/空, 共 {len(tasks)}")

if __name__ == "__main__":
    main()
