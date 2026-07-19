"""BB体育 API 调用器 —— 直接 HTTP 调用 api.447a9.com

替代 Chrome DOM 提取器 (bb_extract_odds.py)，直接从 API 获取：
- 比赛列表（含球队名、联赛、开赛时间）
- 完整盘口数据：独赢(1X2)、让球(所有线)、大小(所有线)
- 附加市场：平局退款(DNB)、双重机会(DC)

输出格式兼容 bb_vs_pinnacle.py 的 odds_ft / odds_ht 结构。

用法:
    python3 -m src.scrapers.bb_api_fetcher [--all-sports]
"""
import json, time, sys, os, re
import concurrent.futures
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

# API 端点（BB体育）
API_BASE = "https://api.447a9.com"

# 多平台配置（BB体育 + FB体育，DB体育待定-Protobuf API）
PLATFORMS = {
    "BB": {
        "api_base": "https://api.447a9.com",
        "auth_header": "h5-token",
        "label": "BB体育",
        "label_short": "BB",
    },
    "FB": {
        "api_base": "https://api.5c4r3.com",
        "auth_header": "user-token",
        "label": "FB体育",
        "label_short": "FB",
    },
}

# HTTP session（复用连接，避免 Python 3.14 urllib IncompleteRead bug）
_SESSION = requests.Session()
_SESSION.trust_env = False  # 避免自动读取系统代理

# 运动配置
SPORTS = [
    (1, "football", "足球"),
    (3, "basketball", "篮球"),
    (5, "tennis", "网球"),
    (7, "baseball", "棒球"),
    (6, "american_football", "美式足球"),
]

# Market type 映射 (按运动)
MARKET_TYPES = {
    1: {  # 足球
        "ml": 1005, "hc": 1000, "ou": 1007, "dnb": 1089, "dc": 1012, "btts": 1027,
    },
    3: {  # 篮球 (3004=独赢, 3003=大小, 3002=让分)
        "ml": 3004, "ou": 3003, "hc": 3002,
    },
    5: {  # 网球 (5001=独赢, 5004=让盘, 5003=大小/总局数, 5012=下一盘)
        "ml": 5001, "hc": 5004, "ou": 5003,
    },
    7: {  # 棒球
        "ml": 7003, "hc": 7001, "ou": 7002,
    },
    6: {  # 美式足球 (NFL/大学/室内)
        "ml": 6001, "hc": 6002, "ou": 6003,
    },
}

# 各运动的市场显示中文名
MARKET_LABELS = {
    "football":  {"ml_name": "独赢", "hc_name": "让球", "ou_name": "大小",
                  "dnb_name": "平局退款", "dc_name": "双重机会"},
    "basketball": {"ml_name": "独赢", "hc_name": "让分", "ou_name": "大小"},
    "tennis":     {"ml_name": "独赢", "hc_name": "让盘", "ou_name": "大小"},
    "baseball":   {"ml_name": "独赢", "hc_name": "让分", "ou_name": "大小"},
    "american_football": {"ml_name": "独赢", "hc_name": "让分", "ou_name": "大小"},
}

# 各运动的 period 编码
SPORT_PERIODS = {
    1: {"ft": 1001, "ht": 1002, "2h": 1003},
    3: {"ft": 3001},
    5: {"ft": 5001, "ht": 5002, "2h": 5003},
    7: {"ft": 7001},
    6: {"ft": 6001},
}


# ─── Token 提取 ───────────────────────────────────────────────

def _get_h5_token_from_chrome():
    """从 Chrome localStorage 通过 AppleScript 获取 h5-token（有效认证凭据）。

    实际有效的 API token 是 `h5-token` (localStorage key)，作为请求头 `h5-token` 发送。
    旧的 Authorization header (st-auth) 已过期。
    """
    # 先检查环境变量（用于测试/备用）
    env_token = os.environ.get("BB_API_TOKEN")
    if env_token:
        return env_token
    # 再检查 .bb_token 文件（持久化缓存）
    token_file = str(DATA_DIR / ".bb_token")
    if os.path.isfile(token_file):
        tok = open(token_file).read().strip()
        if tok and len(tok) > 30:
            logger.info("从 .bb_token 文件读取 API token")
            return tok

    # 通过 AppleScript 从正在运行的 Chrome 获取
    import subprocess, tempfile
    ascript = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "get_h5_token.applescript")
    if not os.path.isfile(ascript):
        ascript = "/tmp/get_h5_token.applescript"
    try:
        out = subprocess.check_output(["osascript", ascript], text=True, timeout=15)
        ls = json.loads(out.strip())
        # bb60.com 存的是 h5-token，pc.x14ff.com 存的是 user-token（值相同）
        h5 = ls.get("h5-token", "") or ls.get("user-token", "")
        if h5:
            logger.info("从 Chrome localStorage 获取到 API token")
            return h5
        logger.warning("Chrome localStorage 中未找到 h5-token 或 user-token")
    except Exception as e:
        logger.warning("从 Chrome 获取 token 失败: %s", e)

    # 备选：从 LevelDB 搜索
    leveldb_dir = os.path.expanduser(
        "~/Library/Application Support/Google/Chrome/Default/Local Storage/leveldb"
    )
    _MAX_LDB_BYTES = 50 * 1024 * 1024  # 跳过 >50MB 的大文件
    if os.path.isdir(leveldb_dir):
        for fname in sorted(os.listdir(leveldb_dir)):
            if not (fname.endswith(".log") or fname.endswith(".ldb")):
                continue
            fpath = os.path.join(leveldb_dir, fname)
            try:
                if os.path.getsize(fpath) > _MAX_LDB_BYTES:
                    logger.debug("跳过超大 LevelDB 文件: %s (%dMB)", fname, os.path.getsize(fpath) // 1024 // 1024)
                    continue
                data = open(fpath, "rb").read()
            except (OSError, IOError):
                continue
            # 搜索 h5-token 或 user-token（新格式含 tt_ 前缀和点号）
            m = re.search(rb'(?:h5-token|user-token).\x01([A-Za-z0-9_.-]{30,80})', data)
            if m:
                token = m.group(1).decode()
                logger.info("从 Chrome LevelDB %s 提取到 h5-token", fname)
                return token

    logger.warning("未获取到 BB h5-token，请确认已登录 bb60.com")
    return None


_TOKEN_SENTINEL = object()

def _ensure_token():
    """获取 h5-token，带缓存。401 后清缓存，下次自动重试。"""
    cache = getattr(_ensure_token, "_cache", _TOKEN_SENTINEL)
    if cache is _TOKEN_SENTINEL:
        _ensure_token._cache = _get_h5_token_from_chrome()
        cache = _ensure_token._cache
    return cache


# ─── 直接 API 调用 ────────────────────────────────────────────

_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def api_post(endpoint, params, platform="BB"):
    """直接 HTTP POST 调用指定平台的 API。

    使用 requests 库避免 Python 3.14 urllib IncompleteRead 截断问题。
    返回解析后的 JSON dict，或 None。
    """
    platform_config = PLATFORMS.get(platform, PLATFORMS["BB"])
    token = _ensure_token()
    if not token:
        logger.error("无 %s API token，请先登录 pc.x14ff.com", platform_config["label"])
        return None

    url = f"{platform_config['api_base']}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        platform_config["auth_header"]: token,
        "User-Agent": _USER_AGENT,
    }

    try:
        resp = _SESSION.post(url, json=params, headers=headers, timeout=30)
        if resp.status_code == 401:
            logger.warning("API 401 认证失败，token 可能已过期，下次调用将重新获取")
            _ensure_token._cache = _TOKEN_SENTINEL
            return None
        if resp.status_code != 200:
            logger.warning("API HTTP %s: %s", resp.status_code, endpoint)
            return None
        body = resp.json()
        return body
    except requests.exceptions.Timeout:
        logger.warning("API 超时: %s", endpoint)
        return None
    except requests.exceptions.RequestException as e:
        logger.warning("API 请求失败 %s: %s", endpoint, e)
        return None
    except json.JSONDecodeError as e:
        logger.warning("API 响应异常 %s: %s", endpoint, e)
        return None


# ─── 提取函数 ─────────────────────────────────────────────────

def fetch_sport(sport_id, platform="BB", page_size=100):
    """获取一个运动的所有比赛（含分页）。

    type=2 表示早盘/未来72小时，type=3 只返回当天少量比赛。
    """
    all_records = []
    page = 1

    while True:
        params = {
            "sportId": sport_id,
            "type": 2,
            "current": page,
            "pageSize": page_size,
            "isPC": True,
            "languageType": "CMN",
        }
        resp = api_post("/v1/match/getList", params, platform=platform)
        if not resp or not resp.get("success"):
            logger.warning("API 返回空 (page=%d)", page)
            break

        data = resp.get("data", {})
        records = data.get("records", [])
        total = data.get("total", 0)
        pages = data.get("pageTotal", 1)

        all_records.extend(records)
        print(f"    第{page}/{pages} 页: {len(records)} 条 (累计 {len(all_records)}/{total})")

        if page >= pages:
            break
        page += 1

    return all_records


def _get_match_teams(record):
    """从 API 记录中提取主客队名。"""
    teams = record.get("ts", [])
    if len(teams) >= 2:
        return teams[0].get("na", ""), teams[1].get("na", "")
    nm = record.get("nm", "")
    if " vs " in nm:
        parts = nm.split(" vs ", 1)
        return parts[0].strip(), parts[1].strip()
    return "", ""


def _find_market_group(record, mty_code, period=1001):
    """在比赛记录中找指定市场类型和时期的 group。"""
    mg = record.get("mg", [])
    for g in mg:
        if g.get("mty") == mty_code and g.get("pe", g.get("period")) == period:
            return g
    return None


def _get_market_options(market):
    """从 market 中取出选项列表。"""
    return market.get("op", market.get("options", []))


def _get_line_value(market):
    """从 market 中取让球/大小线值。"""
    li = market.get("li")
    if li is None:
        return None
    try:
        return float(li)
    except (ValueError, TypeError):
        return None


def extract_match_odds(record, sport_key, platform="BB"):
    """从 API 记录中提取结构化赔率数据。"""
    sport_id = record.get("sid")
    home, away = _get_match_teams(record)
    league = record.get("lg", {}).get("na", "")

    result = {
        "home": home,
        "away": away,
        "league": league,
        "sport": sport_key,
        "platform": platform,
        "sport_cn": {"football": "足球", "basketball": "篮球",
                     "tennis": "网球", "baseball": "棒球",
                     "american_football": "美式足球"}.get(sport_key, ""),
        "id": record.get("id"),
        "bt": record.get("bt"),
        "nm": record.get("nm", ""),
        "odds_ft": {},
        "odds_ht": {},
        "_bb_view": "main",
        "_bb_source": "api",
    }

    mt = MARKET_TYPES.get(sport_id, {})
    periods = SPORT_PERIODS.get(sport_id, {"ft": 1001, "ht": 1002})
    ft_period = periods.get("ft", 1001)
    ht_period = periods.get("ht")

    # ─── FT ──────────────────────────────────────────────

    def _extract_ml(period):
        mty_code = mt.get("ml")
        if not mty_code:
            return None
        group = _find_market_group(record, mty_code, period)
        if not group:
            return None
        markets = group.get("mks", group.get("markets", []))
        if not markets:
            return None
        ops = _get_market_options(markets[0])
        if not ops:
            return None

        home_name, away_name = _get_match_teams(record)

        parsed = []
        for op in ops:
            od = op.get("od")
            ty = op.get("ty", 0)
            na = (op.get("na", "") or "").strip()
            if od and isinstance(od, (int, float)) and od > 0:
                parsed.append({"na": na, "ty": ty, "od": float(od)})

        if len(parsed) < 2:
            return None

        def _name_similarity(a, b):
            if not a or not b:
                return 0
            a = a.lower().strip()
            b = b.lower().strip()
            if a == b:
                return 1.0
            if a in b or b in a:
                return 0.8
            return 0.0

        # 2-way / 3-way
        if sport_key not in ("football",) or len(parsed) < 3:
            home_od = away_od = None
            for p in parsed:
                na = p["na"]
                sim_h = _name_similarity(na, home_name) if home_name else 0
                sim_a = _name_similarity(na, away_name) if away_name else 0
                if sim_h > sim_a and sim_h >= 0.5:
                    home_od = p["od"]
                elif sim_a > sim_h and sim_a >= 0.5:
                    away_od = p["od"]
            if home_od is None:
                for p in parsed:
                    if p["ty"] == 1:
                        home_od = p["od"]
                        break
            if away_od is None:
                for p in parsed:
                    if p["ty"] == 2:
                        away_od = p["od"]
                        break
            result = [home_od] if home_od else []
            if away_od:
                result.append(away_od)
            return result if len(result) >= 2 else None

        # 3-way (football)
        home_od = away_od = draw_od = None
        draw_keywords = ("和", "和局", "平局", "平", "Draw", "平局退款")

        for p in parsed:
            na = p["na"]
            sim_h = _name_similarity(na, home_name) if home_name else 0
            sim_a = _name_similarity(na, away_name) if away_name else 0

            if sim_h >= 0.5 and sim_h > sim_a:
                home_od = p["od"]
            elif sim_a >= 0.5 and sim_a > sim_h:
                away_od = p["od"]
            elif any(kw in na for kw in draw_keywords):
                draw_od = p["od"]
            elif p["ty"] == 1:
                home_od = p["od"]
            elif p["ty"] == 3:
                draw_od = p["od"]
            elif p["ty"] == 2:
                away_od = p["od"]

        if draw_od is None and home_od and away_od:
            assigned = {home_od, away_od}
            for p in parsed:
                if p["od"] not in assigned:
                    draw_od = p["od"]
                    break

        result = []
        if home_od:
            result.append(home_od)
        if draw_od:
            result.append(draw_od)
        if away_od:
            result.append(away_od)
        if len(result) == 2:
            result.insert(1, result[1])
        return result if len(result) >= 3 else None

    def _extract_handicap(period):
        mty_code = mt.get("hc")
        if not mty_code:
            return None
        group = _find_market_group(record, mty_code, period)
        if not group:
            return None
        markets = group.get("mks", group.get("markets", []))
        if not markets:
            return None

        lines = []
        for mk in markets:
            ops = _get_market_options(mk)
            if len(ops) < 2:
                continue
            line_val = _get_line_value(mk)

            home_op = away_op = None
            for op in ops:
                ty = op.get("ty", 0)
                if ty == 1:
                    home_op = op
                elif ty == 2:
                    away_op = op
            if not home_op or not away_op:
                home_op, away_op = ops[0], ops[1]

            home_odds = float(home_op.get("od", 0))
            away_odds = float(away_op.get("od", 0))
            home_line_str = home_op.get("nm", "")
            away_line_str = away_op.get("nm", "")

            if home_odds <= 0 or away_odds <= 0:
                continue

            lines.append({
                "home_line": line_val,
                "away_line": -line_val if line_val else None,
                "home_line_str": home_line_str,
                "away_line_str": away_line_str,
                "home_odds": home_odds,
                "away_odds": away_odds,
            })

        if not lines:
            return None
        return {"primary": lines[0], "alternates": lines[1:]}

    def _extract_ou(period):
        mty_code = mt.get("ou")
        if not mty_code:
            return None
        group = _find_market_group(record, mty_code, period)
        if not group:
            return None
        markets = group.get("mks", group.get("markets", []))
        if not markets:
            return None

        lines = []
        for mk in markets:
            ops = _get_market_options(mk)
            if len(ops) < 2:
                continue
            line_val = _get_line_value(mk)
            over_op = ops[0]
            under_op = ops[1]
            over_odds = float(over_op.get("od", 0))
            under_odds = float(under_op.get("od", 0))
            line_str = over_op.get("nm", "")
            if over_odds <= 0 or under_odds <= 0:
                continue
            lines.append({
                "line": line_val,
                "line_str": line_str,
                "over_odds": over_odds,
                "under_odds": under_odds,
            })

        if not lines:
            return None
        return {"primary": lines[0], "alternates": lines[1:]}

    def _extract_dnb(period):
        mty_code = mt.get("dnb")
        if not mty_code:
            return None
        group = _find_market_group(record, mty_code, period)
        if not group:
            return None
        markets = group.get("mks", group.get("markets", []))
        if not markets:
            return None
        ops = _get_market_options(markets[0])
        if len(ops) < 2:
            return None
        home_op = ops[0]
        away_op = ops[1]
        home_odds = float(home_op.get("od", 0))
        away_odds = float(away_op.get("od", 0))
        if home_odds <= 0 or away_odds <= 0:
            return None
        return {
            "home_odds": home_odds,
            "away_odds": away_odds,
            "home_line_str": home_op.get("nm", ""),
            "away_line_str": away_op.get("nm", ""),
        }

    def _extract_dc(period):
        mty_code = mt.get("dc")
        if not mty_code:
            return None
        group = _find_market_group(record, mty_code, period)
        if not group:
            return None
        markets = group.get("mks", group.get("markets", []))
        if not markets:
            return None
        ops = _get_market_options(markets[0])
        if len(ops) < 3:
            return None
        odds_list = []
        for op in ops:
            od = float(op.get("od", 0))
            if od > 1:
                odds_list.append(od)
        if len(odds_list) >= 3:
            return odds_list[:3]

    def _extract_btts(period):
        """Extract Both Teams To Score (双边进球) from mty=1027."""
        mty_code = mt.get("btts")
        if not mty_code:
            return None
        group = _find_market_group(record, mty_code, period)
        if not group:
            return None
        markets = group.get("mks", group.get("markets", []))
        if not markets:
            return None
        ops = _get_market_options(markets[0])
        if len(ops) < 2:
            return None
        yes_op = no_op = None
        for op in ops:
            na = (op.get("na", "") or "").strip()
            od = float(op.get("od", 0))
            if od <= 0:
                continue
            if na in ("是", "Yes", "是进球"):
                yes_op = od
            elif na in ("否", "No"):
                no_op = od
            elif yes_op is None:
                yes_op = od
            elif no_op is None:
                no_op = od
        if yes_op and no_op:
            return {"yes_odds": yes_op, "no_odds": no_op}
        return None
        return None

    # FT
    ft_ml = _extract_ml(ft_period)
    ft_hc = _extract_handicap(ft_period)
    ft_ou = _extract_ou(ft_period)

    ft_dict = {}
    if ft_ml:
        ft_dict["ml"] = ft_ml
    if ft_hc:
        ft_dict["handicap"] = ft_hc["primary"]
        ft_dict["alternate_handicaps"] = ft_hc["alternates"]
    if ft_ou:
        ft_dict["total"] = ft_ou["primary"]
        ft_dict["alternate_totals"] = ft_ou["alternates"]

    if sport_key == "football":
        ft_dnb = _extract_dnb(ft_period)
        if ft_dnb:
            ft_dict["dnb"] = ft_dnb
        ft_dc = _extract_dc(ft_period)
        if ft_dc:
            ft_dict["dc"] = ft_dc
        ft_btts = _extract_btts(ft_period)
        if ft_btts:
            ft_dict["btts"] = ft_btts

    result["odds_ft"] = ft_dict

    # HT
    ht_ml = _extract_ml(ht_period) if ht_period else None
    ht_hc = _extract_handicap(ht_period) if ht_period else None
    ht_ou = _extract_ou(ht_period) if ht_period else None

    ht_dict = {}
    if ht_ml:
        ht_dict["ml"] = ht_ml
    if ht_hc:
        ht_dict["handicap"] = ht_hc["primary"]
        ht_dict["alternate_handicaps"] = ht_hc["alternates"]
    if ht_ou:
        ht_dict["total"] = ht_ou["primary"]
        ht_dict["alternate_totals"] = ht_ou["alternates"]

    if sport_key == "football":
        if ht_period:
            ht_dnb = _extract_dnb(ht_period)
            if ht_dnb:
                ht_dict["dnb"] = ht_dnb
            ht_dc = _extract_dc(ht_period)
            if ht_dc:
                ht_dict["dc"] = ht_dc

    result["odds_ht"] = ht_dict

    # dnb flat list for backward compat
    dnb_flat = []
    ft_dnb_dict = ft_dict.get("dnb")
    if ft_dnb_dict:
        dnb_flat.append(ft_dnb_dict["home_odds"])
        dnb_flat.append(ft_dnb_dict["away_odds"])
    ht_dnb_dict = ht_dict.get("dnb")
    if ht_dnb_dict:
        dnb_flat.append(ht_dnb_dict["home_odds"])
        dnb_flat.append(ht_dnb_dict["away_odds"])
    if dnb_flat:
        result["odds_dnb"] = dnb_flat
    if ft_dict.get("dc"):
        result["odds_dc"] = ft_dict["dc"]

    return result


def _merge_single_match(platform_matches):
    """合并同一场比赛在多个平台的赔率，取各市场最高值。

    platform_matches: [(platform_key, match_dict), ...]
    Returns: 合并后的 match_dict，包含 platform_sources 字段
    """
    # 以第一个平台的 match 为基础
    base = platform_matches[0][1].copy()
    platforms_in_group = [pm[0] for pm in platform_matches]

    # sources 初始标记为 "BOTH"，只有某平台真正有更高赔率时才标记为它
    sources = {key: "BOTH" for key in ["ml", "handicap", "ou", "dnb", "dc"]}

    def _update_source(market_key, base_val, plat_val, platform):
        """当 base_val < plat_val 时才更新 source 为指定平台，否则不变。"""
        if base_val < plat_val:
            sources[market_key] = platform

    # 遍历其他平台，取各市场最高赔率
    for platform, m in platform_matches[1:]:
        # ML: 逐元素取最大
        base_ml = base.get("odds_ft", {}).get("ml", [])
        plat_ml = m.get("odds_ft", {}).get("ml", [])
        if plat_ml and len(plat_ml) >= len(base_ml):
            for i in range(min(len(base_ml), len(plat_ml))):
                if plat_ml[i] > base_ml[i]:
                    base_ml[i] = plat_ml[i]
                    sources["ml"] = platform
            if len(plat_ml) > len(base_ml):
                base["odds_ft"]["ml"] = plat_ml
                sources["ml"] = platform

        # Handicap: 取主客赔率最大值（前提是线一致）
        base_hc = base.get("odds_ft", {}).get("handicap")
        plat_hc = m.get("odds_ft", {}).get("handicap")
        if base_hc and plat_hc and isinstance(base_hc, dict) and isinstance(plat_hc, dict):
            base_line = base_hc.get("home_line") or base_hc.get("away_line")
            plat_line = plat_hc.get("home_line") or plat_hc.get("away_line")
            if base_line is None or plat_line is None or abs(base_line - plat_line) <= 0.1:
                _update_source("handicap", base_hc.get("home_odds", 0), plat_hc.get("home_odds", 0), platform)
                _update_source("handicap", base_hc.get("away_odds", 0), plat_hc.get("away_odds", 0), platform)
                if plat_hc.get("home_odds", 0) > base_hc.get("home_odds", 0):
                    base_hc["home_odds"] = plat_hc["home_odds"]
                    base_hc["home_line_str"] = plat_hc.get("home_line_str", base_hc.get("home_line_str", ""))
                if plat_hc.get("away_odds", 0) > base_hc.get("away_odds", 0):
                    base_hc["away_odds"] = plat_hc["away_odds"]
                    base_hc["away_line_str"] = plat_hc.get("away_line_str", base_hc.get("away_line_str", ""))
        elif not base_hc and plat_hc:
            base["odds_ft"]["handicap"] = plat_hc
            sources["handicap"] = platform

        # OU: 取大小盘赔率最大值
        base_ou = base.get("odds_ft", {}).get("total")
        plat_ou = m.get("odds_ft", {}).get("total")
        if base_ou and plat_ou and isinstance(base_ou, dict) and isinstance(plat_ou, dict):
            base_line = base_ou.get("line")
            plat_line = plat_ou.get("line")
            if base_line is None or plat_line is None or abs(base_line - plat_line) <= 0.5:
                _update_source("ou", base_ou.get("over_odds", 0), plat_ou.get("over_odds", 0), platform)
                _update_source("ou", base_ou.get("under_odds", 0), plat_ou.get("under_odds", 0), platform)
                if plat_ou.get("over_odds", 0) > base_ou.get("over_odds", 0):
                    base_ou["over_odds"] = plat_ou["over_odds"]
                if plat_ou.get("under_odds", 0) > base_ou.get("under_odds", 0):
                    base_ou["under_odds"] = plat_ou["under_odds"]
        elif not base_ou and plat_ou:
            base["odds_ft"]["total"] = plat_ou
            sources["ou"] = platform

        # DNB
        base_dnb = base.get("odds_ft", {}).get("dnb")
        plat_dnb = m.get("odds_ft", {}).get("dnb")
        if plat_dnb and isinstance(plat_dnb, dict):
            if not base_dnb:
                base["odds_ft"]["dnb"] = plat_dnb
                sources["dnb"] = platform
            elif isinstance(base_dnb, dict):
                _update_source("dnb", base_dnb.get("home_odds", 0), plat_dnb.get("home_odds", 0), platform)
                _update_source("dnb", base_dnb.get("away_odds", 0), plat_dnb.get("away_odds", 0), platform)
                if plat_dnb.get("home_odds", 0) > base_dnb.get("home_odds", 0):
                    base_dnb["home_odds"] = plat_dnb["home_odds"]
                if plat_dnb.get("away_odds", 0) > base_dnb.get("away_odds", 0):
                    base_dnb["away_odds"] = plat_dnb["away_odds"]

        # DC
        base_dc = base.get("odds_dc", [])
        plat_dc = m.get("odds_dc", [])
        if plat_dc and len(plat_dc) >= len(base_dc):
            changed = False
            for i in range(min(len(base_dc), len(plat_dc))):
                if plat_dc[i] > base_dc[i]:
                    base_dc[i] = plat_dc[i]
                    changed = True
            if len(plat_dc) > len(base_dc):
                base["odds_dc"] = plat_dc
                changed = True
            if changed:
                sources["dc"] = platform

        # ── odds_ht 内的各市场也合并 ──
        base_ht = base.get("odds_ht", {})
        plat_ht = m.get("odds_ht", {})
        if plat_ht:
            # HT ML
            base_ht_ml = base_ht.get("ml", [])
            plat_ht_ml = plat_ht.get("ml", [])
            if plat_ht_ml and len(plat_ht_ml) >= len(base_ht_ml):
                for i in range(min(len(base_ht_ml), len(plat_ht_ml))):
                    if plat_ht_ml[i] > base_ht_ml[i]:
                        base_ht_ml[i] = plat_ht_ml[i]
                if len(plat_ht_ml) > len(base_ht_ml):
                    base_ht["ml"] = plat_ht_ml

            # HT Handicap
            base_ht_hc = base_ht.get("handicap")
            plat_ht_hc = plat_ht.get("handicap")
            if base_ht_hc and plat_ht_hc and isinstance(base_ht_hc, dict) and isinstance(plat_ht_hc, dict):
                base_line = base_ht_hc.get("home_line") or base_ht_hc.get("away_line")
                plat_line = plat_ht_hc.get("home_line") or plat_ht_hc.get("away_line")
                if base_line is None or plat_line is None or abs(base_line - plat_line) <= 0.1:
                    if plat_ht_hc.get("home_odds", 0) > base_ht_hc.get("home_odds", 0):
                        base_ht_hc["home_odds"] = plat_ht_hc["home_odds"]
                    if plat_ht_hc.get("away_odds", 0) > base_ht_hc.get("away_odds", 0):
                        base_ht_hc["away_odds"] = plat_ht_hc["away_odds"]
            elif not base_ht_hc and plat_ht_hc:
                base_ht["handicap"] = plat_ht_hc

            # HT Total (OU)
            base_ht_ou = base_ht.get("total")
            plat_ht_ou = plat_ht.get("total")
            if base_ht_ou and plat_ht_ou and isinstance(base_ht_ou, dict) and isinstance(plat_ht_ou, dict):
                base_line = base_ht_ou.get("line")
                plat_line = plat_ht_ou.get("line")
                if base_line is None or plat_line is None or abs(base_line - plat_line) <= 0.5:
                    if plat_ht_ou.get("over_odds", 0) > base_ht_ou.get("over_odds", 0):
                        base_ht_ou["over_odds"] = plat_ht_ou["over_odds"]
                    if plat_ht_ou.get("under_odds", 0) > base_ht_ou.get("under_odds", 0):
                        base_ht_ou["under_odds"] = plat_ht_ou["under_odds"]
            elif not base_ht_ou and plat_ht_ou:
                base_ht["total"] = plat_ht_ou

            # HT DNB
            base_ht_dnb = base_ht.get("dnb")
            plat_ht_dnb = plat_ht.get("dnb")
            if plat_ht_dnb and isinstance(plat_ht_dnb, dict):
                if not base_ht_dnb:
                    base_ht["dnb"] = plat_ht_dnb
                elif isinstance(base_ht_dnb, dict):
                    if plat_ht_dnb.get("home_odds", 0) > base_ht_dnb.get("home_odds", 0):
                        base_ht_dnb["home_odds"] = plat_ht_dnb["home_odds"]
                    if plat_ht_dnb.get("away_odds", 0) > base_ht_dnb.get("away_odds", 0):
                        base_ht_dnb["away_odds"] = plat_ht_dnb["away_odds"]

            # HT DC
            if plat_ht.get("dc"):
                base_ht_dc = base_ht.get("dc", [])
                plat_ht_dc = plat_ht.get("dc", [])
                if not base_ht_dc or any(plat_ht_dc[i] > base_ht_dc[i] for i in range(min(len(base_ht_dc), len(plat_ht_dc)))):
                    if len(plat_ht_dc) >= len(base_ht_dc):
                        base_ht["dc"] = plat_ht_dc

    base["platform"] = "ALL"
    base["platform_sources"] = sources
    return base


def _merge_platform_results(platform_results):
    """合并多个平台的提取结果。

    platform_results: {platform_key: [match_dict, ...]}
    Returns: 合并后的 match 列表
    """
    from collections import OrderedDict
    groups = OrderedDict()

    for platform, matches in platform_results.items():
        for m in matches:
            key = (m.get("home", ""), m.get("away", ""), m.get("league", ""))
            if key not in groups:
                groups[key] = []
            groups[key].append((platform, m))

    merged = []
    for key, platform_matches in groups.items():
        if len(platform_matches) == 1:
            m = platform_matches[0][1].copy()
            m["platform"] = platform_matches[0][0]
            m["platform_sources"] = {"main": platform_matches[0][0]}
            merged.append(m)
        else:
            merged.append(_merge_single_match(platform_matches))

    return merged


def _fetch_one_platform(platform_key: str):
    """提取单个平台所有运动数据。"""
    platform_config = PLATFORMS[platform_key]
    print(f"\n{'=' * 50}")
    print(f"{platform_config['label']} API 提取（{platform_config['api_base']}）")
    print(f"{'=' * 50}")

    platform_matches = []
    sport_counts = {}

    for sport_id, sport_key, sport_cn in SPORTS:
        print(f"\n--- {sport_cn} (sportId={sport_id}) ---")
        records = fetch_sport(sport_id, platform=platform_key)
        if not records:
            print(f"    ⚠️ 无数据")
            continue

        print(f"    共 {len(records)} 场比赛")

        matches = []
        for rec in records:
            m = extract_match_odds(rec, sport_key, platform=platform_key)
            if m["home"] and m["away"]:
                matches.append(m)

        sport_counts[sport_cn] = len(matches)
        platform_matches.extend(matches)
        print(f"    → 结构化 {len(matches)} 场")

        if matches:
            sample = matches[0]
            ml = sample["odds_ft"].get("ml", [])
            hc = sample["odds_ft"].get("handicap", {})
            ou = sample["odds_ft"].get("total", {})
            print(f"    样例: {sample['league']} | {sample['home']} vs {sample['away']}")
            print(f"      独赢: {ml}")
            if hc:
                print(f"      让球(主): {hc.get('home_line_str', '')} @ {hc.get('home_odds', '')}")
                print(f"      让球(客): {hc.get('away_line_str', '')} @ {hc.get('away_odds', '')}")
                alt_hc = sample["odds_ft"].get("alternate_handicaps", [])
                if alt_hc:
                    print(f"      其它让球线: {len(alt_hc)} 条")
            if ou:
                print(f"      大小: {ou.get('line_str', '')} @ {ou.get('over_odds', '')}/{ou.get('under_odds', '')}")
                alt_ou = sample["odds_ft"].get("alternate_totals", [])
                if alt_ou:
                    print(f"      其它大小线: {len(alt_ou)} 条")
            if sample["odds_ft"].get("dnb"):
                print(f"      平局退款: {sample['odds_ft']['dnb']}")
            if sample["odds_ft"].get("dc"):
                print(f"      双重机会: {sample['odds_ft']['dc']}")

    print(f"\n  → {platform_config['label']} 合计: {len(platform_matches)} 场比赛")
    for name, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        print(f"    {name}: {count}")

    return platform_key, platform_matches


def fetch_all_sports():
    """获取所有运动在所有平台的比赛数据并结构化。

    使用多线程并行提取各平台数据，然后按比赛合并取最高赔率。
    """
    all_platform_matches = {}
    total_by_platform = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PLATFORMS)) as executor:
        futures = {executor.submit(_fetch_one_platform, key): key for key in PLATFORMS}
        for future in concurrent.futures.as_completed(futures):
            platform_key, platform_matches = future.result()
            total_by_platform[platform_key] = len(platform_matches)
            all_platform_matches[platform_key] = platform_matches

    # 合并各平台结果（取最高赔率）
    print(f"\n{'=' * 50}")
    print("合并多平台数据（取各市场最高赔率）...")
    merged_matches = _merge_platform_results(all_platform_matches)

    print(f"各平台原始数据:")
    for p, n in sorted(total_by_platform.items()):
        print(f"  {PLATFORMS[p]['label']}: {n} 场")
    print(f"合并后: {len(merged_matches)} 场 (去重+取最高赔率)")

    # 统计合并来源
    source_counts = {}
    for m in merged_matches:
        src = m.get("platform", "?")
        source_counts[src] = source_counts.get(src, 0) + 1
    print(f"来源分布: {source_counts}")
    multi_platform = sum(1 for m in merged_matches if m.get("platform") == "ALL")
    if multi_platform:
        ml_from = {}
        hc_from = {}
        ou_from = {}
        for m in merged_matches:
            if m.get("platform") == "ALL":
                ps = m.get("platform_sources", {})
                for k, v in ps.items():
                    if k == "ml":
                        ml_from[v] = ml_from.get(v, 0) + 1
                    elif k == "handicap":
                        hc_from[v] = hc_from.get(v, 0) + 1
                    elif k == "ou":
                        ou_from[v] = ou_from.get(v, 0) + 1
        print(f"  ML最高赔率来源: {dict(sorted(ml_from.items()))}")
        print(f"  让球最高赔率来源: {dict(sorted(hc_from.items()))}")
        print(f"  大小最高赔率来源: {dict(sorted(ou_from.items()))}")

    return merged_matches


def save_results(matches, single_platform=None):
    """保存结果到 JSON，格式兼容 bb_vs_pinnacle.py。

    Args:
        matches: 比赛列表
        single_platform: 单平台模式下的平台名（如 "BB"），用于调试输出文件名
    """
    timestamp = time.strftime('%Y-%m-%dT%H:%M:%S')

    # 统计各平台来源
    platform_counts = {}
    all_source_keys = set()
    for m in matches:
        plat = m.get("platform", "?")
        platform_counts[plat] = platform_counts.get(plat, 0) + 1
        ps = m.get("platform_sources", {})
        for k in ps:
            all_source_keys.add(k)

    if single_platform:
        source_label = f"{PLATFORMS[single_platform]['label']} (单平台调试)"
    else:
        source_label = f"BB体育+FB体育 (多平台合并)"

    output = {
        "timestamp": timestamp,
        "source": source_label,
        "match_count": len(matches),
        "platform_counts": dict(sorted(platform_counts.items())),
        "sport_counts": {},
        "matches": matches,
    }
    for m in matches:
        sport = m.get("sport_cn", "未知")
        output["sport_counts"][sport] = output["sport_counts"].get(sport, 0) + 1

    if single_platform:
        out_path = DATA_DIR / f"bb_odds_extracted_{single_platform}.json"
    else:
        out_path = DATA_DIR / "bb_odds_extracted.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"\n已保存到 {out_path}")
    return out_path


def check_connectivity():
    """各平台 API 连通性预检。返回 (ok, msg)。"""
    all_ok = True

    for platform_key, platform_config in PLATFORMS.items():
        label = platform_config["label"]
        api_base = platform_config["api_base"]
        auth_header = platform_config["auth_header"]
        domain = api_base.replace("https://", "")

        print(f"\n🔌 {label} API 连通性检测:")
        print(f"  端点: {api_base}")

        # Step 1: DNS 解析 + 基本可达性
        import socket
        try:
            ip = socket.getaddrinfo(domain, 443)[0][4][0]
            print(f"  ✅ DNS 解析: {domain} → {ip}")
        except socket.gaierror as e:
            print(f"  ❌ DNS 解析失败: {e}")
            print(f"    建议: 检查网络连接 / DNS 设置（114.114.114.114）")
            all_ok = False
            continue

        # Step 2: Token 检查
        token = _ensure_token()
        if not token:
            print(f"  ❌ 未获取到 {auth_header}")
            print(f"    建议: 确认已登录 bb60.com，且 Chrome 正在运行")
            all_ok = False
            continue
        print(f"  ✅ Token: {token[:15]}...{token[-8:]} ({len(token)} chars)")

        # Step 3: 实际 API 调用测试
        try:
            params = {"sportId": 1, "type": 2, "current": 1, "pageSize": 1,
                      "isPC": True, "languageType": "CMN"}
            resp = _SESSION.post(f"{api_base}/v1/match/getList", json=params, headers={
                "Content-Type": "application/json",
                auth_header: token,
                "User-Agent": _USER_AGENT,
            }, timeout=15)

            if resp.status_code == 200:
                data = resp.json()
                code = data.get("code", -1)
                if code == 0:
                    total = data.get("data", {}).get("total", 0)
                    print(f"  ✅ API 响应正常 (total={total})")
                else:
                    print(f"  ❌ API 返回异常 code={code}: {data.get('msg', '')}")
                    all_ok = False
            elif resp.status_code == 401:
                print(f"  ❌ API 401 — {auth_header} 过期")
                print(f"    建议: 重新登录 bb60.com 刷新 token")
                _ensure_token._cache = None
                all_ok = False
            else:
                print(f"  ❌ API HTTP {resp.status_code}")
                all_ok = False
        except requests.exceptions.SSLError as e:
            print(f"  ❌ SSL 失败: {e}")
            print(f"    建议: 检查系统时间")
            all_ok = False
        except requests.exceptions.ConnectionError as e:
            print(f"  ❌ 连接失败: {e}")
            print(f"    建议: 检查网络 / Shadowrocket")
            all_ok = False
        except requests.exceptions.Timeout:
            print(f"  ❌ 超时")
            print(f"    建议: 检查网络延迟")
            all_ok = False
        except Exception as e:
            print(f"  ❌ 异常 ({type(e).__name__}): {e}")
            all_ok = False

    return all_ok


def main():
    """主入口：提取所有运动（多平台）并保存。"""
    # --check 只跑连通性检测
    if "--check" in sys.argv:
        ok = check_connectivity()
        sys.exit(0 if ok else 1)

    # --platform BB 只跑指定平台
    platform_override = None
    for arg in sys.argv:
        if arg.startswith("--platform="):
            platform_override = arg.split("=", 1)[1].upper()

    if platform_override:
        # 单平台模式
        print(f"🔧 单平台模式: {PLATFORMS.get(platform_override, {}).get('label', platform_override)}")
        matches = _fetch_single_platform(platform_override)
        if matches:
            save_results(matches, single_platform=platform_override)
            print(f"提取完成，共 {len(matches)} 场比赛")
        else:
            print("⚠️ 未提取到任何比赛！")
            sys.exit(1)
    else:
        # 全平台模式
        matches = fetch_all_sports()
        if matches:
            save_results(matches)
            print(f"\n多平台提取完成，共 {len(matches)} 场比赛")
        else:
            print("⚠️ 未提取到任何比赛！")
            sys.exit(1)


def _fetch_single_platform(platform_key):
    """单平台提取（用于调试）。"""
    platform_config = PLATFORMS.get(platform_key)
    if not platform_config:
        print(f"❌ 未知平台: {platform_key}")
        return None

    all_matches = []
    for sport_id, sport_key, sport_cn in SPORTS:
        print(f"\n--- {sport_cn} (sportId={sport_id}) ---")
        records = fetch_sport(sport_id, platform=platform_key)
        if not records:
            print(f"    ⚠️ 无数据")
            continue
        print(f"    共 {len(records)} 场比赛")
        for rec in records:
            m = extract_match_odds(rec, sport_key, platform=platform_key)
            if m["home"] and m["away"]:
                all_matches.append(m)
        print(f"    → 结构化 {sum(1 for m in all_matches if m.get('sport') == sport_key)} 场")
    return all_matches


if __name__ == "__main__":
    main()
