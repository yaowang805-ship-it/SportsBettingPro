"""比赛匹配引擎 — 队名匹配+赔率时机匹配

从 bb_vs_pinnacle.py 提取，保持函数签名兼容。
"""
import re
from collections import defaultdict

from src.scrapers.pinnacle_api import get_decimal_price
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

from src.scrapers.bb_data import detect_sport, extract_bb_1x2, TWO_WAY_SPORTS
from src.scrapers.pinnacle_league_map import TEAM_NAME_MAP

def team_name_score(bb_home, bb_away, pin_home, pin_away,
                    pin_candidates: list = None):
    """Score how well BB team names (Chinese) match Pinnacle team names (English).
    Uses TEAM_NAME_MAP + V5 TF-IDF/pinyin matcher for unknown names.
    Returns score 0.0-1.0.
    """
    def lookup_cn(name):
        # V4.4: strip gender suffix (女) and country suffix before lookup
        import re as _re
        clean = _re.sub(r'\s*[（(][^)）]*[)）]', '', name).strip()
        # Try exact match first, then stripped version
        if name in TEAM_NAME_MAP:
            return TEAM_NAME_MAP[name]
        if clean != name and clean in TEAM_NAME_MAP:
            return TEAM_NAME_MAP[clean]
        # V5: TF-IDF+pinyin 智能匹配
        if pin_candidates and _re.search(r'[一-鿿]', name):
            try:
                from src.scrapers.team_matcher import match_team
                matched = match_team(name, pin_candidates, min_score=0.6)
                if matched:
                    return matched
            except ImportError:
                pass
        return name.lower()

    bb_home_en = lookup_cn(bb_home)
    bb_away_en = lookup_cn(bb_away)
    # Normalize: lowercase for comparison
    bb_home_en_l = bb_home_en.lower() if bb_home_en else ""
    bb_away_en_l = bb_away_en.lower() if bb_away_en else ""
    pin_home_l = pin_home.lower()
    pin_away_l = pin_away.lower()

    # V4.4: 纯英文名 (NBA/UFC/PSG) 不需映射即可匹配
    # 如果原始名就是纯 ASCII，说明队名本身就是英文，直接参与匹配
    bb_home_is_ascii = bb_home.isascii() if bb_home else False
    bb_away_is_ascii = bb_away.isascii() if bb_away else False
    bb_home_mapped = bb_home_en != bb_home.lower() or bb_home_is_ascii
    bb_away_mapped = bb_away_en != bb_away.lower() or bb_away_is_ascii

    def name_match(bb_en_l, pin_l):
        """Check if BB English name matches Pinnacle name, case-insensitive."""
        if not bb_en_l or not pin_l:
            return False
        # Exact match
        if bb_en_l == pin_l:
            return True
        # BB name is a substring of Pinnacle name (e.g., "Inverness" in "inverness ct")
        if bb_en_l in pin_l or pin_l in bb_en_l:
            return True
        # Pinnacle name is in BB name (e.g., "east fife" in "east fife...")
        return False

    home_match = name_match(bb_home_en_l, pin_home_l) if bb_home_mapped else False
    away_match = name_match(bb_away_en_l, pin_away_l) if bb_away_mapped else False

    # 检查交叉配对：BB的home可能对应Pinnacle的away（拳击等项目常见主客互换）
    cross_home_match = name_match(bb_home_en_l, pin_away_l) if bb_home_mapped else False
    cross_away_match = name_match(bb_away_en_l, pin_home_l) if bb_away_mapped else False

    if (home_match and away_match) or (cross_home_match and cross_away_match):
        return 1.0
    elif home_match or away_match or cross_home_match or cross_away_match:
        return 0.6
    else:
        return 0.0


def get_pin_ml_sorted(pin_match, sport="football"):
    """Get Pinnacle moneyline odds sorted for the given sport.
    3-way (足球): returns [home, draw, away]; 2-way: returns [home, away].
    """
    min_req = 2 if sport in TWO_WAY_SPORTS else 3
    for ml in pin_match.get("moneyline", []):
        if ml["period"] == 0:
            prices = ml.get("prices_sorted", ml.get("prices", []))
            odds = []
            for p in prices:
                if get_decimal_price(p) and 1.01 <= get_decimal_price(p) <= 51.0:
                    odds.append(get_decimal_price(p))
            if len(odds) >= min_req:
                return odds[:min_req]
    return []


def get_pin_ml_sorted_from_source(ml_source, sport="football"):
    """Get Pinnacle moneyline odds from a market source list (any period).
    3-way: [home, draw, away]; 2-way: [home, away].
    """
    min_req = 2 if sport in TWO_WAY_SPORTS else 3
    for ml in ml_source:
        prices = ml.get("prices_sorted", ml.get("prices", []))
        odds = []
        for p in prices:
            if get_decimal_price(p) and 1.01 <= get_decimal_price(p) <= 51.0:
                odds.append(get_decimal_price(p))
        if len(odds) >= min_req:
            return odds[:min_req]
    return []


def get_pin_spread(pin_match, target_line=None, source=None):
    """Get Pinnacle spread (handicap).

    source: 直接传入 spread 列表（如 ht_spread），不传则用 pin_match["spread"] period=0
    Returns (home_p, away_p, is_alternate) — is_alternate=True 表示用了备用盘口线而非主线
    """
    candidates = []
    entries = source if source is not None else pin_match.get("spread", [])
    for sp in entries:
        if source is None and sp.get("period", 0) != 0:
            continue
        prices = sp.get("prices", [])
        home_p = None
        away_p = None
        for p in prices:
            if p.get("designation") == "home":
                home_p = p
            elif p.get("designation") == "away":
                away_p = p
        if home_p and away_p:
            candidates.append((home_p, away_p))

    if not candidates:
        return None, None, False
    if target_line is None:
        return candidates[0][0], candidates[0][1], False

    # 找线值最接近的候选项，但要求偏差 ≤ 0.5
    # 铁律：BB 有什么线就比什么线，线不对就不比
    best = candidates[0]
    best_diff = abs(target_line - candidates[0][0].get("points", 0))
    for home_p, away_p in candidates[1:]:
        diff = abs(target_line - home_p.get("points", 0))
        if diff <= best_diff:
            best_diff = diff
            best = (home_p, away_p)

    # 偏差超过 0.5 就认为线不匹配，丢弃
    if best_diff > 0.5:
        return None, None, False

    is_alternate = best is not candidates[0]
    return best[0], best[1], is_alternate


def get_pin_total(pin_match, target_line=None, source=None):
    """Get Pinnacle total (over/under).

    source: 直接传入 total 列表（如 ht_total），不传则用 pin_match["total"] period=0
    """
    candidates = []
    entries = source if source is not None else pin_match.get("total", [])
    for t in entries:
        if source is None and t.get("period", 0) != 0:
            continue
        prices = t.get("prices", [])
        over_p = None
        under_p = None
        for p in prices:
            if p.get("designation") == "over":
                over_p = p
            elif p.get("designation") == "under":
                under_p = p
        if over_p and under_p:
            candidates.append((over_p, under_p))

    if not candidates:
        return None, None
    if target_line is None:
        return candidates[0]

    best = candidates[0]
    best_diff = abs(target_line - candidates[0][0].get("points", 0))
    for over_p, under_p in candidates[1:]:
        diff = abs(target_line - over_p.get("points", 0))
        if diff < best_diff:
            best_diff = diff
            best = (over_p, under_p)

    if best_diff > 0.1:
        return None, None
    return best


def _pinyin_match_names(bb_home: str, bb_away: str, pin_list: list) -> tuple:
    """Fallback: pinyin-based fuzzy matching for CJK names (e.g. tennis players).

    Uses pypinyin to convert Chinese names to pinyin, then difflib
    to compare against Pinnacle English names.  Only kicks in when
    the BB names contain CJK characters and TEAM_NAME_MAP has no entry.
    Returns (match, score) or (None, 0.0).
    """
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None, 0.0
    from difflib import SequenceMatcher as _SM

    def _is_cjk(s):
        return any('一' <= c <= '鿿' for c in s)

    def _pinyin_key(name):
        # Strip country suffix "(xxx)" and whitespace
        raw = name.rsplit("(", 1)[0].strip()
        if not _is_cjk(raw):
            return ""
        # Normalize "·" to "." so "戴维德·托托拉" splits into ["戴维德", "托托拉"]
        cleaned = raw.replace("·", ".").replace("‧", ".")
        # Convert each dot-separated syllable to pinyin, strip non-alphanum
        syllables = cleaned.split(".")
        parts = []
        for s in syllables:
            py = "".join(lazy_pinyin(s)).lower()
            py = "".join(c for c in py if c.isalnum())
            parts.append(py)
        return " ".join(parts)

    bb_home_py = _pinyin_key(bb_home)
    bb_away_py = _pinyin_key(bb_away)
    if not bb_home_py and not bb_away_py:
        return None, 0.0

    best_match = None
    best_score = 0.0

    for pin in pin_list:
        pin_home_l = pin.get("home", "").lower()
        pin_away_l = pin.get("away", "").lower()
        scores = []
        if bb_home_py:
            scores.append(_SM(None, bb_home_py, pin_home_l).ratio())
        if bb_away_py:
            scores.append(_SM(None, bb_away_py, pin_away_l).ratio())
        avg = sum(scores) / len(scores) if scores else 0
        if avg > best_score:
            best_score = avg
            best_match = pin

    # V4.5: 0.42→0.35 进一步放宽 (网球匹配率仅3.4%, 漏掉170场)
    # 个人运动无交叉错配风险, 低分匹配+时间窗口+赔率校验三重保护
    if best_score >= 0.35:
        return best_match, best_score
    return None, 0.0


def find_pin_match_by_name(bb_home, bb_away, pin_list):
    """Find Pinnacle match by team name mapping.

    Phase 1: TEAM_NAME_MAP (exact Chinese→English).
    Phase 2: pinyin-based fuzzy matching (for tennis etc.).
    Returns (match, score) or (None, 0).
    """
    # V4.4: strip gender/country suffix before lookup
    import re as _re2
    bb_home_clean = _re2.sub(r'\s*[（(][^)）]*[)）]', '', bb_home).strip()
    bb_away_clean = _re2.sub(r'\s*[（(][^)）]*[)）]', '', bb_away).strip()
    bb_home_en = (TEAM_NAME_MAP.get(bb_home) or TEAM_NAME_MAP.get(bb_home_clean, "")).lower()
    bb_away_en = (TEAM_NAME_MAP.get(bb_away) or TEAM_NAME_MAP.get(bb_away_clean, "")).lower()

    if not bb_home_en and not bb_away_en:
        return _pinyin_match_names(bb_home, bb_away, pin_list)

    best_match = None
    best_score = 0.0

    for pin in pin_list:
        pin_home_l = pin.get("home", "").lower()
        pin_away_l = pin.get("away", "").lower()

        score_parts = []

        if bb_home_en:
            if bb_home_en == pin_home_l:
                score_parts.append(1.0)
            elif bb_home_en in pin_home_l or pin_home_l in bb_home_en:
                score_parts.append(0.9)
            else:
                score_parts.append(0.0)

        if bb_away_en:
            if bb_away_en == pin_away_l:
                score_parts.append(1.0)
            elif bb_away_en in pin_away_l or pin_away_l in bb_away_en:
                score_parts.append(0.9)
            else:
                score_parts.append(0.0)

        avg = sum(score_parts) / len(score_parts) if score_parts else 0

        if avg > best_score:
            best_score = avg
            best_match = pin

    if best_score >= 0.8:
        return best_match, best_score
    return None, 0.0


def _bb_to_epoch(bb_match):
    """Convert BB match time to epoch seconds (UTC).

    支持两种格式:
    1. API 数据: bt 字段 (Unix 毫秒时间戳)
    2. DOM 提取: period("07/15") + time("03:00") (GMT+8)
    """
    # API 数据: bt 是毫秒时间戳
    bt = bb_match.get("bt")
    if bt:
        try:
            return int(int(bt) / 1000)
        except (ValueError, TypeError):
            pass

    # DOM 提取: period + time (GMT+8)
    period = bb_match.get("period", "")
    btime = bb_match.get("time", "")
    if not period or not btime:
        return None
    try:
        dt_str = f"2026-{period[:2]}-{period[3:5]}T{btime[:2]}:{btime[3:5]}:00"
        dt_naive = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        dt_utc = dt_naive - timedelta(hours=8)
        return int(dt_utc.replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, IndexError):
        return None


def _pin_to_epoch(pin_match):
    """Convert Pinnacle matchup start_time (UTC) to epoch seconds."""
    start = pin_match.get("start_time", "")
    if not start or "T" not in start:
        return None
    try:
        # Handle "2026-07-14T19:00:00Z" by replacing Z with +00:00
        start_clean = start.replace("Z", "+00:00")
        dt = datetime.fromisoformat(start_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, IndexError):
        return None


def _odds_similarity(bb_1x2, pin_1x2, min_odds=3, sport="football"):
    """Compute odds similarity score (0-1)."""
    if len(bb_1x2) < min_odds or len(pin_1x2) < min_odds:
        return 0.0
    ratios = []
    for i in range(min_odds):
        bb_o = bb_1x2[i]
        pin_o = pin_1x2[i]
        if pin_o > 0:
            ratios.append(min(bb_o, pin_o) / max(bb_o, pin_o))
        else:
            ratios.append(0)

    avg_ratio = sum(ratios) / len(ratios) if ratios else 0
    if len(ratios) >= min_odds and max(ratios) - min(ratios) > 0.15:
        # 网球赔率经常严重倾斜（如 1.10 vs 6.50），ratio spread 必然 > 0.15，
        # 这个 ×0.8 惩罚对网球不合理，会导致 valid 匹配 score 被压低。
        if sport != "tennis":
            avg_ratio *= 0.8
    return avg_ratio


def _make_bb_key(bb):
    return f"{bb.get('home','')}|{bb.get('away','')}|{bb.get('league','')}"


def _pinyin_name_similarity(cn_name, en_name):
    """Calculate similarity between Chinese transliterated name and English name via pinyin.

    Used for individual sports (boxing/MMA/tennis) where BB has Chinese names
    and Pinnacle has English names. Returns 0.0-1.0.
    """
    import re
    from difflib import SequenceMatcher
    try:
        from pypinyin import pinyin, Style
    except ImportError:
        return 0.0

    if not cn_name or not en_name:
        return 0.0
    # Only compute for non-ASCII names (Chinese characters)
    if cn_name.isascii():
        return 0.0

    try:
        py_parts = pinyin(cn_name, style=Style.NORMAL)
        py_str = ''.join(p[0] for p in py_parts)
    except Exception:
        return 0.0

    # Normalize
    py_str = re.sub(r'[^a-z]', '', py_str.lower())
    en_str = re.sub(r'[^a-z]', '', en_name.lower())

    if not py_str or not en_str:
        return 0.0

    # Direct string similarity
    direct = SequenceMatcher(None, py_str, en_str).ratio()

    # Syllable-level matching
    py_syllables = [p[0] for p in py_parts]
    en_parts = en_name.lower().split()
    syl_scores = []
    for py_syl in py_syllables:
        best = 0.0
        for en_part in en_parts:
            s = SequenceMatcher(None, py_syl, en_part).ratio()
            if s > best:
                best = s
        syl_scores.append(best)
    syl_avg = sum(syl_scores) / len(syl_scores) if syl_scores else 0.0

    return max(direct, syl_avg * 0.9)


def _compute_combined_score(bb, bb_1x2, bb_epoch, pin, pin_ml, sport="football"):
    """Combined score = odds_similarity × time_factor × name_boost (0-1)."""
    min_odds = 2 if sport in TWO_WAY_SPORTS else 3
    odds_score = _odds_similarity(bb_1x2, pin_ml, min_odds, sport)
    time_factor = 1.0
    if bb_epoch:
        pin_epoch = _pin_to_epoch(pin)
        if pin_epoch is not None:
            diff = abs(bb_epoch - pin_epoch)
            # 网球/拳击/MMA: 赛程灵活，时间窗口放宽
            if sport in ("tennis", "boxing", "mma"):
                if diff < 1800: time_factor = 1.0
                elif diff < 7200: time_factor = 0.95
                elif diff < 14400: time_factor = 0.85
                elif diff < 21600: time_factor = 0.60  # 4-6h
                else: time_factor = 0.30
            else:
                if diff < 600: time_factor = 1.0
                elif diff < 1800: time_factor = 0.97
                elif diff < 3600: time_factor = 0.93
                elif diff < 7200: time_factor = 0.88
                elif diff < 14400: time_factor = 0.50
                else: time_factor = 0.20

    # V4.5: 个人运动中英文名拼音相似度加成 — 解决中文名vs英文名无法匹配的问题
    name_boost = 1.0
    if sport in ("boxing", "mma", "tennis"):
        bb_home = bb.get("home", "").strip()
        bb_away = bb.get("away", "").strip()
        pin_home = pin.get("home", "").strip()
        pin_away = pin.get("away", "").strip()
        if bb_home and bb_away and pin_home and pin_away:
            try:
                ns = _pinyin_name_similarity
                # 尝试两种方向 (home↔home + away↔away 或 home↔away + away↔home)
                direct = (ns(bb_home, pin_home) + ns(bb_away, pin_away)) / 2
                crossed = (ns(bb_home, pin_away) + ns(bb_away, pin_home)) / 2
                best_name_score = max(direct, crossed)
                # 名字匹配加成: 0.5分 → 1.5倍; 0.3分 → 1.15倍
                name_boost = 1.0 + best_name_score * 0.5
            except Exception:
                pass

    return odds_score * time_factor * name_boost


def find_matches_by_odds(bb_matches, pin_matches_by_league):
    """Match BB体育 games to Pinnacle games.

    Two-phase:
    1. Name-based (preferred) — uses TEAM_NAME_MAP
    2. Global greedy: build all (BB, Pin) pairs per league, sort by
       combined (time × odds) score, greedily assign.  Prevents the
       common swap-error when two matches share a kickoff time.
    """
    name_matched = []
    matched = []
    used_pin_ids = set()
    used_bb_keys = set()

    # Pre-compute BB data（检测运动类型）
    bb_data = {}
    for bb in bb_matches:
        sport = detect_sport(bb)
        bb_1x2, valid = extract_bb_1x2(bb, sport)
        min_odds = 2 if sport in TWO_WAY_SPORTS else 3
        if not valid or len(bb_1x2) < min_odds:
            bb_1x2 = []  # No ML market — still include for HC/OU comparison
        bb_data[_make_bb_key(bb)] = {
            "match": bb, "bb_1x2": bb_1x2, "epoch": _bb_to_epoch(bb),
            "sport": sport,
        }

    # Phase 1: Name-based — for each Pin match, find best BB match
    # Group BB data by league
    bb_by_league = {}
    for bb_key, bd in bb_data.items():
        league = bd["match"].get("league", "")
        bb_by_league.setdefault(league, []).append((bb_key, bd))

    for bb_league, bb_entries in bb_by_league.items():
        pin_list = pin_matches_by_league.get(bb_league, [])
        # For each Pin match, find the best available BB match
        for pin in pin_list:
            pin_id = pin.get("matchup_id", id(pin))
            if pin_id in used_pin_ids:
                continue
            best_bb_key = None
            best_bd = None
            best_name_score = 0.0
            for bb_key, bd in bb_entries:
                if bb_key in used_bb_keys:
                    continue
                _, name_score = find_pin_match_by_name(
                    bd["match"].get("home", ""), bd["match"].get("away", ""), [pin],
                )
                if name_score > best_name_score:
                    best_name_score = name_score
                    best_bb_key = bb_key
                    best_bd = bd
            # V4.5: 运动特定门槛 — 网球/拳击/MMA 放宽至0.38 (个人运动无团队交叉错配)
            sport = best_bd["sport"] if best_bd else ""
            min_name_score = 0.38 if sport in ("tennis", "boxing", "mma") else 0.50
            if not best_bd or best_name_score < min_name_score:
                continue
            # 硬时间窗口：同队名但开赛时间差 >4h → 不同比赛（防双赛日混淆）
            bb_epoch = best_bd["epoch"]
            pin_epoch = _pin_to_epoch(pin)
            if bb_epoch is not None and pin_epoch is not None:
                if abs(bb_epoch - pin_epoch) > 14400:
                    continue
            bb_ml = best_bd.get("bb_1x2", [])
            min_odds = 2 if sport in TWO_WAY_SPORTS else 3
            pin_ml = []
            if len(bb_ml) >= min_odds:
                pin_ml = get_pin_ml_sorted(pin, sport)
                if len(pin_ml) < min_odds:
                    continue
            # BB has no ML → still match for HC/OU comparison
            used_pin_ids.add(pin_id)
            used_bb_keys.add(best_bb_key)
            name_matched.append({
                "bb": best_bd["match"], "pin": pin, "league": bb_league,
                "match_score": 1.0, "team_score": best_name_score,
                "match_type": "name",
                "bb_1x2": bb_ml, "pin_1x2": pin_ml,
                "sport": sport,
            })

    # Phase 2: Global greedy per-league
    # V4.5: 网球禁用 Phase 2 (时间+赔率匹配在同赛事同时段多场次中错误率高)
    for bb_league, pin_list in pin_matches_by_league.items():
        pairs = []
        for bb_key, bd in bb_data.items():
            if bb_key in used_bb_keys:
                continue
            if bd["match"].get("league", "") != bb_league:
                continue
            sport = bd["sport"]
            if sport == "tennis":
                # V4.5: 重新启用 Phase 2 — 拼音相似度已集成, 提高门槛防错配
                pass
            min_odds = 2 if sport in TWO_WAY_SPORTS else 3
            for pin in pin_list:
                pin_id = pin.get("matchup_id", id(pin))
                if pin_id in used_pin_ids:
                    continue
                pin_ml = get_pin_ml_sorted(pin, sport)
                if len(pin_ml) < min_odds:
                    continue
                combined = _compute_combined_score(
                    bd["match"], bd["bb_1x2"], bd["epoch"], pin, pin_ml, sport,
                )
                # 网球的时间匹配：降低门限以覆盖更多 ITF 赛事
                # ITF 赔率差异大 + 时间经常微调，纯时间和赔率匹配很难高分
                # 已从 0.70 → 0.55 → 0.45（进一步放松以匹配更多网球比赛）
                # 放宽阈值: 非足球运动依赖Phase2, 更低门槛匹配更多比赛供队名学习
                min_threshold = 0.45 if sport in ("tennis", "boxing", "mma") else 0.50
                if combined >= min_threshold:
                    pairs.append((combined, bb_key, bd["match"], pin,
                                  bd["bb_1x2"], pin_ml, pin_id, sport))
                elif sport == "tennis" and combined > 0.4 and not bd['match'].get('league','').startswith('世界'):
                    bb_t = bd['match'].get('bt','')
                    pin_t = pin.get('start_time','')
                    bb_odds = bd.get('bb_1x2',[])
                    pin_odds_list = pin_ml
                    print(f"  [网球 Phase 2] {bd['match'].get('home','')} vs {bd['match'].get('away','')}")
                    print(f"    combined={combined:.3f} (阈值={min_threshold})")
                    print(f"    BB时间={bb_t} Pin时间={pin_t}")
                    print(f"    BB赔率={bb_odds} Pin赔率={pin_odds_list}")

        pairs.sort(key=lambda x: -x[0])
        for combined, bb_key, bb, pin, bb_1x2, pin_ml, pin_id, sport in pairs:
            if bb_key in used_bb_keys or pin_id in used_pin_ids:
                continue
            # Phase 2 队名校验
            if sport == "tennis":
                tn_score = (_pinyin_name_similarity(bb.get("home", ""), pin.get("home", "")) +
                            _pinyin_name_similarity(bb.get("away", ""), pin.get("away", ""))) / 2
                if tn_score < 0.35:
                    continue
            else:
                # V5: 获取当前联赛的 Pinnacle 候选队名列表
                _pin_candidates = set()
                _bb_league = bb.get("league", "")
                if _bb_league in pin_matches_by_league:
                    for _pm in pin_matches_by_league[_bb_league]:
                        _pin_candidates.add(_pm.get("home", ""))
                        _pin_candidates.add(_pm.get("away", ""))
                tn_score = team_name_score(
                    bb.get("home", ""), bb.get("away", ""),
                    pin.get("home", ""), pin.get("away", ""),
                    pin_candidates=list(_pin_candidates) if _pin_candidates else None
                )
                if tn_score < 0.3 and _HAS_RAPIDFUZZ:
                    fs, _ = fuzzy_match_teams(
                        bb.get("home", ""), bb.get("away", ""),
                        pin.get("home", ""), pin.get("away", ""), threshold=75
                    )
                    tn_score = max(tn_score, fs)
                if sport not in ("tennis", "boxing", "mma"):
                    if tn_score < 0.3 and combined < 0.90:
                        continue
            used_bb_keys.add(bb_key)
            used_pin_ids.add(pin_id)
            matched.append({
                "bb": bb, "pin": pin, "league": bb_league,
                "match_score": round(combined, 3), "match_type": "time",
                "bb_1x2": bb_1x2, "pin_1x2": pin_ml, "sport": sport,
            })

    return name_matched + matched


# =====================================================================
# V4.5 业界级映射增强
# =====================================================================

# ── 1. rapidfuzz 模糊匹配 ──
try:
    from rapidfuzz import fuzz, process
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False


def _normalize_team(name: str) -> str:
    """标准化队名: 去国家后缀、去括号、去空格、小写."""
    name = re.sub(r'\s*[（(][^)）]*[)）]', '', (name or ''))
    name = re.sub(r'\s+', ' ', name).strip().lower()
    return name


def _generate_aliases(name: str) -> list:
    """自动生成队名别名.
    曼联 → [曼联, Man United, Manchester United, Man Utd]
    """
    name = (name or '').strip()
    aliases = [name]
    # 取最后一部分 (俱乐部名)
    parts = name.split()
    if len(parts) >= 2:
        aliases.append(parts[-1])  # e.g. "Manchester United" → "United"
    # 去掉常见后缀
    for suffix in ['FC', 'SC', 'CF', 'United', 'City', 'Club']:
        if name.endswith(' ' + suffix):
            aliases.append(name[:-len(suffix)-1])
    return aliases


def fuzzy_match_teams(bb_home, bb_away, pin_home, pin_away, threshold=85):
    """使用 rapidfuzz 模糊匹配队名. 返回 (score, is_swapped)."""
    if not _HAS_RAPIDFUZZ:
        return 0.0, False

    bb_h = _normalize_team(bb_home)
    bb_a = _normalize_team(bb_away)
    pin_h = _normalize_team(pin_home)
    pin_a = _normalize_team(pin_away)

    if not bb_h or not bb_a or not pin_h or not pin_a:
        return 0.0, False

    # 直向匹配
    direct_h = max(fuzz.ratio(bb_h, pin_h), fuzz.partial_ratio(bb_h, pin_h),
                   fuzz.token_sort_ratio(bb_h, pin_h))
    direct_a = max(fuzz.ratio(bb_a, pin_a), fuzz.partial_ratio(bb_a, pin_a),
                   fuzz.token_sort_ratio(bb_a, pin_a))
    direct = (direct_h + direct_a) / 200  # 标准化到 0-1

    # 交叉匹配 (home↔away swap)
    cross_h = max(fuzz.ratio(bb_h, pin_a), fuzz.partial_ratio(bb_h, pin_a),
                  fuzz.token_sort_ratio(bb_h, pin_a))
    cross_a = max(fuzz.ratio(bb_a, pin_h), fuzz.partial_ratio(bb_a, pin_h),
                  fuzz.token_sort_ratio(bb_a, pin_h))
    cross = (cross_h + cross_a) / 200

    if direct >= cross and direct >= threshold/100:
        return direct, False
    elif cross >= threshold/100:
        return cross, True
    return 0.0, False


# ── 2. Union-Find 同一比赛去重 ──
class UnionFind:
    """并查集 — 将等价比赛聚类."""
    def __init__(self):
        self.parent = {}
        self.rank = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx == ry: return
        if self.rank[rx] < self.rank[ry]:
            self.parent[rx] = ry
        elif self.rank[rx] > self.rank[ry]:
            self.parent[ry] = rx
        else:
            self.parent[ry] = rx
            self.rank[rx] += 1


def dedup_cross_league(matched_entries):
    """Union-Find 去重: 同一Pinnacle比赛被不同BB联赛匹配时, 只保留最高分."""
    if len(matched_entries) <= 1:
        return matched_entries

    uf = UnionFind()
    # 用 Pin matchup_id 作为聚类 key
    pin_to_entries = defaultdict(list)
    for i, entry in enumerate(matched_entries):
        pin = entry.get("pin", {})
        pin_id = pin.get("matchup_id", pin.get("id", id(pin)))
        pin_to_entries[pin_id].append(i)

    # 同一 Pin ID → 只保留最高分
    kept = []
    seen_pins = set()
    for entry in matched_entries:
        pin = entry.get("pin", {})
        pin_id = pin.get("matchup_id", pin.get("id", id(pin)))
        if pin_id in seen_pins:
            # 找此 pin 已保留的条目, 保留更高分
            for i, e in enumerate(kept):
                e_pin = e.get("pin", {})
                e_pid = e_pin.get("matchup_id", e_pin.get("id", id(e_pin)))
                if e_pid == pin_id:
                    if entry.get("match_score", 0) > e.get("match_score", 0):
                        kept[i] = entry
                    break
        else:
            seen_pins.add(pin_id)
            kept.append(entry)

    return kept


# ── 3. 日期无关匹配 (篮球/网球延期) ──
def try_date_independent_match(bb_matches, pin_matches_by_league, sport="tennis"):
    """当时间窗口匹配失败时, 尝试纯赔率+名模糊匹配.

    适用场景: 比赛延期、时区错误、赛程调整.
    仅用于个人运动 (tennis/boxing/mma), 团队运动风险太高.
    """
    if sport not in ("tennis", "boxing", "mma"):
        return []

    results = []
    from src.scrapers.pinnacle_league_map import find_pinnacle_league_ids

    for bb in bb_matches:
        bb_league = bb.get("league", "")
        if bb_league not in pin_matches_by_league:
            continue

        bb_1x2, valid = extract_bb_1x2(bb, sport)
        if not valid or len(bb_1x2) < 2:
            continue

        best_score = 0.45  # 更高的门槛
        best_pin = None

        for pin in pin_matches_by_league.get(bb_league, []):
            pin_ml = get_pin_ml_sorted(pin, sport)
            if len(pin_ml) < 2: continue

            # 纯赔率相似度 (无时间因素)
            odds_score = _odds_similarity(bb_1x2, pin_ml, 2, sport)
            # 名字模糊匹配
            name_score, _ = fuzzy_match_teams(
                bb.get("home", ""), bb.get("away", ""),
                pin.get("home", ""), pin.get("away", ""), threshold=70
            )
            # 综合: odds主导 + name加成
            combined = odds_score * 0.7 + name_score * 0.3
            # V4.5: 拼音必须≥0.3 (过滤完全错配)
            if name_score >= 0.3 and combined > best_score:
                best_score = combined
                best_pin = pin

        if best_pin and best_score >= 0.55:
            results.append({
                "bb": bb, "pin": best_pin, "league": bb_league,
                "match_score": round(best_score, 3), "match_type": "date_indep",
                "bb_1x2": bb_1x2, "pin_1x2": get_pin_ml_sorted(best_pin, sport),
                "sport": sport,
            })

    return results

