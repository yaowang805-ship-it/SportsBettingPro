"""多运动辅助对比 — 通过 the-odds-api 补 Pinnacle 不覆盖的运动/盘口。

支持的 the-odds-api sport keys:
  basketball_wnba, boxing_boxing, tennis_atp_*, tennis_wta_*,
  americanfootball_ncaaf, baseball_mlb (已有 Pinnacle 但可补盘口)

用法: python3 -m src.scrapers.odds_api_compare
"""
import json, sys, time, requests
from pathlib import Path
from collections import defaultdict

SRC_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC_DIR))

from config.settings import ODDS_API_KEYS, DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

BB_EXTRACTED = DATA_DIR / "bb_odds_extracted.json"
TEAM_MAP_FILE = DATA_DIR / "team_name_map.json"
OUTPUT_DIR = DATA_DIR

# BB运动 → the-odds-api sport key 映射
SPORT_KEY_MAP = {
    "basketball_wnba": "basketball_wnba",
    "boxing": "boxing_boxing",
    "american_football": "americanfootball_ncaaf",
    "tennis": None,  # 网球按赛事匹配，见下方
}

# 网球赛事关键词 → the-odds-api key
TENNIS_KEY_MAP = {
    "ATP - 华盛顿公开赛": "tennis_atp_washington_open",
    "WTA - 华盛顿公开赛": "tennis_wta_washington_open",
}


def _get_api_key():
    """返回可用配额最多的 key。"""
    if not ODDS_API_KEYS:
        return None
    # 简单选第 2 个（通常配额更多）
    return ODDS_API_KEYS[1] if len(ODDS_API_KEYS) > 1 else ODDS_API_KEYS[0]


def _get_best_odds(match_data, market_key):
    """从多个 bookmaker 取最高赔率。"""
    best = {}
    for bm in match_data.get("bookmakers", []):
        for mk in bm.get("markets", []):
            if mk.get("key") == market_key:
                for o in mk.get("outcomes", []):
                    name = o.get("name")
                    price = o.get("price")
                    if name and price and (name not in best or price > best[name]):
                        best[name] = price
    return best


def _de_vig(prices: dict) -> dict:
    if len(prices) < 2:
        return prices
    total = sum(1.0 / p for p in prices.values())
    return {k: round(v * total, 4) for k, v in prices.items()} if total > 0 else prices


def _load_team_map():
    tm = {}
    if TEAM_MAP_FILE.exists():
        tm = json.loads(TEAM_MAP_FILE.read_text())
    return tm


def compare_sport(odds_sport_key, bb_sport_filter=None):
    """通用运动对比。odds_sport_key: the-odds-api sport key"""
    key = _get_api_key()
    if not key:
        return []

    # BB 数据
    if not BB_EXTRACTED.exists():
        return []
    bb_all = json.loads(BB_EXTRACTED.read_text())
    if bb_sport_filter:
        bb_matches = [m for m in bb_all.get("matches", []) if m.get("sport") == bb_sport_filter]
    else:
        bb_matches = list(bb_all.get("matches", []))

    if not bb_matches:
        return []

    # the-odds-api
    try:
        resp = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{odds_sport_key}/odds",
            params={
                "apiKey": key, "regions": "us",
                "markets": "h2h,spreads,totals", "oddsFormat": "decimal",
            }, timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"the-odds-api {odds_sport_key}: {resp.status_code}")
            return []
        odds_data = resp.json()
    except Exception as e:
        logger.warning(f"the-odds-api {odds_sport_key}: {e}")
        return []

    tm = _load_team_map()
    details = []

    for bb_m in bb_matches:
        bb_home = bb_m.get("home", "").strip()
        bb_away = bb_m.get("away", "").strip()
        en_home = tm.get(bb_home, bb_home)
        en_away = tm.get(bb_away, bb_away)

        # 匹配
        match = None
        for om in odds_data:
            h = om.get("home_team", "")
            a = om.get("away_team", "")
            home_match = en_home.lower() in h.lower() or h.lower() in en_home.lower()
            away_match = en_away.lower() in a.lower() or a.lower() in en_away.lower()
            if home_match and away_match:
                match = om; break

        if not match:
            continue

        entry = {
            "sport": bb_m.get("sport", "basketball"),
            "league": bb_m.get("league", odds_sport_key),
            "home_bb": bb_home, "away_bb": bb_away,
            "home_pin": match.get("home_team", ""),
            "away_pin": match.get("away_team", ""),
            "match_score": 1.0, "match_type": "name",
            "bb_price_source": "BB",
            "start_time_pin_epoch": 0,
            "opportunities": [], "handicap": [], "over_under": [],
        }

        bb_ml = bb_m.get("odds_ft", {}).get("ml", [])
        best_ml = _get_best_odds(match, "h2h")
        if len(best_ml) >= 2 and len(bb_ml) >= 2:
            fair = _de_vig(best_ml)
            for label, bb_odds, side in [
                (bb_home, bb_ml[0], en_home),
                (bb_away, bb_ml[1], en_away),
            ]:
                fair_price = fair.get(side, 0)
                if fair_price > 0:
                    ev = round((bb_odds - fair_price) / fair_price * 100, 2)
                    if ev > 1:
                        entry["opportunities"].append({
                            "designation": label, "bb_odds": bb_odds,
                            "pin_odds": best_ml.get(side, 0),
                            "fair_price": fair_price, "ev_pct": ev,
                            "_market": "1x2",
                        })

        # HC
        bb_hc = bb_m.get("odds_ft", {}).get("handicap", {})
        if bb_hc and bb_hc.get("home_odds") and bb_hc.get("away_odds"):
            best_sp = _get_best_odds(match, "spreads")
            if best_sp:
                fair_sp = _de_vig(best_sp)
                bb_hl = bb_hc.get("home_line")
                for name, price in best_sp.items():
                    if name in (en_home, en_away) and name in fair_sp:
                        fp = fair_sp[name]
                        bb_o = bb_hc["home_odds"] if name == en_home else bb_hc["away_odds"]
                        ev = round((bb_o - fp) / fp * 100, 2)
                        if ev > 1:
                            entry["handicap"].append({
                                "designation": f"让球{'主胜' if name == en_home else '客胜'}",
                                "line": str(bb_hl), "bb_odds": bb_o,
                                "pin_odds": price, "fair_price": fp,
                                "ev_pct": ev, "_market": "hc",
                            })

        # OU
        bb_ou = bb_m.get("odds_ft", {}).get("ou", {})
        if bb_ou and bb_ou.get("over_odds") and bb_ou.get("under_odds"):
            best_tot = _get_best_odds(match, "totals")
            if best_tot:
                fair_tot = _de_vig(best_tot)
                for label, key in [("大球", "Over"), ("小球", "Under")]:
                    fp = fair_tot.get(key, 0)
                    bo = bb_ou["over_odds"] if label == "大球" else bb_ou["under_odds"]
                    if fp > 0:
                        ev = round((bo - fp) / fp * 100, 2)
                        if ev > 1:
                            entry["over_under"].append({
                                "designation": f"{label}({bb_ou.get('line','?')})",
                                "line": str(bb_ou.get("line", "")),
                                "bb_odds": bo, "pin_odds": best_tot.get(key, 0),
                                "fair_price": fp, "ev_pct": ev, "_market": "ou",
                            })

        if entry["opportunities"] or entry["handicap"] or entry["over_under"]:
            details.append(entry)

    return details


def run_all():
    """对所有可用运动运行辅助对比。"""
    all_details = []

    # WNBA
    wnba = compare_sport("basketball_wnba", "basketball")
    wnba = [d for d in wnba if "WNBA" in d.get("league", "")]
    all_details.extend(wnba)
    logger.info(f"WNBA: {len(wnba)} 条")

    # Boxing
    boxing = compare_sport("boxing_boxing", "boxing")
    all_details.extend(boxing)
    logger.info(f"Boxing: {len(boxing)} 条")

    # 网球 (各赛事)
    for bb_league, odds_key in TENNIS_KEY_MAP.items():
        bb_all = json.loads(BB_EXTRACTED.read_text())
        tennis_matches = [m for m in bb_all.get("matches", [])
                         if m.get("sport") == "tennis" and m.get("league") == bb_league]
        if not tennis_matches:
            continue
        # 临时替换数据
        import io
        fake = {"matches": tennis_matches}
        old_path = BB_EXTRACTED
        BB_EXTRACTED_temp = DATA_DIR / "_temp_tennis.json"
        BB_EXTRACTED_temp.write_text(json.dumps(fake))
        # HACK: reuse compare_sport but with filtered data
        # 简单直接：对网球不走通用函数，单独处理
        BB_EXTRACTED_temp.unlink()

    # 保存
    output = {
        "version": "1.0",
        "source": "the-odds-api (auxiliary)",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "details": all_details,
    }
    out_file = OUTPUT_DIR / "bb_vs_oddsapi_comparison.json"
    out_file.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    logger.info(f"辅助对比完成: {len(all_details)} 条 → {out_file}")
    return all_details


def main():
    details = run_all()
    print(json.dumps({"details_count": len(details)}, indent=2))


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
