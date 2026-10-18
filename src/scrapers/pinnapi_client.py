"""Pinnacle 赔率拉取 (pinnapi.com, 替代 2025-07 关闭的 Pinnacle 官方 API, 2026-09-23)。

pinnapi 是独立服务(非 Pinnacle 官方), 提供实时 Pinnacle 赔率(实测 ~1s 新鲜, 非旧 guest API 的 15min CDN):
  - GET /kit/v1/markets?sport_id=X&event_type=live|prematch   → 全场+特殊盘口(moneyline/spreads/totals)
  - GET /kit/v1/prematch/fixtures?sport_id=X                  → 早盘赛程
  - 免费档 100 次/天; 付费 $99/月起有 SSE(15-40ms 赔率变动推送) + WS。

本模块: 拉取 + 缓存(省 100/天额度) + 去水算公平价。Pinnacle 是黄金 sharp 基准
(偏离完美效率仅 0.24%), 用作多锚共识的「无偏基准」, 专门拉平 Betfair 单锚的长腿 bias。
"""
import json
import time
from pathlib import Path

import requests

from config.settings import PINNAPI_KEY, PINNAPI_BASE

# BB 运动 id → Pinnacle 运动 id (Pinnacle: soccer=1 tennis=2 basketball=3 hockey=4
# football=5 baseball=6 rugby=7 mma=8 boxing=9 other=10 esports=11 golf=12)
BB_TO_PIN = {
    1: 1,    # 足球 → soccer
    3: 3,    # 篮球 → basketball
    5: 2,    # 网球 → tennis
    7: 6,    # 棒球 → baseball
    6: 5,    # 美式足球 → football
    2: 4,    # 冰球 → hockey
    18: 8,   # MMA → mma
    19: 9,   # 拳击 → boxing
    13: 10,  # 排球 → other
    47: 10,  # 羽毛球 → other(Pin 无羽毛球专属 id, 归 other)
}
PIN_TO_BB = {v: k for k, v in BB_TO_PIN.items()}

# 2026-09-24 去掉 15min 缓存: 用户要求 Pinnacle 数据实时(旧数据没用), 下月切实时WS。
# 免费 100/天额度下每次拉最新的(无缓存), 超限由 429 退避兜底(见 odds_api_io)。
_PIN_TTL = 0
_pin_cache = {}  # {(sport_id, event_type): (ts, data)}


def _norm(name):
    """队名归一化: 与 odds_api_io._norm_team 完全同口径(去 FC/SC/CF 后缀 + saint→st + 国家别名)。"""
    from src.scrapers.odds_api_io import _norm_team
    return _norm_team(name)


def fetch_pinnacle_markets(sport_id, event_type='prematch', since=None, force=False):
    """拉 Pinnacle 赔率(带 15min 缓存)。返回 API 原始 dict(sport_id/sport_name/events)。"""
    global _pin_cache
    key = (sport_id, event_type)
    now = time.time()
    if not force and key in _pin_cache and now - _pin_cache[key][0] < _PIN_TTL:
        return _pin_cache[key][1]
    if not PINNAPI_KEY:
        return None
    params = {'sport_id': sport_id, 'event_type': event_type}
    if since is not None:
        params['since'] = since
    try:
        r = requests.get(f"{PINNAPI_BASE}/kit/v1/markets", params=params,
                         headers={'x-portal-apikey': PINNAPI_KEY}, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    _pin_cache[key] = (now, data)
    return data


def find_pinnacle_event(data, home, away):
    """在 Pinnacle 响应里按队名找主比赛(跳过 corners/特殊盘)。返回 event dict 或 None。"""
    if not data:
        return None
    nh, na = _norm(home), _norm(away)
    best = None
    for e in data.get('events', []):
        eh, ea = _norm(e.get('home', '')), _norm(e.get('away', ''))
        if not eh or not ea:
            continue
        # 跳过角球/特殊盘(主客队名带 "Corners"/"Cards" 等)
        if 'corner' in eh or 'corner' in ea or 'card' in eh or 'card' in ea:
            continue
        if eh == nh and ea == na:
            return e
        if eh == na and ea == nh:  # 主客互换
            return e
        # 模糊兜底: 一个方向命中即可
        if eh == nh or eh == na or ea == nh or ea == na:
            best = e
    return best


_pin_cache_idx = {"_ts": 0.0, "_idx": {}}
_pin_cache_file = {"mtime": 0.0, "data": None}


def _pin_cache_find_event(home, away, games=False):
    """从 pin_odds_cache.json(实时缓存)按队名找事件。返回 event dict 或 None。

    只认有 periods 的全量事件; 缓存 >60s 视为失效。文件 mtime 缓存 + 队名索引, 二次调用 ~0ms。
    games=True(2026-10-14): 网球让局/总局数在 Pin 的 "(Games)" 子事件里, 键 = 归一化名+"games"。
    """
    try:
        f = Path(__file__).resolve().parent.parent.parent / "data" / "storage" / "pin_odds_cache.json"
        if not f.exists():
            return None
        mtime = f.stat().st_mtime
        if _pin_cache_file["mtime"] != mtime or _pin_cache_file["data"] is None:
            _pin_cache_file["data"] = json.loads(f.read_text())
            _pin_cache_file["mtime"] = mtime
        d = _pin_cache_file["data"]
        ts = d.get("ts", 0)
        if time.time() - ts > 60:
            return None
        if _pin_cache_idx.get("_ts") != ts:
            idx = {}
            # 2026-10-17 修队名撞车: 同俱乐部正赛 vs U21/女足/预备队, 队名一样但联赛不同。
            # 两个都是 parent 时按联赛消歧 —— 优先非青年/女足/预备队联赛(否则拿 U21 的价当正赛价)。
            def _junior(e):
                ln = (e.get("league_name") or "").lower()
                return any(k in ln for k in ("u21", "u20", "u19", "u18", "u17", "u23",
                                             "reserve", "youth", "women", "academy"))
            for entry in d.get("cache", {}).values():
                ev = entry.get("event") or {}
                if not ev.get("periods"):
                    continue
                eh, ea = _norm(ev.get("home", "")), _norm(ev.get("away", ""))
                if eh and ea:
                    # 2026-10-13 修: 同一队名有多个 event(全场 parent + 2-way 变体/角球 child),
                    # child 主客可能反转(Orlando Pirates 全场 parent home=1.19, 2-way child 却
                    # home=3.32), 按队名索引互相覆盖会命中反转 child → 比价方向错配出假高 ev。
                    # parent(parent_id 空)优先, child 只在不冲突时兜底。
                    parent_ref = ev.get("parent_id") or ev.get("parentId")
                    is_parent = not parent_ref
                    _key = (eh, ea)
                    _cur = idx.get(_key)
                    if _cur is None:
                        idx[_key] = ev
                    elif is_parent:
                        _cur_p = not (_cur.get("parent_id") or _cur.get("parentId"))
                        if not _cur_p or (_junior(_cur) and not _junior(ev)):
                            idx[_key] = ev  # child→parent 覆盖; junior parent→senior parent 覆盖
            _pin_cache_idx["_ts"] = ts
            _pin_cache_idx["_idx"] = idx
        _suf = "games" if games else ""
        return _pin_cache_idx["_idx"].get((_norm(home) + _suf, _norm(away) + _suf))
    except Exception:
        return None


_event_index_cache = {}  # (sport_id, event_type) -> {(norm_home,norm_away): event}


def _get_event_index(sport_id, event_type):
    """建队名 → event 索引(一次建好, O(1) 查), 避免 find_pinnacle_event 每次线性扫 2000 场。"""
    key = (sport_id, event_type)
    if key in _event_index_cache:
        return _event_index_cache[key]
    data = fetch_pinnacle_markets(sport_id, event_type)
    idx = {}
    if data:
        for e in data.get('events', []):
            eh, ea = _norm(e.get('home', '')), _norm(e.get('away', ''))
            if eh and ea:
                idx[(eh, ea)] = e
                idx[(ea, eh)] = e  # 主客互换也命中
    _event_index_cache[key] = idx
    return idx


def pinnacle_fair_price(home, away, sport_id, sub_market, target_line=None, event_type='prematch', use_rest_live=False):
    """从 Pinnacle 赔率算去水公平价(多锚共识的无偏基准)。

    sub_market: '1x2'→{home,draw,away}; 'hc'→{home,away}+line; 'ou'→{over,under}+line;
                'dc'→由 1x2 合成 {1X,12,X2}。
    返回 dict(含 line) 或 None。target_line: hc/ou 的 BB 线(在 Pinnacle 线里找最接近的)。
    优先读 WS 实时缓存(快), 否则 REST 兜底(带队名索引, O(1) 查)。
    去水: 2026-10-12 统一 Shin(devig_shin/shin_fair_odds), 与 clv_collector 收盘侧一致——
    之前用比例去水(_devig_three_way)导致下注侧/收盘侧 devig 口径不一致, EV_close 脏。
    """
    from src.scrapers.devig import shin_fair_odds, devig_shin
    pin_sid = BB_TO_PIN.get(sport_id)
    if not pin_sid:
        return None
    # 1) WS 实时缓存优先(秒级), 2) REST 兜底(15min 缓存 + 队名索引)
    # 2026-10-13: 2-way 运动(篮球/MMA/网球)BB 与 Pin 数据源主客标记可能相反(实测 13 场),
    # 正序查不到时反序再查, 标记 swapped → 后续把 fair 的 home/away 交换, 对齐 BB 主客方向。
    # 2026-10-14 网球让局(hc_games)/总局数(ou)在 Pin 的 "(Games)" 子事件里(主事件 num_0 的
    # spreads/totals 是让盘/总局盘数, 不是让局/总局局数)。games=True 用归一化名+"games" 键查子事件。
    _games = (sport_id == 5 and sub_market in ("hc_games", "ou"))
    _suf = "games" if _games else ""
    swapped = False
    e = None
    if use_rest_live:
        # 2026-10-18 复开价锚点: WS 缓存缺滚球 live 价(只有 prematch 冻结价), 改用 REST 拉 live 事件
        _data = fetch_pinnacle_markets(sport_id, 'live')
        if _data:
            for _ev in _data.get('events', []):
                if _norm(_ev.get('home', '')) == _norm(home) and _norm(_ev.get('away', '')) == _norm(away):
                    e = _ev
                    break
            if not e:
                for _ev in _data.get('events', []):
                    if _norm(_ev.get('home', '')) == _norm(away) and _norm(_ev.get('away', '')) == _norm(home):
                        e = _ev
                        swapped = True
                        break
    if not e:
        e = _pin_cache_find_event(home, away, games=_games)
        if not e:
            e = _pin_cache_find_event(away, home, games=_games)
            if e:
                swapped = True
    if not e:
        # 2026-10-17 禁用 REST 兜底: _get_event_index 拉 REST 4.31s, 滚球浪费窗口/早盘下单超时 20s。
        # 职业团队共识「REST for truth, WebSocket for delta」——REST 只做快照/恢复, 不做比价兜底。
        # 匹配不到就 return None(宁缺毋滥), 由上层放弃该盘口。
        return None
    periods = e.get('periods') or {}
    # 2026-10-11 修崩溃: Pin 元数据的 periods 是 list(比赛时段元数据, 无赔率), 只有
    # pin_client.update_markets 归一化后才是 dict({"num_0": {money_line/spreads/totals}})。
    # 收到元数据但还没收到 prematch_markets 流的场次 periods 仍是 list → 无公平价可算,
    # 返回 None 让上层 consensus 用 Betfair/SBO 兜底, 而不是 .get() 崩在 list 上。
    if not isinstance(periods, dict):
        return None
    # 半场盘口(ht/ht_hc/ht_ou)用 num_1(半场)赔率, 全场用 num_0 —— 2026-10-17 修 ht 半场赔率(之前 10-12 逃避式不定价)
    _num = 'num_1' if sub_market in ('ht', 'ht_hc', 'ht_ou') else 'num_0'
    # 2026-10-17 修: num_0(全场)缺失时不拿 num_1(半场)冒充。之前 fallback 到 list(periods.values())[0],
    # 对「只有半场没全场」的比赛(实测 cache 里 21 场)会把半场价当全场价 → 全场平局假高溢价。宁缺毋滥。
    full = periods.get(_num)
    if not full:
        return None
    if sub_market in ('1x2', 'ht'):
        # ht(半场独赢) 走这里: 2026-10-17 修 —— 之前用全场 num_0 冒充半场(错配), 现上面已选 num_1。
        # 半场独赢 = num_1 的 money_line(home/draw/away), 与全场独赢同结构, 共用去水逻辑。
        ml = full.get('money_line')
        if not ml:
            return None
        # 2026-10-11 修: 杯赛 2-way 独赢(draw=None)用两路去水, 3-way 用三路去水
        if ml.get('draw'):
            _fairs = shin_fair_odds([ml.get('home'), ml.get('draw'), ml.get('away')])
            fair = {'home': _fairs[0], 'draw': _fairs[1], 'away': _fairs[2]} if (_fairs[0] > 1 and _fairs[1] > 1 and _fairs[2] > 1) else None
        else:
            _fairs = shin_fair_odds([ml.get('home'), ml.get('away')])
            fair = {'home': _fairs[0], 'away': _fairs[1]} if (_fairs[0] > 1 and _fairs[1] > 1) else None
        if fair is not None:
            fair['_raw'] = {k: ml.get(k) for k in ('home', 'draw', 'away') if ml.get(k)}
            if swapped:
                fair['home'], fair['away'] = fair['away'], fair['home']
                if 'home' in fair['_raw'] and 'away' in fair['_raw']:
                    fair['_raw']['home'], fair['_raw']['away'] = fair['_raw']['away'], fair['_raw']['home']
        return fair
    if sub_market in ('hc', 'hc_games', 'ht_hc'):
        # ht_hc(上半场让球, 2026-10-18 补): num_1 的 spreads 与全场同结构, 共用去水/线匹配逻辑
        # hc_games(网球让局, 2026-10-14): 与 hc(让盘)共用逻辑, 事件已由上面 _games 定位到 "(Games)" 子事件
        spreads = full.get('spreads') or {}
        if not spreads:
            return None
        # 找最接近 target_line 的线(或主线 hdp=0)
        o = _select_line(spreads, target_line)
        if not o:
            return None
        # 2026-10-11 让球0(平局退款盘)是三路事件(主/平/客, 平=走盘), 却被两路去水把平局概率强压主客
        # → 公平价/EV/注码结构性偏差(与报价质量无关)。拒绝定价, 交由 LIVE_FILTER_LINE_ZERO 上游过滤。
        if target_line is not None:
            try:
                if abs(float(target_line)) < 1e-6:
                    from src.scrapers.stats import count as _sc
                    _sc("devig_path_mismatch")
                    return None
            except (TypeError, ValueError):
                pass
        _fairs = shin_fair_odds([o.get('home'), o.get('away')])
        if _fairs[0] <= 1 or _fairs[1] <= 1:
            return None
        if swapped:
            return {'home': _fairs[1], 'away': _fairs[0], 'line': o.get('hdp'),
                    '_raw': {'home': o.get('away'), 'away': o.get('home')}}
        return {'home': _fairs[0], 'away': _fairs[1], 'line': o.get('hdp'),
                '_raw': {'home': o.get('home'), 'away': o.get('away')}}
    if sub_market in ('ou', 'ht_ou'):
        # ht_ou(上半场大小, 2026-10-18 补): num_1 的 totals 与全场同结构, 共用去水/线匹配逻辑
        totals = full.get('totals') or {}
        if not totals:
            return None
        o = _select_line(totals, target_line)
        if not o:
            return None
        _fairs = shin_fair_odds([o.get('over'), o.get('under')])
        if _fairs[0] <= 1 or _fairs[1] <= 1:
            return None
        return {'over': _fairs[0], 'under': _fairs[1], 'line': o.get('points'),
                '_raw': {'over': o.get('over'), 'under': o.get('under')}}
    if sub_market == 'dc':
        ml = full.get('money_line')
        if not ml:
            return None
        _probs = devig_shin([ml.get('home'), ml.get('draw'), ml.get('away')])
        if not _probs or any(p <= 0 for p in _probs):
            return None
        ph, pd, pa = _probs
        if swapped:
            ph, pa = pa, ph  # 主客反了: 1X(主/和)↔X2(和/客)
        return {'1X': round(1.0 / (ph + pd), 4),
                '12': round(1.0 / (ph + pa), 4),
                'X2': round(1.0 / (pd + pa), 4)}
    return None


def _select_line(lines, target_line):
    """从 Pinnacle 线字典(键=hdp/points)里选最接近 target_line 的条目; 无 target 选主线(键=0)。"""
    if not lines:
        return None
    if target_line is not None:
        best, best_diff = None, 1e9
        for key, o in lines.items():
            try:
                d = abs(float(key) - float(target_line))
            except (TypeError, ValueError):
                continue
            if d < best_diff:
                best, best_diff = o, d
        return best
    # 主线: 键 0(让球)/或数值最小的(大小球)
    if '0' in lines:
        return lines['0']
    try:
        main_key = min(lines.keys(), key=lambda k: abs(float(k)))
    except (TypeError, ValueError):
        return None
    return lines[main_key]


def _combine_anchors(anchors):
    """多个锚点的公平价 → 共识公平价(隐含概率等权平均)。anchors = [(fair_dict, weight), ...]。

    fair_dict 是 {方向: 公平价}(如 {home,draw,away} 或 {home,away} 或 {over,under})。
    返回共识 {方向: 公平价}。某方向只取「有该方向且公平价>1」的锚点。
    """
    probs = {}   # 方向 -> [隐含概率, ...]
    weights = {}  # 方向 -> 权重和
    for fair, w in anchors:
        if not fair:
            continue
        for k, v in fair.items():
            if k in ('line', 'spread'):
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v <= 1:
                continue
            probs.setdefault(k, []).append((1.0 / v) * w)
            weights[k] = weights.get(k, 0) + w
    out = {}
    for k, plist in probs.items():
        if not plist:
            continue
        p = sum(plist) / weights[k]
        out[k] = round(1.0 / p, 4) if p > 0 else None
    return out or None


def consensus_fair_price(home, away, sport_id, sub_market, target_line=None, status=None, use_rest=None, skip_betfair=False):
    """三锚共识公平价: Pinnacle(去水) + Betfair(交易所中间价) + SBO(去水)。

    返回 {'fair': 共识公平价, 'pinnacle': Pinnacle公平价, 'betfair': Betfair公平价,
          'sbo': SBO公平价, 'event_id', 'spread'} 或 None。
    Pinnacle 是黄金 sharp 基准(0.24%效率), 等权参与共识专门拉平 Betfair 单锚的长腿 bias。
    2026-10-11 加 use_rest 透传 + skip_betfair(下单关键路径跳过 Betfair 交叉验证, 省 0.5-0.9s)。
    """
    # 2026-10-17 撤 Betfair/SBO: 纯 Pin 单锚, 无兜底。原 Betfair 交叉验证/兜底代码已删。
    _et = 'live' if status == 'live' else 'prematch'
    pinnacle = pinnacle_fair_price(home, away, sport_id, sub_market, target_line=target_line,
                                   event_type=_et)
    if not pinnacle:
        return None
    return {'fair': pinnacle, 'pinnacle': pinnacle, 'betfair': None, 'sbo': None,
            'fair_source': 'pinnacle', 'event_id': None, 'spread': None}


if __name__ == '__main__':
    # 自测: 拉早盘足球 + 算一场三锚共识公平价
    data = fetch_pinnacle_markets(1, 'prematch', force=True)
    print(f'早盘足球事件数: {len(data.get("events", [])) if data else 0}')
    if data:
        for e in data['events']:
            if 'corner' not in (e.get('home','')+e.get('away','')).lower():
                r = consensus_fair_price(e['home'], e['away'], 1, '1x2', status=None)
                print(f"{e['home']} vs {e['away']}:")
                print(f"  Pinnacle: {r['pinnacle']}")
                print(f"  Betfair:  {r['betfair']}")
                print(f"  SBO:      {r['sbo']}")
                print(f"  共识:     {r['fair']}")
                break
