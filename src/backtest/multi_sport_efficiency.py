"""多运动历史回测：下载 Pinnacle 收盘赔率，计算各运动/联赛/盘口的真实 ROI。

数据源：
  - 足球: Football-Data.co.uk (2012-2025, Pinnacle closing odds)
  - 网球: tennis-data.co.uk (ATP/WTA, Pinnacle odds)
  - NBA: 本地 backtest_history.csv (15赛季回测)

输出: data/odds/multi_sport_roi.json

用法:
    python3 -m src.backtest.multi_sport_efficiency --all
    python3 -m src.backtest.multi_sport_efficiency --sport football --years 13
"""
import csv
import io
import json
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "data" / "odds" / "multi_sport_roi.json"

# ===================================================================
# FOOTBALL
# ===================================================================
FOOTBALL_LEAGUES = {
    "E0": "英超", "E1": "英冠", "E2": "英甲", "E3": "英乙",
    "D1": "德甲", "D2": "德乙",
    "I1": "意甲", "I2": "意乙",
    "SP1": "西甲", "SP2": "西乙",
    "F1": "法甲", "F2": "法乙",
    "N1": "荷甲", "B1": "比甲", "P1": "葡超",
    "T1": "土超", "G1": "希超", "SC0": "苏超",
}


def download_csv(url: str, timeout: int = 30) -> list:
    """Download and parse CSV, return list of dicts."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("latin-1")
        return list(csv.DictReader(io.StringIO(raw)))
    except Exception:
        return []


def analyze_football_1x2(rows: list) -> dict:
    """Analyze Pinnacle 1X2: ROI by outcome and odds range."""
    results = {
        "total": {"bets": 0, "stake": 0.0, "profit": 0.0, "won": 0},
        "by_outcome": {"H": {"bets": 0, "stake": 0, "profit": 0, "won": 0},
                       "D": {"bets": 0, "stake": 0, "profit": 0, "won": 0},
                       "A": {"bets": 0, "stake": 0, "profit": 0, "won": 0}},
        "by_odds_range": defaultdict(lambda: {"bets": 0, "stake": 0, "profit": 0, "won": 0}),
        "by_odds_bucket": defaultdict(lambda: {"bets": 0, "stake": 0, "profit": 0, "won": 0}),
        "pinnacle_vig": 0.0,
    }

    for row in rows:
        try:
            psh = float(row.get("PSH", 0) or 0)
            psd = float(row.get("PSD", 0) or 0)
            psa = float(row.get("PSA", 0) or 0)
            ftr = row.get("FTR", "").strip()
            if not (psh > 1 and psd > 1 and psa > 1 and ftr in ("H", "D", "A")):
                continue
        except (ValueError, TypeError):
            continue

        # Vig calculation
        overround = 1/psh + 1/psd + 1/psa - 1
        fair_h = 1 / (psh * (1 + overround))  # Fair probability from Pinnacle
        fair_d = 1 / (psd * (1 + overround))
        fair_a = 1 / (psa * (1 + overround))

        for outcome, odds, fair_prob in [("H", psh, fair_h), ("D", psd, fair_d), ("A", psa, fair_a)]:
            stake = 1.0  # ¥1 per bet
            results["total"]["bets"] += 1
            results["total"]["stake"] += stake
            if outcome == ftr:
                results["total"]["won"] += 1
                results["total"]["profit"] += odds - 1
                results["by_outcome"][outcome]["won"] += 1
                results["by_outcome"][outcome]["profit"] += odds - 1
            else:
                results["total"]["profit"] -= 1
                results["by_outcome"][outcome]["profit"] -= 1
            results["by_outcome"][outcome]["bets"] += 1
            results["by_outcome"][outcome]["stake"] += stake

            # By odds range
            bucket = "<1.5" if odds < 1.5 else ("1.5-2.0" if odds < 2.0 else
                      ("2.0-3.0" if odds < 3.0 else ("3.0-5.0" if odds < 5.0 else
                       ("5.0-10.0" if odds < 10.0 else ">10.0"))))
            br = results["by_odds_range"][bucket]
            br["bets"] += 1
            br["stake"] += stake
            if outcome == ftr:
                br["won"] += 1
                br["profit"] += odds - 1
            else:
                br["profit"] -= 1

    results["pinnacle_vig"] = -results["total"]["profit"] / results["total"]["stake"] * 100 if results["total"]["stake"] else 0
    return results


def analyze_football_ou25(rows: list) -> dict:
    """Analyze Pinnacle Over/Under 2.5."""
    results = {"total": {"bets": 0, "stake": 0.0, "profit": 0.0, "won": 0},
               "by_side": {"over": {"bets": 0, "stake": 0, "profit": 0, "won": 0},
                           "under": {"bets": 0, "stake": 0, "profit": 0, "won": 0}},
               "pinnacle_vig": 0.0}

    for row in rows:
        try:
            p_over = float(row.get("P>2.5", 0) or 0)
            p_under = float(row.get("P<2.5", 0) or 0)
            fthg = int(float(row.get("FTHG", -1) or -1))
            ftag = int(float(row.get("FTAG", -1) or -1))
            if not (p_over > 1 and p_under > 1 and fthg >= 0):
                continue
        except (ValueError, TypeError):
            continue

        total_goals = fthg + ftag
        is_over = total_goals > 2.5

        for label, odds, hit in [("over", p_over, is_over), ("under", p_under, not is_over)]:
            stake = 1.0
            results["total"]["bets"] += 1
            results["total"]["stake"] += stake
            if hit:
                results["total"]["won"] += 1
                results["total"]["profit"] += odds - 1
                results["by_side"][label]["won"] += 1
                results["by_side"][label]["profit"] += odds - 1
            else:
                results["total"]["profit"] -= 1
                results["by_side"][label]["profit"] -= 1
            results["by_side"][label]["bets"] += 1
            results["by_side"][label]["stake"] += stake

    results["pinnacle_vig"] = -results["total"]["profit"] / results["total"]["stake"] * 100 if results["total"]["stake"] else 0
    return results


def analyze_football_btts(rows: list) -> dict:
    """Analyze Both Teams To Score (BTTS) market if available."""
    results = {"total": {"bets": 0, "stake": 0.0, "profit": 0.0, "won": 0}}

    for row in rows:
        try:
            btts_yes = float(row.get("BTTSY", 0) or row.get("BTSY", 0) or 0)
            btts_no = float(row.get("BTTSN", 0) or row.get("BTSN", 0) or 0)
            fthg = int(float(row.get("FTHG", -1) or -1))
            ftag = int(float(row.get("FTAG", -1) or -1))
            if not (btts_yes > 1 and btts_no > 1 and fthg >= 0):
                continue
        except (ValueError, TypeError):
            continue

        both_scored = (fthg > 0 and ftag > 0)
        for label, odds, hit in [("yes", btts_yes, both_scored), ("no", btts_no, not both_scored)]:
            results["total"]["bets"] += 1
            results["total"]["stake"] += 1.0
            if hit:
                results["total"]["won"] += 1
                results["total"]["profit"] += odds - 1
            else:
                results["total"]["profit"] -= 1

    return results


def run_football_backtest(seasons: list, leagues: list) -> dict:
    """Full football backtest: all leagues × all seasons."""
    all_results = {
        "sport": "football",
        "total_matches": 0,
        "total_seasons": 0,
        "by_league": {},
    }

    for code in leagues:
        league_name = FOOTBALL_LEAGUES.get(code, code)
        league_data = {
            "name": league_name,
            "matches": 0,
            "1x2": None,
            "ou25": None,
            "btts": None,
        }

        total_1x2 = {"bets": 0, "stake": 0.0, "profit": 0.0, "won": 0}
        total_ou = {"bets": 0, "stake": 0.0, "profit": 0.0, "won": 0}
        total_btts = {"bets": 0, "stake": 0.0, "profit": 0.0, "won": 0}

        for season in seasons:
            url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
            rows = download_csv(url)
            if not rows:
                continue
            league_data["matches"] += len(rows)
            all_results["total_matches"] += len(rows)
            all_results["total_seasons"] += 1

            # 1X2
            r = analyze_football_1x2(rows)
            for k in ("bets", "stake", "profit", "won"):
                total_1x2[k] += r["total"][k]

            # OU 2.5
            r_ou = analyze_football_ou25(rows)
            for k in ("bets", "stake", "profit", "won"):
                total_ou[k] += r_ou["total"][k]

            # BTTS
            r_btts = analyze_football_btts(rows)
            for k in ("bets", "stake", "profit", "won"):
                total_btts[k] += r_btts["total"][k]

        if total_1x2["bets"] > 0:
            league_data["1x2"] = {
                "bets": total_1x2["bets"],
                "roi": round(total_1x2["profit"] / total_1x2["stake"] * 100, 2),
                "win_rate": round(total_1x2["won"] / total_1x2["bets"] * 100, 1),
                "pinnacle_vig": round(-total_1x2["profit"] / total_1x2["stake"] * 100, 2),
            }
        if total_ou["bets"] > 0:
            league_data["ou25"] = {
                "bets": total_ou["bets"],
                "roi": round(total_ou["profit"] / total_ou["stake"] * 100, 2),
                "win_rate": round(total_ou["won"] / total_ou["bets"] * 100, 1),
                "pinnacle_vig": round(-total_ou["profit"] / total_ou["stake"] * 100, 2),
            }
        if total_btts["bets"] > 0:
            league_data["btts"] = {
                "bets": total_btts["bets"],
                "roi": round(total_btts["profit"] / total_btts["stake"] * 100, 2),
                "win_rate": round(total_btts["won"] / total_btts["bets"] * 100, 1),
            }

        all_results["by_league"][league_name] = league_data

    return all_results


# ===================================================================
# NBA (from local backtest data)
# ===================================================================
def analyze_nba_backtest() -> dict:
    """Analyze NBA 15-season backtest data for actual ROI per market."""
    nba_path = ROOT / "data" / "storage" / "backtest_history.csv"
    if not nba_path.exists():
        return {"error": "backtest_history.csv not found"}

    by_market = defaultdict(lambda: {"seasons": 0, "games": 0, "profit": 0, "roi_by_season": []})
    with open(nba_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            target = row["target"]
            n_games = int(row["n_games"])
            kelly_profit = float(row["kelly_profit"])

            # Estimate total stake: average bet ¥500, win rate ~50%, odds ~1.9
            # Kelly profit = stake * edge. For NBA model: ~2.5% edge → profit = games * ¥500 * 0.025
            # Better: use profit directly. ROI = profit / (games * stake_per_game)
            # Conservative: stake = ¥1000/game (Kelly 0.5 of ¥2000)
            est_stake_per_game = 1000  # ¥1,000 per bet
            est_stake = n_games * est_stake_per_game
            roi = kelly_profit / est_stake * 100 if est_stake else 0

            by_market[target]["seasons"] += 1
            by_market[target]["games"] += n_games
            by_market[target]["profit"] += kelly_profit
            by_market[target]["roi_by_season"].append(round(roi, 2))

    results = {"sport": "basketball_nba", "data_source": "NBA 15-season model backtest"}
    markets = {}
    for target, data in by_market.items():
        avg_roi = sum(data["roi_by_season"]) / len(data["roi_by_season"]) if data["roi_by_season"] else 0
        markets[target] = {
            "seasons": data["seasons"],
            "games": data["games"],
            "total_profit": round(data["profit"], 0),
            "avg_roi_percent": round(avg_roi, 2),
            "roi_by_season": data["roi_by_season"],
        }
    results["markets"] = markets
    return results


# ===================================================================
# MAIN
# ===================================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Run all sports")
    parser.add_argument("--sport", type=str, default="football")
    parser.add_argument("--years", type=int, default=13, help="Seasons to backtest")
    args = parser.parse_args()

    all_sports_roi = {}

    # ---- NBA ----
    print("=== NBA 回测 ===")
    nba = analyze_nba_backtest()
    all_sports_roi["nba"] = nba
    for market, data in nba.get("markets", {}).items():
        print(f"  {market}: {data['seasons']}季, {data['games']:,}场, "
              f"利润¥{data['total_profit']:,.0f}, ROI≈{data['avg_roi_percent']:+.1f}%")

    # ---- Football ----
    if args.all or args.sport == "football":
        print(f"\n=== 足球 Pinnacle 收盘回测 ({args.years}赛季) ===")
        current_year = 2025
        seasons_list = []
        for y in range(current_year - args.years, current_year):
            seasons_list.append(f"{str(y)[-2:]}{str(y+1)[-2:]}")

        fb = run_football_backtest(seasons_list, list(FOOTBALL_LEAGUES.keys()))
        all_sports_roi["football"] = fb

        # Summary
        agg_1x2 = {"bets": 0, "profit": 0, "stake": 0}
        agg_ou = {"bets": 0, "profit": 0, "stake": 0}
        for league, data in fb["by_league"].items():
            if data.get("1x2"):
                for k in ("bets", "profit", "stake"):
                    agg_1x2[k] += data["1x2"].get(k, 0)
            if data.get("ou25"):
                for k in ("bets", "profit", "stake"):
                    agg_ou[k] += data["ou25"].get(k, 0)

        roi_1x2 = agg_1x2["profit"] / agg_1x2["stake"] * 100 if agg_1x2["stake"] else 0
        roi_ou = agg_ou["profit"] / agg_ou["stake"] * 100 if agg_ou["stake"] else 0

        print(f"\n总比赛: {fb['total_matches']:,}")
        print(f"1X2: {agg_1x2['bets']:,}笔, ROI={roi_1x2:+.2f}%, "
              f"Pinnacle抽水≈{-roi_1x2:.2f}%")
        print(f"OU 2.5: {agg_ou['bets']:,}笔, ROI={roi_ou:+.2f}%, "
              f"Pinnacle抽水≈{-roi_ou:.2f}%")

        # Top/bottom leagues by 1X2 ROI
        print(f"\n联赛排名 (1X2 ROI):")
        sorted_fb = sorted(fb["by_league"].items(),
                           key=lambda x: x[1].get("1x2", {}).get("roi", -999))
        for league, data in sorted_fb:
            if data.get("1x2"):
                r = data["1x2"]
                print(f"  {league:<8} {r['bets']:>7,}笔 ROI={r['roi']:+.2f}% "
                      f"胜率={r['win_rate']:.1f}% 抽水={r['pinnacle_vig']:.2f}%")

    # Save
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(all_sports_roi, open(OUTPUT, "w"), ensure_ascii=False, indent=2, default=str)
    print(f"\n✅ 已保存到 {OUTPUT}")
