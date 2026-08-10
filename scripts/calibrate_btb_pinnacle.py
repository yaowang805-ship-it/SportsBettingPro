"""Beat the Bookie Closing Odds → V4 Weight Matrix Calibration.

Processes closing_odds.csv.gz (479K matches, 32 bookmakers avg incl. Pinnacle)
into per-league, per-odds-bin win rates for the V4 weight matrix.

Output: config/btb_calibrated.py — importable by weight_matrix_v4.py
"""
import gzip, csv, json, math, sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "pinnacle_historical" / "kaggle_beat_bookie"
OUTPUT = ROOT / "config" / "btb_calibrated.py"
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


def process_1x2():
    """从 closing_odds 提取 1X2 胜率数据。"""
    filepath = DATA_DIR / "closing_odds.csv.gz"
    if not filepath.exists():
        print(f"  ❌ {filepath} not found")
        return {}

    # {league: {bin_idx: [wins, bets, sum_odds]}}
    data = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    total = 0

    with gzip.open(filepath, 'rt', encoding='latin-1') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                home_odds = float(row.get("avg_odds_home_win", 0))
                draw_odds = float(row.get("avg_odds_draw", 0))
                away_odds = float(row.get("avg_odds_away_win", 0))
                home_score = int(float(row.get("home_score", -1)))
                away_score = int(float(row.get("away_score", -1)))
                league = row.get("league", "").strip()
            except (ValueError, TypeError):
                continue

            if home_score < 0 or away_score < 0:
                continue
            if not (1.01 <= home_odds <= 51.0 and 1.01 <= draw_odds <= 51.0 and 1.01 <= away_odds <= 51.0):
                continue

            total += 1

            # Home win
            bi_h = bin_index(home_odds)
            data[league][bi_h][1] += 1
            data[league][bi_h][2] += home_odds
            if home_score > away_score:
                data[league][bi_h][0] += 1

            # Draw
            bi_d = bin_index(draw_odds)
            data[league][bi_d][1] += 1
            data[league][bi_d][2] += draw_odds
            if home_score == away_score:
                data[league][bi_d][0] += 1

            # Away win
            bi_a = bin_index(away_odds)
            data[league][bi_a][1] += 1
            data[league][bi_a][2] += away_odds
            if away_score > home_score:
                data[league][bi_a][0] += 1

    print(f"  1X2: {total:,} matches, {len(data)} leagues")
    return dict(data)


def process_ou():
    """Check if odds_series has OU data — Beat the Bookie only has 1X2.
    OU data would need to come from odds_series spread/total columns.
    For now, return empty — OU calibration stays with existing data.
    """
    return {}


def _aggregate(data_dict):
    """Aggregate all leagues into a single cross-league dataset."""
    agg = defaultdict(lambda: [0, 0, 0.0])
    for league, bins in data_dict.items():
        for bi, (wins, bets, sum_odds) in bins.items():
            agg[bi][0] += wins
            agg[bi][1] += bets
            agg[bi][2] += sum_odds
    return dict(agg)


def _format_data(data_dict):
    """Format for Python file output, filtering low-sample bins (n < 200)."""
    out = {}
    for league, bins in sorted(data_dict.items()):
        league_key = league.replace("'", "").replace('"', '')
        out[league_key] = {}
        for bi in sorted(bins.keys()):
            wins, bets, sum_odds = bins[bi]
            if bets >= 200:
                wr = wins / bets
                avg_odds = sum_odds / bets
                out[league_key][bi] = (round(wr, 4), round(avg_odds, 4), bets)
    return out


def main():
    print("=" * 60)
    print("Beat the Bookie → V4 Calibration")
    print("=" * 60)

    # Process 1X2
    print("\n[1/2] Processing 1X2 closing odds...")
    raw_1x2 = process_1x2()

    # Aggregate
    print("\n[2/2] Aggregating & writing output...")
    formatted = _format_data(raw_1x2)
    agg = _format_data({"_AGGREGATE": _aggregate(raw_1x2)})
    formatted.update(agg)

    # Count total bets
    total_bets = sum(
        bets for league_data in formatted.values()
        for wins, avg_odds, bets in league_data.values()
    )
    print(f"  Total usable bins: {sum(len(v) for v in formatted.values())}")
    print(f"  Total bets (n >= 200): {total_bets:,}")
    print(f"  Leagues: {len(formatted)} (incl. _AGGREGATE)")

    # Write output
    with open(OUTPUT, 'w') as f:
        f.write('"""BTB Calibrated Data — Beat the Bookie closing odds (32 bookmakers avg, incl. Pinnacle)."""\n')
        f.write(f'# Generated: {__import__("datetime").datetime.now().isoformat()}\n')
        f.write(f'# Total bets: {total_bets:,}\n')
        f.write(f'# Leagues: {len(formatted)}\n\n')
        f.write('BTB_1X2_DATA = ')
        f.write(json.dumps(formatted, indent=2, ensure_ascii=False))
        f.write('\n')
        f.write('# Aggregate across all leagues\n')
        f.write('BTB_OU_DATA = {}  # Beat the Bookie closing_odds does not have OU\n')

    print(f"\n✅ Saved to {OUTPUT}")
    print(f"   File size: {OUTPUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
