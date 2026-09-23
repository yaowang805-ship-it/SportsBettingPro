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
}
PIN_TO_BB = {v: k for k, v in BB_TO_PIN.items()}

# 免费档缓存: 15min TTL, 省 100/天额度(早盘赔率小时级变, 15min 够; 滚球实时另说)
_PIN_TTL = 900
_pin_cache = {}  # {(sport_id, event_type): (ts, data)}


def _norm(name):
    """队名归一化(与 odds_api_io._norm_team 同口径): 小写+去非字母数字。"""
    import re
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


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


def _devig_two_way(o1, o2):
    """两路盘比例去水 → (fair1, fair2)。返回十进制公平价或 None。"""
    try:
        o1, o2 = float(o1), float(o2)
    except (TypeError, ValueError):
        return None
    if o1 <= 1 or o2 <= 1:
        return None
    p1, p2 = 1.0 / o1, 1.0 / o2
    s = p1 + p2
    if s <= 0:
        return None
    p1, p2 = p1 / s, p2 / s
    return round(1.0 / p1, 4), round(1.0 / p2, 4)


def _devig_three_way(home, draw, away):
    """三路盘比例去水 → {'home':..,'draw':..,'away':..} 或 None。"""
    try:
        home, draw, away = float(home), float(draw), float(away)
    except (TypeError, ValueError):
        return None
    if home <= 1 or draw <= 1 or away <= 1:
        return None
    ph, pd, pa = 1.0 / home, 1.0 / draw, 1.0 / away
    s = ph + pd + pa
    if s <= 0:
        return None
    return {'home': round(1.0 / (ph / s), 4),
            'draw': round(1.0 / (pd / s), 4),
            'away': round(1.0 / (pa / s), 4)}


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


def pinnacle_fair_price(home, away, sport_id, sub_market, target_line=None, event_type='prematch'):
    """从 Pinnacle 赔率算去水公平价(多锚共识的无偏基准)。

    sub_market: '1x2'→{home,draw,away}; 'hc'→{home,away}+line; 'ou'→{over,under}+line;
                'dc'→由 1x2 合成 {1X,12,X2}。
    返回 dict(含 line) 或 None。target_line: hc/ou 的 BB 线(在 Pinnacle 线里找最接近的)。
    """
    pin_sid = BB_TO_PIN.get(sport_id)
    if not pin_sid:
        return None
    data = fetch_pinnacle_markets(pin_sid, event_type)
    e = find_pinnacle_event(data, home, away)
    if not e:
        return None
    periods = e.get('periods') or {}
    full = periods.get('num_0') or (list(periods.values())[0] if periods else None)
    if not full:
        return None
    if sub_market in ('1x2', 'ht'):
        ml = full.get('money_line')
        if not ml:
            return None
        fair = _devig_three_way(ml.get('home'), ml.get('draw'), ml.get('away'))
        return fair
    if sub_market in ('hc', 'ht_hc'):
        spreads = full.get('spreads') or {}
        if not spreads:
            return None
        # 找最接近 target_line 的线(或主线 hdp=0)
        o = _select_line(spreads, target_line)
        if not o:
            return None
        pair = _devig_two_way(o.get('home'), o.get('away'))
        if pair is None:
            return None
        return {'home': pair[0], 'away': pair[1], 'line': o.get('hdp')}
    if sub_market in ('ou', 'ht_ou'):
        totals = full.get('totals') or {}
        if not totals:
            return None
        o = _select_line(totals, target_line)
        if not o:
            return None
        pair = _devig_two_way(o.get('over'), o.get('under'))
        if pair is None:
            return None
        return {'over': pair[0], 'under': pair[1], 'line': o.get('points')}
    if sub_market == 'dc':
        ml = full.get('money_line')
        if not ml:
            return None
        three = _devig_three_way(ml.get('home'), ml.get('draw'), ml.get('away'))
        if not three:
            return None
        ph = 1.0 / three['home']; pd = 1.0 / three['draw']; pa = 1.0 / three['away']
        s = ph + pd + pa
        ph, pd, pa = ph / s, pd / s, pa / s
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


def consensus_fair_price(home, away, sport_id, sub_market, target_line=None, status=None):
    """三锚共识公平价: Pinnacle(去水) + Betfair(交易所中间价) + SBO(去水)。

    返回 {'fair': 共识公平价, 'pinnacle': Pinnacle公平价, 'betfair': Betfair公平价,
          'sbo': SBO公平价, 'event_id', 'spread'} 或 None。
    Pinnacle 是黄金 sharp 基准(0.24%效率), 等权参与共识专门拉平 Betfair 单锚的长腿 bias。
    下月切 Pinnacle WS 单锚时, 只需把这里改成「只返回 pinnacle」即可。
    """
    from src.scrapers.odds_api_io import fair_price_bb, sbo_fair_price, match_event_orient

    # 1. Betfair + SBO(现有锚)
    bf = fair_price_bb(home, away, sport_id, sub_market, target_line=target_line, status=status)
    if not bf:
        return None
    betfair = bf.get('fair')
    sbo = bf.get('confidence')

    # 2. Pinnacle(新锚)
    _et = 'live' if status == 'live' else 'prematch'
    pinnacle = pinnacle_fair_price(home, away, sport_id, sub_market, target_line=target_line,
                                   event_type=_et)

    # 3. 共识(等权平均隐含概率)
    #    权重: Pinnacle 0.5(黄金基准), Betfair 0.3, SBO 0.2
    fair = _combine_anchors([(pinnacle, 0.5), (betfair, 0.3), (sbo, 0.2)])
    if not fair:
        # 至少 Betfair 有(单锚兜底)
        fair = betfair
    # line 从 Betfair/Pinnacle 取(线对齐用)
    if 'line' not in fair:
        for src in (pinnacle, betfair):
            if src and src.get('line') is not None:
                fair['line'] = src['line']
                break
    return {'fair': fair, 'pinnacle': pinnacle, 'betfair': betfair, 'sbo': sbo,
            'event_id': bf.get('event_id'), 'spread': bf.get('spread')}


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
