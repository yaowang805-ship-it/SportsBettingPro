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


def fetch_betfair_anchor(tournament_ids, participants_map=None):
    """拿 Betfair Exchange 的 1X2 + ht 独赢赔率。

    Args:
        tournament_ids: list[str] 或逗号分隔字符串, 如 [17, 8, 23]
        participants_map: 可选, {participantId: name} 用于队名映射(可后续补)

    Returns:
        list of {fixtureId, tournamentId, startTime, participant1Id, participant2Id,
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

    out = []
    for fx in data:
        bf = fx.get("bookmakerOdds", {}).get("betfair-ex", {})
        markets = bf.get("markets", {})
        rec = {
            "fixtureId": fx.get("fixtureId"),
            "tournamentId": fx.get("tournamentId"),
            "startTime": fx.get("startTime"),
            "participant1Id": fx.get("participant1Id"),
            "participant2Id": fx.get("participant2Id"),
            "1x2": _extract_three_way(markets.get("101"), _ANCHOR_MARKETS["1x2"]),
            "ht": _extract_three_way(markets.get("10208"), _ANCHOR_MARKETS["ht"]),
        }
        if rec["1x2"] or rec["ht"]:
            out.append(rec)
    return out


def pin_ht_anchor_compare(pin_ht_ml, betfair_ht):
    """对比 Pin 的 ht 独赢 vs Betfair 的 ht 独赢, 检测 Pin 平局偏差。

    Returns:
        dict: {draw_dev_pp: 平局隐含概率差(pp), pin_draw_imp, bf_draw_imp, flagged: bool}
    """
    if not pin_ht_ml or not betfair_ht:
        return None
    def imp(odds):
        return 1.0 / odds if odds and odds > 1 else 0.0
    # Pin ht 独赢: [home, draw, away] (3 个)
    if len(pin_ht_ml) < 3:
        return None
    pin_h, pin_d, pin_a = pin_ht_ml[0], pin_ht_ml[1], pin_ht_ml[2]
    bf_h, bf_d, bf_a = betfair_ht["home"], betfair_ht["draw"], betfair_ht["away"]
    pin_draw_imp = imp(pin_d) / (imp(pin_h) + imp(pin_d) + imp(pin_a))
    bf_draw_imp = imp(bf_d) / (imp(bf_h) + imp(bf_d) + imp(bf_a))
    dev_pp = (pin_draw_imp - bf_draw_imp) * 100
    return {
        "pin_draw_imp": round(pin_draw_imp * 100, 1),
        "bf_draw_imp": round(bf_draw_imp * 100, 1),
        "draw_dev_pp": round(dev_pp, 1),
        "flagged": abs(dev_pp) >= 3.0,  # 平局隐含概率差 ≥3pp 视为 Pin 偏差
    }
