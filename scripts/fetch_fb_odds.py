#!/usr/bin/env python3
"""快速下载 football-data.co.uk 历史赔率（并发，仅含 Pinnacle 的主流赛季）。"""
import csv, io, sys, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config.logging_config import get_logger
logger = get_logger(__name__)

# 主流联赛（5大联赛 + 荷甲葡超比甲苏超）
LEAGUES = ['E0', 'E1', 'E2', 'E3', 'SC0', 'D1', 'D2', 'I1', 'I2', 'SP1', 'SP2', 'F1', 'F2', 'N1', 'B1', 'P1', 'T1', 'G1']

# 已知存在的赛季（2013/14 ~ 2024/25）
SEASONS = {}
for y in range(13, 25):
    code = f'{y}{y+1}'
    SEASONS[code] = f'mmz4281/{code}'

OUT_DIR = Path('data/raw')
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read().decode('utf-8-sig', errors='replace')
        if '<html' in raw[:300].lower():
            return None
        return raw
    except Exception:
        return None


def download_one(sq):
    season_code, fmt, league = sq
    url = f'https://www.football-data.co.uk/{fmt}/{league}.csv'
    raw = fetch(url)
    if raw is None or len(raw.splitlines()) < 2:
        return None
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows:
        return None
    for r in rows:
        r['_season'] = season_code
        r['_league'] = league
    has_ps = 'PSH' in rows[0] and rows[0]['PSH'].strip()
    return (rows, has_ps, league, season_code)


def main():
    tasks = [(sc, fmt, lg) for sc, fmt in SEASONS.items() for lg in LEAGUES]
    logger.info(f'下载 {len(tasks)} 个 CSV（并发 10 线程）')

    all_rows, ps_rows = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(download_one, t): t for t in tasks}
        for f in as_completed(futs):
            done += 1
            result = f.result()
            sq = futs[f]
            if result is None:
                if done % 50 == 0:
                    logger.info(f'  进度: {done}/{len(tasks)}')
                continue
            rows, has_ps, lg, sc = result
            all_rows.extend(rows)
            if has_ps:
                ps_rows.extend(rows)
            if done % 30 == 0:
                logger.info(f'  进度: {done}/{len(tasks)}  | 已获取: {lg} {sc} ({len(rows)}行)')

    logger.info(f'\n完成: 总计 {len(all_rows)} 行, Pinnacle {len(ps_rows)} 行')

    # 保存全量
    if all_rows:
        cols = list(all_rows[0].keys())
        p = OUT_DIR / 'fb_odds_raw.csv'
        with open(p, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
            w.writeheader(); w.writerows(all_rows)
        logger.info(f'已保存: {p} ({len(all_rows)}行)')

    # 保存 Pinnacle 子集
    if ps_rows:
        pcols = ['_season','_league','Date','Time','HomeTeam','AwayTeam','FTHG','FTAG','FTR','PSH','PSD','PSA']
        p = OUT_DIR / 'fb_pinnacle_odds.csv'
        with open(p, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=pcols, extrasaction='ignore')
            w.writeheader()
            for r in ps_rows:
                w.writerow({k: r.get(k,'') for k in pcols})
        logger.info(f'已保存: {p} ({len(ps_rows)}行)')
    else:
        logger.warning('⚠️  未找到 Pinnacle 赔率')

    logger.info(f'\n✅ 完成: {len(all_rows)} 行全量, {len(ps_rows)} 行 Pinnacle')


if __name__ == '__main__':
    main()
