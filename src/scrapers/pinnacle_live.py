"""Pinnacle 滚球(live)赔率提取 (2026-09-06 逆向)。

关键结论:
  - live 数据**不在** /live/ 端点(404), 而是嵌在 /sports/{sport_id}/matchups 里,
    用 `isLive=True` 标识。实测足球(sportId=29) 15816 场里 295 场 live。
  - live 赔率在 /leagues/{league_id}/markets/straight, 与 pre-match 同端点。

结构差异(与 pre-match 比):
  - matchup: isLive=True / status="started"(非"live") / liveMode="both" / periods。
  - market 的 prices 用 **designation**(home/away/draw) 而非 pre-match 的 participantId。
  - market.status = "open"/"closed"(滚球盘口可能临时关闭)。

注意: /sports/{id}/matchups 响应 ~4MB(足球), 需长超时 + 结果缓存。
"""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# 有滚球的主要运动(足球/篮球/棒球/冰球/网球/电竞)
LIVE_SPORT_IDS = (29, 4, 3, 19, 33, 12)

# BB 盘口类型 mty → 子盘口 key(只做主盘口, 特殊盘口不接)
_MTY_TO_SUB = {
    1000: "hc", 1011: "hc",   # 让球
    1005: "1x2",               # 独赢
    1007: "ou",               # 大小
    1012: "dc",               # 双重机会
}
# option type(ty) → 方向
_TY_TO_DIR = {1: "主", 2: "客", 3: "和", 4: "大", 5: "小"}

# 结果缓存(避免每次扫描拉 4MB)
_CACHE_FILE = ROOT / "data" / "storage" / "pin_live_matchups.json"
_CACHE_TTL = 30  # 30 秒内复用(滚球赔率变动快, 不能缓存太久)

# 滚球公平价缓存(15s): 避免秒级监控每 2s 轮询时反复拉 Pin markets 触发风控
_FAIR_CACHE = {"ts": 0.0, "data": {}}
_FAIR_TTL = 15


def fetch_live_matchups(sport_ids=LIVE_SPORT_IDS, use_cache=True):
    """拉 /sports/{id}/matchups, 过滤 isLive=True 的比赛。

    返回 list[matchup]。每个含 id(matchupId)/league.id/participants(name)/
    status/isLive/liveMode/periods。
    """
    from src.scrapers.pinnacle_api import SESSION, API_BASE, _load_cookie
    _load_cookie()

    if use_cache and _CACHE_FILE.exists():
        try:
            data = json.loads(_CACHE_FILE.read_text())
            if time.time() - data.get("ts", 0) < _CACHE_TTL:
                return data.get("live", [])
        except Exception:
            pass

    live = []
    for sid in sport_ids:
        try:
            r = SESSION.get(f"{API_BASE}/sports/{sid}/matchups", timeout=(20, 60))
            ms = r.json()
            n = len([m for m in ms if m.get("isLive")])
            live.extend(m for m in ms if m.get("isLive"))
            print(f"[pin_live] sport {sid}: {n} 场 live (总 {len(ms)} matchups)")
        except Exception as e:
            print(f"[pin_live] sport {sid} 失败: {type(e).__name__} {str(e)[:60]}")

    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({"ts": time.time(), "live": live}, ensure_ascii=False))
    except Exception:
        pass
    return live


def fetch_live_odds(live_matchups):
    """给 live matchups 拉 straight markets 赔率。

    返回 {matchupId: [market, ...]}, market 含 type/period/status/prices(designation+price)。
    """
    from src.scrapers.pinnacle_api import SESSION, API_BASE, _load_cookie
    _load_cookie()

    # 按 league 分组, 每联赛只拉一次 markets
    live_leagues = {}
    for m in live_matchups:
        lid = m.get("league", {}).get("id")
        if lid:
            live_leagues.setdefault(lid, set()).add(m.get("id"))

    odds = {}
    for lid, mids in live_leagues.items():
        try:
            r = SESSION.get(f"{API_BASE}/leagues/{lid}/markets/straight", timeout=30)
            mks = r.json()
            for k in mks:
                if k.get("matchupId") in mids:
                    odds.setdefault(k["matchupId"], []).append(k)
        except Exception as e:
            print(f"[pin_live] 联赛 {lid} markets 失败: {str(e)[:60]}")
    return odds


def fetch_live_fair_prices(sport_ids=LIVE_SPORT_IDS):
    """滚球公平价: live matchups + odds → 按队名索引的 {home/away → 赔率}。

    返回 {matchupId: {"home": name, "away": name, "status", "moneyline": [dec, dec, dec]}}
    (moneyline 三列: 主/和/客, 用 Pinnacle 美式价转十进制)。供秒级监控匹配 BB matchId。
    15s 缓存: 秒级监控每 2s 轮询, 若每次都拉 Pin markets 会超风控(26 联赛×2s≈13req/s)。
    """
    if time.time() - _FAIR_CACHE["ts"] < _FAIR_TTL:
        return _FAIR_CACHE["data"]
    live = fetch_live_matchups(sport_ids)
    odds = fetch_live_odds(live)
    result = {}
    for m in live:
        # 只留主比赛(type=matchup), 跳过 special 子比赛(道具/让球/双重机会等)
        if m.get("type") != "matchup":
            continue
        mid = m.get("id")
        parts = m.get("participants", [])
        home = next((p.get("name", "") for p in parts if p.get("alignment") == "home"), "")
        away = next((p.get("name", "") for p in parts if p.get("alignment") == "away"), "")
        mks = odds.get(mid, [])
        ml = next((k for k in mks if k.get("type") == "moneyline" and k.get("status") == "open"), None)
        # 按 designation 排序成 [home, draw, away], 供 1x2 devig
        price_by_desig = {}
        for p in (ml.get("prices", []) if ml else []):
            price_by_desig[p.get("designation")] = p.get("price")
        ml_dec = _us_to_decimal([price_by_desig.get(d) for d in ("home", "draw", "away")])
        # maxRiskStake = 定价信心(薄盘=噪声大=假 edge)
        _max_stake = 0
        if ml:
            for lim in ml.get("limits", []):
                if lim.get("type") == "maxRiskStake":
                    _max_stake = lim.get("amount", 0)
        # spread(让球) + total(大小球) 2-way 盘口: {line(points): [dec1, dec2]}
        spreads = {}; totals = {}
        for k in mks:
            if k.get("status") != "open":
                continue
            t = k.get("type"); prices = k.get("prices", [])
            if len(prices) < 2:
                continue
            if t == "spread":
                spreads[prices[0].get("points")] = _us_to_decimal([p.get("price") for p in prices])
            elif t == "total":
                totals[prices[0].get("points")] = _us_to_decimal([p.get("price") for p in prices])
        result[mid] = {
            "home": home, "away": away,
            "status": m.get("status"),
            "isLive": bool(m.get("isLive")),
            "moneyline": ml_dec,  # [home, draw, away] 十进制
            "spread": spreads,    # {line: [home_dec, away_dec]}
            "total": totals,      # {line: [over_dec, under_dec]}
            "league_id": m.get("league", {}).get("id"),
            "max_stake": _max_stake,
        }
    _FAIR_CACHE["ts"] = time.time()
    _FAIR_CACHE["data"] = result
    return result


def fetch_bb_live_matches(sport_ids=(1, 3, 5, 7, 6)):
    """BB 滚球比赛(getList type=1, languageType=EN → 英文队名直配 Pin)。

    返回 {matchId: {"home_en": str, "away_en": str, "sport": int}}。
    """
    from src.betting.bb_auto_bet import read_token, read_domain, _session
    token = read_token(); domain = read_domain()
    if not token:
        return {}
    s = _session()
    result = {}
    for sid in sport_ids:
        try:
            r = s.post(f"{domain}/v1/match/getList",
                       json={"sportId": sid, "type": 1, "current": 1, "pageSize": 50,
                             "isPC": True, "languageType": "EN"},
                       headers={"Content-Type": "application/json", "user-token": token,
                                "User-Agent": _UA}, timeout=15, verify=False)
            d = r.json()
            if d.get("code") != 0:
                continue
            for m in (d.get("data") or {}).get("records") or []:
                ts = m.get("ts") or []
                if len(ts) < 2:
                    continue
                # 提取赔率(只取主盘口 hc/1x2/ou/dc, 在售 ss=1, 且只取全场 pe=1001)
                # BB 滚球有 pe=1001(全场) vs pe=1011(半场), 半场赔率不同会错配方向 → 只取全场
                markets = []
                for mg in m.get("mg") or []:
                    if mg.get("pe") != 1001:
                        continue
                    sub = _MTY_TO_SUB.get(mg.get("mty"))
                    if not sub:
                        continue
                    for mk in (mg.get("mks") or []):
                        if mk.get("ss") != 1:
                            continue
                        for op in (mk.get("op") or []):
                            od = op.get("od", 0)
                            if od <= 0:
                                continue
                            markets.append({
                                "sub": sub,
                                "market_id": mk.get("id"),
                                "option_type": op.get("ty"),
                                "odds": od,
                                "line": _parse_line(op.get("li")),
                                "direction": _TY_TO_DIR.get(op.get("ty")),
                            })
                result[int(m.get("id"))] = {
                    "home_en": ts[0].get("na", ""),
                    "away_en": ts[1].get("na", ""),
                    "sport": sid,
                    "markets": markets,
                }
        except Exception:
            continue
    return result


def _norm(name):
    """队名归一化: 小写 + 去非字母数字(空格/./FC等)。"""
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _parse_line(li):
    """BB 盘口线字符串('-0.5'/'+0/0.5') → float。quarter-ball(含 /)返回 None(跳过)。"""
    if li is None:
        return None
    s = str(li)
    if "/" in s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm_team(name):
    """队名归一化(模糊匹配用): 小写 + 去非字母数字 + 去俱乐部前后缀 + Saint→St。"""
    n = "".join(c for c in (name or "").lower() if c.isalnum())
    for suf in ("footballclub", "club", "cfc", "afc", "fc", "sc", "cf", "cd", "ac"):
        if n.endswith(suf) and len(n) > len(suf) + 3:
            n = n[:-len(suf)]
            break
    return n.replace("saint", "st")


def _match_score(h1, a1, h2, a2):
    """队名/球员名匹配得分: 精确/子串/rapidfuzz/交叉(主客互换)/姓名token顺序反转(网球)。"""
    import re
    from rapidfuzz import fuzz

    def clean(name):
        return re.sub(r"\([^)]*\)", "", name or "").strip()  # 去国家码 (THA)/(CHN)

    nh1, na1 = _norm_team(clean(h1)), _norm_team(clean(a1))
    nh2, na2 = _norm_team(clean(h2)), _norm_team(clean(a2))
    if nh1 == nh2 and na1 == na2:
        return 1.0

    def sub(x, y):
        return x and y and len(x) > 4 and len(y) > 4 and (x in y or y in x)
    if sub(nh1, nh2) and sub(na1, na2):
        return 0.8
    # rapidfuzz 模糊(处理拼写/别名差异)
    hs, as_ = fuzz.ratio(nh1, nh2), fuzz.ratio(na1, na2)
    if hs >= 80 and as_ >= 80:
        return 0.7
    # 交叉(主客互换)
    hs2, as2 = fuzz.ratio(nh1, na2), fuzz.ratio(na1, nh2)
    if hs2 >= 80 and as2 >= 80:
        return 0.6
    # 姓名 token 顺序反转(网球个人: "Sun Qian" vs "Qian Sun", 姓/名顺序不同)
    def toks(name):
        return sorted(re.findall(r"[a-z0-9]+", clean(name).lower()))
    t = toks(h1), toks(a1), toks(h2), toks(a2)
    if t[0] == t[2] and t[1] == t[3] and len(t[0]) >= 2 and len(t[1]) >= 2:
        return 0.5
    return 0.0


def _is_sub_market(name):
    """Pin 队名带括号(如 'Atlas (Corners)')是角球/罚牌子比赛, 跳过。"""
    return "(" in (name or "") or ")" in (name or "")


def _match_2way_line(line, d2way):
    """匹配 2-way 盘口线(spread/total)。主/客方向线符号相反, 试 line 和 -line。"""
    if not d2way:
        return None
    if line in d2way:
        return d2way[line]
    if line is not None and -line in d2way:
        return d2way[-line]
    return None


def fetch_live_opportunities(threshold=3.0):
    """轮询 getList type=1 + 匹配 Pin live 公平价 → 返回 +EV 滚球机会列表。

    返回 [{bb_match_id, home, away, sub, direction, bb_odds, fair, ev, market_id,
           option_type, line, pin_matchup_id, league_id, max_stake}]。
    """
    from src.scrapers.devig import shin_fair_odds
    bb = fetch_bb_live_matches()
    pin = fetch_live_fair_prices()
    # 过滤角球/罚牌子比赛, 只留主比赛
    pin_list = [(mid, v) for mid, v in pin.items()
                if not _is_sub_market(v["home"]) and not _is_sub_market(v["away"])]
    opps = []
    for bmid, b in bb.items():
        # 模糊匹配(精确优先, 子串兜底)
        best = None; best_score = 0.0
        for pin_mid, pv in pin_list:
            s = _match_score(b["home_en"], b["away_en"], pv["home"], pv["away"])
            if s > best_score:
                best_score = s; best = (pin_mid, pv)
        if not best or best_score == 0.0:
            continue
        pin_mid, pv = best
        if float(pv.get("max_stake", 0) or 0) < 200:  # 薄盘过滤
            continue
        for mk in b["markets"]:
            sub = mk["sub"]; d = mk["direction"]
            if not d:
                continue
            bb_odds = mk["odds"]
            if sub == "1x2":
                ml = pv.get("moneyline") or []
                if len(ml) != 3 or not any(ml):
                    continue
                raw = ml; idx = {"主": 0, "和": 1, "客": 2}
            elif sub == "hc":
                raw = _match_2way_line(mk["line"], pv.get("spread"))
                if not raw:
                    continue
                idx = {"主": 0, "客": 1}
            elif sub == "ou":
                raw = _match_2way_line(mk["line"], pv.get("total"))
                if not raw:
                    continue
                idx = {"大": 0, "小": 1}
            else:
                continue
            i = idx.get(d)
            if i is None:
                continue
            try:
                fair = shin_fair_odds(raw)
            except Exception:
                continue
            if not fair or len(fair) <= i or fair[i] <= 0:
                continue
            fair_p = fair[i]
            ev = (bb_odds - fair_p) / fair_p * 100.0
            # EV 上限 12%(防临时高价假机会/数据错配): BB 滚球价远高于 Pin 多是假 edge
            if ev >= threshold and ev <= 12.0:
                opps.append({
                    "bb_match_id": bmid, "home": pv["home"], "away": pv["away"],
                    "sub": sub, "direction": d, "bb_odds": bb_odds, "fair": fair_p, "ev": ev,
                    "market_id": mk["market_id"], "option_type": mk["option_type"], "line": mk["line"],
                    "pin_matchup_id": pin_mid, "league_id": pv.get("league_id"),
                    "max_stake": pv.get("max_stake", 0),
                })
    return opps


def match_live_bb_pin():
    """匹配 BB 滚球 ↔ Pin 滚球(英文队名), 返回 {bb_match_id: {pin_matchup_id, home, away, moneyline}}。"""
    bb = fetch_bb_live_matches()
    pin = fetch_live_fair_prices()
    # Pin 按 (norm_home, norm_away) 索引
    pin_by_name = {}
    for mid, v in pin.items():
        pin_by_name[(_norm(v["home"]), _norm(v["away"]))] = (mid, v)
    result = {}
    for bmid, b in bb.items():
        key = (_norm(b["home_en"]), _norm(b["away_en"]))
        hit = pin_by_name.get(key)
        if hit:
            pin_mid, pv = hit
            result[bmid] = {
                "pin_matchup_id": pin_mid,
                "home": pv["home"], "away": pv["away"],
                "moneyline": pv["moneyline"],  # [主, 和, 客] 十进制
                "spread": pv.get("spread", {}),
                "total": pv.get("total", {}),
                "league_id": pv.get("league_id"),
                "max_stake": pv.get("max_stake", 0),
                "sport": b["sport"],
            }
    return result


def reverify_live_markets(pin_matchup_id, league_id):
    """下注前重拉指定联赛 markets, 返回该滚球 match 最新 {moneyline, spread, total}。

    用于滚球延迟修正: 缓存(30s)可能过期, 下单前重验 Pin 滚球价是否已漂移。返回 None=失败。
    moneyline: [home, draw, away] 十进制(无则 None); spread/total: {line: [dec1, dec2]}。
    """
    from src.scrapers.pinnacle_api import SESSION, API_BASE, _load_cookie
    _load_cookie()
    try:
        r = SESSION.get(f"{API_BASE}/leagues/{league_id}/markets/straight", timeout=30)
        mks = r.json()
        result = {"moneyline": None, "spread": {}, "total": {}}
        for k in mks:
            if k.get("matchupId") != pin_matchup_id or k.get("status") != "open":
                continue
            t = k.get("type"); prices = k.get("prices", [])
            if t == "moneyline" and len(prices) >= 3:
                pbd = {p.get("designation"): p.get("price") for p in prices}
                result["moneyline"] = _us_to_decimal([pbd.get(d) for d in ("home", "draw", "away")])
            elif t == "spread" and len(prices) >= 2:
                result["spread"][prices[0].get("points")] = _us_to_decimal([p.get("price") for p in prices])
            elif t == "total" and len(prices) >= 2:
                result["total"][prices[0].get("points")] = _us_to_decimal([p.get("price") for p in prices])
        return result
    except Exception:
        return None


def _us_to_decimal(us_prices):
    """Pinnacle 美式价 → 十进制。[-119, 634, 151] → [1.84, 7.34, 2.51]"""
    out = []
    for p in us_prices:
        if p is None:
            out.append(0.0)
        elif p > 0:
            out.append(round(1 + p / 100.0, 4))
        else:
            out.append(round(1 + 100.0 / abs(p), 4))
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    res = fetch_live_fair_prices()
    print(f"\n滚球公平价: {len(res)} 场")
    n_with_ml = sum(1 for v in res.values() if v["moneyline"])
    print(f"有 moneyline 赔率的: {n_with_ml} 场")
    for mid, v in list(res.items())[:5]:
        print(f"  {v['home']} vs {v['away']} | status={v['status']} ml={v['moneyline']}")
