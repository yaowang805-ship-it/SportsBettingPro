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
from src.scrapers.pinnacle_api import get_decimal_price
from src.scrapers.devig import devig_mult, shin_fair_odds
from src.scrapers.dixon_coles import correct_score_probs  # 正确比分公平概率
from src.scrapers.pinnacle_markets import get_league_matchups_and_markets, get_league_corner_markets
from src.scrapers.matching_engine import (
    get_pin_ml_sorted, get_pin_spread, get_pin_total, _pin_to_epoch,
)


# ── BTTS ──

def add_btts_opportunities(entry, bb_yes, bb_no, yes_fair, no_fair, pin_yes=None, pin_no=None, source="direct", prefix=""):
    """计算并添加 BTTS 双边进球机会到 entry。"""
    if not all([bb_yes, bb_no, yes_fair, no_fair]):
        return
    p = (prefix + "双边进球-") if prefix else "双边进球-"
    ev_yes = round((bb_yes - yes_fair) / yes_fair * 100, 2)
    ev_no = round((bb_no - no_fair) / no_fair * 100, 2)
    if ev_yes > 1:
        entry["opportunities"].append({
            "designation": p + "是",
            "bb_odds": bb_yes,
            "pin_odds": pin_yes or yes_fair,
            "fair_price": yes_fair,
            "ev_pct": ev_yes,
            "_market": "btts",
            "_price_source": source,
        })
    if ev_no > 1:
        entry["opportunities"].append({
            "designation": p + "否",
            "bb_odds": bb_no,
            "pin_odds": pin_no or no_fair,
            "fair_price": no_fair,
            "ev_pct": ev_no,
            "_market": "btts",
            "_price_source": source,
        })


# ── Odd/Even ──

def add_oe_opportunities(entry, bb_odd, bb_even, odd_fair, even_fair, pin_odd=None, pin_even=None, prefix=""):
    """计算并添加 Odd/Even (单/双) 机会到 entry。"""
    if not all([bb_odd, bb_even, odd_fair, even_fair]):
        return
    p = (prefix + "单双-") if prefix else "单双-"
    ev_odd = round((bb_odd - odd_fair) / odd_fair * 100, 2)
    ev_even = round((bb_even - even_fair) / even_fair * 100, 2)
    if ev_odd > 1:
        entry["opportunities"].append({
            "designation": p + "单",
            "bb_odds": bb_odd,
            "pin_odds": pin_odd or odd_fair,
            "fair_price": odd_fair,
            "ev_pct": ev_odd,
            "_market": "oe",
        })
    if ev_even > 1:
        entry["opportunities"].append({
            "designation": p + "双",
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
    pin_prices_list: Pinnacle 价格 list，每个含 designation(如\"home/home\")和 price_decimal
    """
    if not bb_htft_dict or not pin_prices_list or len(pin_prices_list) < 9:
        return

    # 按 designation 标签建立索引（不再靠位置猜测）
    pin_by_key = {}
    for p in pin_prices_list:
        des = p.get("designation", "").lower().replace(" ", "")
        val = get_decimal_price(p) or 0
        if val > 0 and des:
            pin_by_key[des] = val

    if len(pin_by_key) < 9:
        return

    # 去抽水
    imp = sum(1.0 / v for v in pin_by_key.values())
    fair_by_key = {k: round(v * imp, 4) for k, v in pin_by_key.items()}

    for i in range(9):
        key = HTFT_KEYS[i]  # "home/home", "home/draw", ...
        bb_val = bb_htft_dict.get(key, 0)
        pin_val = pin_by_key.get(key, 0)
        fair_val = fair_by_key.get(key, 0)
        if not bb_val or bb_val <= 1 or not fair_val:
            continue
        ev = round((bb_val - fair_val) / fair_val * 100, 2)
        if ev > 1:
            entry["opportunities"].append({
                "designation": HTFT_LABELS[i],
                "bb_odds": bb_val,
                "pin_odds": pin_val,
                "fair_price": fair_val,
                "ev_pct": ev,
                "_market": "htft",
            })


# ── Corner (角球) ──

def fetch_corner_opportunities(bb_matches, all_pin_leagues, matched_leagues):
    """角球市场对比：从 Pinnacle 基础联赛提取角球数据并与 BB 角球赔率对比。

    Pinnacle 角球是基础联赛里的子比赛 (league.name 以 " Corners" 结尾,
    units == "Corners"), 不是独立联赛。所以映射 BB 联赛 → 基础联赛 id,
    再用 get_league_corner_markets 从基础联赛 matchups 里提取角球市场。

    Returns list of corner opportunity entries (empty list if none).
    """
    # 1. 找出哪些基础联赛名有 " Corners" 子比赛
    corner_base_names = set()
    for info in all_pin_leagues.values():
        name = info.get("name", "")
        if name.endswith(" Corners"):
            corner_base_names.add(name[:-8])

    if not corner_base_names:
        return []

    # 2. 映射 BB 联赛 → 基础 Pinnacle 联赛 id (该基础联赛需有角球子比赛)
    from src.scrapers.pinnacle_league_map import lookup_pin_league
    bb_league_to_base = {}
    for bb_league in matched_leagues:
        for pid in matched_leagues[bb_league]:
            info = lookup_pin_league(all_pin_leagues, pid)
            base_name = info.get("name", "")
            if base_name in corner_base_names:
                bb_league_to_base[bb_league] = pid
                break

    if not bb_league_to_base:
        return []

    # 3. 收集有角球数据的 BB 比赛
    bb_corner_matches = []
    for m in bb_matches:
        if detect_sport(m) != "football":
            continue
        bb_league = m.get("league", "?")
        if bb_league not in bb_league_to_base:
            continue
        odds_ft = m.get("odds_ft", {})
        if not isinstance(odds_ft, dict):
            continue
        if not any([odds_ft.get("corner_ml"), odds_ft.get("corner_hc"), odds_ft.get("corner_ou")]):
            continue
        bb_corner_matches.append(m)

    if not bb_corner_matches:
        return []

    # 4. 获取 Pinnacle 角球比赛（基础联赛去重）
    base_leagues_to_fetch = sorted(set(bb_league_to_base.values()))
    print(f"\n{'='*60}")
    print(f"📐 角球对比 ({len(base_leagues_to_fetch)} 个基础联赛)")
    print(f"{'='*60}")
    for lid in base_leagues_to_fetch:
        info = lookup_pin_league(all_pin_leagues, lid)
        print(f"  • {info.get('name', lid)}")

    pin_corner_matchups = []
    import concurrent.futures

    def _fetch_one_corner(lid):
        time.sleep(random.uniform(0.1, 0.3))
        matchups = get_league_corner_markets(lid)
        return lid, matchups

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_one_corner, lid): lid
                   for lid in base_leagues_to_fetch}
        for fut in concurrent.futures.as_completed(futures):
            lid, matchups = fut.result()
            info = lookup_pin_league(all_pin_leagues, lid)
            name = info.get("name", lid)
            if matchups:
                print(f"  [角球] {name}: {len(matchups)} 场")
            else:
                print(f"  [角球] {name}: ⚠️ 无角球数据")
            pin_corner_matchups.extend(matchups)

    if not pin_corner_matchups:
        print("  ⚠️ 全部基础联赛无角球返回数据")
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
            "bb_match_id": bb_m.get("id", ""),  # BB比赛ID, 结算按ID精确匹配(角球漏了, 2026-08-25 补)
            "market_type": "角球",
            "match_type": "name" if best_score >= 0.85 else "time",
            "home_bb": bb_home,
            "away_bb": bb_away,
            # V5.5: 中文名(展示用) — 与主对比循环对齐, 否则钉钉推送回退英文队名
            "home_bb_cn": bb_m.get("home_cn") or bb_home,
            "away_bb_cn": bb_m.get("away_cn") or bb_away,
            "league_cn": bb_m.get("league_cn") or bb_league,
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
                if home_sp and away_sp and get_decimal_price(home_sp) and get_decimal_price(away_sp):
                    pin_odds_h = get_decimal_price(home_sp)
                    pin_odds_a = get_decimal_price(away_sp)
                    _fairs = shin_fair_odds([pin_odds_h, pin_odds_a])
                    fair_h = _fairs[0]
                    fair_a = _fairs[1]

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
                    if pin_line is not None and abs(bb_line - pin_line) > 0.01:
                        over_p = under_p = None  # 线不匹配, 拒绝
                if over_p and under_p and get_decimal_price(over_p) and get_decimal_price(under_p):
                    _fairs = shin_fair_odds([get_decimal_price(over_p), get_decimal_price(under_p)])
                    over_fair = _fairs[0]
                    under_fair = _fairs[1]

                    ev_over = (bb_over_odds - over_fair) / over_fair * 100 if over_fair > 0 else 0
                    ev_under = (bb_under_odds - under_fair) / under_fair * 100 if under_fair > 0 else 0

                    if ev_over > 1:
                        entry["over_under"].append({
                            "designation": "角球" + mlabels["over"],
                            "line": corner_ou.get("line_str", str(bb_line)),
                            "bb_odds": bb_over_odds,
                            "pin_odds": get_decimal_price(over_p),
                            "fair_price": over_fair,
                            "ev_pct": round(ev_over, 2),
                            "_market": "corner",
                        })
                    if ev_under > 1:
                        entry["over_under"].append({
                            "designation": "角球" + mlabels["under"],
                            "line": corner_ou.get("line_str", str(bb_line)),
                            "bb_odds": bb_under_odds,
                            "pin_odds": get_decimal_price(under_p),
                            "fair_price": under_fair,
                            "ev_pct": round(ev_under, 2),
                            "_market": "corner",
                        })

        if entry["opportunities"] or entry["handicap"] or entry["over_under"]:
            corner_entries.append(entry)

    return corner_entries


def _norm_scoreline(name):
    """归一化比分线: '4-5' 或 'Arsenal 4, Coventry City 5' -> '4-5'"""
    import re as _re
    nums = _re.findall(r'\d+', str(name))
    if len(nums) >= 2:
        return f"{nums[0]}-{nums[1]}"
    return str(name).strip()


def _norm_correct_score_ht(opts):
    """归一化半场正确比分选项, 兜底项聚合成单一 'others' 桶。

    BB mty=1100 只有 9 条显式比分 + 单个 "Others"; Pinnacle 可能有多个兜底
    ("Any Other Home Win"/"Any Other Away Win"/"Any Other Draw") 或更多显式比分。
    兜底项按隐含概率(1/price)求和合并, 避免 dict 覆盖丢概率。
    返回 {归一化名: 赔率}, 兜底统一为 'others'。
    """
    import re as _re
    norm = {}
    others_inv = 0.0
    for o in opts:
        name = str(o.get("name", "") or "").strip()
        odds = o.get("odds", 0) or 0
        if not name or odds <= 1.0:
            continue
        s = name.lower()
        if "other" in s or "其余" in s:
            others_inv += 1.0 / odds
            continue
        nums = _re.findall(r'\d+', s)
        if len(nums) >= 2:
            key = f"{nums[0]}-{nums[1]}"
            if key not in norm:
                norm[key] = odds
    if others_inv > 0:
        norm["others"] = 1.0 / others_inv
    return norm


def _norm_special_name(name):
    """归一化特殊盘口选项名(净胜球/总进球区间/先进球)。

    'Maitland FC - Win By 1 Goal' -> 'win by 1'
    'Arsenal By 1' -> 'by 1'
    '2 - 3' -> '2-3'
    'Neither' -> 'neither'
    """
    import re as _re
    s = str(name).lower().strip()
    s = _re.sub(r'\s+', ' ', s)
    # 净胜球: "X - Win By N Goal(s)" 或 "X By N" -> "by n"
    m = _re.search(r'by\s*(\d+)', s)
    if m:
        return f"by{m.group(1)}"
    # 总进球区间: "2 - 3" -> "2-3"
    m = _re.search(r'(\d+)\s*-\s*(\d+)', s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    if '7+' in s or '+7' in s:
        return "7+"
    # 先进球: "neither"/"none" -> "neither"
    if 'neither' in s or 'none' in s or s == '无':
        return "neither"
    return s


def _norm_margin_side(name, bb_home, bb_away):
    """净胜球选项名归一化, 区分主/客: 'Sogndal IL - Win By 1 Goal' -> 'home_by1' / 'away_by1' / 'draw'。

    主客判断靠选项名包含主/客队名(允许子串), 避免原来 'by1' 主客碰撞导致赔率互相覆盖。
    """
    import re as _re
    s = str(name).lower().strip()
    s = _re.sub(r'\s+', ' ', s)
    m = _re.search(r'by\s*(\d+)', s)
    if not m:
        return "draw" if 'draw' in s else s
    byn = m.group(1)
    hh = (bb_home or '').lower().strip()
    aa = (bb_away or '').lower().strip()
    if hh and (hh in s or s in hh):
        return f"home_by{byn}"
    if aa and (aa in s or s in aa):
        return f"away_by{byn}"
    return f"by{byn}"


def fetch_special_opportunities(bb_matches, all_pin_leagues, matched_leagues):
    """特殊盘口(正确比分/净胜球/总进球区间/先进球)对比。

    BB mty=1188/1018/1101/1019 vs Pinnacle special 子比赛(Correct Score 等)。
    返回 opportunity entries (复用 fetch_corner_opportunities 的结构)。
    """
    from src.scrapers.pinnacle_markets import get_league_special_markets
    from src.scrapers.pinnacle_league_map import lookup_pin_league
    from src.scrapers.devig import devig_mult, shin_fair_odds

    # BB 有特殊盘口的比赛 (V5.11: 补半场特殊盘 correct_score_ht, 从 odds_ht 取)
    bb_special_matches = []
    for m in bb_matches:
        ft = m.get("odds_ft", {})
        ht = m.get("odds_ht", {})
        if any(ft.get(k) for k in ("correct_score", "winning_margin", "total_goals_range", "first_to_score")) \
                or any(ht.get(k) for k in ("correct_score_ht",)):
            bb_special_matches.append(m)
    if not bb_special_matches:
        return []

    # 每个 matched 联赛拉特殊盘口
    special_by_league = {}
    for bb_league, pin_ids in matched_leagues.items():
        for pid in pin_ids:
            try:
                spec = get_league_special_markets(pid)
                if spec:
                    special_by_league[bb_league] = spec
                    break
            except Exception:
                continue

    SPECIAL_KEY_TO_MKT = {
        # "correct_score" 已删除 (2026-08-18): BB mty=1188「正确比分」只有高比分(2-1/5-1/6-6),
        # 无低比分(0-0/1-0/1-1), 与 Pinnacle Correct Score(含全部比分)错配, 赔率对不上波胆。
        # V5.11: correct_score_ht 半场正确比分 — BB mty=1100 含全比分(0-0/1-0/.../Others),
        # 与 Pinnacle "Correct Score 1st Half" 对齐(半场比分上限低, 9条显式+兜底, 无错配)。
        "winning_margin": ("winning_margin", "净胜球"),
        "total_goals_range": ("total_goals_range", "总进球区间"),
        "first_to_score": ("first_to_score", "先进球"),
        "correct_score_ht": ("correct_score_ht", "上半场正确比分"),
    }

    entries = []
    for m in bb_special_matches:
        bb_league = m.get("league", "?")
        spec_map = special_by_league.get(bb_league)
        if not spec_map:
            continue
        bb_home = (m.get("home") or "").strip()
        bb_away = (m.get("away") or "").strip()
        # 找 Pinnacle 特殊盘口 — 按队名匹配父比赛(与角球一致), 不再用"第一个联赛"错配
        bb_home_en = TEAM_NAME_MAP.get(bb_home, bb_home).lower()
        bb_away_en = TEAM_NAME_MAP.get(bb_away, bb_away).lower()
        pin_specs = None
        best_score = 0.0
        for pid, info in spec_map.items():
            pin_home = (info.get("home") or "").strip().lower()
            pin_away = (info.get("away") or "").strip().lower()
            if not pin_home or not pin_away:
                continue
            _parts = []
            if bb_home_en and pin_home:
                if bb_home_en == pin_home:
                    _parts.append(1.0)
                elif bb_home_en in pin_home or pin_home in bb_home_en:
                    _parts.append(0.9)
                else:
                    _parts.append(_SM(None, bb_home_en, pin_home).ratio() * 0.7)
            if bb_away_en and pin_away:
                if bb_away_en == pin_away:
                    _parts.append(1.0)
                elif bb_away_en in pin_away or pin_away in bb_away_en:
                    _parts.append(0.9)
                else:
                    _parts.append(_SM(None, bb_away_en, pin_away).ratio() * 0.7)
            if not _parts:
                continue
            _sc = sum(_parts) / len(_parts)
            if _sc > best_score:
                best_score = _sc
                pin_specs = info.get("markets")
        if not pin_specs or best_score < 0.70:
            continue

        ft = m.get("odds_ft", {})
        ht = m.get("odds_ht", {})  # V5.11: 半场特殊盘(correct_score_ht)从 odds_ht 取
        # 开赛时间(北京时间) — 之前特殊盘口 entry 漏了, 推送显示"无时间"且不做开赛时间窗过滤
        bb_start = ""
        bb_epoch = None
        bb_bt = m.get("bt")
        if bb_bt:
            try:
                bb_epoch = int(int(bb_bt) / 1000)
                bb_dt = datetime.fromtimestamp(bb_epoch, tz=timezone.utc)
                bb_start = bb_dt.astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
            except (ValueError, TypeError, OSError):
                pass
        entry = {
            "league": bb_league,
            "bb_match_id": m.get("id", ""),  # BB比赛ID, 结算按ID精确匹配(特殊盘口漏了, 2026-08-25 补)
            "market_type": "特殊盘口",
            "match_type": "name" if best_score >= 0.85 else "time",
            "home_bb": bb_home,
            "away_bb": bb_away,
            # V5.5: 中文名(展示用) — 与主对比循环对齐, 否则钉钉推送回退英文队名
            "home_bb_cn": m.get("home_cn") or bb_home,
            "away_bb_cn": m.get("away_cn") or bb_away,
            "league_cn": m.get("league_cn") or bb_league,
            "home_pin": bb_home,
            "away_pin": bb_away,
            "match_score": round(best_score, 3),
            "sport": "football",
            "flags": [],
            "start_time_bb": bb_start,
            "start_time_pin_epoch": bb_epoch if bb_bt else None,
            "opportunities": [],
            "handicap": [],
            "over_under": [],
            "double_chance": [],
            "draw_no_bet": [],
        }
        for bb_key, (pin_key, label) in SPECIAL_KEY_TO_MKT.items():
            # V5.11: correct_score_ht 从 odds_ht 取, 其余从 odds_ft 取
            bb_opts = (ht if bb_key.endswith("_ht") else ft).get(bb_key)
            pin_opts = pin_specs.get(pin_key)
            if not bb_opts or not pin_opts:
                continue
            # 归一化并匹配
            if bb_key in ("correct_score", "correct_score_ht"):
                norm_bb = {_norm_scoreline(o["name"]): o["odds"] for o in bb_opts}
                norm_pin = {_norm_scoreline(o["name"]): o["odds"] for o in pin_opts}
            elif bb_key == "winning_margin":
                # 净胜球: 主/客都要区分, 否则 "主赢1球"和"客赢1球"都归一化成 by1 碰撞(赔率互相覆盖)
                norm_bb = {_norm_margin_side(o["name"], bb_home, bb_away): o["odds"] for o in bb_opts}
                norm_pin = {_norm_margin_side(o["name"], bb_home, bb_away): o["odds"] for o in pin_opts}
            else:
                norm_bb = {_norm_special_name(o["name"]): o["odds"] for o in bb_opts}
                norm_pin = {_norm_special_name(o["name"]): o["odds"] for o in pin_opts}
                # 先进球: BB mty=1019 的 "None" 选项 od=-999(不开放)→2-way(0-0走盘);
                # Pin "First Team To Score" 是 3-way(含 Neither)。BB 无 neither 时, 去掉 Pin 的 neither 重归一化
                if bb_key == "first_to_score" and "neither" not in norm_bb and "neither" in norm_pin:
                    norm_pin.pop("neither")
            # 公平价: 正确比分用 Dixon-Coles 模型(最准), 其它用比例法去抽水
            fair_map = {}
            if bb_key == "correct_score":
                _dc = correct_score_probs(bb_home, bb_away)
                for _name in norm_bb:
                    _p = _dc.get(_name, 0.0)
                    if _p > 0:
                        fair_map[_name] = 1.0 / _p
            if not fair_map:
                pin_decimal = [v for v in norm_pin.values() if v > 1.0]
                if pin_decimal:
                    # V5.10 修复: devig_mult 必须只在「BB 与 Pin 选项集等价」时用全集去抽水。
                    # 净胜球/总进球区间等市场 BB 往往只下注 Pin 全集里的少数几条腿,
                    # 用 Pin 全集做 devig 分母会把其它腿的概率挤到 BB 有的腿上, 公平价
                    # 系统性虚高(实测皇马净胜球 BB 下 2 条, 却算出 +47% 的假 EV)。
                    # 选项集不等价时, 结构不对等 → 不出数(对齐后再算)。
                    if set(norm_bb) != set(norm_pin):
                        continue
                    # 比例法去抽水(devig_mult 返回公平概率, 公平赔率=1/prob)
                    probs = devig_mult(pin_decimal)
                    for i, k in enumerate(norm_pin):
                        if i < len(probs) and probs[i] > 0:
                            fair_map[k] = 1.0 / probs[i]
            for name, bb_odds in norm_bb.items():
                fair = fair_map.get(name)
                if not fair or fair <= 0:
                    continue
                # 正确比分长尾线BB赔率封顶(~251/301)不可靠, 只比可靠区间
                if bb_odds > 30:
                    continue
                ev = (bb_odds - fair) / fair * 100
                if ev > 1:
                    entry["opportunities"].append({
                        "designation": f"{label}{name}",
                        "bb_odds": bb_odds,
                        "pin_odds": norm_pin.get(name, 0),
                        "fair_price": round(fair, 4),
                        "ev_pct": round(ev, 2),
                        "_market": bb_key,  # V5.7: 用真实盘口名(correct_score/winning_margin/...), 不再统一"special"按1X2满仓
                    })
        if entry["opportunities"]:
            entries.append(entry)
    return entries
