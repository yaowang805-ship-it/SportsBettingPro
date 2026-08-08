"""BB体育 数据加载和盘口提取

从 bb_vs_pinnacle.py 提取，保持函数签名兼容。
"""
import json, time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

from config.settings import DATA_DIR

# 运动 ID 映射
# 注: 乒乓球(32)/羽毛球(1)/排球(34) 在 Pinnacle 无对应数据
# 这些运动仅在 BB 端拉取，不会参与 Pinnacle 对比
SPORT_IDS = {29: "足球", 4: "篮球", 33: "网球", 3: "棒球", 15: "美式足球",
             32: "乒乓球", 6: "拳击", 22: "MMA", 1: "羽毛球", 19: "冰球", 34: "排球"}
# Pinnacle 无覆盖的运动 (sport_id 在 league_structure 中不存在或无联赛)
PINNACLE_MISSING_SPORTS = {"pingpong", "badminton", "volleyball"}
TWO_WAY_SPORTS = {"basketball", "tennis", "baseball", "american_football",
                  "pingpong", "boxing", "mma", "badminton", "volleyball"}

# BB体育联赛关键词 → 运动类型
BB_SPORT_KEYWORDS = {
    "欧洲冠军联赛": "football", "欧洲足联欧洲联赛": "football",
    "超级联赛": "football", "西班牙甲级联赛": "football",
    "德国甲级联赛": "football", "世界杯": "football",
    "球会友谊赛": "football", "苏格兰联赛杯": "football",
    "芬兰": "football", "瑞典超级联赛": "football",
    "超级挪威联赛": "football", "乌拉圭甲级联赛": "football",
    "哈萨克斯坦超级联赛": "football", "巴拉圭": "football",
    "俄罗斯甲级联赛": "football", "澳门甲级联赛": "football",
    "白俄罗斯超级联赛": "football", "冰岛甲级联赛": "football",
    "爱沙尼亚甲级联赛": "football",
    "欧足联欧洲协会联赛": "football",
    "澳大利亚杯": "football",
    "厄瓜多尔甲级联赛": "football",
    "NBA": "basketball", "美国职业篮球": "basketball",
    "欧洲篮球联赛": "basketball", "CBA": "basketball",
    "韩国篮球": "basketball", "日本篮球": "basketball",
    "菲律宾篮球": "basketball", "篮球": "basketball",
    "年度最佳": "american_football", "美式足球": "american_football",
    "NFL": "american_football",
    "ATP": "tennis", "WTA": "tennis", "ITF": "tennis", "网球": "tennis",
    "MLB": "baseball", "日本职业棒球": "baseball",
    "韩国棒球": "baseball", "中华职业棒球": "baseball",
    "棒球": "baseball",
    "乒乓球": "pingpong", "WTT": "pingpong", "TT 精英": "pingpong",
    "捷克职业联赛": "pingpong",
    "拳击": "boxing",
    "UFC": "mma", "MMA": "mma",
    "羽毛球": "badminton", "公开赛": "badminton",
    "冰球": "ice_hockey",
    "排球": "volleyball", "FIVB": "volleyball",
}

# 各运动的市场标签
MARKET_LABELS = {
    "football":  {"ml": ["主胜","和局","客胜"], "hc_home":"让球主胜", "hc_away":"让球客胜", "over":"大球", "under":"小球"},
    "basketball": {"ml": ["主胜","客胜"], "hc_home":"让分主胜", "hc_away":"让分客胜", "over":"大分", "under":"小分"},
    "tennis":     {"ml": ["主胜","客胜"], "hc_home":"让盘主胜", "hc_away":"让盘客胜", "over":"大分", "under":"小分"},
    "baseball":   {"ml": ["主胜","客胜"], "hc_home":"让分主胜", "hc_away":"让分客胜", "over":"大分", "under":"小分"},
    "american_football": {"ml": ["主胜","客胜"], "hc_home":"让分主胜", "hc_away":"让分客胜", "over":"大分", "under":"小分"},
    "pingpong":  {"ml": ["主胜","客胜"], "hc_home":"让分主胜", "hc_away":"让分客胜", "over":"大分", "under":"小分"},
    "boxing":    {"ml": ["主胜","客胜"], "over":"大分", "under":"小分"},
    "mma":       {"ml": ["主胜","客胜"], "over":"大分", "under":"小分"},
    "badminton": {"ml": ["主胜","客胜"], "hc_home":"让局主胜", "hc_away":"让局客胜", "over":"大分", "under":"小分"},
    "ice_hockey": {"ml": ["主胜","和局","客胜"], "hc_home":"让球主胜", "hc_away":"让球客胜", "over":"大分", "under":"小分"},
    "volleyball": {"ml": ["主胜","客胜"], "hc_home":"让分主胜", "hc_away":"让分客胜", "over":"大分", "under":"小分"},
}


def detect_sport(bb_match):
    """从 BB 比赛数据中检测运动类型。"""
    sport = bb_match.get("sport", "")
    if sport:
        if sport == "soccer":
            return "football"
        known_sports = ("football", "basketball", "tennis", "baseball",
                        "american_football", "pingpong", "boxing", "mma",
                        "badminton", "ice_hockey", "volleyball")
        if sport in known_sports:
            return sport
    league = bb_match.get("league", "")
    for kw, s in BB_SPORT_KEYWORDS.items():
        if kw in league:
            return s
    return "football"


def load_bb_odds(path=None):
    if path is None:
        path = DATA_DIR / "bb_odds_extracted.json"
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    age_hours = (time.time() - mtime) / 3600
    if age_hours > 2 and path == DATA_DIR / "bb_odds_extracted.json":
        print(f"  ⚠ BB 数据 {age_hours:.1f}小时前抓取，可能已过期")
    data = json.loads(path.read_text())
    matches = data.get("matches", [])
    seen = set()
    unique = []
    for m in matches:
        mid = m.get("id")
        key = (str(mid),) if mid else (m.get("home", ""), m.get("away", ""), m.get("league", ""))
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def extract_bb_1x2(bb_match, sport="football"):
    """Extract 1X2 odds from BB match.

    3-way (足球): odds[0:3] = [home, draw, away]
    2-way (篮球/网球/棒球): odds[0:2] = [home, away]

    Returns (odds_list, is_valid).
    """
    n = 3 if sport not in TWO_WAY_SPORTS else 2
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        if "ml" in odds_ft:
            ft_ml = odds_ft["ml"]
            if isinstance(ft_ml, list) and len(ft_ml) >= n:
                bb_1x2 = [v for v in ft_ml if 1.01 <= v <= 51.0]
                if len(bb_1x2) >= n:
                    return bb_1x2, True
            return [], False
        else:
            return [], False
    odds = bb_match.get("odds_values", [])
    full_text = bb_match.get("full_text", "")
    if len(odds) < n:
        return [], False
    if sport not in TWO_WAY_SPORTS:
        ft_compact = " ".join(full_text.split())
        he_idx = ft_compact.find("和")
        if he_idx >= 0:
            after_he = ft_compact[he_idx:he_idx+30]
            if "-" in after_he.split()[1:4]:
                return [], False
    bb_1x2 = []
    for o in odds[:n]:
        try:
            val = float(o)
            if 1.01 <= val <= 51.0:
                bb_1x2.append(val)
        except (ValueError, TypeError):
            pass
    if len(bb_1x2) < n:
        return [], False
    return bb_1x2, True


def parse_asian_line(line_str):
    """Convert Chinese Asian handicap notation to decimal line."""
    if not line_str:
        return None
    s = line_str.strip()
    if s.startswith('大') or s.startswith('小'):
        try:
            return float(s[1:])
        except ValueError:
            return None
    sign = 1.0
    rest = s
    if s.startswith('+'):
        sign = 1.0
        rest = s[1:]
    elif s.startswith('-'):
        sign = -1.0
        rest = s[1:]
    if '/' in rest:
        parts = rest.split('/')
        try:
            low = float(parts[0])
            high = float(parts[1])
            return sign * (low + high) / 2.0
        except (ValueError, IndexError):
            return None
    try:
        return sign * float(rest)
    except ValueError:
        return None


def extract_bb_handicap(bb_match, sport="football"):
    """Extract handicap odds and line from BB match."""
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        ft_hc = odds_ft.get("handicap")
        if isinstance(ft_hc, dict) and ft_hc.get("home_odds") and ft_hc.get("away_odds"):
            home_line = ft_hc.get("home_line")
            away_line = ft_hc.get("away_line")
            if home_line is not None or away_line is not None:
                return {
                    "home_odds": ft_hc["home_odds"],
                    "away_odds": ft_hc["away_odds"],
                    "home_line": home_line,
                    "away_line": away_line,
                    "home_line_str": ft_hc.get("home_line_str", str(home_line) if home_line is not None else ""),
                    "away_line_str": ft_hc.get("away_line_str", str(away_line) if away_line is not None else ""),
                }
    odds = bb_match.get("odds_values", [])
    full_text = bb_match.get("full_text", "")
    n = 3 if sport not in TWO_WAY_SPORTS else 2
    if len(odds) < n + 2:
        return None
    if sport not in TWO_WAY_SPORTS:
        return _extract_football_hc_positional(odds[n:], full_text)
    else:
        return _extract_2way_hc_positional(odds[n:], full_text)


def _extract_football_hc_positional(odds_slice, full_text):
    """足球让球盘：位置解析（legacy fallback）。"""
    hc_odds = [v for v in odds_slice if 1.01 <= v <= 51.0]
    if len(hc_odds) < 2:
        return None
    hc_lines = []
    ft_parts = full_text.split()
    for i, tok in enumerate(ft_parts):
        if tok in ("让球主胜", "让球客胜"):
            for j in range(i+1, min(i+4, len(ft_parts))):
                line = parse_asian_line(ft_parts[j])
                if line is not None:
                    hc_lines.append(line)
                    break
    if len(hc_lines) >= 2:
        hl, al = hc_lines[0], -hc_lines[1]
        return {"home_odds": hc_odds[0], "away_odds": hc_odds[1],
                "home_line": hl, "away_line": al,
                "home_line_str": str(hl), "away_line_str": str(al)}
    elif len(hc_lines) == 1:
        hl, al = hc_lines[0], -hc_lines[0]
        return {"home_odds": hc_odds[0], "away_odds": hc_odds[1],
                "home_line": hl, "away_line": al,
                "home_line_str": str(hl), "away_line_str": str(al)}
    is_draw = "和" in " ".join(ft_parts[:10])
    if not hc_lines and not is_draw:
        hl, al = -0.25, 0.25
        return {"home_odds": hc_odds[0], "away_odds": hc_odds[1],
                "home_line": hl, "away_line": al,
                "home_line_str": str(hl), "away_line_str": str(al)}
    return None


def _extract_2way_hc_positional(odds_slice, full_text):
    """2-way 让球盘：位置解析（legacy fallback）。"""
    hc_odds = [v for v in odds_slice if 1.01 <= v <= 51.0]
    if len(hc_odds) < 2:
        return None
    lines = []
    for tok in full_text.split():
        if tok in ("大", "小"):
            continue
        line = parse_asian_line(tok)
        if line is not None:
            lines.append(line)
    if lines:
        line_val = lines[0]
        hl, al = -line_val, line_val
        return {"home_odds": hc_odds[0], "away_odds": hc_odds[1],
                "home_line": hl, "away_line": al,
                "home_line_str": str(hl), "away_line_str": str(al)}
    return {"home_odds": hc_odds[0], "away_odds": hc_odds[1],
            "home_line_str": "", "away_line_str": ""}


def extract_bb_ou(bb_match, sport="football"):
    """Extract over/under odds and line from BB match."""
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        # V4.5: BB API uses "total" key, not "over_under"
        ft_ou = odds_ft.get("total") or odds_ft.get("over_under")
        if isinstance(ft_ou, dict) and ft_ou.get("over_odds") and ft_ou.get("under_odds"):
            return {
                "over_odds": ft_ou["over_odds"],
                "under_odds": ft_ou["under_odds"],
                "line": ft_ou.get("line"),
            }
    # HT OU extraction
    odds_ht = bb_match.get("odds_ht", {})
    if isinstance(odds_ht, dict):
        ht_ou = odds_ht.get("total") or odds_ht.get("over_under")
        if isinstance(ht_ou, dict) and ht_ou.get("over_odds") and ht_ou.get("under_odds"):
            return {
                "over_odds": ht_ou["over_odds"],
                "under_odds": ht_ou["under_odds"],
                "line": ht_ou.get("line"),
            }
    # Legacy fallback
    odds = bb_match.get("odds_values", [])
    full_text = bb_match.get("full_text", "")
    n = 3 if sport not in TWO_WAY_SPORTS else 2
    hc_count = 2
    ou_start = n + hc_count
    if len(odds) < ou_start + 2:
        return None
    ou_odds = odds[ou_start:ou_start+2]
    ou_odds = [v for v in ou_odds if 1.01 <= v <= 51.0]
    if len(ou_odds) < 2:
        return None
    p = full_text.split()
    ou_line = None
    for tok in p:
        line = parse_asian_line(tok)
        if line is not None:
            ou_line = abs(line)
    return {"over_odds": ou_odds[0], "under_odds": ou_odds[1], "line": ou_line}


def extract_bb_btts(bb_match):
    """Extract BTTS odds from BB match."""
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        btts = odds_ft.get("btts", {})
        if isinstance(btts, dict):
            yes = btts.get("yes_odds")
            no = btts.get("no_odds")
            if yes and no and yes > 1 and no > 1:
                return yes, no
    return None, None


def extract_bb_oe(bb_match):
    """Extract Odd/Even odds from BB match."""
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        oe = odds_ft.get("oe", {})
        if isinstance(oe, dict):
            odd = oe.get("odd_odds")
            even = oe.get("even_odds")
            if odd and even and odd > 1 and even > 1:
                return odd, even
    return None, None


def extract_bb_htft(bb_match):
    """Extract HT/FT odds from BB match."""
    odds_ft = bb_match.get("odds_ft", {})
    if isinstance(odds_ft, dict):
        htft = odds_ft.get("htft", {})
        if isinstance(htft, dict) and len(htft) >= 9:
            return htft
    return None
