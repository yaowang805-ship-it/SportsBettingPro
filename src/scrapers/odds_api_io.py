"""odds-api.io 实时赔率接入 — 替代 Pin 的公平价源(2026-09-18)。

平台: https://api.odds-api.io/v3 (与 oddspapi.io 是两家公司, 别混)。
锚点书商: Sbobet(亚洲盘+置信度) + Betfair Exchange(公平价)。
公平价 = Betfair 中间价(加 back-lay 价差流动性门槛); SBO devig 只做置信度确认, 不进定价。
用户只在 BB 投注, Betfair 只当参考尺子, 不扣 Betfair 佣金。

盘口映射(BB 子盘口 → odds-api.io 市场名):
  1x2(全场独赢) → ML
  ht(上半场独赢) → ML HT          ← 早盘 ht 主胜 的核心(Betfair有, Sbobet无)
  hc(让球)       → Spread
  ou(大小)       → Totals
  ht_ou(上半大小) → Totals HT
  dc(双机会)     → Double Chance
  btts(双边进球) → Both Teams To Score
"""
import json
import time
from pathlib import Path

import requests

from config.settings import ODDS_API_IO_KEY, ODDS_API_IO_BASE, ODDS_API_IO_BOOKMAKERS

ROOT = Path(__file__).resolve().parent.parent.parent

# BB 子盘口 → Betfair Exchange 市场名(公平价主锚)
_SUB_TO_BETFAIR = {
    "1x2": "ML",
    "ht": "ML HT",          # 半场独赢(Betfair有)
    "hc": "Spread",
    "ou": "Totals",
    "ht_ou": "Totals HT",
    "dc": "Double Chance",
    "dnb": "Draw No Bet",
    "btts": "Both Teams To Score",
}

# BB 子盘口 → Sbobet 市场名(置信度, Sbobet 无半场独赢/双机会/双边进球)
_SUB_TO_SBOBET = {
    "1x2": "ML",
    "hc": "Spread",
    "ou": "Totals",
    "ht_ou": "Totals HT",
    "ht_hc": "Spread HT",   # 半场让球(Sbobet有, Betfair无对应)
}

# BB 运动 id → odds-api.io 运动 slug
_SPORT_ID_TO_SLUG = {
    1: "football",       # 足球
    3: "basketball",     # 篮球
    5: "tennis",         # 网球
    7: "baseball",       # 棒球
    6: "american-football",  # 美式足球
}


def _get(path, params=None, timeout=15):
    """REST GET, 带 apiKey。失败返回 None。"""
    if not ODDS_API_IO_KEY:
        return None
    p = {"apiKey": ODDS_API_IO_KEY}
    if params:
        p.update(params)
    try:
        r = requests.get(f"{ODDS_API_IO_BASE}{path}", params=p, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def mid_price(back, lay, max_spread_pct=None):
    """交易所中间价: 概率空间 (1/back + 1/lay)/2 的倒数。back/lay 是十进制赔率。

    交易所 back=买入价(结果发生), lay=卖出价(结果不发生)。
    无 margin 公平概率 p = (1/back + 1/lay)/2, 公平价 = 1/p。
    流动性门槛(2026-09-18): back-lay 价差过大 → None(无真实共识, 跳过)。
    阈值按赔率分档(实测健康腿价差 1.6%~6.5%): ≤2.0 用 5%, 2.0~5.0 用 10%, >5.0 用 15%。
    返回十进制公平价。None 或 <=1 或价差过大返回 None。
    """
    try:
        b, l = float(back), float(lay)
    except (TypeError, ValueError):
        return None
    if b <= 1 or l <= 1:
        return None
    spread = (l - b) / b * 100.0
    if max_spread_pct is None:
        # 分档: 高赔腿天然 bid-ask 更宽。实测健康腿价差 1.6%~6.5%(draw/under/客 恒偏宽),
        # 退化市场(如 Spread hdp=-4 home12.50/lay146.01)价差 >100%。10% 是安全分界。
        if b <= 2.0:
            max_spread_pct = 5.0
        elif b <= 5.0:
            max_spread_pct = 10.0
        else:
            max_spread_pct = 15.0
    if spread > max_spread_pct:
        return None
    p = (1.0 / b + 1.0 / l) / 2.0
    return round(1.0 / p, 4) if p > 0 else None


def spread_pct(back, lay):
    """back-lay 价差%(流动性指标, 2026-09-19)。越大=盘越薄=公平价越不可信。"""
    try:
        b, l = float(back), float(lay)
    except (TypeError, ValueError):
        return None
    if b <= 1 or l <= 1:
        return None
    return (l - b) / b * 100.0


def _line_spread(o, sub_market):
    """选中线的各腿 back-lay 价差最大值(流动性门槛用)。dc/dnb 只有 back 无 lay → None。"""
    if sub_market in ("1x2", "ht"):
        legs = [("home", "layHome"), ("draw", "layDraw"), ("away", "layAway")]
    elif sub_market in ("ou", "ht_ou"):
        legs = [("over", "layOver"), ("under", "layUnder")]
    elif sub_market == "btts":
        legs = [("yes", "layYes"), ("no", "layNo")]
    elif sub_market == "hc":
        legs = [("home", "layHome"), ("away", "layAway")]
    else:
        return None  # dc/dnb 等 back-only 盘, 无 lay 价差
    spreads = [s for s in (spread_pct(o.get(bk), o.get(lk)) for bk, lk in legs) if s is not None]
    return max(spreads) if spreads else None


def _fair_three_way(odds_dict):
    """从交易所三路 odds 字典算公平价(中间价+归一化)。

    输入: {"home": back, "draw": back, "away": back, "layHome": lay, "layDraw": lay, "layAway": lay}
    输出: {"home": 公平价, "draw": 公平价, "away": 公平价} 或 None。
    """
    mids = {
        "home": mid_price(odds_dict.get("home"), odds_dict.get("layHome")),
        "draw": mid_price(odds_dict.get("draw"), odds_dict.get("layDraw")),
        "away": mid_price(odds_dict.get("away"), odds_dict.get("layAway")),
    }
    # 交易所中间价本身就是公平价(无 margin), 不归一化(归一化在单 leg 时退化)。
    # 价差过大的 leg 返回 None(该方向不可信)。
    if not any(v is not None for v in mids.values()):
        return None
    return mids


def _fair_two_way(b1, l1, b2, l2, max_imb=0.05):
    """两路盘(让球 home/away、大小 over/under、btts yes/no)中间价 + 归一化。

    两路盘是「两个独立挂单簿」, 中间价隐含概率和天然可能 ≠1(低流动性时两边各自偏离)。
    不归一化会让 home/away 或 over/under 两路公平价都偏低 → 隐含和>1 → BB 两个对立方向
    同时 +EV → 对立下注(白交双边抽水)。修复: 算两路中间价后归一化到隐含和=1;
    隐含和偏离 1 超 max_imb(默认5%)判定数据脏(流动性不足)返回 None。
    返回 (fair1, fair2) 或 None。
    """
    f1 = mid_price(b1, l1)
    f2 = mid_price(b2, l2)
    if not f1 or not f2:
        return None
    p1, p2 = 1.0 / f1, 1.0 / f2
    s = p1 + p2
    if s <= 0 or abs(s - 1.0) > max_imb:
        return None
    p1 /= s
    p2 /= s
    return round(1.0 / p1, 4), round(1.0 / p2, 4)


def _select_line(market, target_line=None):
    """从市场 odds 数组里选一条线(主线, 或匹配 target_line 的那条)。

    主线 = 两侧赔率最平衡(最接近 2.0)的一条(home/away 或 over/under 差最小)。
    target_line 传入时 = |hdp - target_line| 最小的一条(供 BB 让球/大小线对齐)。
    返回 odds dict 或 None。target_line 命中失败时仍返回最接近的一条, 由调用侧校验线。
    """
    arr = market.get("odds") or []
    if not arr:
        return None
    if target_line is not None:
        best, best_diff = None, 1e9
        for o in arr:
            hdp = o.get("hdp")
            if hdp is None:
                continue
            try:
                d = abs(float(hdp) - float(target_line))
            except (TypeError, ValueError):
                continue
            if d < best_diff:
                best, best_diff = o, d
        return best
    # 无 target_line: 单条直接返回(ML/HT/DC/DNB/BTTS 都只有一条); 多条取最平衡(Spread/Totals)
    if len(arr) == 1:
        return arr[0]
    best, best_imb = None, 1e9
    for o in arr:
        a = o.get("home") if o.get("home") is not None else o.get("over")
        b = o.get("away") if o.get("away") is not None else o.get("under")
        if not a or not b:
            continue
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            continue
        imb = abs(a - b)
        if imb < best_imb:
            best, best_imb = o, imb
    return best


def get_sports():
    """运动列表 → [{name, slug}]。"""
    return _get("/sports") or []


def get_events(sport="football", status=None):
    """事件列表。status=None 全部, 'live' 滚球。→ [{id, home, away, date, sport, league, status}]。"""
    params = {"sport": sport}
    if status:
        params["status"] = status
    return _get("/events", params) or []


_odds_cache = {}  # {event_id: (ts, bookmakers_dict)}


def get_odds(event_id, bookmakers=None):
    """单场赔率(含所有市场)。→ {bookmaker_name: [markets]}。

    market 结构: {"name": "ML", "updatedAt": "...", "odds": [{home/draw/away/layHome/...}]}。
    优先读 WebSocket 实时缓存(滚球 live, 见 odds_ws.snapshot), 否则 REST 兜底(早盘 prematch)。
    REST 结果带 8s 缓存(早盘批量对比时避免每场重复拉)。
    """
    bks = bookmakers or ODDS_API_IO_BOOKMAKERS
    # 1) WebSocket 实时缓存优先(同进程: 滚球 live/prematch 实时推送价)
    try:
        from src.scrapers import odds_ws
        ws_snap = odds_ws.snapshot(event_id)
        if ws_snap:
            result = {k: v for k, v in ws_snap.items() if k in bks}
            if result:
                return result
        # 1.5) 落盘文件缓存(跨进程: 早盘 bb_vs_pinnacle 读滚球进程落盘的 WS 缓存)
        file_snap = odds_ws.load_file_cache()
        if file_snap and event_id in file_snap:
            result = {k: v for k, v in file_snap[event_id].items() if k in bks}
            if result:
                return result
    except Exception:
        pass
    # 2) REST 兜底(8s 缓存)
    now = time.time()
    if event_id in _odds_cache and now - _odds_cache[event_id][0] < 8:
        return _odds_cache[event_id][1]
    params = {"eventId": event_id, "bookmakers": ",".join(bks)}
    d = _get("/odds", params)
    result = (d or {}).get("bookmakers", {}) or {}
    _odds_cache[event_id] = (now, result)
    return result


def fair_price(event_id, sub_market, bookmakers=None, target_line=None):
    """提取某场比赛某盘口的 Betfair Exchange 公平价(交易所中间价)。

    公平价只认 Betfair Exchange(唯一能叫公平价的东西: P2P 无庄家抽水), Sbobet 是置信度
    不进定价(见 sbo_fair_price)。
    sub_market:
      '1x2'/'ht' → {home,draw,away}; 'dc' → {1X,12,X2}; 'dnb' → {home,away};
      'hc' → {home,away}+line; 'ou'/'ht_ou' → {over,under}+line; 'btts' → {yes,no}。
    target_line: hc/ou/ht_ou 传 BB 的让球/大小线, 用于在 odds 数组里选对应线(主线默认)。
    返回 dict(含各方向公平价 + line) 或 None。
    """
    market_name = _SUB_TO_BETFAIR.get(sub_market)
    if not market_name:
        return None
    odds = get_odds(event_id, bookmakers)
    if not odds:
        return None
    bf_markets = odds.get("Betfair Exchange")
    if not bf_markets:
        return None
    m = next((x for x in bf_markets if x.get("name") == market_name), None)
    if not m:
        return None
    # 带线的盘口(hc/ou/ht_ou)按 target_line 选线, 其余取主线(唯一一条)
    _line_subs = ("hc", "ou", "ht_ou")
    o = _select_line(m, target_line if sub_market in _line_subs else None)
    if not o:
        return None
    _spread = _line_spread(o, sub_market)  # 2026-09-19 流动性指标(back-lay 价差)
    if sub_market in ("1x2", "ht"):
        fair = _fair_three_way(o)
        if fair is not None:
            fair["spread"] = _spread
        return fair
    if sub_market == "dc":
        # Double Chance: 1X/12/X2, 交易所只给 back 无 lay → back 直接当公平价
        fair = {k: float(v) for k, v in o.items() if k in ("1X", "12", "X2") and v}
        if fair:
            fair["spread"] = _spread
        return fair or None
    if sub_market == "dnb":
        # Draw No Bet: home/away, 无 lay → back 直接当公平价
        h, a = o.get("home"), o.get("away")
        if not h or not a:
            return None
        return {"home": float(h), "away": float(a), "spread": _spread}
    if sub_market == "btts":
        pair = _fair_two_way(o.get("yes"), o.get("layYes"), o.get("no"), o.get("layNo"))
        if pair is None:
            return None
        yes, no = pair
        return {"yes": yes, "no": no, "spread": _spread}
    if sub_market in ("ou", "ht_ou"):
        line = o.get("hdp")
        pair = _fair_two_way(o.get("over"), o.get("layOver"), o.get("under"), o.get("layUnder"))
        if line is None or pair is None:
            return None
        over, under = pair
        return {"over": over, "under": under, "line": line, "spread": _spread}
    if sub_market == "hc":
        line = o.get("hdp")
        pair = _fair_two_way(o.get("home"), o.get("layHome"), o.get("away"), o.get("layAway"))
        if line is None or pair is None:
            return None
        home, away = pair
        return {"home": home, "away": away, "line": line, "spread": _spread}
    return None


def sbo_fair_price(event_id, sub_market, target_line=None):
    """SBO 的 devig 公平价(比例去水), 用于置信度确认(不进入定价, 见 fair-price-betfair-confidence-sbo-20260918)。

    比例去水: 各选项隐含概率 1/odds, 按占比缩放到 100%, 公平价 = total × odds。
    返回 dict(各方向公平价 + line) 或 None。
    """
    market_name = _SUB_TO_SBOBET.get(sub_market)
    if not market_name:
        return None
    odds = get_odds(event_id)
    if not odds:
        return None
    sbo = odds.get("Sbobet")
    if not sbo:
        return None
    m = next((x for x in (sbo or []) if x.get("name") == market_name), None)
    if not m:
        return None
    _line_subs = ("hc", "ou", "ht_ou", "ht_hc")
    o = _select_line(m, target_line if sub_market in _line_subs else None)
    if not o:
        return None

    # 提取各方向赔率
    if sub_market in ("1x2", "ht"):
        vals = {"home": float(o.get("home", 0) or 0), "draw": float(o.get("draw", 0) or 0),
                "away": float(o.get("away", 0) or 0)}
    elif sub_market in ("ou", "ht_ou"):
        vals = {"over": float(o.get("over", 0) or 0), "under": float(o.get("under", 0) or 0)}
    elif sub_market in ("hc", "ht_hc"):
        vals = {"home": float(o.get("home", 0) or 0), "away": float(o.get("away", 0) or 0)}
    else:
        return None

    # 比例去水
    total = sum(1.0 / v for v in vals.values() if v > 1.0)
    if total <= 0:
        return None
    fair = {k: round(total * v, 4) if v > 1.0 else None for k, v in vals.items()}
    if sub_market in ("ou", "ht_ou", "hc", "ht_hc") and o.get("hdp") is not None:
        fair["line"] = o.get("hdp")
    return fair


def match_bb_to_oa(sport_id):
    """BB 运动 id → odds-api.io slug。"""
    return _SPORT_ID_TO_SLUG.get(sport_id)


# ── BB 匹配 + 公平价提供(替代 Pin 的入口) ──

_events_cache = {}  # {(sport_slug, status): (ts, events)}
_match_cache = {}   # {(norm_home, norm_away, sport_id, status): (ts, event_id, swapped)}


def _norm_team(name):
    import re
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


# 女足/青年队/预备队联赛关键词: 队名相似(如 Liverpool FC vs Liverpool LFC 女足)会误配,
# 造出假 +EV。这些 odds-api.io 有、BB 早盘没有, 不是套利目标, 匹配时直接跳过。
_SKIP_LEAGUE_KW = ("women", "ladies", "feminin", "femenin", "feminine", "reserve",
                   "academy", "u19", "u20", "u21")


def _is_skip_event(e):
    """女足/青年队/预备队赛事 → True(匹配时跳过, 防队名相似误配)。"""
    lg = ((e.get("league") or {}).get("name") or "").lower()
    if any(k in lg for k in _SKIP_LEAGUE_KW):
        return True
    # 队名后缀 WFC/LFC/(w) = 女足俱乐部(如 Manchester City WFC / Liverpool LFC)
    for side in ("home", "away"):
        n = (e.get(side) or "").strip().lower()
        if n.endswith(" wfc") or n.endswith(" lfc") or n.endswith(" (w)"):
            return True
    return False


def _match_score(h1, a1, h2, a2):
    """队名模糊匹配得分(0-100)。精确=100, 主客互换=99, rapidfuzz 双向取大。"""
    from rapidfuzz import fuzz
    nh1, na1, nh2, na2 = _norm_team(h1), _norm_team(a1), _norm_team(h2), _norm_team(a2)
    if nh1 == nh2 and na1 == na2:
        return 100.0
    if nh1 == na2 and na1 == nh2:
        return 99.0
    s1 = fuzz.ratio(nh1, nh2) + fuzz.ratio(na1, na2)
    s2 = fuzz.ratio(nh1, na2) + fuzz.ratio(na1, nh2)
    return max(s1, s2) / 2.0


def _match_score_orient(h1, a1, h2, a2):
    """队名匹配 → (得分, swapped)。swapped=True 表示 BB 主客与 odds-api.io 相反。"""
    from rapidfuzz import fuzz
    nh1, na1, nh2, na2 = _norm_team(h1), _norm_team(a1), _norm_team(h2), _norm_team(a2)
    if nh1 == nh2 and na1 == na2:
        return 100.0, False
    if nh1 == na2 and na1 == nh2:
        return 99.0, True
    s1 = fuzz.ratio(nh1, nh2) + fuzz.ratio(na1, na2)
    s2 = fuzz.ratio(nh1, na2) + fuzz.ratio(na1, nh2)
    if s2 > s1:
        return s2 / 2.0, True
    return s1 / 2.0, False


def _get_events_cached(sport, status=None):
    """事件列表缓存 60s, 避免每次匹配都拉全量。status=None 全量, 'live' 只拉滚球。"""
    key = (sport, status)
    now = time.time()
    if key in _events_cache and now - _events_cache[key][0] < 60:
        return _events_cache[key][1]
    evs = get_events(sport, status) or []
    _events_cache[key] = (now, evs)
    return evs


def match_event(home, away, sport_id, min_score=85.0, status=None):
    """BB 比赛(home/away/sport_id) → odds-api.io 事件 id。模糊匹配, 返回 id 或 None。"""
    eid, _ = match_event_orient(home, away, sport_id, min_score, status=status)
    return eid


def match_event_orient(home, away, sport_id, min_score=85.0, status=None):
    """BB 比赛 → (event_id, swapped)。swapped=True 表示 BB 主客与 odds-api.io 相反。

    status=None 全量(早盘, 匹配 pending/live 全部); status='live' 只匹配滚球(滚球流程,
    只 2 场, 免掉 O(5000) 模糊匹配)。
    结果缓存 60s(与 _get_events_cached 同步), 避免同一场比赛的多个盘口重复做模糊匹配。
    """
    key = (_norm_team(home), _norm_team(away), sport_id, status)
    now = time.time()
    cached = _match_cache.get(key)
    if cached and now - cached[0] < 60:
        _, eid, sw = cached
        return (eid, sw) if eid else (None, False)
    slug = _SPORT_ID_TO_SLUG.get(sport_id)
    if not slug:
        return None, False
    evs = _get_events_cached(slug, status)
    best_id, best_score, best_swapped = None, 0.0, False
    for e in evs:
        if _is_skip_event(e):
            continue
        sc, sw = _match_score_orient(home, away, e.get('home', ''), e.get('away', ''))
        if sc > best_score:
            best_score, best_id, best_swapped = sc, e.get('id'), sw
    _match_cache[key] = (now, best_id if best_score >= min_score else None, best_swapped)
    return (best_id, best_swapped) if best_score >= min_score else (None, False)


def _swap_fair(fair, sub_market):
    """主客互换: home/away 交换, draw 不动; hc/ht_hc 的 line 取反; dc 的 1X/X2 交换。"""
    if not isinstance(fair, dict):
        return fair
    out = dict(fair)
    if "home" in out or "away" in out:
        out["home"], out["away"] = out.get("away"), out.get("home")
    if "1X" in out and "X2" in out:
        out["1X"], out["X2"] = out["X2"], out["1X"]
    if "line" in out and sub_market in ("hc", "ht_hc"):
        try:
            out["line"] = -float(out["line"])
        except (TypeError, ValueError):
            pass
    return out


def fair_price_bb(home, away, sport_id, sub_market, target_line=None, status=None):
    """BB 比赛的公平价提供(替代 Pin): 匹配 → Betfair中间价(定价) + SBO devig(置信度)。

    返回 {'fair': {...}, 'confidence': {...}, 'event_id': ...} 或 None。
    fair = Betfair 中间价(加流动性门槛), 拿不到就 None(宁可少抓);
    confidence = SBO devig(比例去水, 只做同向确认, 不进定价)。
    target_line: hc/ou/ht_ou 的 BB 让球/大小线, 用于选对应线。
    status: None=全量(早盘), 'live'=只滚球(滚球流程, 匹配快)。
    主客互换(swapped)时 home/away 交换、hc 线取反, 保证返回的是 BB 主客视角的公平价。
    """
    eid, swapped = match_event_orient(home, away, sport_id, status=status)
    if not eid:
        return None
    # 主客互换时, BB 让球线对应 odds-api.io 的相反方向(线取反)
    tl = target_line
    if swapped and target_line is not None and sub_market in ("hc", "ht_hc"):
        tl = -target_line
    fair = fair_price(eid, sub_market, target_line=tl)
    if not fair:
        return None
    if swapped:
        fair = _swap_fair(fair, sub_market)
    conf = sbo_fair_price(eid, sub_market, target_line=tl)
    if swapped and conf:
        conf = _swap_fair(conf, sub_market)
    return {'fair': fair, 'confidence': conf, 'event_id': eid,
            'spread': fair.get('spread')}  # 2026-09-19 流动性门槛指标(back-lay 价差)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    # 自测: 拉一个中超 ht 盘口的公平价
    events = get_events("football", "live")
    print(f"滚球足球事件: {len(events)}")
    if events:
        e = events[0]
        print(f"测试: {e['home']} vs {e['away']}")
        fair = fair_price(e["id"], "ht")
        print(f"  ht 公平价: {fair}")
        fair_1x2 = fair_price(e["id"], "1x2")
        print(f"  1x2 公平价: {fair_1x2}")
