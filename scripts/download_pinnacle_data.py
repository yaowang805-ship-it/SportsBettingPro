#!/usr/bin/env python3
"""下载 football-data.co.uk Pinnacle 历史收盘赔率数据。

数据源: https://www.football-data.co.uk/data.php
每个 CSV 包含 Pinnacle 开盘/收盘 1X2、OU、AH 赔率。

用法:
  python3 scripts/download_pinnacle_data.py          # 下载所有缺失联赛
  python3 scripts/download_pinnacle_data.py --all    # 全部重新下载
"""

import csv
import time
import sys
from pathlib import Path
from urllib.request import urlopen, Request

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data" / "pinnacle_historical"

# 联赛代码 → 名称
LEAGUES = {
    # 已有 (10个) — 只补充缺失赛季
    "E0": "英超", "E1": "英冠", "E2": "英甲", "E3": "英乙", "EC": "英议联",
    "D1": "德甲", "D2": "德乙",
    "I1": "意甲",
    "SC0": "西甲", "SC1": "西乙",
    # 缺失 (11个) — 全部下载
    "F1": "法甲", "F2": "法乙",
    "I2": "意乙",
    "N1": "荷甲",
    "B1": "比甲",
    "P1": "葡超",
    "T1": "土超",
    "G1": "希超",
    "SP1": "西甲(旧)", "SP2": "西乙(旧)",
    "SC2": "西丙", "SC3": "西丁",
}

# 赛季范围 (football-data.co.uk 从 00/01 开始有数据，但 Pinnacle 赔率从 ~2012 开始)
SEASONS = [
    "1213", "1314", "1415", "1516", "1617", "1718", "1819",
    "1920", "2021", "2122", "2223", "2324", "2425",
]

# football-data.co.uk 的 CSV 基础 URL
BASE_URL = "https://www.football-data.co.uk/mmz4281"


def download_csv(league_code: str, season: str, force: bool = False) -> bool:
    """下载单个联赛赛季的 CSV。返回 True=成功/已存在。"""
    league_dir = DATA_DIR / league_code
    league_dir.mkdir(parents=True, exist_ok=True)

    csv_path = league_dir / f"{season}.csv"
    if csv_path.exists() and not force:
        return True  # 已存在，跳过

    url = f"{BASE_URL}/{season}/{league_code}.csv"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urlopen(req, timeout=30) as resp:
            content = resp.read()
        csv_path.write_bytes(content)

        # 验证 CSV 有效性
        with open(csv_path, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            if not headers or "PSH" not in headers:
                csv_path.unlink()
                return False

        n_rows = sum(1 for _ in csv.DictReader(open(csv_path, encoding='utf-8-sig')))
        return n_rows > 10
    except Exception as e:
        if csv_path.exists():
            csv_path.unlink()
        return False


def main():
    force = "--all" in sys.argv
    print(f"数据目录: {DATA_DIR}")
    print(f"{'强制重新下载' if force else '增量下载（跳过已有）'}")
    print()

    total_new = 0
    total_skipped = 0
    failed = []

    for code, name in sorted(LEAGUES.items()):
        league_dir = DATA_DIR / code
        existing = set()
        if league_dir.exists():
            existing = {c.stem for c in league_dir.glob("*.csv")}

        league_new = 0
        for season in SEASONS:
            if season in existing and not force:
                total_skipped += 1
                continue

            ok = download_csv(code, season, force)
            if ok:
                league_new += 1
                total_new += 1
            else:
                failed.append(f"{code}/{season}")

            time.sleep(0.3)  # 礼貌爬取

        if league_new > 0:
            print(f"  ✅ {code} {name}: +{league_new} 季")

    print()
    print(f"新下载: {total_new} 季, 跳过: {total_skipped} 季")
    if failed:
        print(f"失败: {len(failed)} 个 ({', '.join(failed[:5])}{'...' if len(failed) > 5 else ''})")


if __name__ == "__main__":
    main()
