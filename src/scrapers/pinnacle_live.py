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

# 有滚球的主要运动(足球/篮球/棒球/冰球/网球/美式足球)。
# 2026-09-13 修: 旧值 12 是 CS2 电竞(非真运动, 也不在 SPORT_IDS 映射里), 误当运动拉进来;
# 美式足球正确 id 是 15(实测 Pin 有 NCAA 美足 live 10 场), 之前漏拉 → BB 美足 live 无法比价。
LIVE_SPORT_IDS = (29, 4, 3, 19, 33, 15)

# BB 盘口 (mty, pe) → 子盘口 key。mty=盘口类型码, pe=period(1001全场/1002半场)。
# 必须 (mty, pe) 双键: 上半场1x2(1005,1002) 与 全场1x2(1005,1001) 是不同盘口。
# 修正: 1011 是"角球让球"不是"让球", 旧映射错当 hc。
_MTY_PE_TO_SUB = {
    (1005, 1001): "1x2",          # 独赢(全场)
    (1000, 1001): "hc",           # 让球(全场)
    (1007, 1001): "ou",           # 大小(全场)
    (1012, 1001): "dc",           # 双机会(全场)
    (1005, 1002): "ht",           # 上半场独赢
    (1000, 1002): "ht_hc",        # 上半场让球
    (1007, 1002): "ht_ou",        # 上半场大小
    (1012, 1002): "ht_dc",        # 上半场双机会
    (1033, 1001): "htft",         # 半全场
    (1027, 1001): "btts",         # 双边进球
    (1008, 1001): "oe",           # 单双
    (1099, 1001): "correct_score",  # 正确比分(全场)
    (1100, 1002): "correct_score_ht",  # 上半场正确比分
    (1103, 1002): "exact_goals_ht",  # 上半场精确进球
    (1101, 1001): "total_goals",  # 总进球区间
    (1018, 1001): "winning_margin",  # 净胜球
    (1009, 1001): "corner_1x2",   # 角球独赢
    (1010, 1001): "corner_ou",    # 角球大小
    (1011, 1001): "corner_hc",    # 角球让球
    # ── 非足球运动(2026-09-22 补: 之前只扩抓取没扩提取, 非足球盘口全空) ──
    # mty = sport_id*1000 + 市场码, 但各运动市场码不同, 实测逆向。
    # 网球让盘(5002)/大小(5003)按局数结算, 与 fetch_bb_match_result 的盘分(sets)歧义, 暂不采(只采独赢)。
    (3002, 3001): "1x2",    # 篮球独赢
    (3004, 3001): "hc",     # 篮球让分
    (3003, 3001): "ou",     # 篮球大小
    (5001, 5001): "1x2",    # 网球独赢
    (7001, 7001): "1x2",    # 棒球独赢
    (7003, 7001): "hc",     # 棒球让分(run line)
    (7002, 7001): "ou",     # 棒球大小
    (13001, 13001): "1x2",  # 排球独赢
    (13002, 13001): "hc",   # 排球让分
    (13003, 13001): "ou",   # 排球大小
    (15001, 15001): "1x2",  # 乒乓球独赢
}
# option type(ty) → 方向(主盘口 1x2/hc/ou/dc/ht 共用; 特殊盘口 ty 不同, 不在此表)
_TY_TO_DIR = {1: "主", 2: "客", 3: "和", 4: "大", 5: "小"}

# 特殊盘口 option-type → 方向/腿(2026-09-19): 主流盘口 ty 1-5 走上面, 这些盘口 ty 码不同。
# dc/btts 有 Betfair 公平价可收; oe/总进球/净胜球 BB 有但 Betfair 无(无公平价, 不在此表——收不进观察库)。
_SUB_TY_TO_DIR = {
    "dc": {50: "主/和", 51: "主/客", 52: "客/和"},
    "btts": {8: "双方进球", 9: "非双方进球"},
    "htft": {41: "主/主", 35: "主/和", 36: "主/客", 38: "和/主", 43: "和/和",
             39: "和/客", 42: "客/主", 37: "客/和", 40: "客/客"},
}

# 结果缓存(避免每次扫描拉 4MB)
_CACHE_FILE = ROOT / "data" / "storage" / "pin_live_matchups.json"
_CACHE_TTL = 30  # 30 秒内复用(滚球赔率变动快, 不能缓存太久)

# 滚球公平价缓存(15s): 避免秒级监控每 2s 轮询时反复拉 Pin markets 触发风控
_FAIR_CACHE = {"ts": 0.0, "data": {}}
_FAIR_TTL = 15  # Pin markets 本身被 CDN 缓存 15 分钟(见 fetch_live_odds 诊断), 拉到也是旧价, 15s 复用省请求不损新鲜度
# 公平价文件缓存(跨进程共享, 45s): BB 进程每 30s 刷一次, FB 观察库进程直接复用,
# 避免 FB 每 60s 重复拉 Pin markets(60MB足球+几十联赛 markets) 引起 Pin 额外负载/带宽争抢。
_FAIR_FILE = ROOT / "data" / "storage" / "pin_live_fair_prices.json"
_FAIR_FILE_TTL = 45


def _live_reserve():
    """滚球秒级 Pin 请求走跨进程共享限速, 最高优先级(主扫描/CLV 让路)。

    2026-09-14: 之前滚球直接 SESSION.get() 不经过 pin_rate_state, 和主扫描从同一个节点
    出口 IP 抢 Pin —— 是 Cloudflare 反复封 IP 的缺口之一。这里接入共享发号, priority=live
    立即取号(几乎不等待), 但会写 next_slot 让主扫描/CLV 排队让路。
    fail-open: 任何异常都放行, 绝不阻塞滚球秒级链路。
    """
    try:
        from src.scrapers import pin_rate_state
        from src.scrapers.pinnacle_api import (
            _current_min_interval, _REQUEST_BURST_LIMIT, _REQUEST_BURST_WINDOW)
        allowed, wait, reason = pin_rate_state.reserve(
            _current_min_interval(), _REQUEST_BURST_LIMIT, _REQUEST_BURST_WINDOW,
            priority="live")
        if allowed is False:
            # 熔断/封禁冷却中: 不取号, 让请求自然失败(调用方已有回退缓存逻辑)
            return
        if wait > 0:
            time.sleep(wait)
    except Exception:
        pass  # 共享层不可用则 fail-open, 退回无协调(和之前行为一致)


def fetch_live_matchups(sport_ids=LIVE_SPORT_IDS, use_cache=True):
    """拉 /sports/{id}/matchups, 过滤 isLive=True 的比赛。

    返回 list[matchup]。每个含 id(matchupId)/league.id/participants(name)/
    status/isLive/liveMode/periods。
    """
    from src.scrapers.pinnacle_api import SESSION, API_BASE, _load_cookie
    _load_cookie()

    # 上一轮缓存(足球超时兜底用): 读出来供回退, 不因本轮某运动失败丢光其滚球机会
    prev_live = []
    if _CACHE_FILE.exists():
        try:
            data = json.loads(_CACHE_FILE.read_text())
            prev_live = data.get("live", [])
            if use_cache and time.time() - data.get("ts", 0) < _CACHE_TTL:
                return prev_live
        except Exception:
            pass

    # 足球(sportId=29)matchups ~30MB, 慢网络实测 26s; 其它运动 <1MB 秒回。
    # curl_cffi 0.16.3 的 timeout 元组(connect,read)不生效(实测报 8s), 用 float 总超时;
    # connect 15s 快速失败已全局设(pinnacle_api SESSION.CONNECTTIMEOUT_MS)。
    FOOTBALL_SID = 29
    T_FOOTBALL = 45.0  # 足球 60MB: 快 edge(172.64.145.56)实测 9.5s, 45s 留 4.7x 余量; 慢网络波动也能扛
    T_OTHER = 20.0

    live = []
    for sid in sport_ids:
        _timeout = T_FOOTBALL if sid == FOOTBALL_SID else T_OTHER
        _t0 = time.time()
        try:
            _live_reserve()
            r = SESSION.get(f"{API_BASE}/sports/{sid}/matchups", timeout=_timeout)
            _dt = time.time() - _t0
            ms = r.json()
            n = 0
            for m in ms:
                if m.get("isLive"):
                    m["_sport_id"] = sid  # 附加运动标识, 供超时回退按运动过滤
                    live.append(m)
                    n += 1
            print(f"[pin_live] sport {sid}: {n} 场 live (总 {len(ms)} matchups, {_dt:.0f}s)")
            # edge IP 静默退化探针: 足球 60MB 快 edge(172.64.145.56)实测 ~9.5s,
            # 慢网络 <30s; 超 30s 说明 edge 又退化了(请求 200 但慢, 轻端点测不出)。
            if sid == FOOTBALL_SID and _dt > 30:
                print(f"[pin_live] ⚠️ 足球 matchups 下载 {_dt:.0f}s 超阈值(30s), edge IP 可能又退化 — 见 pin-edge-ip-degraded-silent-fix-20260913")

        except Exception as e:
            print(f"[pin_live] sport {sid} 失败: {type(e).__name__} {str(e)[:60]}")
            # 足球超时 → 回退上一轮足球 live(≤30s 旧), 避免丢光足球滚球机会/阻塞整轮
            if sid == FOOTBALL_SID:
                _fb = [m for m in prev_live if m.get("_sport_id") == FOOTBALL_SID]
                if _fb:
                    live.extend(_fb)
                    print(f"[pin_live] sport {sid} 超时, 回退上一轮缓存 {len(_fb)} 场")

    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({"ts": time.time(), "live": live}, ensure_ascii=False))
    except Exception:
        pass
    return live


def fetch_live_odds(live_matchups):
    """给 live matchups 拉 straight markets 赔率。

    2026-09-17 诊断(结论): /markets/straight **无参数** 被 CDN 缓存 15 分钟
    (cf-cache=HIT, cache-control: public,max-age=904, must-revalidate, 由 Pin 源站主动设置),
    且**带任何 query 参数(since/period/type/sportId/... 全试过)都返回 204 空 body**。
    since 游标增量轮询实测不生效 —— 响应里没有 `last` 字段(只有 per-market `version`,
    且 `since={version}` 及任意值都 204), 是 ps3838 付费 API 才有的机制, guest API 没有。
    → Pin 滚球公平价固有 ~15 分钟陈旧, 是 guest API 限流的数据源限制, 非代码 bug。
    已回退全量拉取(每次全量, 靠 CDN max-age 到期后 must-revalidate 每 ~15min 自刷新)。

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
            _live_reserve()
            r = SESSION.get(f"{API_BASE}/leagues/{lid}/markets/straight", timeout=30)
            mks = r.json()
            for k in mks:
                if k.get("matchupId") in mids:
                    odds.setdefault(k["matchupId"], []).append(k)
        except Exception as e:
            print(f"[pin_live] 联赛 {lid} markets 失败: {str(e)[:60]}")
    return odds


def fetch_live_fair_prices(sport_ids=LIVE_SPORT_IDS, use_file_cache=False):
    """滚球公平价: live matchups + odds → 按队名索引的 {home/away → 赔率}。

    返回 {matchupId: {"home": name, "away": name, "status", "moneyline": [dec, dec, dec]}}
    (moneyline 三列: 主/和/客, 用 Pinnacle 美式价转十进制)。供秒级监控匹配 BB matchId。
    15s 缓存: 秒级监控每 2s 轮询, 若每次都拉 Pin markets 会超风控(26 联赛×2s≈13req/s)。

    use_file_cache=True 时(仅 FB 观察库收集进程用): 复用 BB 进程刚写入的公平价文件,
    不重复拉 Pin markets。BB 秒级监控永远 use_file_cache=False(要新鲜公平价)。
    """
    if time.time() - _FAIR_CACHE["ts"] < _FAIR_TTL:
        return _FAIR_CACHE["data"]
    # 文件缓存(跨进程, 仅 FB 收集进程读): 复用 BB 进程刚拉的结果, 避免重复拉 Pin markets
    if use_file_cache:
        try:
            if _FAIR_FILE.exists():
                fd = json.loads(_FAIR_FILE.read_text())
                if time.time() - fd.get("ts", 0) < _FAIR_FILE_TTL:
                    _FAIR_CACHE["ts"] = fd["ts"]
                    _FAIR_CACHE["data"] = fd["data"]
                    return fd["data"]
        except Exception:
            pass
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
        # 只取全场(period=0): 滚球路径只比全场盘口(1x2/hc/ou), 半场(period=1)混进来会错配线
        # (2026-09-16 修: 之前不按 period 过滤, moneyline 可能取到半场, spread/total 全场半场混一 dict)
        ml = next((k for k in mks if k.get("type") == "moneyline" and k.get("status") == "open" and k.get("period") == 0), None)
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
            if k.get("status") != "open" or k.get("period") != 0:  # 只取全场, 防半场线混入
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
            "league_name": m.get("league", {}).get("name", ""),  # 联赛名(通知展示用)
            "max_stake": _max_stake,
        }
    _FAIR_CACHE["ts"] = time.time()
    _FAIR_CACHE["data"] = result
    try:
        _FAIR_FILE.write_text(json.dumps({"ts": time.time(), "data": result}, ensure_ascii=False))
    except Exception:
        pass
    return result


_CN_NAME_CACHE = {"ts": 0.0, "data": {}}  # 早盘中文名缓存(60s TTL, 滚球不拉CMN, 从早盘数据免费拿)


def _load_cn_names():
    """从早盘 bb_odds_extracted.json 读 id→(home_cn,away_cn,league_cn) 映射(60s 缓存)。

    滚球不再单独拉 CMN(中文只做钉钉展示, 不参与比价), 中文名从早盘已抓的中文名免费补。
    """
    global _CN_NAME_CACHE
    if time.time() - _CN_NAME_CACHE["ts"] < 60:
        return _CN_NAME_CACHE["data"]
    try:
        from src.scrapers.bb_incremental_scanner import BB_EXTRACTED
        d = json.loads(BB_EXTRACTED.read_text())
        m = {}
        for x in d.get("matches", []):
            if x.get("home_cn") or x.get("away_cn"):
                m[x.get("id")] = (x.get("home_cn", ""), x.get("away_cn", ""), x.get("league_cn", ""))
        _CN_NAME_CACHE = {"ts": time.time(), "data": m}
    except Exception:
        pass
    return _CN_NAME_CACHE["data"]


# 2026-09-25 后台 CMN 中文名缓存: 滚球实时中文名(推送全中文), 后台线程每 60s 拉, 不占下单链路
_LIVE_CN_CACHE = {"ts": 0.0, "data": {}}
_LIVE_CN_LOCK = _threading.Lock()


def _fetch_live_cn(sport_ids=(1, 3, 5, 7, 13, 15), platform="BB"):
    """后台拉滚球 CMN 中文名(不占下单链路), 更新 _LIVE_CN_CACHE {match_id: (home_cn, away_cn, league_cn)}。"""
    global _LIVE_CN_CACHE
    try:
        from src.betting.bb_auto_bet import read_token, _session
        from src.scrapers.bb_api_fetcher import PLATFORMS
        token = read_token()
        domain = PLATFORMS.get(platform, PLATFORMS["BB"])["api_base"]
        if not token:
            return
        s = _session()

        def _fetch(sid):
            try:
                r = s.post(f"{domain}/v1/match/getList",
                           json={"sportId": sid, "type": 1, "current": 1, "pageSize": 50,
                                 "isPC": True, "languageType": "CMN"},
                           headers={"Content-Type": "application/json", "user-token": token,
                                    "User-Agent": _UA}, timeout=15, verify=False)
                d = r.json()
                if d.get("code") != 0:
                    return []
                return (d.get("data") or {}).get("records") or []
            except Exception:
                return []

        from concurrent.futures import ThreadPoolExecutor
        m = {}
        with ThreadPoolExecutor(max_workers=len(sport_ids)) as ex:
            for rec_list in ex.map(_fetch, sport_ids):
                for rec in rec_list:
                    ts = rec.get("ts") or []
                    if len(ts) < 2:
                        continue
                    m[int(rec.get("id"))] = (ts[0].get("na", ""), ts[1].get("na", ""), "")
        with _LIVE_CN_LOCK:
            _LIVE_CN_CACHE = {"ts": time.time(), "data": m}
    except Exception:
        pass


def start_cn_prefetch(interval=60.0, sport_ids=(1, 3, 5, 7, 13, 15), platform="BB"):
    """启动后台 CMN 中文名预取线程(推送全中文, 2026-09-25)。"""
    def _loop():
        while True:
            try:
                _fetch_live_cn(sport_ids, platform)
            except Exception:
                pass
            time.sleep(interval)
    _threading.Thread(target=_loop, daemon=True, name="cn-prefetch").start()


def fetch_bb_live_matches(sport_ids=(1, 3, 5, 7, 13, 15), platform="BB"):
    """BB/FB 滚球比赛。EN 拉英文队名(直配 Pin) + 提取盘口; CMN 拉中文队名/联赛名(展示用)。

    2026-09-24: 滚球高频(2s轮询)只收 6 个有场次的主运动(足1/篮3/网5/棒7/排13/乒乓15)减CPU/连接池负担;
    早盘低频(--all-sports)才是全量收集。滚球其余运动(美足/冰球/MMA/拳击/羽毛球)几乎无 live 场次, 收也白收。

    platform="BB" 用 BB 域名, "FB" 用 FB 域名(api.5c4r3.com)。两者同一账户 user-token,
    但 match_id 各自独立(FB 的比赛要用 FB 域名 getMatchDetail 结算)。
    返回 {matchId: {home_en, away_en, home_cn, away_cn, league_cn, sport, markets, mc, sc}}。
    """
    from src.betting.bb_auto_bet import read_token, _session
    from src.scrapers.bb_api_fetcher import PLATFORMS
    token = read_token()
    domain = PLATFORMS.get(platform, PLATFORMS["BB"])["api_base"]
    if not token:
        return {}
    s = _session()

    def _fetch(sid, lang):
        try:
            r = s.post(f"{domain}/v1/match/getList",
                       json={"sportId": sid, "type": 1, "current": 1, "pageSize": 50,
                             "isPC": True, "languageType": lang},
                       headers={"Content-Type": "application/json", "user-token": token,
                                "User-Agent": _UA}, timeout=15, verify=False)
            d = r.json()
            if d.get("code") != 0:
                return []
            return (d.get("data") or {}).get("records") or []
        except Exception:
            return []

    result = {}
    # 2026-09-19 优化: EN 并发拉取(足篮), 耗时从 9s 串行降到 ~1s。
    # 2026-09-20 撤销 CMN 并发: CMN 只为钉钉中文展示, 不该占下单路径(会把 BB 拉取 1.6s 拖回 2.5-4.7s)。
    # 中文名回到「早盘快照免费补」的方式(_load_cn_names)。
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(sport_ids)) as _ex:
        _en_records = list(_ex.map(lambda _sid: (_sid, _fetch(_sid, "EN")), sport_ids))
    for sid, _en_list in _en_records:
        # 1. EN: 英文队名 + 盘口提取(英文直配 Pin 用)
        for m in _en_list:
            ts = m.get("ts") or []
            if len(ts) < 2:
                continue
            markets = []
            for mg in m.get("mg") or []:
                sub = _MTY_PE_TO_SUB.get((mg.get("mty"), mg.get("pe")))
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
                            "sub": sub, "market_id": mk.get("id"),
                            "option_type": op.get("ty"), "odds": od,
                            "line": _parse_line(op.get("li")),
                            "direction": _TY_TO_DIR.get(op.get("ty")) or _SUB_TY_TO_DIR.get(sub, {}).get(op.get("ty")),
                        })
            # 当前比分(下注瞬间比分): 滚球让球按「当前比分让球」结算要用(2026-09-13 修根因)。
            # nsg 里 pe=全场码(SCORE_PE_BY_SID) + tyg=5 是比分, sc=[主,客]。
            sc = None
            try:
                from src.scrapers.bb_api_fetcher import SCORE_PE_BY_SID
                pe_full = SCORE_PE_BY_SID.get(sid)
                if pe_full:
                    for sg in (m.get("nsg") or []):
                        if sg.get("pe") == pe_full and sg.get("tyg") == 5:
                            scv = sg.get("sc") or []
                            if len(scv) >= 2:
                                sc = [int(scv[0]), int(scv[1])]
                            break
            except Exception:
                sc = None
            result[int(m.get("id"))] = {
                "home_en": ts[0].get("na", ""), "away_en": ts[1].get("na", ""),
                "home_cn": "", "away_cn": "", "league_cn": "",
                "sport": sid, "markets": markets,
                "mc": (m.get("mc") or {}).get("s", 0),
                "sc": sc,  # [主,客] 当前比分(让球按当前比分结算用)
            }
    # 2. 中文名: 从早盘 bb_odds_extracted.json 的中文名免费补(仅钉钉展示, 匹配不上留空)
    _cn = _load_cn_names()
    for _mid, _info in result.items():
        if _mid in _cn:
            _info["home_cn"], _info["away_cn"], _info["league_cn"] = _cn[_mid]
    return result


def _norm(name):
    """队名归一化: 小写 + 去非字母数字(空格/./FC等)。"""
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _parse_line(li):
    """BB 盘口线字符串('-0.5'/'+0/0.5'/'2/2.5') → float。

    quarter-ball(含 /)取平均(2/2.5→2.25), 与早盘 parse_asian_line 同口径。
    之前返回 None 会丢线: 推送只显示"大球"不带"大几球", 且线匹配被跳过(假EV)。
    """
    if li is None:
        return None
    s = str(li)
    if "/" in s:
        try:
            parts = s.split("/")
            return (float(parts[0]) + float(parts[1])) / 2.0
        except (ValueError, IndexError):
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


def fetch_live_opportunities(threshold=3.0, platform="BB", use_file_cache=False):
    """轮询 getList type=1 + 匹配 Pin live 公平价 → 返回 +EV 滚球机会列表。

    platform="BB"|"FB"。use_file_cache=True 时复用 BB 进程已写入的公平价文件(仅 FB 收集进程用)。
    返回 [{bb_match_id, home, away, sub, direction, bb_odds, fair, ev, market_id,
           option_type, line, pin_matchup_id, league_id, max_stake}]。
    """
    from src.scrapers.devig import shin_fair_odds
    bb = fetch_bb_live_matches(platform=platform)
    _bb_ts = time.time()  # BB 拉取时间(本次 getList 完成时刻)
    pin = fetch_live_fair_prices(use_file_cache=use_file_cache)
    _pin_ts = _FAIR_CACHE.get("ts", time.time())  # Pin 拉取时间(公平价缓存的实际抓取时刻, 15-45s前)
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
                    "bb_match_id": bmid,
                    "home": b.get("home_cn") or pv["home"], "away": b.get("away_cn") or pv["away"],  # 中文队名优先(2026-09-12 推送要中文)
                    "league_cn": b.get("league_cn", "") or pv.get("league_name", ""),  # 中文联赛名(2026-09-12 推送加联赛)
                    "sport": b["sport"],  # BB 运动 id(1足球/3篮球/5网球/7棒球/6美足), 供按运动×盘口分账
                    "sub": sub, "direction": d, "bb_odds": bb_odds, "fair": fair_p, "ev": ev, "pin_raw": raw[i],
                    "market_id": mk["market_id"], "option_type": mk["option_type"], "line": mk["line"],
                    "pin_matchup_id": pin_mid, "league_id": pv.get("league_id"),
                    "max_stake": pv.get("max_stake", 0),
                    "sc": b.get("sc"),  # [主,客] 下注瞬间比分(让球按当前比分结算用, 2026-09-13)
                    "bb_ts": _bb_ts, "pin_ts": _pin_ts,  # BB/Pin 数据拉取时间(供推送展示数据新鲜度)
                    "platform": platform,  # BB/FB(推送时标注在哪平台投注, 2026-09-13)
                })
    return opps


# BB 滚球列表后台预取缓存(2026-09-21): 把 1.6s 的 getList 从下单链路移出, 后台每 ~2s 拉一次,
# 下单时直接读缓存(0ms)。这样 WS触发→下单 的 critical path 去掉 BB 拉取, 从 ~6s 压到 ~4.5s。
import threading as _threading
_bb_cache = {}
_bb_cache_ts = 0.0
_bb_cache_lock = _threading.Lock()
_bb_prefetch_stop = False


def _bb_prefetch_loop(interval=2.0):
    """后台预取 BB 滚球列表。失败(如 token 过期)静默跳过, 缓存保持旧值, 下次再试。"""
    global _bb_cache, _bb_cache_ts, _bb_prefetch_stop
    while not _bb_prefetch_stop:
        try:
            _bb = fetch_bb_live_matches(platform="BB")
            if _bb:
                with _bb_cache_lock:
                    _bb_cache = _bb
                    _bb_cache_ts = time.time()
        except Exception:
            pass
        time.sleep(interval)


def start_bb_prefetch(interval=2.0):
    """启动后台 BB 预取线程。返回 Thread。"""
    t = _threading.Thread(target=_bb_prefetch_loop, args=(interval,), daemon=True, name="bb-prefetch")
    t.start()
    return t


def get_bb_cached(max_age=5.0):
    """读预取的 BB 缓存(返回 (bb_data, ts))。缓存过期/为空则同步拉一次兜底。"""
    global _bb_cache, _bb_cache_ts
    with _bb_cache_lock:
        if _bb_cache and time.time() - _bb_cache_ts < max_age:
            return _bb_cache, _bb_cache_ts
    _bb = fetch_bb_live_matches(platform="BB")
    _ts = time.time()
    return _bb, _ts


# ── WS 触发现拉 BB(2026-09-22): 与 persistence 稳定期并行 ──
# 原始 WS 变动一出现就后台现拉 BB(只足球), 让 BB 价取自"sharp 动那一刻"而非"稳定确认后 2s",
# 且 persistence 确认稳定时 BB 已拉好 → 总耗时 = max(persistence 2s, BB拉取 1.6s) ≈ 2s。
_bb_fresh = {}
_bb_fresh_ts = 0.0
_bb_fresh_busy = False
_bb_fresh_lock = _threading.Lock()


def trigger_fresh_bb_fetch(sport_ids=(1,)):
    """WS 原始变动触发: 后台现拉 BB(只足球, 非足球交给预取慢周期)。幂等(拉取中则跳过)。"""
    global _bb_fresh_busy
    if _bb_fresh_busy:
        return
    _bb_fresh_busy = True

    def _do():
        global _bb_fresh, _bb_fresh_ts, _bb_fresh_busy
        try:
            _bb = fetch_bb_live_matches(sport_ids=sport_ids, platform="BB")
            with _bb_fresh_lock:
                _bb_fresh = _bb
                _bb_fresh_ts = time.time()
        except Exception:
            pass
        finally:
            _bb_fresh_busy = False

    _threading.Thread(target=_do, daemon=True, name="bb-fresh").start()


def get_bb_fresh_or_cached(max_age=5.0, wait_for_fresh=True):
    """WS 触发路径优先读"现拉"的足球 BB; 非足球从预取缓存补(非足球慢周期, 缓存够)。

    wait_for_fresh(2026-09-23): 若现拉 BB 还在进行中(getList 1.6s > persistence 0.5s), 等它完成
    (最多 3s)再返回——否则 poll 在 persistence 0.5s 时触发, 现拉 BB 还没好, 只能退回陈旧缓存,
    "同步拉 BB"白做。等现拉后 BB 价才是"sharp 动那一刻"的价。
    """
    global _bb_fresh, _bb_fresh_ts
    if wait_for_fresh:
        _deadline = time.time() + 3.0
        while _bb_fresh_busy and time.time() < _deadline:
            time.sleep(0.05)
    with _bb_fresh_lock:
        _f = _bb_fresh if (_bb_fresh and time.time() - _bb_fresh_ts < max_age) else None
        _fts = _bb_fresh_ts
    if _f is not None:
        _cached, _ = get_bb_cached(max_age=max_age)
        _merged = dict(_cached)
        _merged.update(_f)  # 新鲜足球覆盖, 非足球保留缓存
        return _merged, _fts
    return get_bb_cached(max_age=max_age)


_nonfb_poll_counter = 0  # 非足球降频计数器(2026-09-22): 非足球不需要 lead-lag 秒级, 每 15 轮匹配一次
NONFB_EVERY = 15  # 非足球公平价匹配频率: 每 15 轮(≈30s)才匹配一次非足球, 省公平价匹配任务数


def fetch_live_opportunities_oa(threshold=3.0, poll_ts=None, platform="BB"):
    """滚球机会: BB live + Sbobet/Betfair 公平价(替代 Pin, 2026-09-18)。

    公平价来源从 Pin(15分钟旧) 换成 odds-api.io 的 Betfair 中间价(实时, 加流动性门槛)
    + Sbobet 置信度。返回 opp 结构对齐 fetch_live_opportunities, 供 _opp_to_sig 直接复用。
    只处理主流盘口 1x2/hc/ou(带线匹配), 特殊盘口不走实盘。
    poll_ts(2026-09-23): 原始WS触发时刻, 供推送「端到端耗时(WS触发→下单完成)」; 缺省用本轮 poll 开始时间。
    platform(2026-09-24): "BB"(默认, 用BB缓存) | "FB"(FB观察库, 现拉FB比赛, 公平价同Betfair)。
    """
    from src.scrapers.odds_api_io import fair_price_bb
    from concurrent.futures import ThreadPoolExecutor
    global _nonfb_poll_counter
    _nonfb_poll_counter += 1
    _do_nonfb = (_nonfb_poll_counter % NONFB_EVERY == 0)
    _t0 = time.time()
    _poll_ts = poll_ts if poll_ts else _t0
    # 2026-09-22 读"现拉优先"的 BB: WS 触发路径用 trigger_fresh_bb_fetch 现拉的足球价(persistence并行),
    # 非足球从预取缓存补。_bb_ts 用现拉时间戳(供快照单新鲜度判断)。
    # 2026-09-24 FB 观察库: 现拉 FB 比赛(不用 BB 缓存), 公平价同样走 Betfair 中间价。
    if platform == "FB":
        bb, _bb_ts = fetch_bb_live_matches(platform="FB")
        _bb_ts = time.time()
    else:
        bb, _bb_ts = get_bb_fresh_or_cached()

    # 收集任务(比赛×盘口)。非足球降频(2026-09-22): 非足球只每 15 轮匹配一次, 省公平价匹配任务,
    # 防 9.5s 尖峰挤占足球让球快照单(≤6s)新鲜度。非足球只收纸单攒数据, 不需要秒级。
    tasks = []
    for bmid, b in bb.items():
        if b.get("sport") != 1 and not _do_nonfb:
            continue
        for mk in b["markets"]:
            sub = mk["sub"]; d = mk["direction"]
            if not d or sub not in ("1x2", "hc", "ou", "dc", "btts", "ht", "ht_ou"):
                continue
            tasks.append((bmid, b, mk))

    def _process(task):
        """单个 (比赛,盘口) → 机会列表。并发执行(2026-09-20), 公平价匹配 11s→2-3s。"""
        bmid, b, mk = task
        sub = mk["sub"]; d = mk["direction"]
        bb_odds = mk["odds"]
        # 2026-09-22 修 quarter-ball 假溢价根因: |line| 是 0.25/0.75 这类四分盘(hc/ou/ht_ou 都有,
        # 实测让球55%/大小球58%是四分盘), BB 与 Betfair 的合成口径不一致(半注挂0/半注挂0.5),
        # 线匹配算出假公平价(实测让球 EV>20% 桶 87% 是 0.25 线, 观察库让球 +13.8pp 假 edge 主因)。
        # 且结算层也无法可靠判半赢半走盘 → 与 line=0 void 同策略不采集。只采 0.5 整数倍线。
        if sub in ("hc", "ou", "ht_ou") and mk.get("line") is not None:
            _l = float(mk["line"])
            if abs(_l * 2 - round(_l * 2)) > 1e-6:
                return []
        # 2026-09-20 修线错配: hc/ou 必须传 BB 的具体线(target_line), 否则公平价永远选 main line,
        # 三个不同让球线(3.17/2.14/1.48)都拿同一个 main line 公平价去比 → 虚高 +37% 假溢价。
        _target_line = mk.get("line") if sub in ("hc", "ou", "ht_ou") else None
        res = fair_price_bb(b["home_en"], b["away_en"], b["sport"], sub, target_line=_target_line, status="live")
        if not res or not res["fair"]:
            return []
        fair = res["fair"]
        if sub == "1x2":
            idx = {"主": "home", "和": "draw", "客": "away"}
        elif sub == "ht":
            idx = {"主": "home", "和": "draw", "客": "away"}  # 上半场独赢(同 1x2 三向)
        elif sub == "hc":
            idx = {"主": "home", "客": "away"}
        elif sub == "dc":
            idx = {"主/和": "1X", "客/和": "X2", "主/客": "12"}
        elif sub == "btts":
            idx = {"双方进球": "yes", "非双方进球": "no"}
        else:  # ou / ht_ou
            idx = {"大": "over", "小": "under"}
        k = idx.get(d)
        fair_p = fair.get(k)
        if not fair_p or fair_p <= 1:
            return []
        # hc/ou 线匹配: BB 线必须 = Betfair 线(否则比的是不同线的价, 假EV)。
        # 2026-09-22 收紧 0.25→0.01: 之前 0.25 容差允许 BB线0.5 匹配 Betfair线0.25(差0.25不被拦),
        # 是假溢价的又一处漏。线是 0.5 整数倍, 正确匹配差=0, 任何差>0.01 都是错配。
        if sub in ("hc", "ou", "ht_ou") and mk.get("line") is not None and fair.get("line") is not None:
            if abs(float(mk["line"]) - float(fair["line"])) > 0.01:
                return []
        ev = (bb_odds - fair_p) / fair_p * 100.0
        _conf = res.get("confidence")
        _conf_p = _conf.get(k) if _conf else None
        _sbo_dir = 'none' if _conf_p is None else ('same' if _conf_p > fair_p else 'diff')
        base = {
            "bb_match_id": bmid,
            "home": b.get("home_cn") or b["home_en"],
            "away": b.get("away_cn") or b["away_en"],
            "league_cn": b.get("league_cn", ""),
            "sport": b["sport"],
            "sub": sub, "direction": d,
            "bb_odds": bb_odds, "fair": fair_p, "ev": round(ev, 2), "pin_raw": 0,
            "market_id": mk["market_id"], "option_type": mk["option_type"], "line": mk["line"],
            "pin_matchup_id": res.get("event_id"),
            "spread": res.get("spread"),  # 2026-09-19 流动性门槛: back-lay 价差
            "league_id": None,
            "max_stake": 0,
            "sc": b.get("sc"),
            "bb_ts": _bb_ts, "pin_ts": _bb_ts,
            "poll_ts": _poll_ts,  # 2026-09-20 poll开始时间; 2026-09-23 改原始WS触发时刻(端到端耗时)
            "platform": "BB",
        }
        if _sbo_dir == 'diff':
            # 2026-09-20 diff 从「跳过」改「半额投注」后, 也必须过滤 EV 上下限——
            # 之前只拦高 EV(>20% 线错配假EV), 漏了负 EV: 负 EV diff(如 -14%) 也进观察库污染统计
            # (2026-09-21 修: 补 ev < threshold 下限, 与 same 同口径)。
            if ev < threshold or ev > 20.0:
                return []
            base["sbo_confirm"] = False
            base["sbo_direction"] = "diff"
            return [base]
        if ev < threshold or ev > 20.0:
            # 2026-09-20 上限 12%→20%(与早盘对齐): 让球真edge能到+7.6pp, 12%会误伤; 20%仍挡+50%线错配假EV
            return []
        base["sbo_confirm"] = (_sbo_dir == 'same')
        base["sbo_direction"] = _sbo_dir
        return [base]

    opps = []
    if tasks:
        # 并发公平价匹配(2026-09-20): 之前串行 ~11s, 现并发 ~2-3s。线程安全: get_odds 读 WS 缓存有 _lock,
        # _events_cache/_odds_cache 是模块级 dict, 并发写是良性竞态(同值覆盖)。
        with ThreadPoolExecutor(max_workers=min(len(tasks), 16)) as ex:
            for out in ex.map(_process, tasks):
                opps.extend(out)
    _t_done = time.time()
    if tasks:
        print(f"[oa] 耗时: BB拉取{_bb_ts-_t0:.1f}s + 公平价匹配{_t_done-_bb_ts:.1f}s = 总{_t_done-_t0:.1f}s ({len(tasks)}任务)", flush=True)
    return opps


def match_live_bb_pin(platform="BB"):
    """匹配 BB/FB 滚球 ↔ Pin 滚球(英文队名), 返回 {bb_match_id: {pin_matchup_id, home, away, moneyline}}。"""
    bb = fetch_bb_live_matches(platform=platform)
    pin = fetch_live_fair_prices()
    # Pin 按 (norm_home, norm_away) 索引(精确匹配 O(1))
    pin_by_name = {}
    pin_list = []
    for mid, v in pin.items():
        pin_by_name[(_norm(v["home"]), _norm(v["away"]))] = (mid, v)
        pin_list.append((mid, v))

    def _build(bmid, b, pin_mid, pv):
        return {
            "pin_matchup_id": pin_mid,
            "home": pv["home"], "away": pv["away"],
            "home_cn": b.get("home_cn", "") or pv["home"],  # BB 中文队名(展示), 兜底 Pin 英文
            "away_cn": b.get("away_cn", "") or pv["away"],
            "moneyline": pv["moneyline"],  # [主, 和, 客] 十进制
            "spread": pv.get("spread", {}),
            "total": pv.get("total", {}),
            "league_id": pv.get("league_id"),
            "league_name": pv.get("league_name", ""),  # 联赛名(通知展示用)
            "league_cn": b.get("league_cn", "") or pv.get("league_name", ""),  # BB 中文联赛名
            "max_stake": pv.get("max_stake", 0),
            "sport": b["sport"],
            "mc": b.get("mc", 0),  # 比赛进行秒数(纯比赛时间)
            "sc": b.get("sc"),  # [主,客] 当前比分(下注瞬间, 让球按当前比分结算用)
        }

    result = {}
    for bmid, b in bb.items():
        key = (_norm(b["home_en"]), _norm(b["away_en"]))
        hit = pin_by_name.get(key)
        if hit:
            result[bmid] = _build(bmid, b, *hit)
            continue
        # 精确匹配失败 → 模糊兜底(美足全名"Nebraska Cornhuskers" vs Pin短名"Nebraska"、
        # 网球个人名"Sun Qian" vs "Qian Sun" 顺序反转)。用 _match_score(子串/rapidfuzz/token反转)。
        h1, a1 = b["home_en"], b["away_en"]
        best = None
        best_sc = 0.0
        for mid, pv in pin_list:
            sc = _match_score(h1, a1, pv["home"], pv["away"])
            if sc > best_sc:
                best_sc = sc
                best = (mid, pv)
            if sc >= 1.0:
                break
        if best and best_sc >= 0.5:
            result[bmid] = _build(bmid, b, *best)
    return result


def reverify_live_markets(pin_matchup_id, league_id, allow_closed=False):
    """下注前重拉指定联赛 markets, 返回该滚球 match 最新 {moneyline, spread, total}。

    用于滚球延迟修正: 缓存(30s)可能过期, 下单前重验 Pin 滚球价是否已漂移。返回 None=失败。
    moneyline: [home, draw, away] 十进制(无则 None); spread/total: {line: [dec1, dec2]}。

    allow_closed: 只给「下注后 CLV」路径开(closed 的最后一笔价 = 真收盘线); 下单前验价必须
    False(绝不能拿 closed 的 stale 价下单)。closed 盘口若无有效 prices 则 len 检查自然过滤, 无副作用。
    """
    from src.scrapers.pinnacle_api import SESSION, API_BASE, _load_cookie
    _load_cookie()
    try:
        _live_reserve()
        r = SESSION.get(f"{API_BASE}/leagues/{league_id}/markets/straight", timeout=30)
        mks = [k for k in r.json() if str(k.get("matchupId")) == str(pin_matchup_id)]
        result = {"moneyline": None, "spread": {}, "total": {}}
        _ok_status = ("open", "closed") if allow_closed else ("open",)
        for k in mks:
            # 只取全场(period=0): 验价/CLV 复验都只针对全场盘口, 半场线混入会错配
            if (k.get("status") not in _ok_status or k.get("period") != 0):
                continue
            t = k.get("type"); prices = k.get("prices", [])
            if t == "moneyline" and len(prices) >= 2:
                pbd = {p.get("designation"): p.get("price") for p in prices}
                if len(prices) >= 3:
                    result["moneyline"] = _us_to_decimal([pbd.get(d) for d in ("home", "draw", "away")])
                else:
                    # 2-way(网球/篮球等无和局): 只 home/away, 供 opportunities 二路 devig
                    result["moneyline"] = _us_to_decimal([pbd.get(d) for d in ("home", "away")])
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
