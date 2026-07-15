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
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

# API 端点
API_BASE = "https://api.447a9.com"

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
        "ml": 1005, "hc": 1000, "ou": 1007, "dnb": 1089, "dc": 1012,
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

    # 通过 AppleScript 从正在运行的 Chrome 获取
    import subprocess, tempfile
    ascript = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "get_h5_token.applescript")
    if not os.path.isfile(ascript):
        ascript = "/tmp/get_h5_token.applescript"
    try:
        out = subprocess.check_output(["osascript", ascript], text=True, timeout=15)
        ls = json.loads(out.strip())
        h5 = ls.get("h5-token", "")
        if h5:
            logger.info("从 Chrome localStorage 获取到 h5-token")
            return h5
        logger.warning("Chrome localStorage 中未找到 h5-token")
    except Exception as e:
        logger.warning("从 Chrome 获取 h5-token 失败: %s", e)

    # 备选：从 LevelDB 搜索
    leveldb_dir = os.path.expanduser(
        "~/Library/Application Support/Google/Chrome/Default/Local Storage/leveldb"
    )
    if os.path.isdir(leveldb_dir):
        for fname in sorted(os.listdir(leveldb_dir)):
            if not (fname.endswith(".log") or fname.endswith(".ldb")):
                continue
            fpath = os.path.join(leveldb_dir, fname)
            try:
                data = open(fpath, "rb").read()
            except (OSError, IOError):
                continue
            # 搜索 h5-token base64 值
            m = re.search(rb'h5-token.\x01([A-Za-z0-9+/=]{40,60})', data)
            if m:
                token = m.group(1).decode()
                logger.info("从 Chrome LevelDB %s 提取到 h5-token", fname)
                return token

    logger.warning("未获取到 BB h5-token，请确认已登录 bb60.com")
    return None


def _ensure_token():
    """获取 h5-token，带缓存。"""
    if not hasattr(_ensure_token, "_cache"):
        _ensure_token._cache = _get_h5_token_from_chrome()
    return _ensure_token._cache


# ─── 直接 API 调用 ────────────────────────────────────────────

_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def api_post(endpoint, params):
    """直接 HTTP POST 调用 BB API。

    使用 requests 库避免 Python 3.14 urllib IncompleteRead 截断问题。
    返回解析后的 JSON dict，或 None。
    """
    token = _ensure_token()
    if not token:
        logger.error("无 BB API token，请先登录 pc.x14ff.com")
        return None

    url = f"{API_BASE}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "h5-token": token,
        "User-Agent": _USER_AGENT,
    }

    try:
        resp = _SESSION.post(url, json=params, headers=headers, timeout=30)
        if resp.status_code == 401:
            logger.warning("API 401 认证失败，token 可能已过期")
            _ensure_token._cache = None
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

def fetch_sport(sport_id, page_size=100):
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
        resp = api_post("/v1/match/getList", params)
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


def extract_match_odds(record, sport_key):
    """从 API 记录中提取结构化赔率数据。"""
    sport_id = record.get("sid")
    home, away = _get_match_teams(record)
    league = record.get("lg", {}).get("na", "")

    result = {
        "home": home,
        "away": away,
        "league": league,
        "sport": sport_key,
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


def fetch_all_sports():
    """获取所有运动的比赛数据并结构化。"""
    print("=" * 50)
    print("BB体育 API 提取（直接 HTTP）")
    print("=" * 50)

    all_matches = []
    sport_counts = {}
    total_all = 0

    for sport_id, sport_key, sport_cn in SPORTS:
        print(f"\n--- {sport_cn} (sportId={sport_id}) ---")
        records = fetch_sport(sport_id)
        if not records:
            print(f"    ⚠️ 无数据")
            continue

        print(f"    共 {len(records)} 场比赛")

        matches = []
        for rec in records:
            m = extract_match_odds(rec, sport_key)
            if m["home"] and m["away"]:
                matches.append(m)

        sport_counts[sport_cn] = len(matches)
        total_all += len(matches)
        all_matches.extend(matches)
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

    print(f"\n{'=' * 50}")
    print(f"全部运动提取完成: {total_all} 场比赛")
    for name, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        print(f"  {name}: {count}")

    return all_matches


def save_results(matches):
    """保存结果到 JSON，格式兼容 bb_vs_pinnacle.py。"""
    timestamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    output = {
        "timestamp": timestamp,
        "source": "BB体育 (API: api.447a9.com) - 全运动",
        "match_count": len(matches),
        "sport_counts": {},
        "matches": matches,
    }
    for m in matches:
        sport = m.get("sport_cn", "未知")
        output["sport_counts"][sport] = output["sport_counts"].get(sport, 0) + 1

    out_path = DATA_DIR / "bb_odds_extracted.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"\n已保存到 {out_path}")
    return out_path


def check_connectivity():
    """BB API 连通性预检。返回 (ok, msg)。"""
    print("\n🔌 BB API 连通性检测:")
    print(f"  端点: {API_BASE}")

    # Step 1: DNS 解析 + 基本可达性
    import socket
    try:
        ip = socket.getaddrinfo("api.447a9.com", 443)[0][4][0]
        print(f"  ✅ DNS 解析: api.447a9.com → {ip}")
    except socket.gaierror as e:
        print(f"  ❌ DNS 解析失败: {e}")
        print(f"    建议: 检查网络连接 / DNS 设置（114.114.114.114）")
        return False

    # Step 2: Token 检查
    token = _ensure_token()
    if not token:
        print(f"  ❌ 未获取到 h5-token")
        print(f"    建议: 确认已登录 bb60.com，且 Chrome 正在运行")
        return False
    print(f"  ✅ Token: {token[:15]}...{token[-8:]} ({len(token)} chars)")

    # Step 3: 实际 API 调用测试
    try:
        params = {"sportId": 1, "type": 2, "current": 1, "pageSize": 1,
                  "isPC": True, "languageType": "CMN"}
        resp = _SESSION.post(f"{API_BASE}/v1/match/getList", json=params, headers={
            "Content-Type": "application/json",
            "h5-token": token,
            "User-Agent": _USER_AGENT,
        }, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            code = data.get("code", -1)
            if code == 0:
                total = data.get("data", {}).get("total", 0)
                print(f"  ✅ API 响应正常 (total={total})")
                return True
            else:
                print(f"  ❌ API 返回异常 code={code}: {data.get('msg', '')}")
                return False
        elif resp.status_code == 401:
            print(f"  ❌ API 401 — h5-token 过期")
            print(f"    建议: 重新登录 bb60.com 刷新 token")
            _ensure_token._cache = None
            return False
        else:
            print(f"  ❌ API HTTP {resp.status_code}")
            return False
    except requests.exceptions.SSLError as e:
        print(f"  ❌ SSL 失败: {e}")
        print(f"    建议: 检查系统时间")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"  ❌ 连接失败: {e}")
        print(f"    建议: 检查网络 / Shadowrocket")
        return False
    except requests.exceptions.Timeout:
        print(f"  ❌ 超时")
        print(f"    建议: 检查网络延迟")
        return False
    except Exception as e:
        print(f"  ❌ 异常 ({type(e).__name__}): {e}")
        return False


def main():
    """主入口：提取所有运动并保存。"""
    # --check 只跑连通性检测
    if "--check" in sys.argv:
        ok = check_connectivity()
        sys.exit(0 if ok else 1)

    matches = fetch_all_sports()
    if matches:
        save_results(matches)
        print(f"提取完成，共 {len(matches)} 场比赛")
    else:
        print("⚠️ 未提取到任何比赛！")
        sys.exit(1)


if __name__ == "__main__":
    main()
