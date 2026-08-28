"""BB体育 API 调用器 —— 直接 HTTP 调用 api.infv1.com (BB体育真实API)

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

# ── 联赛剔除名单 ──
# 友谊赛/热身赛/季前赛/青年队(U19-U23)/3x3 等不靠谱联赛, 提取层直接丢弃,
# 以后不再落盘 (bb_odds_extracted.json)。对比层 bb_vs_pinnacle 仍有兜底过滤。
_BANNED_LEAGUES_CACHE = None


def _load_banned_leagues():
    """加载 banned_leagues.json (与 bb_vs_pinnacle/bb_ev_push 共用同一数据源)。"""
    global _BANNED_LEAGUES_CACHE
    if _BANNED_LEAGUES_CACHE is None:
        _banned_file = DATA_DIR / "banned_leagues.json"
        try:
            _BANNED_LEAGUES_CACHE = json.loads(_banned_file.read_text())
        except Exception:
            _BANNED_LEAGUES_CACHE = []
    return _BANNED_LEAGUES_CACHE


def _is_banned_league(league):
    """子串匹配: 联赛名含任一剔除关键词即视为不靠谱联赛。"""
    league = league or ""
    return any(b in league for b in _load_banned_leagues())

# API 端点（BB体育）
API_BASE = "https://api.infv1.com"

# 多平台配置（BB体育 + FB体育）
# 注意: BB体育真正API是 api.infv1.com（user-token），不是 api.447a9.com（h5-token）
PLATFORMS = {
    "BB": {
        "api_base": "https://api.infv1.com",
        "auth_header": "user-token",
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
    (15, "pingpong", "乒乓球"),
    (19, "boxing", "拳击"),
    (18, "mma", "MMA"),
    (47, "badminton", "羽毛球"),
    (2, "ice_hockey", "冰球"),
    (13, "volleyball", "排球"),
]

# Pinnacle 覆盖的运动（只有这些运动有对比价值）
PINNACLE_SPORTS = {"football", "basketball", "tennis", "baseball",
                    "american_football", "mma", "badminton",
                    "ice_hockey", "volleyball"}

# Market type 映射 (按运动)
MARKET_TYPES = {
    1: {  # 足球
        "ml": 1005, "hc": 1000, "ou": 1007, "dnb": None, "dc": 1012, "btts": 1027,
        "oe": 1008, "htft": 1033,
        "corner_ml": 1009, "corner_ou": 1010, "corner_hc": 1011,
        # V5.5: 特殊盘口 (Pinnacle special 字段有对应)
        # 2026-08-27 修正错配: 正确比分=1099(原1188错), 先进球=1089"1th Goal"(原1019错,
        # 1019实为"Last Team To Score"最后进球)
        "correct_score": 1099, "total_goals_range": 1101, "first_to_score": 1089,
        "winning_margin": 1018,
        # 注: dnb 无平局退款盘口, 置 None 禁用
        # V5.11: 半场特殊盘口 — BB 半场用独立 mty 码(全场码 1099/1101/1018/1089 在 HT period
        # 下不存在)。实测仅两个半场特殊盘: 正确比分上半场=1100(全比分含0-0), 精确进球上半场=1103。
        # 1100 对应 Pinnacle "Correct Score 1st Half"; 1103(Exact Goals 0/1/2/3+) 对应
        # Pinnacle "Exact Total Goals 1st Half"(非"Total Goals Range"区间盘)。
        "correct_score_ht": 1100,
        "exact_goals_ht": 1103,
    },
    3: {  # 篮球 (3004=独赢, 3003=大小, 3002=让分)
        "ml": 3004, "ou": 3003, "hc": 3002,
        "ht_ml": 3020,  # V5.11: 篮球半场独赢是独立 mty(3020), 全场 ml=3004 在 pe=3003 下不存在
    },
    5: {  # 网球 (5001=独赢, 5004=让盘, 5003=大小/总局数, 5012=下一盘)
        "ml": 5001, "hc": 5004, "ou": 5003,
    },
    7: {  # 棒球
        "ml": 7003, "hc": 7001, "ou": [7002, 7005],  # 7002=全场 7005=备选; 7004=F5(前5局)已剔除, 避免F5的4.5被当全场大小球
    },
    6: {  # 美式足球 (NFL/大学/室内) — 6003=独赢(Winner), 6001=让分(Handicap), 6002=大小(Over/Under)
        # 2026-08-28 修复映射: 旧码 ml=6001/hc=6002/ou=6003 是错的(6001实为让分/6003实为独赢),
        # 导致独赢提取成让分、大小提取成独赢。BB/FB 两平台 mty 码一致(实测)。
        "ml": 6003, "hc": 6001, "ou": 6002,
        "ht_ml": 6010,  # 半场独赢独立 mty (Moneyline-1st Half, pe=6003)
    },
    15: {  # 乒乓球
        "ml": 15001, "hc": 15002, "ou": 15003,
    },
    19: {  # 拳击
        "ml": 19002, "ou": 19001,
    },
    18: {  # MMA
        "ml": 18002, "ou": 18001,
    },
    47: {  # 羽毛球 (实测: 全场只有独赢47001+大小47003, 让局47002在第二局)
        "ml": 47001, "ou": 47003,
    },
    2: {  # 冰球
        "ml": 2003, "hc": 2001, "ou": 2002,
    },
    13: {  # 排球
        "ml": 13001, "hc": 13002, "ou": 13003,
    },
}

# 各运动的市场显示中文名
MARKET_LABELS = {
    "football":  {"ml_name": "独赢", "hc_name": "让球", "ou_name": "大小",
                  "dnb_name": "平局退款", "dc_name": "双重机会",
                  "oe_name": "单/双", "htft_name": "半全场"},
    "basketball": {"ml_name": "独赢", "hc_name": "让分", "ou_name": "大小"},
    "tennis":     {"ml_name": "独赢", "hc_name": "让盘", "ou_name": "大小"},
    "baseball":   {"ml_name": "独赢", "hc_name": "让分", "ou_name": "大小"},
    "american_football": {"ml_name": "独赢", "hc_name": "让分", "ou_name": "大小"},
    "pingpong":  {"ml_name": "独赢", "hc_name": "让分", "ou_name": "大小"},
    "boxing":    {"ml_name": "独赢", "ou_name": "大小"},
    "mma":       {"ml_name": "独赢", "ou_name": "大小"},
    "badminton": {"ml_name": "独赢", "hc_name": "让局", "ou_name": "大小"},
    "ice_hockey": {"ml_name": "1X2", "hc_name": "让球", "ou_name": "大小"},
    "volleyball": {"ml_name": "独赢", "hc_name": "让分", "ou_name": "大小"},
}

# 各运动的 period 编码
SPORT_PERIODS = {
    1: {"ft": 1001, "ht": 1002, "2h": 1003},
    3: {"ft": 3001, "ht": 3003},  # 篮球半场 pe=3003 (1st Half)
    5: {"ft": 5001, "ht": 5002, "2h": 5003},
    7: {"ft": 7001, "f5": 7004},  # F5 = First 5 Innings (Pinnacle period 3)
    6: {"ft": 6001, "ht": 6003},  # 美式足球半场 pe=6003 (1st Half)
    15: {"ft": 15001},
    19: {"ft": 19001},
    18: {"ft": 18001},
    47: {"ft": 47001},
    2: {"ft": 2001},
    13: {"ft": 13001},
}


# ─── Token 提取 ───────────────────────────────────────────────

def _get_h5_token_from_chrome():
    """从 Chrome localStorage 获取 API token（user-token/h5-token 值相同）。

    BB体育真实API使用 `user-token` 请求头，FB体育也用 `user-token`。
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
    V4.5: 增加指数退避重试 (3次, 1s/2s/4s)。
    返回解析后的 JSON dict，或 None。
    """
    import time as _time
    platform_config = PLATFORMS.get(platform, PLATFORMS["BB"])

    for attempt in range(3):
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
                logger.warning("API 401 认证失败，token 可能已过期，重新获取...")
                _ensure_token._cache = _TOKEN_SENTINEL
                continue  # retry with new token
            if resp.status_code in (429, 503, 502):
                wait = 2 ** attempt
                logger.warning("API HTTP %s: %s, %ds后重试(%d/3)", resp.status_code, endpoint, wait, attempt+1)
                _time.sleep(wait)
                continue
            if resp.status_code != 200:
                logger.warning("API HTTP %s: %s", resp.status_code, endpoint)
                return None
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < 2:
                logger.warning("API 超时: %s, %ds后重试(%d/3)", endpoint, 2**attempt, attempt+1)
                _time.sleep(2 ** attempt)
                continue
            logger.warning("API 超时(3次): %s", endpoint)
            return None
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                _time.sleep(2 ** attempt)
                continue
            logger.warning("API 请求失败(3次) %s: %s", endpoint, e)
            return None
        except json.JSONDecodeError as e:
            logger.warning("API 响应异常 %s: %s", endpoint, e)
            return None


# ─── 比分获取 (getMatchDetail) ────────────────────────────────
# BB 比分藏在 nsg 字段: pe=全场period + tyg=5(比分) → sc=[主队, 客队]
# tyg 语义: 5=比分, 6=角球, 7=加时/点球 (见 settlement-clv 记忆)
# 各运动"全场" period 码 (与 SPORT_PERIODS 的 ft 一致; 足球=1000 为让球盘, 即全场):
SCORE_PE_BY_SID = {
    1: 1000,   # 足球 全场
    3: 3001,   # 篮球 全场
    5: 5000,   # 网球 全场盘分 (pe=5000 tyg=5 = [主队盘数, 客队盘数], 2026-08-26 实测)
    6: 6001,   # 美式足球 全场 (mg 结构实测 pe=6001 = 全场盘口)
    7: 7001,   # 棒球 全场 (mg 结构实测 pe=7001 = 全场盘口)
}

# V5.10: 半场比分 period 码。实测 getMatchDetail 的 nsg 里足球有
#   pe=1000/1001 全场, pe=1002 上半场, pe=1003 下半场 (tyg=5 为比分)
# 且满足 HT + 2H == FT (5 场抽样 4 场自洽, 1 场异常 → 取值时做自校验)。
# 此前半场比分一直没被提取, 导致 ht/ht_dc 盘口全部被无脑判 void ——
# 实测 133 笔 void 里 60 笔(45%)是这么来的, 白白丢掉 ¥7,838 下注额的结算。
HT_SCORE_PE_BY_SID = {
    1: 1002,   # 足球 上半场
}
SECOND_HALF_PE_BY_SID = {
    1: 1003,   # 足球 下半场 (仅用于自校验 HT+2H==FT)
}

_SID_TO_SPORT_KEY = {sid: sk for sid, sk, _cn in SPORTS}

# 比赛状态码 ms → 标签 (实测: 4=未开赛[bt在未来,无nsg], 5=进行中[bt在过去,有nsg+sb])
# 完赛码未实测到(探测窗口内无完赛样本), 3/6 为推测, 待完赛样本确认
MATCH_STATUS_LABELS = {
    4: "not_started",
    5: "live",
    3: "finished",   # 推测, 未验证
    6: "finished",   # 推测, 未验证
}


def fetch_bb_match_result(match_id, language_type="EN"):
    """用 /v1/match/getMatchDetail 拿单场比赛比分 (棒球/美足等 ESPN 覆盖不到的联赛)。

    端点: POST {api_base}/v1/match/getMatchDetail, body {"matchId": id, "languageType": "EN"}
    比分路径: data.nsg[] 中 pe=全场period + tyg=5 的条目 → sc=[主队, 客队]

    Args:
        match_id: BB 比赛 id (getList 记录的 `id` 字段, 如 4856615)
        language_type: "EN"(英文队名) 或 "CMN"(中文队名)

    Returns:
        {
            "id", "sport", "home", "away",
            "home_score", "away_score",  # int; 无比分时 None
            "status",   # "finished"/"live"/"not_started"/"ms_N"
            "ms",       # 原始状态码
            "completed",# status == "finished"
        }
        或 None (比赛不存在/接口失败)。
    """
    resp = api_post("/v1/match/getMatchDetail",
                    {"matchId": match_id, "languageType": language_type},
                    platform="BB")
    if not resp or not resp.get("success"):
        return None
    data = resp.get("data") or {}
    sid = data.get("sid")
    teams = data.get("ts", [])
    home = teams[0].get("na", "") if teams else ""
    away = teams[1].get("na", "") if len(teams) > 1 else ""

    home_score = away_score = None
    pe_full = SCORE_PE_BY_SID.get(sid)

    def _score_at(pe, tyg=5):
        for sg in data.get("nsg", []):
            if sg.get("pe") == pe and sg.get("tyg") == tyg:
                sc = sg.get("sc", [])
                if len(sc) >= 2:
                    try:
                        return int(sc[0]), int(sc[1])
                    except (ValueError, TypeError):
                        return None
                return None
        return None

    if pe_full:
        full = _score_at(pe_full)
        if full:
            home_score, away_score = full

    # 网球: 额外取总局数(让盘/大小盘口用, 线是局数不是盘数)。pe=5001 tyg=5556 = [主局数, 客局数]
    # (2026-08-26 实测 16:15/13:9)。独赢仍用盘分(home_score/away_score)。
    games_home = games_away = None
    if sid == 5 and home_score is not None:
        g = _score_at(5001, tyg=5556)
        if g:
            games_home, games_away = g

    # V5.10: 顺带取半场比分, 带自校验 —— HT + 2H 必须等于 FT, 对不上说明这场的
    # period 语义异常(实测确有此类样本), 宁可不给也不能拿可疑比分去结算。
    ht_home = ht_away = None
    pe_ht = HT_SCORE_PE_BY_SID.get(sid)
    pe_2h = SECOND_HALF_PE_BY_SID.get(sid)
    if pe_ht and home_score is not None:
        ht = _score_at(pe_ht)
        h2 = _score_at(pe_2h) if pe_2h else None
        if ht and h2 and ht[0] + h2[0] == home_score and ht[1] + h2[1] == away_score:
            ht_home, ht_away = ht

    ms = data.get("ms")
    status = MATCH_STATUS_LABELS.get(ms, f"ms_{ms}")
    # 完赛判定(2026-08-26 收严后改): 完赛码 0/3/6/7 各不相同, 枚举总漏 —— ms=0 曾死4天
    # (8/15→8/19 链路全失效), 现在 ms=7 又被漏(Scotland Challenge Cup 5390765 比分1:0、
    # ms=7 却结算不了, 触发静默失效告警)。改为以"有最终比分"为准:
    #   home+away 都非 None 且 不是未开赛(4)/进行中(5) = 完赛。
    # 这样 0/3/6/7 全都能结算, 且仍保守(无比分 或 live/未开赛 不会误判)。
    if home_score is not None and away_score is not None and ms not in (4, 5):
        status = "finished"
    return {
        "id": match_id,
        "sport": _SID_TO_SPORT_KEY.get(sid, ""),
        "home": home,
        "away": away,
        "home_score": home_score,
        "away_score": away_score,
        # V5.10: 半场比分(通过 HT+2H==FT 自校验才给值, 否则为 None)
        "ht_home_score": ht_home,
        "ht_away_score": ht_away,
        # 网球: 总局数(让盘/大小判定用, 线是局数)
        "games_home": games_home,
        "games_away": games_away,
        "status": status,
        "ms": ms,
        "completed": status == "finished",
    }


# ─── 提取函数 ─────────────────────────────────────────────────

_CN_CACHE_FILE = DATA_DIR / "team_cn_cache.json"
_LEAGUE_EN_TO_CN_FILE = DATA_DIR / "league_en_to_cn.json"
_league_en_to_cn_cache = None


def _load_league_en_to_cn():
    global _league_en_to_cn_cache
    if _league_en_to_cn_cache is None:
        try:
            _league_en_to_cn_cache = json.loads(_LEAGUE_EN_TO_CN_FILE.read_text())
        except Exception:
            _league_en_to_cn_cache = {}
    return _league_en_to_cn_cache
_cn_cache = None
_cn_cache_lock = __import__('threading').Lock()  # V5.5: 并行拉取时保护缓存


def _load_cn_cache():
    global _cn_cache
    if _cn_cache is None:
        with _cn_cache_lock:
            if _cn_cache is None:
                try:
                    _cn_cache = json.loads(_CN_CACHE_FILE.read_text())
                except Exception:
                    _cn_cache = {}
    return _cn_cache


def _save_cn_cache():
    if _cn_cache is not None:
        with _cn_cache_lock:
            try:
                tmp = _CN_CACHE_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(_cn_cache, ensure_ascii=False))
                tmp.replace(_CN_CACHE_FILE)
            except Exception:
                pass


def fetch_sport(sport_id, platform="BB", page_size=100):
    """获取一个运动的所有比赛（含分页）。

    type=2 表示早盘/未来72小时，type=3 只返回当天少量比赛。

    V5.5: 双语言 — EN 用于匹配 Pinnacle(英文队名), CMN 用于展示(中文界面)。
    中文名按 team_id/league_id 缓存到磁盘, 只在有新球队时补拉 CMN, 否则单倍速。
    """
    # V5.9: 网球(sportId=5)的 ATP/WTA 正赛(辛辛那提/Masters/大满贯)在 type=4, ITF/Challenger 在 type=2。
    # 只拉 type=2 会漏掉正赛 → 网球拉 (2,4) 合并, 其他运动保持 type=2(72h窗口)。
    _types = (2, 4) if sport_id == 5 else (2,)

    def _fetch_pages(lang):
        recs = []
        seen = set()
        for typ in _types:
            page = 1
            while True:
                params = {
                    "sportId": sport_id,
                    "type": typ,
                    "current": page,
                    "pageSize": page_size,
                    "isPC": True,
                    "languageType": lang,
                }
                resp = api_post("/v1/match/getList", params, platform=platform)
                if not resp or not resp.get("success"):
                    logger.warning("API 返回空 (type=%d, page=%d, lang=%s)", typ, page, lang)
                    break
                data = resp.get("data", {})
                records = data.get("records", [])
                total = data.get("total", 0)
                pages = data.get("pageTotal", 1)
                for rec in records:
                    mid = rec.get("id")
                    if mid and mid in seen:
                        continue
                    if mid:
                        seen.add(mid)
                    recs.append(rec)
                print(f"    type={typ} 第{page}/{pages} 页: {len(records)} 条 (累计 {len(recs)}/{total}, {lang})")
                if page >= pages:
                    break
                page += 1
        return recs

    cache = _load_cn_cache()

    # 英文记录(用于匹配 Pinnacle)
    en_records = _fetch_pages("EN")

    # 检查缓存是否缺失(新球队/新联赛)
    need_cn = False
    for rec in en_records:
        for t in rec.get("ts", []):
            tid = t.get("id")
            if tid and f"t_{tid}" not in cache:
                need_cn = True
                break
        if not need_cn:
            lid = (rec.get("lg") or {}).get("id")
            if lid and f"l_{lid}" not in cache:
                need_cn = True
        if need_cn:
            break

    # 有新球队/联赛时才补拉 CMN(全量, 顺便刷新缓存)
    if need_cn:
        cn_records = _fetch_pages("CMN")
        for rec in cn_records:
            for t in rec.get("ts", []):
                tid = t.get("id")
                if tid:
                    cache[f"t_{tid}"] = t.get("na", "")
            lg = rec.get("lg") or {}
            if lg.get("id"):
                cache[f"l_{lg.get('id')}"] = lg.get("na", "")
        _save_cn_cache()
    else:
        print("    ♻️ 中文名缓存命中, 跳过 CMN 拉取")

    # 从缓存 enrich 中文名
    for rec in en_records:
        teams = rec.get("ts", [])
        rec["_cn_home"] = cache.get(f"t_{teams[0].get('id')}", "") if teams else ""
        rec["_cn_away"] = cache.get(f"t_{teams[1].get('id')}", "") if len(teams) > 1 else ""
        rec["_cn_league"] = cache.get(f"l_{(rec.get('lg') or {}).get('id')}", "")
    return en_records


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


def _fix_af_ou_hc(period_dict, sport_key):
    """V5: 美式足球 OU/HC 数据交换 — FB API mty=6002(让分)/6003(大小) 数据错位。
    让分线>20(如37.5)显然是大小球线，此时交换 OU 和 HC。
    period_dict: ft_dict, ht_dict, or sh_dict (会被原地修改)。
    """
    if sport_key != "american_football":
        return
    hc = period_dict.get("handicap", {})
    ou = period_dict.get("total", {})
    if not isinstance(hc, dict) or not isinstance(ou, dict):
        return
    hc_line = hc.get("home_line")
    ou_line = ou.get("line")
    if hc_line is None:
        return
    # >20绝对是大小球线 (NFL常规总分~40-50, 让分通常<20)
    hc_is_ou = abs(float(hc_line)) > 20
    ou_is_broken = ou_line is None
    if hc_is_ou and ou_is_broken:
        period_dict["total"] = {
            "line": abs(float(hc_line)),
            "line_str": hc.get("home_line_str", ""),
            "over_odds": hc.get("home_odds", 0),
            "under_odds": hc.get("away_odds", 0),
        }
        period_dict["handicap"] = {}  # 清空: 无法从FB获取真正的让分


def extract_match_odds(record, sport_key, platform="BB"):
    """从 API 记录中提取结构化赔率数据。"""
    sport_id = record.get("sid")
    home, away = _get_match_teams(record)
    league = record.get("lg", {}).get("na", "")

    result = {
        "home": home,
        "away": away,
        "league": league,
        # V5.5: 中文名(展示用) — 从双语言拉取的 _cn_* 字段取
        "home_cn": record.get("_cn_home", ""),
        "away_cn": record.get("_cn_away", ""),
        "league_cn": record.get("_cn_league", "") or _load_league_en_to_cn().get(league, ""),
        "sport": sport_key,
        "platform": platform,
        "sport_cn": {"football": "足球", "basketball": "篮球",
                     "tennis": "网球", "baseball": "棒球",
                     "american_football": "美式足球",
                     "pingpong": "乒乓球", "boxing": "拳击",
                     "mma": "MMA", "badminton": "羽毛球",
                     "ice_hockey": "冰球", "volleyball": "排球"}.get(sport_key, ""),
        "id": record.get("id"),
        "bt": record.get("bt"),
        "nm": record.get("nm", ""),
        "odds_ft": {},
        "odds_ht": {},
        "odds_sh": {},
        "_bb_view": "main",
        "_bb_source": "api",
    }

    mt = MARKET_TYPES.get(sport_id, {})
    periods = SPORT_PERIODS.get(sport_id, {"ft": 1001, "ht": 1002})
    ft_period = periods.get("ft", 1001)
    ht_period = periods.get("ht")
    sh_period = periods.get("2h")
    f5_period = periods.get("f5")  # baseball first 5 innings

    # ─── FT ──────────────────────────────────────────────

    def _extract_ml(period, mty_code=None):
        if mty_code is None:
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
        # 3-way 缺平局时不能伪造(旧代码把客队赔率复制成平局), 直接返回 None
        return result if len(result) == 3 else None

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

            # 优先用 na 字段（队名）确定主客，na 为空则 fallback 到 ty 字段
            cb_home, cb_away = _get_match_teams(record)
            assigned_home = assigned_away = None
            for op in ops:
                op_na = (op.get("na", "") or "").strip()
                if cb_home and op_na == cb_home:
                    assigned_home = op
                elif cb_away and op_na == cb_away:
                    assigned_away = op
            if not assigned_home or not assigned_away:
                # na 不包含队名，回退 ty 方案
                assigned_home = assigned_away = None
                for op in ops:
                    ty = op.get("ty", 0)
                    if ty == 1:
                        assigned_home = op
                    elif ty == 2:
                        assigned_away = op
                if not assigned_home or not assigned_away:
                    assigned_home, assigned_away = ops[0], ops[1]

            home_op = assigned_home
            away_op = assigned_away
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
        mty_codes = mt.get("ou")
        if not mty_codes:
            return None
        if not isinstance(mty_codes, list):
            mty_codes = [mty_codes]

        lines = []
        for mty_code in mty_codes:
            group = _find_market_group(record, mty_code, period)
            if not group:
                continue
            markets = group.get("mks", group.get("markets", []))
            if not markets:
                continue
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
            elif od <= 0:
                # V5.9: od=-999 = 该选项关闭/不开放 (如 FB 的 DC「主/和局」在强热门时关闭),
                # 保留 0 占位, 否则 len<3 导致整个 DC 盘口被丢弃 (FB DC 4.91 因此漏推)。
                odds_list.append(0.0)
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

    def _extract_oe(period):
        """Extract Odd/Even (单/双) from mty=1008."""
        mty_code = mt.get("oe")
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
        odd_op = even_op = None
        for op in ops:
            na = (op.get("na", "") or "").strip()
            od = float(op.get("od", 0))
            if od <= 0:
                continue
            if na in ("单", "Odd", "odd"):
                odd_op = od
            elif na in ("双", "Even", "even"):
                even_op = od
            elif odd_op is None:
                odd_op = od
            elif even_op is None:
                even_op = od
        if odd_op and even_op:
            return {"odd_odds": odd_op, "even_odds": even_op}
        return None

    def _extract_htft(period):
        """Extract Half-Time/Full-Time (半全场) from mty=1033.

        Returns dict with 9 keys: home/home, home/draw, home/away,
        draw/home, draw/draw, draw/away, away/home, away/draw, away/away
        """
        mty_code = mt.get("htft")
        if not mty_code:
            return None
        group = _find_market_group(record, mty_code, period)
        if not group:
            return None
        markets = group.get("mks", group.get("markets", []))
        if not markets:
            return None
        ops = _get_market_options(markets[0])
        if len(ops) < 9:
            return None

        # 9 options in order: 主/主, 主/和, 主/客, 和/主, 和/和, 和/客, 客/主, 客/和, 客/客
        keys = ["home/home", "home/draw", "home/away",
                "draw/home", "draw/draw", "draw/away",
                "away/home", "away/draw", "away/away"]
        result = {}
        for i, op in enumerate(ops):
            if i >= len(keys):
                break
            od = float(op.get("od", 0))
            if od > 1:
                result[keys[i]] = od
        if len(result) >= 9:
            return result
        return None

    def _extract_corner_ml(period):
        """Extract Corner 1X2 (角球独赢) from mty=1009."""
        mty_code = mt.get("corner_ml")
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
        return [float(op.get("od", 0)) for op in ops[:3] if float(op.get("od", 0)) > 1]

    def _extract_corner_hc(period):
        """Extract Corner Handicap (角球让球) from mty=1011."""
        mty_code = mt.get("corner_hc")
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
            home_op, away_op = ops[0], ops[1]
            home_odds = float(home_op.get("od", 0))
            away_odds = float(away_op.get("od", 0))
            if home_odds <= 0 or away_odds <= 0:
                continue
            lines.append({
                "home_line_str": home_op.get("nm", ""),
                "away_line_str": away_op.get("nm", ""),
                "home_odds": home_odds,
                "away_odds": away_odds,
            })
        if not lines:
            return None
        # 主线=最接近0的线(角球让球主线最平衡)。BB API 市场顺序不稳定, 直接 lines[0]
        # 会把备用线(如 +1)当主线, 与 Pinnacle 主线错位 → 幻影 EV。改为按 |线| 排序。
        from src.scrapers.bb_data import parse_asian_line as _pal
        def _line_abs(l):
            v = _pal(l.get("home_line_str") or l.get("away_line_str") or "")
            return abs(v) if v is not None else 999.0
        lines.sort(key=_line_abs)
        return {"primary": lines[0], "alternates": lines[1:]}

    def _extract_corner_ou(period):
        """Extract Corner Over/Under (角球大小) from mty=1010."""
        mty_code = mt.get("corner_ou")
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
            over_op, under_op = ops[0], ops[1]
            over_odds = float(over_op.get("od", 0))
            under_odds = float(under_op.get("od", 0))
            if over_odds <= 0 or under_odds <= 0:
                continue
            # Use li field for line value (e.g. "9", "9.5"), fallback to nm parsing
            line_val = None
            li_raw = mk.get("li") or over_op.get("li")
            if li_raw is not None:
                try:
                    line_val = float(li_raw)
                except (ValueError, TypeError):
                    pass
            line_str = over_op.get("nm", "")
            if line_val is None and line_str:
                # Try Chinese format "大 9" or English format "o 9"
                for prefix in ("大 ", "o ", "O ", "ov "):
                    if line_str.lower().startswith(prefix):
                        try:
                            line_val = float(line_str[len(prefix):])
                        except ValueError:
                            pass
                        break
            lines.append({
                "line": line_val,
                "line_str": line_str,
                "over_odds": over_odds,
                "under_odds": under_odds,
            })
        if not lines:
            return None
        # 主线 = 大小赔率最平衡(|over-under| 最小)的线。BB API 市场顺序不稳定, 直接 lines[0]
        # 可能把备用线(如 8.5)当主线, 与 Pinnacle 主线(9.0)错位 → 线值错配杀光机会。
        # 改为按 |over_odds-under_odds| 排序取最平衡的当主线(与 _extract_corner_hc 按 |线| 排序同理)。
        lines.sort(key=lambda l: abs(l.get("over_odds", 0) - l.get("under_odds", 0)))
        return {"primary": lines[0], "alternates": lines[1:]}

    def _extract_special_market(mty_code, period):
        """提取特殊盘口(正确比分/净胜球/总进球区间/先进球), 返回 [{name, odds}]。

        mty=1188 正确比分: 38个market, 每个1个比分选项(如"3-7");
        其它(1018/1101/1019): 1个market, 多个选项。
        """
        if not mty_code:
            return None
        group = _find_market_group(record, mty_code, period)
        if not group:
            return None
        markets = group.get("mks", group.get("markets", []))
        if not markets:
            return None
        result = []
        for mk in markets:
            for op in _get_market_options(mk):
                name = (op.get("na", "") or op.get("nm", "") or "").strip()
                try:
                    odds = float(op.get("od", 0))
                except (TypeError, ValueError):
                    odds = 0.0
                if name and odds > 1.0:
                    result.append({"name": name, "odds": odds})
        return result or None

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
        ft_oe = _extract_oe(ft_period)
        if ft_oe:
            ft_dict["oe"] = ft_oe
        ft_htft = _extract_htft(ft_period)
        if ft_htft:
            ft_dict["htft"] = ft_htft

        # 角球市场
        ft_corner_ml = _extract_corner_ml(ft_period)
        if ft_corner_ml:
            ft_dict["corner_ml"] = ft_corner_ml
        ft_corner_hc = _extract_corner_hc(ft_period)
        if ft_corner_hc:
            ft_dict["corner_hc"] = ft_corner_hc["primary"]
            ft_dict["alternate_corner_hc"] = ft_corner_hc["alternates"]
        ft_corner_ou = _extract_corner_ou(ft_period)
        if ft_corner_ou:
            ft_dict["corner_ou"] = ft_corner_ou["primary"]
            ft_dict["alternate_corner_ou"] = ft_corner_ou["alternates"]
        # V5.5: 特殊盘口 (正确比分/净胜球/总进球区间/先进球) — Pinnacle special 有对应
        for _key, _mty in [("correct_score", mt.get("correct_score")),
                           ("winning_margin", mt.get("winning_margin")),
                           ("total_goals_range", mt.get("total_goals_range")),
                           ("first_to_score", mt.get("first_to_score"))]:
            _spec = _extract_special_market(_mty, ft_period)
            if _spec:
                ft_dict[_key] = _spec

    # V5: 美式足球 OU/HC 数据交换 — FB API mty=6002/6003 数据错位
    # 让分线>20(如37.5)显然是大小球 → 交换 OU 和 HC
    _fix_af_ou_hc(ft_dict, sport_key)
    result["odds_ft"] = ft_dict

    # HT
    ht_ml = _extract_ml(ht_period, mt.get("ht_ml")) if ht_period else None
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

            ht_btts = _extract_btts(ht_period)
            if ht_btts:
                ht_dict["btts"] = ht_btts
            ht_oe = _extract_oe(ht_period)
            if ht_oe:
                ht_dict["oe"] = ht_oe

            # 角球 HT
            ht_corner_ml = _extract_corner_ml(ht_period)
            if ht_corner_ml:
                ht_dict["corner_ml"] = ht_corner_ml
            ht_corner_hc = _extract_corner_hc(ht_period)
            if ht_corner_hc:
                ht_dict["corner_hc"] = ht_corner_hc["primary"]
                ht_dict["alternate_corner_hc"] = ht_corner_hc["alternates"]
            ht_corner_ou = _extract_corner_ou(ht_period)
            if ht_corner_ou:
                ht_dict["corner_ou"] = ht_corner_ou["primary"]
                ht_dict["alternate_corner_ou"] = ht_corner_ou["alternates"]

            # V5.11: 半场特殊盘口(正确比分上半场/精确进球上半场) — 对应 Pinnacle
            # "Correct Score 1st Half" / "Exact Total Goals 1st Half"
            for _key, _mty in [("correct_score_ht", mt.get("correct_score_ht")),
                               ("exact_goals_ht", mt.get("exact_goals_ht"))]:
                _spec = _extract_special_market(_mty, ht_period)
                if _spec:
                    ht_dict[_key] = _spec

    # V5: 美式足球 OU/HC 交换
    _fix_af_ou_hc(ht_dict, sport_key)
    result["odds_ht"] = ht_dict

    # SH (Second Half / 下半场)
    if sh_period:
        sh_ml = _extract_ml(sh_period)
        sh_hc = _extract_handicap(sh_period)
        sh_ou = _extract_ou(sh_period)

        sh_dict = {}
        if sh_ml:
            sh_dict["ml"] = sh_ml
        if sh_hc:
            sh_dict["handicap"] = sh_hc["primary"]
            sh_dict["alternate_handicaps"] = sh_hc["alternates"]
        if sh_ou:
            sh_dict["total"] = sh_ou["primary"]
            sh_dict["alternate_totals"] = sh_ou["alternates"]

        if sport_key == "football":
            sh_dc = _extract_dc(sh_period)
            if sh_dc:
                sh_dict["dc"] = sh_dc
            sh_oe = _extract_oe(sh_period)
            if sh_oe:
                sh_dict["oe"] = sh_oe

        # V5: 美式足球 OU/HC 交换
        _fix_af_ou_hc(sh_dict, sport_key)
        result["odds_sh"] = sh_dict

    # F5 (First 5 Innings — 棒球)
    if f5_period:
        f5_ou = _extract_ou(f5_period)
        f5_dict = {}
        if f5_ou:
            f5_dict["total"] = f5_ou["primary"]
            f5_dict["alternate_totals"] = f5_ou["alternates"]
        result["odds_f5"] = f5_dict

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

    # sources 初始标记为第一个平台，只有某平台真正有更高赔率时才标记为它
    first_plat = platform_matches[0][0]
    sources = {key: first_plat for key in ["ml", "handicap", "ou", "dnb", "dc"]}
    # 逐方向(主/和/客)的来源 —— 之前 sources["ml"] 是整场单值, FB 的主/和高就整场标 FB,
    # 导致"客胜其实是 BB 更高、却被标成 FB价"的错标(2026-08-23 用户反馈)。
    sources["ml_dir"] = None
    sources["ht_ml_dir"] = None

    def _update_source(market_key, base_val, plat_val, platform):
        """当 base_val < plat_val 时才更新 source 为指定平台，否则不变。"""
        if base_val < plat_val:
            sources[market_key] = platform

    # 遍历其他平台，取各市场最高赔率
    for platform, m in platform_matches[1:]:
        # ML: 逐元素取最大（差异>25%视为不同比赛,不合并）
        base_ml = base.get("odds_ft", {}).get("ml", [])
        plat_ml = m.get("odds_ft", {}).get("ml", [])
        if plat_ml and len(plat_ml) >= len(base_ml):
            if sources["ml_dir"] is None or len(sources["ml_dir"]) != len(base_ml):
                sources["ml_dir"] = [first_plat] * len(base_ml)
            for i in range(min(len(base_ml), len(plat_ml))):
                if plat_ml[i] > base_ml[i]:
                    # 安全校验: 同场比赛不同平台赔率差异不应>25%
                    if base_ml[i] > 0 and plat_ml[i] / base_ml[i] < 1.25:
                        base_ml[i] = plat_ml[i]
                        sources["ml"] = platform
                        sources["ml_dir"][i] = platform
            if len(plat_ml) > len(base_ml):
                base["odds_ft"]["ml"] = plat_ml
                sources["ml"] = platform

        # Handicap: 取主客赔率最大值（前提是线一致）
        # V5.5: 0.1 → 0.01, BB quarter线(-1/1.5)与FB整球线(-1)不能合并
        base_hc = base.get("odds_ft", {}).get("handicap")
        plat_hc = m.get("odds_ft", {}).get("handicap")
        if base_hc and plat_hc and isinstance(base_hc, dict) and isinstance(plat_hc, dict):
            base_line = base_hc.get("home_line") or base_hc.get("away_line")
            plat_line = plat_hc.get("home_line") or plat_hc.get("away_line")
            if base_line is None or plat_line is None or abs(base_line - plat_line) <= 0.01:
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

        # OU: 取大小盘赔率最大值（前提是线一致）
        # V5.5: 0.5 → 0.01, BB 2.75 vs FB 3.0 是不同盘口线, 不能合并
        base_ou = base.get("odds_ft", {}).get("total")
        plat_ou = m.get("odds_ft", {}).get("total")
        if base_ou and plat_ou and isinstance(base_ou, dict) and isinstance(plat_ou, dict):
            base_line = base_ou.get("line")
            plat_line = plat_ou.get("line")
            if base_line is None or plat_line is None or abs(base_line - plat_line) <= 0.01:
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
                if sources["ht_ml_dir"] is None or len(sources["ht_ml_dir"]) != len(base_ht_ml):
                    sources["ht_ml_dir"] = [first_plat] * len(base_ht_ml)
                for i in range(min(len(base_ht_ml), len(plat_ht_ml))):
                    if plat_ht_ml[i] > base_ht_ml[i]:
                        base_ht_ml[i] = plat_ht_ml[i]
                        sources["ht_ml_dir"][i] = platform
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

            # ── odds_sh 跨平台合并 ──
            base_sh = base.get("odds_sh", {})
            plat_sh = m.get("odds_sh", {})
            if plat_sh:
                # SH ML
                base_sh_ml = base_sh.get("ml", [])
                plat_sh_ml = plat_sh.get("ml", [])
                if plat_sh_ml and len(plat_sh_ml) >= len(base_sh_ml):
                    for i in range(min(len(base_sh_ml), len(plat_sh_ml))):
                        if plat_sh_ml[i] > base_sh_ml[i]:
                            base_sh_ml[i] = plat_sh_ml[i]
                    if len(plat_sh_ml) > len(base_sh_ml):
                        base_sh["ml"] = plat_sh_ml

                # SH Handicap
                base_sh_hc = base_sh.get("handicap")
                plat_sh_hc = plat_sh.get("handicap")
                if base_sh_hc and plat_sh_hc and isinstance(base_sh_hc, dict) and isinstance(plat_sh_hc, dict):
                    bl = base_sh_hc.get("home_line") or base_sh_hc.get("away_line")
                    pl = plat_sh_hc.get("home_line") or plat_sh_hc.get("away_line")
                    if bl is None or pl is None or abs(bl - pl) <= 0.1:
                        if plat_sh_hc.get("home_odds", 0) > base_sh_hc.get("home_odds", 0):
                            base_sh_hc["home_odds"] = plat_sh_hc["home_odds"]
                        if plat_sh_hc.get("away_odds", 0) > base_sh_hc.get("away_odds", 0):
                            base_sh_hc["away_odds"] = plat_sh_hc["away_odds"]
                elif not base_sh_hc and plat_sh_hc:
                    base_sh["handicap"] = plat_sh_hc

                # SH Total (OU)
                base_sh_ou = base_sh.get("total")
                plat_sh_ou = plat_sh.get("total")
                if base_sh_ou and plat_sh_ou and isinstance(base_sh_ou, dict) and isinstance(plat_sh_ou, dict):
                    bl = base_sh_ou.get("line")
                    pl = plat_sh_ou.get("line")
                    if bl is None or pl is None or abs(bl - pl) <= 0.5:
                        if plat_sh_ou.get("over_odds", 0) > base_sh_ou.get("over_odds", 0):
                            base_sh_ou["over_odds"] = plat_sh_ou["over_odds"]
                        if plat_sh_ou.get("under_odds", 0) > base_sh_ou.get("under_odds", 0):
                            base_sh_ou["under_odds"] = plat_sh_ou["under_odds"]
                elif not base_sh_ou and plat_sh_ou:
                    base_sh["total"] = plat_sh_ou

                # SH DC
                if plat_sh.get("dc"):
                    base_sh_dc = base_sh.get("dc", [])
                    plat_sh_dc = plat_sh.get("dc", [])
                    if not base_sh_dc or any(plat_sh_dc[i] > base_sh_dc[i] for i in range(min(len(base_sh_dc), len(plat_sh_dc)))):
                        if len(plat_sh_dc) >= len(base_sh_dc):
                            base_sh["dc"] = plat_sh_dc

    # ── FT/HC 备用让球盘（alternate_handicaps）跨平台合并 ──
    def _merge_alternates(base_alts, plat_alts, line_key, odds_keys):
        """合并备用盘口列表，同线取最高赔率。

        核心原则：只合并两个平台都存在的线，不添加对方平台独有的线。
        因为用户只在 BB/FB APP 上下注，不在的线推了也没意义。
        """
        if not plat_alts or not base_alts:
            return base_alts or []
        result = list(base_alts)
        for pa in plat_alts:
            pa_line = pa.get(line_key)
            if pa_line is None:
                continue
            for ba in result:
                ba_line = ba.get(line_key)
                if ba_line is not None and abs(ba_line - pa_line) <= 0.05:
                    for ok in odds_keys:
                        if pa.get(ok, 0) > ba.get(ok, 0):
                            ba[ok] = pa[ok]
                    break
        return result

    for period in ("odds_ft", "odds_ht", "odds_sh", "odds_f5"):
        base_period = base.get(period, {})
        plat_period = m.get(period, {})
        if not plat_period:
            continue
        base_alts = base_period.get("alternate_handicaps", [])
        plat_alts = plat_period.get("alternate_handicaps", [])
        merged_hc = _merge_alternates(base_alts, plat_alts, "home_line", ["home_odds", "away_odds"])
        if merged_hc is not plat_alts:
            base_period["alternate_handicaps"] = merged_hc
        base_alts = base_period.get("alternate_totals", [])
        plat_alts = plat_period.get("alternate_totals", [])
        merged_ou = _merge_alternates(base_alts, plat_alts, "line", ["over_odds", "under_odds"])
        if merged_ou is not plat_alts:
            base_period["alternate_totals"] = merged_ou

    base["platform"] = "ALL"
    base["platform_sources"] = sources
    return base


def _merge_platform_results(platform_results):
    """合并多个平台的提取结果。

    platform_results: {platform_key: [match_dict, ...]}
    Returns: 合并后的 match 列表
    """
    from collections import OrderedDict

    # 加载队名映射表，归一化 home/away 名称使不同平台同一球队能合并
    _team_map = {}
    _tm_path = DATA_DIR / "team_name_map.json"
    if _tm_path.exists():
        _team_map = json.loads(_tm_path.read_text())

    def _norm(name):
        return _team_map.get(name, name)

    groups = OrderedDict()

    for platform, matches in platform_results.items():
        for m in matches:
            key = (_norm(m.get("home", "")), _norm(m.get("away", "")), m.get("league", ""))
            if key not in groups:
                groups[key] = []
            groups[key].append((platform, m))

    merged = []
    for key, platform_matches in groups.items():
        if len(platform_matches) == 1:
            only_platform = platform_matches[0][0]
            # FB-only 场次(BB 不覆盖)不放行: FB 只用于 BB 也覆盖的比赛上"取更高赔率",
            # 不补 BB 没有的比赛(2026-08-24 用户要求)。
            if only_platform == "FB":
                continue
            m = platform_matches[0][1].copy()
            m["platform"] = only_platform
            m["platform_sources"] = {"main": only_platform}
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

    # V5.5: 并行拉取各运动原始记录(网络IO), 提取结构化保持串行(避免打印错乱)
    def _fetch_records(sid, sc):
        return sc, fetch_sport(sid, platform=platform_key)

    _sport_records = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as _exec:
        _futs = {_exec.submit(_fetch_records, sid, sc): sc for sid, sk, sc in SPORTS}
        for _fut in concurrent.futures.as_completed(_futs):
            _sc, _recs = _fut.result()
            _sport_records[_sc] = _recs

    for sport_id, sport_key, sport_cn in SPORTS:
        records = _sport_records.get(sport_cn, [])
        if not records:
            print(f"--- {sport_cn} (sportId={sport_id}): ⚠️ 无数据")
            continue

        matches = []
        _banned_skipped = 0
        for rec in records:
            m = extract_match_odds(rec, sport_key, platform=platform_key)
            if m["home"] and m["away"]:
                if _is_banned_league(m.get("league", "")) or _is_banned_league(m.get("league_cn", "")):
                    _banned_skipped += 1
                    continue
                matches.append(m)

        sport_counts[sport_cn] = len(matches)
        platform_matches.extend(matches)
        print(f"--- {sport_cn}: {len(matches)} 场", end="")
        if _banned_skipped:
            print(f" (🚫剔除{_banned_skipped}场不靠谱联赛)", end="")
        print()

    print(f"\n  → {platform_config['label']} 合计: {len(platform_matches)} 场比赛")
    for name, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
        print(f"    {name}: {count}")

    return platform_key, platform_matches


def fetch_all_sports(with_fb=False):
    """获取所有运动在所有平台的比赛数据并结构化。

    with_fb: 是否同时提取 FB 平台（默认 False，因为 BB 赔率通常更优）。

    使用多线程并行提取各平台数据，然后按比赛合并取最高赔率。
    """
    platforms_to_fetch = ["BB"]
    if with_fb:
        platforms_to_fetch.append("FB")

    all_platform_matches = {}
    total_by_platform = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(platforms_to_fetch)) as executor:
        futures = {executor.submit(_fetch_one_platform, key): key for key in platforms_to_fetch}
        for future in concurrent.futures.as_completed(futures):
            platform_key, platform_matches = future.result()
            total_by_platform[platform_key] = len(platform_matches)
            all_platform_matches[platform_key] = platform_matches

    # 保存各平台原始数据（用于FB独立对比）
    for plat_key, plat_matches in all_platform_matches.items():
        plat_path = DATA_DIR / f"bb_odds_extracted_{plat_key}.json"
        plat_output = {
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S'),
            "source": f"{PLATFORMS[plat_key]['label']} (原始数据)",
            "match_count": len(plat_matches),
            "matches": plat_matches,
        }
        # V5.10 原子写: 直接 write_text 会在并发读时被读到半截文件(JSON解析失败,
        # 实测让 near 扫描 FAILED 89 分钟)。先写 tmp 再 rename, 读端永远读不到半截。
        _tmp = plat_path.with_suffix(".json.tmp")
        _tmp.write_text(json.dumps(plat_output, ensure_ascii=False, default=str))
        _tmp.replace(plat_path)
        print(f"  {PLATFORMS[plat_key]['label']} 原始数据已保存: {plat_path.name} ({len(plat_matches)} 场)")

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
        has_fb = any(m.get("platform") == "ALL" or m.get("platform") == "FB" for m in matches)
        source_label = "BB体育+FB体育 (多平台合并)" if has_fb else "BB体育 (仅BB)"

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
    # V5.10 原子写: 主文件 bb_odds_extracted.json 与分平台文件一致, 都走 tmp+rename。
    # 直接 write 会在增量扫描并发读时被读到半截(JSONDecodeError), 实测让 urgent
    # 扫描间歇 FAILED(2026-08-21 13:55/14:00/15:05 三次)。rename 原子, 读端永读不到半截。
    _tmp = out_path.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    _tmp.replace(out_path)
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

    with_fb = "--with-fb" in sys.argv

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
        # 默认模式：仅 BB（除非 --with-fb）
        label = "BB体育" if not with_fb else "BB体育+FB体育"
        print(f"🔧 模式: {label}" + (" (含FB)" if with_fb else " (仅BB)"))
        matches = fetch_all_sports(with_fb=with_fb)
        if matches:
            save_results(matches)
            print(f"\n提取完成，共 {len(matches)} 场比赛 ({label})")
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
        if sport_key not in PINNACLE_SPORTS:
            print(f"\n--- {sport_cn} (sportId={sport_id}) ⏭️ 跳过 (Pinnacle 无覆盖)")
            continue
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
