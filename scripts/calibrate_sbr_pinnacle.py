"""SBR Consensus Closing Odds → V4 Weight Matrix Calibration.

Processes SportsbookReview 10Y archives (NBA 14K, MLB 26K, NFL 3K, NHL 14K)
into per-sport, per-odds-bin win rates.

SBR consensus closing lines aggregate multiple sportsbooks including Pinnacle.
These are the most efficient market prices available.

Output: config/sbr_calibrated.py
"""
import json, math
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "pinnacle_historical" / "sportsbookreview-scraper" / "data"
OUTPUT = ROOT / "config" / "sbr_calibrated.py"

# Same bins as V4 weight matrix
ODDS_BINS = [
    1.30, 1.50, 1.70, 1.90, 2.10, 2.30, 2.50, 2.70, 2.90,
    3.10, 3.30, 3.50, 3.70, 3.90, 4.20, 4.50, 4.80,
    5.20, 5.60, 6.00, 6.50, 7.00, 7.50, 8.00, 9.00,
    10.00, 12.00, 15.00, 20.00, float('inf')
]

OU_BINS = [2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 12.5, 15.5, 20.5, float('inf')]

SPREAD_BINS = [-20, -15, -10, -7, -5, -3, -1.5, 1.5, 3, 5, 7, 10, 15, 20, float('inf')]


def bin_index(odds, bins):
    for i, t in enumerate(bins):
        if odds < t:
            return i
    return len(bins) - 1


def am_to_dec(am_odds):
    """Convert American odds to decimal. Handles edge cases."""
    try:
        am = float(am_odds)
    except (ValueError, TypeError):
        return None
    if am == 0:
        return None
    if am > 0:
        return round(1.0 + am / 100.0, 4)
    else:
        return round(1.0 + 100.0 / abs(am), 4)


def process_sport(filepath, sport_name, is_3way=False):
    """Process one sport's JSON archive into binned win rates.

    Returns {market: {bin_idx: (wr, avg_odds, n)}} where market is 'ml', 'ou', 'spread'.
    For 2-way sports, 'ml' maps to {bin_idx: (wr, avg_odds, n)}.
    """
    with open(filepath) as f:
        data = json.load(f)

    ml_data = defaultdict(lambda: [0, 0, 0.0])  # {bin: [wins, bets, sum_odds]}
    ou_data = defaultdict(lambda: [0, 0, 0.0])
    spread_data = defaultdict(lambda: [0, 0, 0.0])
    game_count = 0

    for g in data:
        try:
            home_score = int(g.get("home_final", -1))
            away_score = int(g.get("away_final", -1))
            if home_score < 0:
                continue

            # Moneyline
            home_ml = g.get("home_close_ml")
            away_ml = g.get("away_close_ml")
            if home_ml is not None and away_ml is not None:
                home_dec = am_to_dec(float(home_ml))
                away_dec = am_to_dec(float(away_ml))
                if home_dec is None or away_dec is None:
                    continue
                if not (1.01 <= home_dec <= 51.0 and 1.01 <= away_dec <= 51.0):
                    continue

                # Home ML
                bi_h = bin_index(home_dec, ODDS_BINS)
                ml_data[bi_h][1] += 1
                ml_data[bi_h][2] += home_dec
                if home_score > away_score:
                    ml_data[bi_h][0] += 1

                # Away ML
                bi_a = bin_index(away_dec, ODDS_BINS)
                ml_data[bi_a][1] += 1
                ml_data[bi_a][2] += away_dec
                if away_score > home_score:
                    ml_data[bi_a][0] += 1

                game_count += 1

            # Over/Under
            ou_line = g.get("close_over_under")
            if ou_line is not None and ou_line > 0:
                total = home_score + away_score
                bi = bin_index(float(ou_line), OU_BINS)
                ou_data[bi][1] += 1
                ou_data[bi][2] += float(ou_line)
                if total > ou_line:
                    ou_data[bi][0] += 1  # 'over' wins

            # Spread (home spread)
            home_spread = g.get("home_close_spread")
            if home_spread is not None:
                home_cover = home_score + float(home_spread)
                bi = bin_index(float(home_spread), SPREAD_BINS)
                spread_data[bi][1] += 1
                spread_data[bi][2] += float(home_spread)
                if home_cover > away_score:
                    spread_data[bi][0] += 1  # home covers

        except (ValueError, TypeError, KeyError):
            continue

    # Filter low-sample bins
    def _filter(d, min_n=20):
        return {str(k): (round(v[0]/v[1], 4), round(v[2]/v[1], 4), v[1])
                for k, v in d.items() if v[1] >= min_n}

    result = {
        "ml": _filter(ml_data),
        "ou": _filter(ou_data),
        "spread": _filter(spread_data),
        "n_games": game_count,
    }
    print(f"  {sport_name}: {game_count:,} games, ML bins={len(result['ml'])}, OU bins={len(result['ou'])}, Spread bins={len(result['spread'])}")
    return result


def main():
    print("=" * 60)
    print("SBR Consensus → V4 Calibration")
    print("=" * 60)

    files = {
        "NBA": DATA_DIR / "nba_archive_10Y.json",
        "MLB": DATA_DIR / "mlb_archive_10Y.json",
        "NFL": DATA_DIR / "nfl_archive_10Y.json",
        "NHL": DATA_DIR / "nhl_archive_10Y.json",
    }

    calibrated = {}
    for sport, filepath in files.items():
        if filepath.exists():
            print(f"\n[{sport}] Processing {filepath.name}...")
            calibrated[sport] = process_sport(filepath, sport)
        else:
            print(f"[{sport}] File not found: {filepath}")

    # Write output
    with open(OUTPUT, 'w') as f:
        f.write('"""SBR Calibrated Data — SportsbookReview consensus closing odds (incl. Pinnacle)."""\n')
        f.write(f'# Generated: {__import__("datetime").datetime.now().isoformat()}\n')
        f.write(f'# Source: github.com/flancast90/sportsbookreview-scraper\n\n')
        f.write('SBR_DATA = ')
        f.write(json.dumps(calibrated, indent=2, ensure_ascii=False))
        f.write('\n')

    total_games = sum(v["n_games"] for v in calibrated.values())
    total_bins = sum(len(v["ml"]) + len(v["ou"]) + len(v["spread"]) for v in calibrated.values())
    print(f"\n✅ Saved to {OUTPUT}")
    print(f"   Total games: {total_games:,}")
    print(f"   Total bins: {total_bins}")
    print(f"   File size: {OUTPUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
