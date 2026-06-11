"""套利检测模块 — 跨博彩公司发现无风险/低风险套利机会。

支持两种套利类型:
  1. H2H 套利: 不同公司对同一场比赛的胜平负报价存在无风险利润空间
  2. Spread 套利: 同一盘口跨公司的定价差偏离合理范围

用法:
    from src.monitor.arbitrage import scan_arbitrage
    opportunities = scan_arbitrage('soccer_epl')
"""
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict

from config.logging_config import get_logger
logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
ARBITRAGE_LOG = ROOT / "data" / "storage" / "arbitrage_log.json"


def _extract_h2h_prices(bookmakers: List[Dict]) -> Dict[str, Dict[str, float]]:
    """提取所有博彩公司的胜平负赔率。

    Returns:
        {bookmaker_name: {home: float, draw: float, away: float}}
    """
    result = {}
    for bm in bookmakers:
        bm_name = bm.get("title", bm.get("key", "未知"))
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            prices = {}
            for out in outcomes:
                name = out.get("name", "").strip()
                price = out.get("price")
                if price:
                    prices[name] = price
            if prices:
                result[bm_name] = prices
    return result


def _detect_h2h_arb(match_data: Dict) -> List[Dict]:
    """检测胜平负套利机会。

    套利条件: max(1/home) + max(1/draw) + max(1/away) < 1.0
    """
    home_team = match_data.get("home_team", "")
    away_team = match_data.get("away_team", "")
    bookmaker_prices = _extract_h2h_prices(match_data.get("bookmakers", []))

    if len(bookmaker_prices) < 2:
        return []

    # 找每个选项的最优赔率及其公司
    best_prices = {}  # outcome_name -> {price, bookmaker}
    for bm_name, prices in bookmaker_prices.items():
        for outcome_name, price in prices.items():
            if outcome_name not in best_prices or price > best_prices[outcome_name]["price"]:
                best_prices[outcome_name] = {"price": price, "bookmaker": bm_name}

    if len(best_prices) < 2:
        return []

    # 计算套利利润: 1 - sum(1/price)
    inv_sum = sum(1.0 / v["price"] for v in best_prices.values())
    profit_pct = (1.0 - inv_sum) * 100

    opportunities = []
    if profit_pct > 0:
        # 计算最优下注比例
        stakes = {}
        total_stake = 1000  # 假设总本金 1000 元
        for outcome_name, info in best_prices.items():
            stake_ratio = (1.0 / info["price"]) / inv_sum
            stakes[outcome_name] = round(total_stake * stake_ratio, 2)

        opportunities.append({
            "type": "h2h",
            "home_team": home_team,
            "away_team": away_team,
            "profit_pct": round(profit_pct, 3),
            "best_prices": {k: round(v["price"], 4) for k, v in best_prices.items()},
            "bookmakers": {k: v["bookmaker"] for k, v in best_prices.items()},
            "recommended_stakes": stakes,
            "guaranteed_return": round(total_stake * profit_pct / 100, 2),
        })

    return opportunities


def _detect_spread_arb(match_data: Dict) -> List[Dict]:
    """检测让分盘套利机会。

    两种模式:
      1. 真套利: 用 A 公司的 home_price 和 B 公司的 away_price 组合，1/home + 1/away < 1
      2. 同盘口比价: 同一盘口跨公司的价格差（line shopping）
    """
    home_team = match_data.get("home_team", "")
    away_team = match_data.get("away_team", "")
    bookmakers = match_data.get("bookmakers", [])

    if len(bookmakers) < 2:
        return []

    # 收集所有公司的让分盘报价
    spread_quotes = []
    for bm in bookmakers:
        bm_name = bm.get("title", bm.get("key", "未知"))
        for market in bm.get("markets", []):
            if market.get("key") != "spreads":
                continue
            outcomes = market.get("outcomes", [])
            home_out = next((o for o in outcomes if o.get("name", "").strip().lower() == home_team.strip().lower()), None)
            away_out = next((o for o in outcomes if o.get("name", "").strip().lower() == away_team.strip().lower()), None)
            if home_out and away_out:
                spread_quotes.append({
                    "bookmaker": bm_name,
                    "home_point": home_out.get("point"),
                    "home_price": home_out.get("price"),
                    "away_point": away_out.get("point"),
                    "away_price": away_out.get("price"),
                })

    if len(spread_quotes) < 2:
        return []

    opportunities = []

    # 模式1: 真套利——跨公司组合 home/away
    # 用公司A的主队价格 + 公司B的客队价格（或同公司），综合计算
    for i in range(len(spread_quotes)):
        for j in range(len(spread_quotes)):
            q_home = spread_quotes[i]
            q_away = spread_quotes[j]
            if q_home["home_price"] and q_away["away_price"]:
                inv_sum = 1.0 / q_home["home_price"] + 1.0 / q_away["away_price"]
                profit_pct = (1.0 - inv_sum) * 100
                # 同公司真套利或跨公司组合
                if profit_pct > 0.5:
                    total_stake = 1000
                    home_stake = round(total_stake / (1.0 + q_home["home_price"] / q_away["away_price"]), 2)
                    away_stake = round(total_stake - home_stake, 2)
                    opportunities.append({
                        "type": "spread_arb",
                        "home_team": home_team,
                        "away_team": away_team,
                        "point": q_home["home_point"],
                        "profit_pct": round(profit_pct, 3),
                        "home_bookmaker": q_home["bookmaker"],
                        "home_price": q_home["home_price"],
                        "away_bookmaker": q_away["bookmaker"],
                        "away_price": q_away["away_price"],
                        "recommended_stakes": {"home": home_stake, "away": away_stake},
                        "guaranteed_return": round(total_stake * profit_pct / 100, 2),
                        "description": f"让分套利 {q_home['home_point']}: {q_home['bookmaker']}(主{q_home['home_price']}) + {q_away['bookmaker']}(客{q_away['away_price']})",
                    })

    # 模式2: 同盘口比价（line shopping）
    for i in range(len(spread_quotes)):
        for j in range(i + 1, len(spread_quotes)):
            q1, q2 = spread_quotes[i], spread_quotes[j]
            if q1["home_point"] == q2["home_point"]:
                price_diff = abs(q1["home_price"] - q2["home_price"])
                if price_diff > 0.05:
                    better = q1 if q1["home_price"] > q2["home_price"] else q2
                    worse = q2 if q1["home_price"] > q2["home_price"] else q1
                    opportunities.append({
                        "type": "line_shopping",
                        "home_team": home_team,
                        "away_team": away_team,
                        "point": q1["home_point"],
                        "best_bookmaker": better["bookmaker"],
                        "best_price": better["home_price"],
                        "worst_bookmaker": worse["bookmaker"],
                        "worst_price": worse["home_price"],
                        "price_gap": round(price_diff, 4),
                        "description": f"让分比价 {q1['home_point']}: {better['bookmaker']} "
                                       f"({better['home_price']}) vs {worse['bookmaker']} "
                                       f"({worse['home_price']})",
                    })

    return opportunities


def scan_arbitrage(sport_key: str, force: bool = False) -> List[Dict]:
    """扫描指定联赛的所有比赛，检测套利机会。

    Args:
        sport_key: Odds API sport key (e.g. 'soccer_epl', 'basketball_nba')
        force: 是否强制重新拉取赔率

    Returns:
        套利机会列表，按利润率降序
    """
    try:
        from fetchers.odds_api import fetch_odds_api
        data = fetch_odds_api(sport_key, force=force)
    except Exception as e:
        logger.warning("⚠️ 套利扫描拉取赔率失败 %s: %s", sport_key, e)
        return []

    if not data:
        logger.info("   %s: 无比赛数据", sport_key)
        return []

    all_opportunities = []
    for match in data:
        try:
            opps_h2h = _detect_h2h_arb(match)
            all_opportunities.extend(opps_h2h)

            opps_spread = _detect_spread_arb(match)
            all_opportunities.extend(opps_spread)
        except Exception as e:
            logger.warning("  扫描异常 %s vs %s: %s",
                          match.get("home_team", "?"), match.get("away_team", "?"), e)

    if all_opportunities:
        all_opportunities.sort(key=lambda x: x.get("profit_pct", x.get("price_gap", 0)), reverse=True)

    return all_opportunities


def scan_all_leagues(sport_keys: List[str] = None, force: bool = False) -> Dict[str, List[Dict]]:
    """扫描所有指定联赛的套利机会。

    Args:
        sport_keys: 联赛列表，默认扫描主要联赛

    Returns:
        {sport_key: [opportunities]}
    """
    if sport_keys is None:
        sport_keys = [
            "basketball_nba",
            "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
            "soccer_italy_serie_a", "soccer_france_ligue_one",
        ]

    results = {}
    total = 0
    for key in sport_keys:
        opps = scan_arbitrage(key, force=force)
        results[key] = opps
        total += len(opps)
        if opps:
            logger.info("  %s: %d 个套利机会", key, len(opps))

    logger.info("  共发现 %d 个套利机会", total)
    return results


def report_arbitrage(opportunities: Dict[str, List[Dict]] = None, force: bool = False):
    """汇总打印套利报告并保存到文件。"""
    if opportunities is None:
        opportunities = scan_all_leagues(force=force)

    logger.info("\n" + "=" * 60)
    logger.info("  💰 套利检测报告 - %s", datetime.now().strftime('%Y-%m-%d %H:%M'))
    logger.info("=" * 60)

    all_opps = []
    for sport_key, opps in opportunities.items():
        for opp in opps:
            opp["_sport"] = sport_key
            all_opps.append(opp)

    h2h_opps = [o for o in all_opps if o.get("type") == "h2h"]
    spread_arb = [o for o in all_opps if o.get("type") == "spread_arb"]
    line_shop = [o for o in all_opps if o.get("type") == "line_shopping"]

    if h2h_opps:
        logger.info("\n  🔵 H2H 无风险套利:")
        h2h_opps.sort(key=lambda x: x["profit_pct"], reverse=True)
        for opp in h2h_opps:
            bm_str = " / ".join(f"{k}={v}" for k, v in opp["bookmakers"].items())
            logger.info("    %+.2f%% | %s vs %s | %s",
                       opp["profit_pct"], opp["home_team"], opp["away_team"], bm_str)
            logger.info("      赔率: %s", opp["best_prices"])
            logger.info("      投入 ¥1000 → 稳赚 ¥%.2f", opp["guaranteed_return"])

    if spread_arb:
        logger.info("\n  🔴 让分盘真套利:")
        spread_arb.sort(key=lambda x: x["profit_pct"], reverse=True)
        for opp in spread_arb[:5]:
            logger.info("    %+.2f%% | %s vs %s | %s",
                       opp["profit_pct"], opp["home_team"], opp["away_team"], opp["description"])
            logger.info("      投入 ¥1000 → 稳赚 ¥%.2f", opp["guaranteed_return"])

    if line_shop:
        logger.info("\n  🟡 让分盘比价机会 (line shopping):")
        for opp in line_shop[:5]:
            logger.info("    %s vs %s | %s",
                       opp["home_team"], opp["away_team"], opp["description"])

    if not all_opps:
        logger.info("\n  ✅ 当前未发现套利机会")

    # 保存
    ARBITRAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "timestamp": datetime.now().isoformat(),
        "n_total": len(all_opps),
        "n_h2h": len(h2h_opps),
        "n_spread_arb": len(spread_arb),
        "n_line_shopping": len(line_shop),
        "opportunities": all_opps,
    }
    with open(ARBITRAGE_LOG, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info("\n  已保存到 %s", ARBITRAGE_LOG)
    logger.info("=" * 60)

    return output


if __name__ == "__main__":
    report_arbitrage()
