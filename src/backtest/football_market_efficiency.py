"""足球历史回测：从 Football-Data.co.uk 下载 Pinnacle 收盘赔率全量数据。

分析 Pinnacle 各市场（1X2, OU2.5, 亚盘）的实际效率，输出数据驱动的市场权重。

数据覆盖：英超/英冠/英甲/英乙/德甲/意甲/西甲/法甲 (2004-2025, ~10万场)

用法：
    python3 -m src.backtest.football_market_efficiency
    python3 -m src.backtest.football_market_efficiency --leagues E0,E1,D1,I1,SP1,F1 --years 10
"""
import csv
import io
import json
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "data" / "odds" / "football_market_efficiency.json"

# League codes from Football-Data.co.uk
LEAGUES = {
    "E0": "英超",
    "E1": "英冠",
    "E2": "英甲",
    "E3": "英乙",
    "D1": "德甲",
    "D2": "德乙",
    "I1": "意甲",
    "I2": "意乙",
    "SP1": "西甲",
    "SP2": "西乙",
    "F1": "法甲",
    "F2": "法乙",
    "N1": "荷甲",
    "B1": "比甲",
    "P1": "葡超",
    "T1": "土超",
    "G1": "希超",
    "SC0": "苏超",
}


def download_league(league_code: str, season: str) -> list:
    """下载单个联赛-赛季的CSV。

    Args:
        league_code: E0/E1/D1 etc.
        season: "2425"/"2324" etc.

    Returns:
        list of dict rows
    """
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{league_code}.csv"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("latin-1")
        reader = csv.DictReader(io.StringIO(raw))
        return list(reader)
    except Exception as e:
        print(f"  ⚠️ {league_code} {season}: {e}")
        return []


def parse_pinnacle_1x2(rows: list) -> dict:
    """分析 Pinnacle 1X2 市场效率。

    计算:
    - Pinnacle 隐含概率的校准度
    - 按赔率范围分组的实际回报率
    - 是否存在 Favorite-Longshot Bias

    Pinnacle columns: PSH (Home), PSD (Draw), PSA (Away)
    """
    results = {"bets": 0, "correct": 0, "profit": 0.0, "stake": 0.0,
               "by_odds_range": defaultdict(lambda: {"bets": 0, "correct": 0, "profit": 0.0, "stake": 0.0}),
               "by_outcome": {"H": {"bets": 0, "profit": 0.0}, "D": {"bets": 0, "profit": 0.0}, "A": {"bets": 0, "profit": 0.0}}}

    for row in rows:
        try:
            psh = float(row.get("PSH", 0) or 0)
            psd = float(row.get("PSD", 0) or 0)
            psa = float(row.get("PSA", 0) or 0)
            ftr = row.get("FTR", "").strip()
            if not all([psh > 1, psd > 1, psa > 1, ftr in ("H", "D", "A")]):
                continue
        except (ValueError, TypeError):
            continue

        # Simulate betting on all outcomes (test Pinnacle calibration)
        for outcome, odds in [("H", psh), ("D", psd), ("A", psa)]:
            results["bets"] += 1
            results["stake"] += 1.0  # ¥1 per bet
            if outcome == ftr:
                results["correct"] += 1
                results["profit"] += odds - 1
            else:
                results["profit"] -= 1

            results["by_outcome"][outcome]["bets"] += 1
            if outcome == ftr:
                results["by_outcome"][outcome]["profit"] += odds - 1
            else:
                results["by_outcome"][outcome]["profit"] -= 1

            # By odds range
            if odds < 1.5:
                bucket = "<1.5"
            elif odds < 2.0:
                bucket = "1.5-2.0"
            elif odds < 3.0:
                bucket = "2.0-3.0"
            elif odds < 5.0:
                bucket = "3.0-5.0"
            elif odds < 10.0:
                bucket = "5.0-10.0"
            else:
                bucket = ">10.0"
            br = results["by_odds_range"][bucket]
            br["bets"] += 1
            br["stake"] += 1.0
            if outcome == ftr:
                br["correct"] += 1
                br["profit"] += odds - 1
            else:
                br["profit"] -= 1

    return results


def parse_pinnacle_ou25(rows: list) -> dict:
    """分析 Pinnacle Over/Under 2.5 市场效率。

    Pinnacle columns: P>2.5, P<2.5 (Over/Under 2.5 goals)
    """
    results = {"bets": 0, "correct": 0, "profit": 0.0, "stake": 0.0,
               "over": {"bets": 0, "profit": 0.0}, "under": {"bets": 0, "profit": 0.0}}

    for row in rows:
        try:
            p_over = float(row.get("P>2.5", 0) or 0)
            p_under = float(row.get("P<2.5", 0) or 0)
            fthg = int(row.get("FTHG", -1) or -1)
            ftag = int(row.get("FTAG", -1) or -1)
            if not (p_over > 1 and p_under > 1 and fthg >= 0):
                continue
        except (ValueError, TypeError):
            continue

        total_goals = fthg + ftag
        is_over = total_goals > 2.5

        for label, odds, hit in [("over", p_over, is_over), ("under", p_under, not is_over)]:
            results["bets"] += 1
            results["stake"] += 1.0
            if hit:
                results["correct"] += 1
                results["profit"] += odds - 1
            else:
                results["profit"] -= 1

            side = results["over"] if label == "over" else results["under"]
            side["bets"] += 1
            if hit:
                side["profit"] += odds - 1
            else:
                side["profit"] -= 1

    return results


def parse_asian_handicap(rows: list) -> dict:
    """分析 Pinnacle 亚洲让球盘效率。

    使用 AvgH/AvgD/AvgA (市场平均赔率) 近似亚盘表现。
    亚盘等价于: 让球方赔率 ~2.0, 受让方赔率 ~1.9
    此方法用平均赔率推导亚盘盈亏。
    """
    results = {"bets": 0, "correct": 0, "profit": 0.0, "stake": 0.0}

    for row in rows:
        try:
            psh = float(row.get("PSH", 0) or 0)
            psa = float(row.get("PSA", 0) or 0)
            ftr = row.get("FTR", "").strip()
            if not (psh > 1 and psa > 1 and ftr in ("H", "D", "A")):
                continue
        except (ValueError, TypeError):
            continue

        # Asian handicap approximation: bet on the favorite with -0.5 spread
        # Favorite = lower odds team
        if psh < psa:
            fav_odds = psh
            dog_odds = psa
            fav_result = (ftr == "H")
        else:
            fav_odds = psa
            dog_odds = psh
            fav_result = (ftr == "A")

        # Pinnacle AH typically offers ~1.90-1.95 on each side
        # We approximate with the actual odds ratio
        imp = 1/fav_odds + 1/dog_odds
        ah_odds = round(1 / (1/fav_odds / imp), 2)  # Fair + vig removed

        results["bets"] += 1
        results["stake"] += 1.0
        if fav_result:
            results["correct"] += 1
            results["profit"] += ah_odds - 1
        else:
            results["profit"] -= 1

    return results


def analyze_all(seasons: list = None, leagues: list = None):
    """主分析函数：下载并分析所有联赛赛季数据。

    Returns:
        dict: 包含市场效率分析结果
    """
    if seasons is None:
        # Default: last 10 seasons
        seasons = ["2425", "2324", "2223", "2122", "2021", "1920", "1819", "1718", "1617", "1516"]

    if leagues is None:
        leagues = list(LEAGUES.keys())

    results_1x2 = defaultdict(lambda: {"bets": 0, "profit": 0.0, "stake": 0.0})
    results_ou = defaultdict(lambda: {"bets": 0, "profit": 0.0, "stake": 0.0})
    results_ah = defaultdict(lambda: {"bets": 0, "profit": 0.0, "stake": 0.0})

    total_matches = 0
    successful_seasons = 0

    for code in leagues:
        league_name = LEAGUES.get(code, code)
        for season in seasons:
            print(f"  {league_name} {season}...", end=" ", flush=True)
            rows = download_league(code, season)
            if not rows:
                print("无数据")
                continue
            total_matches += len(rows)
            successful_seasons += 1

            # 1X2 analysis
            r_1x2 = parse_pinnacle_1x2(rows)
            results_1x2[league_name]["bets"] += r_1x2["bets"]
            results_1x2[league_name]["profit"] += r_1x2["profit"]
            results_1x2[league_name]["stake"] += r_1x2["stake"]

            # OU 2.5 analysis
            r_ou = parse_pinnacle_ou25(rows)
            results_ou[league_name]["bets"] += r_ou["bets"]
            results_ou[league_name]["profit"] += r_ou["profit"]
            results_ou[league_name]["stake"] += r_ou["stake"]

            # Asian Handicap analysis
            r_ah = parse_asian_handicap(rows)
            results_ah[league_name]["bets"] += r_ah["bets"]
            results_ah[league_name]["profit"] += r_ah["profit"]
            results_ah[league_name]["stake"] += r_ah["stake"]

            roi_1x2 = r_1x2["profit"] / r_1x2["stake"] * 100 if r_1x2["stake"] else 0
            roi_ou = r_ou["profit"] / r_ou["stake"] * 100 if r_ou["stake"] else 0
            print(f"1X2={roi_1x2:+.1f}% OU={roi_ou:+.1f}%")

    # Aggregate
    agg_1x2 = {"bets": 0, "profit": 0.0, "stake": 0.0}
    agg_ou = {"bets": 0, "profit": 0.0, "stake": 0.0}
    agg_ah = {"bets": 0, "profit": 0.0, "stake": 0.0}
    for v in results_1x2.values():
        agg_1x2["bets"] += v["bets"]
        agg_1x2["profit"] += v["profit"]
        agg_1x2["stake"] += v["stake"]
    for v in results_ou.values():
        agg_ou["bets"] += v["bets"]
        agg_ou["profit"] += v["profit"]
        agg_ou["stake"] += v["stake"]
    for v in results_ah.values():
        agg_ah["bets"] += v["bets"]
        agg_ah["profit"] += v["profit"]
        agg_ah["stake"] += v["stake"]

    roi_1x2 = agg_1x2["profit"] / agg_1x2["stake"] * 100 if agg_1x2["stake"] else 0
    roi_ou = agg_ou["profit"] / agg_ou["stake"] * 100 if agg_ou["stake"] else 0
    roi_ah = agg_ah["profit"] / agg_ah["stake"] * 100 if agg_ah["stake"] else 0

    total_vig_1x2 = -agg_1x2["profit"] / agg_1x2["stake"] * 100 if agg_1x2["stake"] else 0
    total_vig_ou = -agg_ou["profit"] / agg_ou["stake"] * 100 if agg_ou["stake"] else 0

    output = {
        "data_source": "Football-Data.co.uk Pinnacle closing odds",
        "total_matches": total_matches,
        "total_leagues": len(leagues),
        "total_seasons": successful_seasons,
        "total_1x2_bets": agg_1x2["bets"],
        "total_ou_bets": agg_ou["bets"],
        "total_ah_bets": agg_ah["bets"],
        "markets": {
            "1x2": {
                "bets": agg_1x2["bets"],
                "roi": round(roi_1x2, 2),
                "implied_vig": round(total_vig_1x2, 2),
                "by_league": {k: {"roi": round(v["profit"]/v["stake"]*100, 2) if v["stake"] else 0, "bets": v["bets"]}
                              for k, v in sorted(results_1x2.items())},
            },
            "over_under_25": {
                "bets": agg_ou["bets"],
                "roi": round(roi_ou, 2),
                "implied_vig": round(total_vig_ou, 2),
                "by_league": {k: {"roi": round(v["profit"]/v["stake"]*100, 2) if v["stake"] else 0, "bets": v["bets"]}
                              for k, v in sorted(results_ou.items())},
            },
            "asian_handicap": {
                "bets": agg_ah["bets"],
                "roi": round(roi_ah, 2),
                "by_league": {k: {"roi": round(v["profit"]/v["stake"]*100, 2) if v["stake"] else 0, "bets": v["bets"]}
                              for k, v in sorted(results_ah.items())},
            },
        },
        "recommended_weights": {
            "1x2": round(max(0.3, 1.0 + roi_1x2 / 50), 2),
            "ou": round(max(0.3, 1.0 + roi_ou / 50), 2),
            "hc": round(max(0.3, 1.0 + roi_ah / 50), 2),
        },
    }

    return output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="足球市场效率回测")
    parser.add_argument("--leagues", type=str, default="E0,E1,E2,D1,I1,SP1,F1",
                        help="联赛代码，逗号分隔")
    parser.add_argument("--years", type=int, default=5, help="回测年数")
    parser.add_argument("--output", type=str, default=str(OUTPUT),
                        help="输出文件路径")
    args = parser.parse_args()

    league_list = [l.strip() for l in args.leagues.split(",")]
    current_year = 2025  # 2025/26 season
    seasons_list = []
    for y in range(current_year - args.years, current_year):
        s = f"{str(y)[-2:]}{str(y+1)[-2:]}"  # 2425, 2324, etc.
        seasons_list.append(s)

    print(f"=== 足球 Pinnacle 市场效率回测 ===")
    print(f"联赛: {len(league_list)} 个 ({', '.join(LEAGUES.get(l,l) for l in league_list)})")
    print(f"赛季: {seasons_list[0]} ~ {seasons_list[-1]} ({len(seasons_list)} 赛季)")
    print(f"数据源: Football-Data.co.uk Pinnacle closing odds")
    print()

    result = analyze_all(seasons=seasons_list, leagues=league_list)
    print()

    print("=" * 60)
    print("结果汇总")
    print("=" * 60)
    print(f"总比赛: {result['total_matches']:,}")
    print(f"总1X2投注: {result['total_1x2_bets']:,}")
    print(f"总OU投注: {result['total_ou_bets']:,}")
    print(f"总AH投注: {result['total_ah_bets']:,}")
    print()

    for market, data in result["markets"].items():
        print(f"--- {market} ---")
        print(f"  投注数: {data['bets']:,}")
        print(f"  ROI: {data['roi']:+.2f}%")
        if "implied_vig" in data:
            print(f"  Pinnacle隐含抽水: {data['implied_vig']:.2f}%")
        # Top/bottom leagues
        leagues_sorted = sorted(data["by_league"].items(), key=lambda x: -x[1]["roi"])
        print(f"  最佳联赛: {leagues_sorted[0][0]} ({leagues_sorted[0][1]['roi']:+.1f}%)")
        print(f"  最差联赛: {leagues_sorted[-1][0]} ({leagues_sorted[-1][1]['roi']:+.1f}%)")
        print()

    print("--- 推荐权重 ---")
    for market, weight in result["recommended_weights"].items():
        print(f"  {market}: {weight:.2f}")

    # Save
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(result, open(args.output, "w"), ensure_ascii=False, indent=2, default=str)
    print(f"\n已保存到 {args.output}")
