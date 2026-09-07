"""OddsPapi 双锚数据源 — 拿 Betfair Exchange 真实赔率做 Pin 交叉验证。

目的(2026-08-30): Pin 对某些盘口(尤其 ht 上半场)有系统性偏差(平局定低, 真实平局57%
vs 隐含46%), 导致 ht_dc 等盘口 CLV 假正、实盘 ROI 负。用 Betfair Exchange(无 margin
真实市场)做第二锚点, 交叉验证 Pin 的 fair price, 发现偏差。

数据源: OddsPapi 聚合API(355 bookmaker), bookmaker=betfair-ex。
  - market 101   = Full Time Result (1X2), outcomes 101/102/103 = home/draw/away
  - market 10208 = First Half Result (ht独赢), outcomes 10208/10209/10210 = home/draw/away
  每个 outcome 的 players[0] 有 price(赔率) + exchangeMeta.availableToBack(真实挂单深度)

免费 500 次/天, 只对重点联赛查, 不逐场调用。
"""
import requests
from config.settings import ODDSPAPI_KEY, ODDSPAPI_BASE

# 足球联赛 tournamentId 映射(常用, 避免每次查 /tournaments 浪费配额)
SOCCER_TOURNAMENTS = {
    "英超": 17, "英冠": 18, "西甲": 8, "意甲": 23, "法甲": 34, "德甲": 35,
    "欧冠": 7, "欧联": None, "荷甲": 37, "葡超": None, "比甲": 38,
}

# market ID → (sub_market, outcome偏移)
# 1X2: outcome 101/102/103 = home/draw/away
# ht:  outcome 10208/10209/10210 = home/draw/away
_ANCHOR_MARKETS = {
    "1x2": {"market": "101", "home": "101", "draw": "102", "away": "103"},
    "ht": {"market": "10208", "home": "10208", "draw": "10209", "away": "10210"},
}


def _best_back_price(outcome: dict) -> float:
    """取 Betfair Exchange 真实最佳 back 价格(无 margin 市场的买价)。"""
    try:
        meta = outcome.get("players", {}).get("0", {}).get("exchangeMeta", {})
        backs = meta.get("availableToBack", [])
        if backs:
            return float(backs[0].get("price", 0))
    except Exception:
        pass
    # 降级: 用 price 字段
    try:
        return float(outcome.get("players", {}).get("0", {}).get("price", 0))
    except Exception:
        return 0.0


def _extract_three_way(market: dict, cfg: dict):
    """从 OddsPapi market 提取 3-way 赔率 (home, draw, away)。"""
    if not market:
        return None
    outcomes = market.get("outcomes", {})
    def _p(key):
        return _best_back_price(outcomes.get(key, {}))
    h, d, a = _p(cfg["home"]), _p(cfg["draw"]), _p(cfg["away"])
    if h > 1.0 and d > 1.0 and a > 1.0:
        return {"home": h, "draw": d, "away": a}
    return None


def _fetch_participant_names(tournament_ids):
    """拿 participantId → 队名映射(通过 /fixtures)。/fixtures 只支持单数 tournamentId, 逐个查。"""
    name_map = {}
    ids = tournament_ids.split(",") if isinstance(tournament_ids, str) else tournament_ids
    for tid in ids:
        try:
            r = requests.get(
                f"{ODDSPAPI_BASE}/fixtures",
                params={"apiKey": ODDSPAPI_KEY, "sportId": 10, "tournamentId": str(tid).strip()},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            continue
        for fx in data:
            p1, p2 = fx.get("participant1Id"), fx.get("participant2Id")
            n1, n2 = fx.get("participant1Name"), fx.get("participant2Name")
            if p1 and n1:
                name_map[str(p1)] = n1
            if p2 and n2:
                name_map[str(p2)] = n2
    return name_map


def fetch_betfair_anchor(tournament_ids, participants_map=None):
    """拿 Betfair Exchange 的 1X2 + ht 独赢赔率(含队名)。

    Args:
        tournament_ids: list[str] 或逗号分隔字符串, 如 [17, 8, 23]
        participants_map: 可选, 预置 {participantId: name}(避免重复调 /fixtures)

    Returns:
        list of {fixtureId, tournamentId, startTime, home, away,
                 "1x2": {home,draw,away} 或 None, "ht": {home,draw,away} 或 None}
    """
    if not ODDSPAPI_KEY:
        return []
    if isinstance(tournament_ids, (list, tuple)):
        tournament_ids = ",".join(str(t) for t in tournament_ids)
    try:
        r = requests.get(
            f"{ODDSPAPI_BASE}/odds-by-tournaments",
            params={
                "apiKey": ODDSPAPI_KEY,
                "bookmaker": "betfair-ex",
                "tournamentIds": tournament_ids,
                "oddsFormat": "decimal",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ❌ OddsPapi Betfair 拉取失败: {e}")
        return []

    if participants_map is None:
        participants_map = _fetch_participant_names(tournament_ids)

    out = []
    for fx in data:
        bf = fx.get("bookmakerOdds", {}).get("betfair-ex", {})
        markets = bf.get("markets", {})
        p1, p2 = str(fx.get("participant1Id", "")), str(fx.get("participant2Id", ""))
        rec = {
            "fixtureId": fx.get("fixtureId"),
            "tournamentId": fx.get("tournamentId"),
            "startTime": fx.get("startTime"),
            "home": participants_map.get(p1, ""),
            "away": participants_map.get(p2, ""),
            "1x2": _extract_three_way(markets.get("101"), _ANCHOR_MARKETS["1x2"]),
            "ht": _extract_three_way(markets.get("10208"), _ANCHOR_MARKETS["ht"]),
        }
        if rec["1x2"] or rec["ht"]:
            out.append(rec)
    return out


# Pin league_name 关键词 → OddsPapi tournamentId(只映射 Betfair 有 ht 覆盖的主流联赛)
PIN_LEAGUE_TOURNAMENT_KW = [
    ("Premier League", 17), ("Championship", 18), ("Serie A", 23),
    ("La Liga", 8), ("Ligue 1", 34), ("Bundesliga", 35),
    ("Champions League", 7), ("Eredivisie", 37), ("Primeira Liga", None),
]


def _league_to_tournament(league_name):
    """Pin league_name → OddsPapi tournamentId(关键词匹配)。"""
    if not league_name:
        return None
    for kw, tid in PIN_LEAGUE_TOURNAMENT_KW:
        if kw.lower() in str(league_name).lower():
            return tid
    return None


def pin_3way_anchor_compare(pin_ml, betfair_3way):
    """对比 Pin 3-way(独赢/半场) vs Betfair 3-way, 检测 Pin 各腿偏差。

    检测主/平/客三腿的隐含概率偏差, 返回偏差最大的腿。≥3pp 视为 Pin 偏差(假 EV 源)。
    """
    if not pin_ml or not betfair_3way:
        return None
    def imp(odds):
        return 1.0 / odds if odds and odds > 1 else 0.0
    if len(pin_ml) < 3:
        return None
    pin_h, pin_d, pin_a = float(pin_ml[0]), float(pin_ml[1]), float(pin_ml[2])
    bf_h, bf_d, bf_a = betfair_3way["home"], betfair_3way["draw"], betfair_3way["away"]
    pin_sum = imp(pin_h) + imp(pin_d) + imp(pin_a)
    bf_sum = imp(bf_h) + imp(bf_d) + imp(bf_a)
    if pin_sum <= 0 or bf_sum <= 0:
        return None
    devs = {
        "home": (imp(pin_h) / pin_sum - imp(bf_h) / bf_sum) * 100,
        "draw": (imp(pin_d) / pin_sum - imp(bf_d) / bf_sum) * 100,
        "away": (imp(pin_a) / pin_sum - imp(bf_a) / bf_sum) * 100,
    }
    max_label = max(devs, key=lambda k: abs(devs[k]))
    return {
        "deviations": {k: round(v, 1) for k, v in devs.items()},
        "max_label": max_label,
        "max_dev_pp": round(devs[max_label], 1),
        "flagged": abs(devs[max_label]) >= 3.0,
    }


def pin_ht_anchor_compare(pin_ht_ml, betfair_ht):
    """兼容旧名: 只返回平局偏差(等价 pin_3way_anchor_compare 的 draw 腿)。"""
    r = pin_3way_anchor_compare(pin_ht_ml, betfair_ht)
    if not r:
        return None
    return {
        "draw_dev_pp": r["deviations"]["draw"],
        "flagged": abs(r["deviations"]["draw"]) >= 3.0,
    }


def _similar(a, b):
    """宽松队名匹配(归一化后 精确 或 子串)。"""
    import re as _re
    def _n(s):
        if not s:
            return ""
        s = str(s).lower()
        s = _re.sub(r"\b(fc|afc|sc|cf)\b", "", s)
        s = _re.sub(r"[&.\-]", " ", s)
        return _re.sub(r"\s+", " ", s).strip()
    a, b = _n(a), _n(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _swap_3way(bf_3way):
    """主客互换(匹配到反向队名时用)。"""
    if not bf_3way:
        return None
    return {"home": bf_3way["away"], "draw": bf_3way["draw"], "away": bf_3way["home"]}


def _dev_mult(dev_pp):
    """按偏差梯度降权: ≥10pp 拦截, 6-10pp 压到30%, 3-6pp 压到50%。"""
    return 0.0 if dev_pp >= 10 else (0.3 if dev_pp >= 6 else 0.5)


def _downweight_markets(e, markets, mult):
    """把指定盘口(_market 属于 markets)的 EV 乘以 mult。"""
    for _field in ("opportunities", "handicap", "over_under",
                   "double_chance", "draw_no_bet"):
        for opp in e.get(_field, []):
            if opp.get("_market") in markets:
                opp["ev_pct"] = round(opp.get("ev_pct", 0) * mult, 2)


def cross_validate_entries(entries):
    """Betfair 双锚交叉验证(全盘口): 对比 Pin vs Betfair Exchange 的 1X2(全场) + ht(半场)。

    检测 Pin 各腿(主/平/客)系统性偏差, 偏差≥3pp 就标记 flags + 降权受影响的盘口 EV。
    只对联赛可映射到 OddsPapi 的主流联赛检查(免费配额 250次/天限制)。
    """
    if not ODDSPAPI_KEY or not entries:
        return entries
    from collections import defaultdict
    by_tournament = defaultdict(list)
    for e in entries:
        has_1x2 = e.get("_pin_ml") and len(e.get("_pin_ml", [])) >= 3
        has_ht = e.get("_pin_ht_ml") and len(e.get("_pin_ht_ml", [])) >= 3
        if not (has_1x2 or has_ht):
            continue
        tid = _league_to_tournament(e.get("league", ""))
        if tid is None:
            continue
        by_tournament[tid].append(e)
    if not by_tournament:
        return entries

    # 批量查 Betfair(每批最多3个tournamentId, Betfair Exchange 限制)
    tids = list(by_tournament.keys())
    for i in range(0, len(tids), 3):
        batch = tids[i:i + 3]
        try:
            bf = fetch_betfair_anchor(batch)
        except Exception:
            continue
        bf_map = {(r["home"], r["away"]): r for r in bf}
        for tid in batch:
            for e in by_tournament[tid]:
                home, away = e.get("home_pin", ""), e.get("away_pin", "")
                matched = None
                for (bh, ba), r in bf_map.items():
                    if _similar(home, bh) and _similar(away, ba):
                        matched = r
                        break
                    if _similar(home, ba) and _similar(away, bh):
                        matched = {"1x2": _swap_3way(r.get("1x2")), "ht": _swap_3way(r.get("ht"))}
                        break
                if not matched:
                    continue
                # 1. 全场 1X2 双锚
                if e.get("_pin_ml") and matched.get("1x2"):
                    cmp = pin_3way_anchor_compare(e["_pin_ml"], matched["1x2"])
                    if cmp and cmp["flagged"]:
                        mult = _dev_mult(abs(cmp["max_dev_pp"]))
                        e.setdefault("flags", []).append(
                            f"Pin 1x2{cmp['max_label']}偏差{cmp['max_dev_pp']:+.0f}pp(降权×{mult:.1f})")
                        _downweight_markets(e, ("1x2", "hc", "ou", "dc", "dnb"), mult)
                # 2. 半场 ht 双锚
                if e.get("_pin_ht_ml") and matched.get("ht"):
                    cmp = pin_3way_anchor_compare(e["_pin_ht_ml"], matched["ht"])
                    if cmp and cmp["flagged"]:
                        mult = _dev_mult(abs(cmp["max_dev_pp"]))
                        e.setdefault("flags", []).append(
                            f"Pin ht{cmp['max_label']}偏差{cmp['max_dev_pp']:+.0f}pp(降权×{mult:.1f})")
                        _downweight_markets(e, ("ht", "ht_hc", "ht_ou", "ht_dc", "ht_dnb"), mult)
    return entries


# 兼容旧调用名
cross_validate_ht_entries = cross_validate_entries
