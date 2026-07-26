"""Pinnacle 市场数据获取 — 联赛比赛+盘口数据

从 bb_vs_pinnacle.py 提取，保持函数签名兼容。
"""
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

from src.scrapers.pinnacle_api import get_decimal_price,  api_get, us_to_decimal

def sort_ml_prices(prices):
    """Sort moneyline prices to [home, draw, away] order by designation."""
    order = {"home": 0, "draw": 1, "away": 2}
    sorted_p = sorted(prices, key=lambda p: order.get(p.get("designation", ""), 99))
    return sorted_p


def get_league_matchups_and_markets(league_id):
    """Get matchups and markets for a specific league"""
    matchups = api_get(f"/leagues/{league_id}/matchups")
    if not matchups:
        return []

    mu_map = {m["id"]: m for m in matchups}

    markets = api_get(f"/leagues/{league_id}/markets/straight")
    if not markets:
        return []

    # 价格有效性校验: Pinnacle API cookie部分失效时返回null价格
    null_count = 0
    total_count = 0
    for m in markets:
        for p in m.get("prices", []):
            total_count += 1
            if get_decimal_price(p) is None:
                null_count += 1
    if total_count > 0 and null_count / total_count > 0.5:
        # 超过50%的价格为null → cookie失效, 数据不可用
        import logging
        logging.getLogger(__name__).warning(
            "Pinnacle API 价格无效: %d/%d null (%.0f%%) — cookie可能过期, 拒绝使用",
            null_count, total_count, null_count / total_count * 100)
        return []  # 返回空, 触发上层降级逻辑
    elif null_count > 0:
        import logging
        logging.getLogger(__name__).warning(
            "Pinnacle API 部分价格null: %d/%d (%.0f%%)",
            null_count, total_count, null_count / total_count * 100)

    mm = {}
    for m in markets:
        mid = m.get("matchupId")
        if mid not in mm:
            mm[mid] = []
        mm[mid].append(m)

    result = []
    for mid, mkt_list in mm.items():
        mu = mu_map.get(mid)
        if not mu:
            continue

        league = mu.get("league", {})
        participants = mu.get("participants", [])

        # 网球等运动：Pinnacle 顶层 participants 名称含 "(Games)" 后缀导致队名无法匹配，
        # 优先从 parent.participants 取干净名称
        parent_mu = mu.get("parent", {})
        parent_participants = parent_mu.get("participants", []) if parent_mu else []

        home, away = "", ""
        if parent_participants:
            for p in parent_participants:
                if p.get("alignment") == "home":
                    home = p.get("name", "")
                elif p.get("alignment") == "away":
                    away = p.get("name", "")
        if not home and not away:
            for p in participants:
                if p.get("alignment") == "home":
                    home = p.get("name", "")
                elif p.get("alignment") == "away":
                    away = p.get("name", "")

        # 标记此条目是否为 Games 类型（网球局数盘）
        _is_games = any(p.get("name", "").endswith(" (Games)") for p in participants)

        # 跳过非网球子比赛（DC、BTTS、正确比分、让球、大小等特殊市场），
        # 这些通过 parent 继承队名但有自己的 matchupId，不应在主列表中。
        # 网球 Games 盘是例外：它需要 parent 名称获取干净队名。
        if parent_participants and not _is_games:
            continue

        if not home and not away:
            continue

        moneyline, spread, total = [], [], []
        ht_moneyline, ht_spread, ht_total = [], [], []
        team_total = []
        btts = []
        double_chance = []
        draw_no_bet = []
        for mkt in mkt_list:
            mtype = mkt.get("type", "")
            per = mkt.get("period", 0)
            prices = [{
                "designation": p.get("designation", ""),
                "price_decimal": us_to_decimal(p.get("price")),
                "points": p.get("points"),  # handicap line / total line
            } for p in mkt.get("prices", [])]

            entry = {"period": per, "prices": prices}
            if mtype == "moneyline":
                entry["prices_sorted"] = sort_ml_prices(prices)
                if per == 0:
                    moneyline.append(entry)
                elif per == 1:
                    ht_moneyline.append(entry)
            elif mtype == "spread":
                if per == 0:
                    spread.append(entry)
                elif per == 1:
                    ht_spread.append(entry)
            elif mtype == "total":
                if per == 0:
                    total.append(entry)
                elif per == 1:
                    ht_total.append(entry)
            elif mtype == "team_total":
                entry["side"] = mkt.get("side", "")
                team_total.append(entry)
            elif mtype == "both_to_score" and per == 0:
                btts.append(entry)
            elif mtype == "double_chance" and per == 0:
                double_chance.append(entry)

        result.append({
            "matchup_id": mid,
            "league_name": league.get("name", ""),
            "league_group": league.get("group", ""),
            "home": home,
            "away": away,
            "start_time": mu.get("startTime", ""),
            "moneyline": moneyline,
            "spread": spread,
            "total": total,
            "ht_moneyline": ht_moneyline,
            "ht_spread": ht_spread,
            "ht_total": ht_total,
            "team_total": team_total,
		    "btts": btts,
		    "double_chance": double_chance,
            "draw_no_bet": draw_no_bet,
            "_is_games": _is_games,
        })

    # 对网球：把 Games 条目（局数让分/大小）合并到常规条目
    games_map = {}
    for r in result:
        if r.get("_is_games"):
            games_map[(r["home"], r["away"])] = {"spread": r["spread"], "total": r["total"]}
    for r in result:
        if not r.get("_is_games"):
            g = games_map.get((r["home"], r["away"]))
            if g:
                r["games_spread"] = g["spread"]
                r["games_total"] = g["total"]

    # 如果 Games 条目没有对应的非 Games 条目合并进去，直接用自己的 spread/total
    for r in result:
        if r.get("_is_games") and not r.get("games_total"):
            r["games_spread"] = r["spread"]
            r["games_total"] = r["total"]

    # 清理临时标记
    for r in result:
        r.pop("_is_games", None)

    # --- 第二阶段：从子比赛识别 Pinnacle 特殊市场 ---
    # Pinnacle 对 DC/BTTS 等特殊市场不返回独立 mtype，而是用子比赛（child matchups）
    # 并通过参与者名称标识。所有子比赛市场类型都是 moneyline。
    # 双重机会 (DC): ["TeamA Or Draw", "Draw Or TeamB", "TeamA Or TeamB"]
    for mu in matchups:
        participants = mu.get("participants", [])
        pnames = [p.get("name", "") for p in participants]
        if len(pnames) != 3 or not any(" Or Draw" in n for n in pnames):
            continue
        mid = mu["id"]
        dc_markets = mm.get(mid, [])
        dc_prices = None
        for mkt in dc_markets:
            if mkt.get("type") == "moneyline" and mkt.get("period") == 0:
                prices = [{
                    "designation": p.get("designation", ""),
                    "price_decimal": us_to_decimal(p.get("price")),
                    "points": p.get("points"),
                } for p in mkt.get("prices", [])]
                if len(prices) >= 3:
                    dc_prices = prices
                break
        if not dc_prices:
            continue
        home_dc = pnames[0].replace(" Or Draw", "").strip()
        away_dc = pnames[1].replace("Draw Or ", "").strip()
        if not home_dc or not away_dc:
            continue
        for entry in result:
            eh = entry.get("home", "")
            ea = entry.get("away", "")
            if not eh or not ea:
                continue
            if (home_dc.lower() in eh.lower() or eh.lower() in home_dc.lower()) and \
               (away_dc.lower() in ea.lower() or ea.lower() in away_dc.lower()):
                entry["double_chance"] = [{
                    "period": 0,
                    "prices": dc_prices,
                }]
                break

    # --- 双边进球 (BTTS)：["Yes", "No"] 子比赛 ---
    # 注意：Pinnacle 有多种 Yes/No 子比赛（BTTS、Either Team To Score、
    # TeamX To Score、Win to Nil 等），必须用 special.description 区分。
    for mu in matchups:
        parent_id = mu.get("parentId")
        if not parent_id:
            continue
        pnames = [p.get("name", "") for p in mu.get("participants", [])]
        if pnames != ["Yes", "No"]:
            continue
        spec = mu.get("special", {})
        desc = spec.get("description", "")
        if "Both Teams To Score" not in desc:
            continue
        mid = mu["id"]
        btts_markets = mm.get(mid, [])
        if not btts_markets:
            continue
        # 建立 participantId → name 映射
        pid_to_name = {str(p.get("id")): p.get("name", "") for p in mu.get("participants", [])}

        btts_entries = []
        for mkt in btts_markets:
            if mkt.get("type") != "moneyline":
                continue
            period = mkt.get("period", 0)
            prices = []
            for p in mkt.get("prices", []):
                pid = str(p.get("participantId", ""))
                name = pid_to_name.get(pid, "")
                # 用 participantId 映射到正确的 Yes/No 标签
                desig = p.get("designation", "")
                if not desig or desig == "None":
                    desig = name.lower() if name else ""
                prices.append({
                    "designation": desig,
                    "price_decimal": us_to_decimal(p.get("price")),
                    "points": p.get("points"),
                })
            if len(prices) >= 2:
                btts_entries.append({"period": period, "prices": prices})
        if not btts_entries:
            continue
        for entry in result:
            if entry.get("matchup_id") != parent_id:
                continue
            existing = entry.get("btts", [])
            if not existing:
                entry["btts"] = btts_entries
            else:
                existing_periods = {e["period"] for e in existing if "period" in e}
                for be in btts_entries:
                    if be["period"] not in existing_periods:
                        existing.append(be)
            break

    # --- 单/双 (Odd/Even)：["Odd", "Even"] 子比赛 ---
    for mu in matchups:
        parent_id = mu.get("parentId")
        if not parent_id:
            continue
        pnames = [p.get("name", "") for p in mu.get("participants", [])]
        if pnames != ["Odd", "Even"]:
            continue
        spec = mu.get("special", {})
        desc = spec.get("description", "")
        if "Odd/Even" not in desc:
            continue
        # 只取"Total Goals Odd/Even"（跳过球队级别的"X Goals Odd/Even"）
        if not desc.startswith("Total Goals Odd/Even"):
            continue
        mid = mu["id"]
        oe_markets = mm.get(mid, [])
        if not oe_markets:
            continue
        oe_entries = []
        for mkt in oe_markets:
            if mkt.get("type") != "moneyline":
                continue
            period = mkt.get("period", 0)
            prices = [{
                "designation": p.get("designation", ""),
                "price_decimal": us_to_decimal(p.get("price")),
                "points": p.get("points"),
            } for p in mkt.get("prices", [])]
            if len(prices) >= 2:
                oe_entries.append({"period": period, "prices": prices})
        if not oe_entries:
            continue
        for entry in result:
            if entry.get("matchup_id") != parent_id:
                continue
            existing = entry.get("oe", [])
            if not existing:
                entry["oe"] = oe_entries
            else:
                existing_periods = {e["period"] for e in existing if "period" in e}
                for oe in oe_entries:
                    if oe["period"] not in existing_periods:
                        existing.append(oe)
            break

    # --- 半全场 (HT/FT)：9 个参与者子比赛 ---
    for mu in matchups:
        parent_id = mu.get("parentId")
        if not parent_id:
            continue
        pnames = [p.get("name", "") for p in mu.get("participants", [])]
        if len(pnames) != 9:
            continue
        spec = mu.get("special", {})
        desc = spec.get("description", "")
        if "Half-Time/Full-Time" not in desc:
            continue
        mid = mu["id"]
        htft_markets = mm.get(mid, [])
        if not htft_markets:
            continue
        htft_entries = []
        for mkt in htft_markets:
            if mkt.get("type") != "moneyline":
                continue
            period = mkt.get("period", 0)
            prices = [{
                "designation": p.get("designation", ""),
                "price_decimal": us_to_decimal(p.get("price")),
                "points": p.get("points"),
            } for p in mkt.get("prices", [])]
            if len(prices) >= 9:
                htft_entries.append({"period": period, "prices": prices})
        if not htft_entries:
            continue
        for entry in result:
            if entry.get("matchup_id") != parent_id:
                continue
            existing = entry.get("htft", [])
            if not existing:
                entry["htft"] = htft_entries
            else:
                existing_periods = {e["period"] for e in existing if "period" in e}
                for htft in htft_entries:
                    if htft["period"] not in existing_periods:
                        existing.append(htft)
            break

    # --- 平局退款 (DNB)：["Home Or Draw", "Away Or Draw"] 子比赛 ---
    # 注意：Pinnacle 用 "Or Draw" 区分 DC(三结果)和 DNB(两结果)
    for mu in matchups:
        parent_id = mu.get("parentId")
        if not parent_id:
            continue
        pnames = [p.get("name", "") for p in mu.get("participants", [])]
        if len(pnames) != 2:
            continue
        if not all("Or Draw" in n for n in pnames):
            continue
        # 排除 DC（DC 有3个参与者，DNB有2个）
        spec = mu.get("special", {})
        desc = spec.get("description", "")
        if "Double Chance" in desc:
            continue  # 这是DC, 跳过
        mid = mu["id"]
        dnb_markets = mm.get(mid, [])
        if not dnb_markets:
            continue
        pid_to_name = {str(p.get("id")): p.get("name", "") for p in mu.get("participants", [])}
        dnb_entries = []
        for mkt in dnb_markets:
            if mkt.get("type") != "moneyline":
                continue
            period = mkt.get("period", 0)
            prices = []
            for p in mkt.get("prices", []):
                pid = str(p.get("participantId", ""))
                name = pid_to_name.get(pid, "")
                desig = p.get("designation", "")
                if not desig or desig == "None":
                    desig = name.lower() if name else ""
                prices.append({
                    "designation": desig,
                    "price_decimal": us_to_decimal(p.get("price")),
                    "points": p.get("points"),
                })
            if len(prices) >= 2:
                dnb_entries.append({"period": period, "prices": prices})
        if not dnb_entries:
            continue
        for entry in result:
            if entry.get("matchup_id") != parent_id:
                continue
            existing = entry.get("draw_no_bet", [])
            if not existing:
                entry["draw_no_bet"] = dnb_entries
            break

    return result
