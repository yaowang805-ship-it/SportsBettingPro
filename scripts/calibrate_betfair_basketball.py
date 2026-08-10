"""Betfair Basketball → V4 Weight Matrix Calibration.

Processes betfair_140901.csv (1.3M rows, all sports, 2014 season)
Extracts basketball (SPORTS_ID=7522): 24,712 rows
  - FIBA Basketball World Cup 2014
  - WNBA 2014
  - Asean Basketball League 2014
  - Club Friendlies 2014
  - International Friendlies 2014

Output: config/betfair_basketball_calibrated.py
"""
import csv, json
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "pinnacle_historical" / "betfair" / "betfair_140901.csv"
OUTPUT = ROOT / "config" / "betfair_basketball_calibrated.py"

BASKETBALL_SPORT_ID = "7522"
ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.30, 2.50, 2.70, 2.90,
    3.10, 3.30, 3.50, 3.70, 3.90, 4.20, 4.50, 4.80,
    5.20, 5.60, 6.00, 6.50, 7.00, 7.50, 8.00, 9.00,
    10.00, 12.00, 15.00, 20.00, float('inf')
]


def bin_index(odds):
    for i, t in enumerate(ODDS_BINS):
        if odds < t:
            return i
    return len(ODDS_BINS) - 1


def process():
    # {league: {bin: [wins, bets, sum_odds]}}
    data = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    total = 0

    with open(DATA_FILE) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("SPORTS_ID") != BASKETBALL_SPORT_ID:
                continue

            try:
                odds = float(row.get("ODDS", 0))
                win = int(row.get("WIN_FLAG", -1))
                desc = row.get("FULL_DESCRIPTION", "")
            except (ValueError, TypeError):
                continue

            if win < 0 or odds < 1.01 or odds > 51.0:
                continue

            # Extract league from description
            parts = desc.split("/")
            league = parts[0].strip() if parts else "Unknown"
            # Normalize: strip year suffix
            if league.endswith(" 2014"):
                league = league[:-5]
            if "WNBA" in league:
                league = "WNBA"
            elif "FIBA" in league and "World Cup" in league:
                league = "FIBA World Cup"
            elif "Asean" in league:
                league = "ASEAN Basketball"
            elif "Club Friendlies" in league:
                league = "Club Friendlies"
            elif "International Friendlies" in league:
                league = "International Friendlies"

            bi = bin_index(odds)
            data[league][bi][1] += 1
            data[league][bi][2] += odds
            if win == 1:
                data[league][bi][0] += 1
            total += 1

    print(f"Basketball bets: {total:,}")
    print(f"Leagues: {len(data)}")
    for lg in sorted(data.keys()):
        n = sum(v[1] for v in data[lg].values())
        print(f"  {lg}: {n:,} bets")

    # Filter low-sample bins
    def _filter(d, min_n=20):
        return {str(k): (round(v[0]/v[1], 4), round(v[2]/v[1], 4), v[1])
                for k, v in d.items() if v[1] >= min_n}

    calibrated = {}
    for league, bins in data.items():
        filtered = _filter(bins)
        if filtered:
            calibrated[league] = filtered

    # Aggregate
    agg = defaultdict(lambda: [0, 0, 0.0])
    for league, bins in data.items():
        for bi, (wins, bets, sum_odds) in bins.items():
            agg[bi][0] += wins
            agg[bi][1] += bets
            agg[bi][2] += sum_odds
    calibrated["_AGGREGATE"] = _filter(agg, min_n=50)

    total_bins = sum(len(v) for v in calibrated.values())
    print(f"\nTotal calibrated bins: {total_bins}")

    with open(OUTPUT, 'w') as f:
        f.write('"""Betfair Basketball Calibrated Data — 2014 exchange closing odds.\\n')
        f.write('   Leagues: WNBA, FIBA World Cup, ASEAN, Club Friendlies, International\\n')
        f.write('   Betfair exchange odds are a strong proxy for Pinnacle closing lines.\\n')
        f.write('"""\n')
        f.write(f'# Generated: {__import__("datetime").datetime.now().isoformat()}\n')
        f.write(f'# Total bets: {total:,}\n\n')
        f.write('BETFAIR_BASKETBALL = ')
        f.write(json.dumps(calibrated, indent=2, ensure_ascii=False))
        f.write('\n')

    print(f"✅ Saved to {OUTPUT} ({OUTPUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    process()
