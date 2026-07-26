"""子市场机会创建：BTTS/OE/HT/FT/角球

从 bb_vs_pinnacle.py 提取，保持函数签名兼容。
"""
import random, time
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher as _SM
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

from src.scrapers.bb_data import (
    MARKET_LABELS, detect_sport, parse_asian_line,
)
from src.scrapers.pinnacle_league_map import TEAM_NAME_MAP
from src.scrapers.pinnacle_markets import get_league_matchups_and_markets
from src.scrapers.matching_engine import (
    get_pin_ml_sorted, get_pin_spread, get_pin_total, _pin_to_epoch,
)


# ── BTTS ──

def add_btts_opportunities(entry, bb_yes, bb_no, yes_fair, no_fair, pin_yes=None, pin_no=None, source="direct"):
    """计算并添加 BTTS 双边进球机会到 entry。"""
    if not all([bb_yes, bb_no, yes_fair, no_fair]):
        return
    ev_yes = round((bb_yes - yes_fair) / yes_fair * 100, 2)
    ev_no = round((bb_no - no_fair) / no_fair * 100, 2)
    if ev_yes > 1:
        entry["opportunities"].append({
            "designation": "双边进球-是",
            "bb_odds": bb_yes,
            "pin_odds": pin_yes or yes_fair,
            "fair_price": yes_fair,
            "ev_pct": ev_yes,
            "_market": "btts",
            "_price_source": source,
        })
    if ev_no > 1:
        entry["opportunities"].append({
            "designation": "双边进球-否",
            "bb_odds": bb_no,
            "pin_odds": pin_no or no_fair,
            "fair_price": no_fair,
            "ev_pct": ev_no,
            "_market": "btts",
            "_price_source": source,
        })


# ── Odd/Even ──

def add_oe_opportunities(entry, bb_odd, bb_even, odd_fair, even_fair, pin_odd=None, pin_even=None):
    """计算并添加 Odd/Even (单/双) 机会到 entry。"""
    if not all([bb_odd, bb_even, odd_fair, even_fair]):
        return
    ev_odd = round((bb_odd - odd_fair) / odd_fair * 100, 2)
    ev_even = round((bb_even - even_fair) / even_fair * 100, 2)
    if ev_odd > 1:
        entry["opportunities"].append({
            "designation": "单",
            "bb_odds": bb_odd,
            "pin_odds": pin_odd or odd_fair,
            "fair_price": odd_fair,
            "ev_pct": ev_odd,
            "_market": "oe",
        })
    if ev_even > 1:
        entry["opportunities"].append({
            "designation": "双",
            "bb_odds": bb_even,
            "pin_odds": pin_even or even_fair,
            "fair_price": even_fair,
            "ev_pct": ev_even,
            "_market": "oe",
        })


# ── HT/FT ──

HTFT_LABELS = [
    "半/全场-主/主", "半/全场-主/和局", "半/全场-主/客",
    "半/全场-和局/主", "半/全场-和局/和局", "半/全场-和局/客",
    "半/全场-客/主", "半/全场-客/和局", "半/全场-客/客",
]

HTFT_KEYS = [
    "home/home", "home/draw", "home/away",
    "draw/home", "draw/draw", "draw/away",
    "away/home", "away/draw", "away/away",
]


def add_htft_opportunities(entry, bb_htft_dict, pin_prices_list):
    """计算并添加 HT/FT (半全场) 机会到 entry。

    bb_htft_dict: BB赔率 dict，键为 HTFT_KEYS，值为赔率
    pin_prices_list: Pinnacle 9个价格 dict 列表（含 price_decimal）
    """
    if not bb_htft_dict or not pin_prices_list or len(pin_prices_list) < 9:
        return
    raw_prices = []
    for p in pin_prices_list:
        val = p.get("price_decimal", 0)
        if val <= 0:
            return
        raw_prices.append(val)
    if len(raw_prices) < 9:
        return
    imp = sum(1.0 / v for v in raw_prices)
    fair_prices = [round(v * imp, 4) for v in raw_prices]
    for i in range(9):
        key = HTFT_KEYS[i]
        bb_val = bb_htft_dict.get(key, 0)
        if not bb_val or bb_val <= 1:
            continue
        ev = round((bb_val - fair_prices[i]) / fair_prices[i] * 100, 2)
        if ev > 1:
            entry["opportunities"].append({
                "designation": HTFT_LABELS[i],
                "bb_odds": bb_val,
                "pin_odds": raw_prices[i],
                "fair_price": fair_prices[i],
                "ev_pct": ev,
                "_market": "htft",
            })


# ── Corner (角球) ──

def fetch_corner_opportunities(bb_matches, all_pin_leagues, matched_leagues):
    """角球市场对比：从 Pinnacle 角球联赛提取数据并与 BB 角球赔率对比。

    Pinnacle 角球联赛命名规则：原联赛名 + " Corners"
    例如 "Brazil - Serie A" → "Brazil - Serie A Corners"

    Returns list of corner opportunity entries (empty list if none).
    """
    # 1. 找出所有 Pinnacle 角球联赛（含比赛）
    pin_corner_by_base = {}
    for lid, info in all_pin_leagues.items():
        name = info.get("name", "")
        if name.endswith(" Corners") and info.get("matchup_count", 0) > 0:
            base = name[:-8]
            pin_corner_by_base[base] = {"id": lid, "name": name, "sport_id": info.get("sport_id")}

    if not pin_corner_by_base:
        return []

    # 2. 映射 BB 联赛 → 角球联赛
    bb_league_to_corner = {}
    for bb_league in matched_leagues:
        pin_ids = matched_leagues[bb_league]
        for pid in pin_ids:
            base_name = all_pin_leagues.get(pid, {}).get("name", "")
            if base_name in pin_corner_by_base:
                bb_league_to_corner[bb_league] = pin_corner_by_base[base_name]
                break

    if not bb_league_to_corner:
        return []

    # 3. 收集有角球数据的 BB 比赛
    bb_corner_matches = []
    for m in bb_matches:
        if detect_sport(m) != "football":
            continue
        bb_league = m.get("league", "?")
        if bb_league not in bb_league_to_corner:
            continue
        odds_ft = m.get("odds_ft", {})
        if not isinstance(odds_ft, dict):
            continue
        if not any([odds_ft.get("corner_ml"), odds_ft.get("corner_hc"), odds_ft.get("corner_ou")]):
            continue
        bb_corner_matches.append(m)

    if not bb_corner_matches:
        return []

    # 4. 获取 Pinnacle 角球比赛（联赛去重）
    corner_leagues_to_fetch = {}
    for bb_league, cinfo in bb_league_to_corner.items():
        lid = cinfo["id"]
        if lid not in corner_leagues_to_fetch:
            corner_leagues_to_fetch[lid] = cinfo

    league_names = sorted(cinfo["name"] for cinfo in corner_leagues_to_fetch.values())
    print(f"\n{'='*60}")
    print(f"📐 角球对比 ({len(corner_leagues_to_fetch)} 个联赛)")
    print(f"{'='*60}")
    for cn in league_names:
        print(f"  • {cn}")

    pin_corner_matchups = []
    for lid, cinfo in corner_leagues_to_fetch.items():
        time.sleep(random.uniform(0.3, 0.5))
        matchups = get_league_matchups_and_markets(lid)
        if matchups:
            print(f"  [角球] {cinfo['name']}: {len(matchups)} 场")
        else:
            print(f"  [角球] {cinfo['name']}: ⚠️ 无数据")
        pin_corner_matchups.extend(matchups)

    if not pin_corner_matchups:
        print("  ⚠️ 全部角球联赛无返回数据")
        return []

    # 5. 匹配 BB → Pinnacle 角球 + EV 计算
    mlabels = MARKET_LABELS["football"]
    corner_entries = []

    for bb_m in bb_corner_matches:
        bb_league = bb_m.get("league", "?")
        bb_home = bb_m.get("home_team", bb_m.get("home", "")).strip()
        bb_away = bb_m.get("away_team", bb_m.get("away", "")).strip()

        # 找最佳匹配的 Pinnacle 角球比赛
        best_pin = None
        best_score = 0.0

        for pin_m in pin_corner_matchups:
            pin_home = pin_m.get("home", "").strip().lower()
            pin_away = pin_m.get("away", "").strip().lower()
            bb_home_en = TEAM_NAME_MAP.get(bb_home, bb_home).lower()
            bb_away_en = TEAM_NAME_MAP.get(bb_away, bb_away).lower()

            score_parts = []
            if bb_home_en and pin_home:
                if bb_home_en == pin_home:
                    score_parts.append(1.0)
                elif bb_home_en in pin_home or pin_home in bb_home_en:
                    score_parts.append(0.9)
                else:
                    sm = _SM(None, bb_home_en, pin_home)
                    score_parts.append(sm.ratio() * 0.7)

            if bb_away_en and pin_away:
                if bb_away_en == pin_away:
                    score_parts.append(1.0)
                elif bb_away_en in pin_away or pin_away in bb_away_en:
                    score_parts.append(0.9)
                else:
                    sm = _SM(None, bb_away_en, pin_away)
                    score_parts.append(sm.ratio() * 0.7)

            avg = sum(score_parts) / len(score_parts) if score_parts else 0
            if avg > best_score:
                best_score = avg
                best_pin = pin_m

        if best_score < 0.70:
            continue

        odds_ft = bb_m.get("odds_ft", {})
        corner_ml = odds_ft.get("corner_ml", [])
        corner_hc = odds_ft.get("corner_hc")
        corner_ou = odds_ft.get("corner_ou")

        if not any([corner_ml, corner_hc, corner_ou]):
            continue

        # 开赛时间
        bb_bt = bb_m.get("bt")
        bb_start = ""
        if bb_bt:
            try:
                bb_epoch = int(int(bb_bt) / 1000)
                bb_dt = datetime.fromtimestamp(bb_epoch, tz=timezone.utc)
                bb_bj = bb_dt.astimezone(timezone(timedelta(hours=8)))
                bb_start = bb_bj.strftime("%m/%d %H:%M")
            except (ValueError, TypeError, OSError):
                pass

        entry = {
            "league": bb_league,
            "market_type": "角球",
            "match_type": "name" if best_score >= 0.85 else "time",
            "home_bb": bb_home,
            "away_bb": bb_away,
            "home_pin": best_pin.get("home", ""),
            "away_pin": best_pin.get("away", ""),
            "match_score": round(best_score, 3),
            "sport": "football",
            "flags": [],
            "start_time_bb": bb_start,
            "start_time_pin": best_pin.get("start_time", ""),
            "start_time_pin_epoch": _pin_to_epoch(best_pin),
            "platform_sources": bb_m.get("platform_sources", {}),
            "bb_price_source": bb_m.get("platform", "BB"),
            "opportunities": [],
            "handicap": [],
            "over_under": [],
            "double_chance": [],
            "draw_no_bet": [],
        }

        # --- 角球独赢 (Corner ML) ---
        if corner_ml and len(corner_ml) >= 3:
            pin_ml = get_pin_ml_sorted(best_pin, "football")
            if len(pin_ml) >= 3:
                total_implied = sum(1.0 / p for p in pin_ml if p and p > 0)
                for i in range(3):
                    bb_o = corner_ml[i]
                    pin_o = pin_ml[i]
                    if pin_o and pin_o > 0:
                        fair = round(pin_o * total_implied, 4) if total_implied > 0 else round(pin_o, 2)
                        ev = (bb_o - fair) / fair * 100 if fair > 0 else 0
                        if ev > 1:
                            entry["opportunities"].append({
                                "designation": mlabels["ml"][i] + "(角球)",
                                "bb_odds": bb_o,
                                "pin_odds": pin_o,
                                "fair_price": fair,
                                "ev_pct": round(ev, 2),
                                "_market": "corner",
                            })

        # --- 角球让球 (Corner HC) ---
        if isinstance(corner_hc, dict):
            bb_home_str = corner_hc.get("home_line_str", "")
            bb_away_str = corner_hc.get("away_line_str", "")
            bb_home_odds = corner_hc.get("home_odds")
            bb_away_odds = corner_hc.get("away_odds")

            bb_hl_val = parse_asian_line(bb_home_str) if bb_home_str else None
            if bb_hl_val is None:
                bb_hl_val = parse_asian_line(bb_away_str) if bb_away_str else None

            if bb_hl_val is not None and bb_home_odds and bb_away_odds:
                home_sp, away_sp, _ = get_pin_spread(best_pin, target_line=bb_hl_val)
                # 校准: 角球让球线必须精确匹配
                if home_sp and away_sp:
                    pin_line = home_sp.get("points")
                    if pin_line is not None and abs(bb_hl_val - pin_line) > 0.001:
                        home_sp = away_sp = None  # 线不匹配, 拒绝
                if home_sp and away_sp and home_sp.get("price_decimal") and away_sp.get("price_decimal"):
                    pin_odds_h = home_sp["price_decimal"]
                    pin_odds_a = away_sp["price_decimal"]
                    imp = 1.0 / pin_odds_h + 1.0 / pin_odds_a
                    fair_h = round(pin_odds_h * imp, 4)
                    fair_a = round(pin_odds_a * imp, 4)

                    ev_h = (bb_home_odds - fair_h) / fair_h * 100 if fair_h > 0 else 0
                    ev_a = (bb_away_odds - fair_a) / fair_a * 100 if fair_a > 0 else 0

                    if ev_h > 1:
                        entry["handicap"].append({
                            "designation": "角球" + mlabels["hc_home"],
                            "line": bb_home_str,
                            "bb_odds": bb_home_odds,
                            "pin_odds": pin_odds_h,
                            "fair_price": fair_h,
                            "ev_pct": round(ev_h, 2),
                            "_market": "corner",
                        })
                    if ev_a > 1:
                        entry["handicap"].append({
                            "designation": "角球" + mlabels["hc_away"],
                            "line": bb_away_str,
                            "bb_odds": bb_away_odds,
                            "pin_odds": pin_odds_a,
                            "fair_price": fair_a,
                            "ev_pct": round(ev_a, 2),
                            "_market": "corner",
                        })

        # --- 角球大小 (Corner OU) ---
        if isinstance(corner_ou, dict):
            bb_line = corner_ou.get("line")
            bb_over_odds = corner_ou.get("over_odds")
            bb_under_odds = corner_ou.get("under_odds")

            if bb_line is not None and bb_over_odds and bb_under_odds:
                over_p, under_p = get_pin_total(best_pin, target_line=bb_line)
                # 校准: 角球大小线必须精确匹配
                if over_p and under_p:
                    pin_line = over_p.get("points")
                    if pin_line is not None and abs(bb_line - pin_line) > 0.1:
                        over_p = under_p = None  # 线不匹配, 拒绝
                if over_p and under_p and over_p.get("price_decimal") and under_p.get("price_decimal"):
                    imp = 1.0 / over_p["price_decimal"] + 1.0 / under_p["price_decimal"]
                    over_fair = round(over_p["price_decimal"] * imp, 4)
                    under_fair = round(under_p["price_decimal"] * imp, 4)

                    ev_over = (bb_over_odds - over_fair) / over_fair * 100 if over_fair > 0 else 0
                    ev_under = (bb_under_odds - under_fair) / under_fair * 100 if under_fair > 0 else 0

                    if ev_over > 1:
                        entry["over_under"].append({
                            "designation": "角球" + mlabels["over"],
                            "line": corner_ou.get("line_str", str(bb_line)),
                            "bb_odds": bb_over_odds,
                            "pin_odds": over_p["price_decimal"],
                            "fair_price": over_fair,
                            "ev_pct": round(ev_over, 2),
                            "_market": "corner",
                        })
                    if ev_under > 1:
                        entry["over_under"].append({
                            "designation": "角球" + mlabels["under"],
                            "line": corner_ou.get("line_str", str(bb_line)),
                            "bb_odds": bb_under_odds,
                            "pin_odds": under_p["price_decimal"],
                            "fair_price": under_fair,
                            "ev_pct": round(ev_under, 2),
                            "_market": "corner",
                        })

        if entry["opportunities"] or entry["handicap"] or entry["over_under"]:
            corner_entries.append(entry)

    return corner_entries
