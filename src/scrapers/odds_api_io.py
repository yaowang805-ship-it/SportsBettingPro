"""odds-api.io 实时赔率接入 — 交易所 sharp 公平价(替代 Pin 的 15 分钟陈旧)。

平台: https://api.odds-api.io/v3 (与 oddspapi.io 是两家公司, 别混)。
已选书商: Betfair Exchange + Orbit Exchange(两家交易所, 无 margin = sharp 公平价)。
关键: 交易所赔率带 back/lay, 公平价取中间价, 不用 devig(交易所本身无抽水)。

盘口映射(BB 子盘口 → odds-api.io 市场名):
  1x2(全场独赢) → ML
  ht(上半场独赢) → ML HT          ← 早盘 ht 主胜 的核心
  hc(让球)       → Spread
  ou(大小)       → Totals
  ht_ou(上半大小) → Totals HT
  dc(双机会)     → Double Chance
  btts(双边进球) → Both Teams To Score
"""
import json
from pathlib import Path

import requests

from config.settings import ODDS_API_IO_KEY, ODDS_API_IO_BASE, ODDS_API_IO_BOOKMAKERS

ROOT = Path(__file__).resolve().parent.parent.parent

# BB 子盘口 → odds-api.io 市场名
_SUB_TO_MARKET = {
    "1x2": "ML",
    "ht": "ML HT",
    "hc": "Spread",
    "ou": "Totals",
    "ht_ou": "Totals HT",
    "dc": "Double Chance",
    "btts": "Both Teams To Score",
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


def mid_price(back, lay):
    """交易所中间价: 概率空间 (1/back + 1/lay)/2 的倒数。back/lay 是十进制赔率。

    交易所 back=买入价(结果发生), lay=卖出价(结果不发生)。
    无 margin 公平概率 p ≈ (1/back + (1 - 1/lay)) / 2, 但用 (1/back+1/lay)/2 更稳(对称)。
    返回十进制公平价。None 或 <=1 返回 None。
    """
    try:
        b, l = float(back), float(lay)
    except (TypeError, ValueError):
        return None
    if b <= 1 or l <= 1:
        return None
    p = (1.0 / b + 1.0 / l) / 2.0
    return round(1.0 / p, 4) if p > 0 else None


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
    if any(v is None for v in mids.values()):
        return None
    # 归一化: 三路概率和应为 1
    total = sum(1.0 / v for v in mids.values())
    if total <= 0:
        return None
    return {k: round(1.0 / ((1.0 / v) / total), 4) for k, v in mids.items()}


def get_sports():
    """运动列表 → [{name, slug}]。"""
    return _get("/sports") or []


def get_events(sport="football", status=None):
    """事件列表。status=None 全部, 'live' 滚球。→ [{id, home, away, date, sport, league, status}]。"""
    params = {"sport": sport}
    if status:
        params["status"] = status
    return _get("/events", params) or []


def get_odds(event_id, bookmakers=None):
    """单场赔率(含所有市场)。→ {bookmaker_name: [markets]}。

    market 结构: {"name": "ML", "updatedAt": "...", "odds": [{home/draw/away/layHome/...}]}。
    """
    bks = bookmakers or ODDS_API_IO_BOOKMAKERS
    params = {"eventId": event_id, "bookmakers": ",".join(bks)}
    d = _get("/odds", params)
    if not d:
        return {}
    return d.get("bookmakers", {}) or {}


def fair_price(event_id, sub_market, bookmakers=None):
    """提取某场比赛某盘口的交易所公平价(两家书商平均)。

    sub_market: '1x2'/'ht' 返回 {home,draw,away}; 'ou' 返回 {over,under}+线; 'hc' 返回 {home,away}+线。
    返回 dict(含各方向公平价 + line) 或 None。
    """
    market_name = _SUB_TO_MARKET.get(sub_market)
    if not market_name:
        return None
    odds = get_odds(event_id, bookmakers)
    if not odds:
        return None
    # 两家书商都取, 平均公平价
    results = []
    for bk_name, markets in odds.items():
        m = next((x for x in (markets or []) if x.get("name") == market_name), None)
        if not m:
            continue
        o = (m.get("odds") or [{}])[0]
        if sub_market in ("1x2", "ht"):
            fair = _fair_three_way(o)
            if fair:
                results.append(fair)
        elif sub_market in ("ou", "ht_ou"):
            # over/under: 有 hdp(线) + over/under + layOver/layUnder
            line = o.get("hdp")
            over = mid_price(o.get("over"), o.get("layOver"))
            under = mid_price(o.get("under"), o.get("layUnder"))
            if line is not None and over and under:
                results.append({"over": over, "under": under, "line": line})
        elif sub_market == "hc":
            line = o.get("hdp")
            home = mid_price(o.get("home"), o.get("layHome"))
            away = mid_price(o.get("away"), o.get("layAway"))
            if line is not None and home and away:
                results.append({"home": home, "away": away, "line": line})
    if not results:
        return None
    # 两家书商平均(有 line 的用最后一个的 line, 因为线可能不同)
    out = {}
    for k in results[0]:
        vals = [r.get(k) for r in results if r.get(k) is not None]
        if not vals:
            continue
        out[k] = round(sum(vals) / len(vals), 4) if isinstance(vals[0], (int, float)) else vals[0]
    return out


def match_bb_to_oa(sport_id):
    """BB 运动 id → odds-api.io slug。"""
    return _SPORT_ID_TO_SLUG.get(sport_id)


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
