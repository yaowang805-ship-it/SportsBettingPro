"""football-data.co.uk 角球亚洲让球 (Corner AH) → V5 权重矩阵校准。

数据源: data/pinnacle_historical/<LEAGUE_CODE>/<season>.csv + football_<CODE>_2526.csv
列: HC(主队角球数) AC(客队角球数) AHCh(角球让球线, 主队) AvgCAHH/AvgCAHA(平均角球AH赔率)
    PCAHH/PCAHA(Pinnacle角球AH赔率, 覆盖~39%)

角球让球结算 (主队视角):
    主胜 = HC + AHCh > AC ; 平局(void) = HC + AHCh == AC ; 客胜 = HC + AHCh < AC

输出: config/corner_calibrated.py — CORNER_HC_DATA = {league: {bin: (wr, avg_odds, n)}}
      用平均角球AH赔率(共识价), 覆盖~6.8万场。角球无大小球(OU)历史数据。

用法: python3 scripts/calibrate_corner_ah.py
"""
import csv, sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "pinnacle_historical"
OUTPUT = ROOT / "config" / "corner_calibrated.py"

# 联赛代码 → 中文名 (与 download_pinnacle_data.py 一致)
LEAGUES = {
    "E0": "英超", "E1": "英冠", "E2": "英甲", "E3": "英乙", "EC": "英议联",
    "D1": "德甲", "D2": "德乙",
    "I1": "意甲", "I2": "意乙",
    "SC0": "西甲", "SC1": "西乙", "SC2": "西丙", "SC3": "西丁",
    "SP1": "西甲", "SP2": "西乙",  # 旧代码合并到西甲/西乙
    "F1": "法甲", "F2": "法乙",
    "N1": "荷甲", "B1": "比甲", "P1": "葡超", "T1": "土超", "G1": "希超",
    "ARG": "阿根廷甲", "BRA": "巴西甲", "AUT": "奥地利甲", "SWE": "瑞典超",
    "DEN": "丹麦超", "NOR": "挪威超", "FIN": "芬兰超", "POL": "波兰甲",
    "CZE": "捷克甲", "ROU": "罗马尼亚甲", "SWZ": "瑞士超", "RUS": "俄罗斯超",
    "JPN": "日本J1", "MEX": "墨西哥超", "USA": "美国MLS", "KOR": "韩国K1", "AUS": "澳洲甲",
}

ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.30, 2.50, 2.70, 2.90,
    3.10, 3.30, 3.50, 3.70, 3.90, 4.20, 4.50, 4.80,
    5.20, 5.60, 6.00, 6.50, 7.00, 7.50, 8.00, 9.00,
    10.00, 12.00, 15.00, 20.00, float('inf'),
]


def bin_index(odds):
    for i, t in enumerate(ODDS_BINS):
        if odds < t:
            return i
    return len(ODDS_BINS) - 1


def _f(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def process_file(path, league_name, data):
    """读一个 football-data.co.uk CSV, 累加角球AH胜率。data: {league: {bin: [wins,bets,sum_odds]}}"""
    try:
        rows = list(csv.DictReader(open(path, encoding="latin-1")))
    except Exception:
        return 0
    n = 0
    for r in rows:
        hc = _f(r.get("HC"))
        ac = _f(r.get("AC"))
        line = _f(r.get("AHCh"))
        home_odds = _f(r.get("AvgCAHH")) or _f(r.get("PCAHH"))
        away_odds = _f(r.get("AvgCAHA")) or _f(r.get("PCAHA"))
        if hc is None or ac is None or line is None:
            continue
        # 主队结算
        diff = hc + line - ac
        # 主胜
        if home_odds and 1.01 <= home_odds <= 51.0 and abs(diff) > 1e-6:
            bi = bin_index(home_odds)
            data[league_name][bi][1] += 1
            data[league_name][bi][2] += home_odds
            if diff > 0:
                data[league_name][bi][0] += 1
        # 客胜
        if away_odds and 1.01 <= away_odds <= 51.0 and abs(diff) > 1e-6:
            bi = bin_index(away_odds)
            data[league_name][bi][1] += 1
            data[league_name][bi][2] += away_odds
            if diff < 0:
                data[league_name][bi][0] += 1
        n += 1
    return n


def main():
    data = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    total = 0

    # 1. 顶级 2526 赛季文件
    for code, name in LEAGUES.items():
        p = DATA_DIR / f"football_{code}_2526.csv"
        if p.exists():
            total += process_file(p, name, data)

    # 2. 历史赛季子目录 <CODE>/<season>.csv
    for code, name in LEAGUES.items():
        d = DATA_DIR / code
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.csv")):
            total += process_file(p, name, data)

    # 汇总所有联赛, 生成 _AGGREGATE
    agg = defaultdict(lambda: [0, 0, 0.0])
    for league, bins in data.items():
        for bi, (wins, bets, sum_odds) in bins.items():
            agg[bi][0] += wins
            agg[bi][1] += bets
            agg[bi][2] += sum_odds

    # 格式化输出 (n >= 20 才保留, 角球样本比1X2少)
    out = {}
    for league, bins in sorted(data.items()):
        out[league] = {}
        for bi in sorted(bins.keys()):
            wins, bets, sum_odds = bins[bi]
            if bets >= 20:
                out[league][bi] = (round(wins / bets, 4), round(sum_odds / bets, 4), bets)
    out["_AGGREGATE"] = {}
    for bi in sorted(agg.keys()):
        wins, bets, sum_odds = agg[bi]
        if bets >= 20:
            out["_AGGREGATE"][bi] = (round(wins / bets, 4), round(sum_odds / bets, 4), bets)

    # 写文件
    lines = [
        '"""角球亚洲让球(Corner AH)校准数据 — football-data.co.uk 平均角球AH收盘价。',
        "列: HC/AC=角球数, AHCh=角球让球线(主), AvgCAHH/AvgCAHA=平均角球AH赔率。",
        f"生成: {sys.argv[0] if sys.argv else 'calibrate_corner_ah.py'} | 总场次 {total:,}",
        "格式: CORNER_HC_DATA = {league: {bin: (win_rate, avg_odds, n)}}",
        '"""',
        "",
        "CORNER_HC_DATA = {",
    ]
    for league in sorted(out.keys()):
        lines.append(f'    "{league}": {{')
        for bi in sorted(out[league].keys()):
            wr, avg_o, n = out[league][bi]
            lines.append(f'        {bi}: ({wr}, {avg_o}, {n}),')
        lines.append("    },")
    lines.append("}")
    lines.append("")

    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    # 统计
    total_bets = sum(agg[bi][1] for bi in agg)
    print(f"✅ 角球AH校准完成: {total:,} 场, {len(data)} 联赛, {total_bets:,} 注 → {OUTPUT.name}")


if __name__ == "__main__":
    main()
