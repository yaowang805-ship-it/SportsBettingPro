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

        # 足球: 跳过 DC/BTTS/正确比分等衍生市场子比赛
        # 棒球/篮球等: parent 结构包含真正的比赛信息，需要保留
        _is_football = league.get('sport', {}).get('name', '') in ('Soccer', '足球') if isinstance(league, dict) else False
        if _is_football and parent_participants and not _is_games:
            continue

        # V4.5: 棒球等运动Pinnacle用parent存队名, child存盘口(Over/Under)
        if not home and not away:
            if parent_mu:
                parent_parts = parent_mu.get("participants", [])
                for p in parent_parts:
                    if p.get("alignment") == "home":
                        home = p.get("name", "")
                    elif p.get("alignment") == "away":
                        away = p.get("name", "")
            if not home and not away:
                continue

        moneyline, spread, total = [], [], []
        ht_moneyline, ht_spread, ht_total = [], [], []
        team_total = []
        btts = []
        double_chance = []
        draw_no_bet = []
        # 构建 participantId → designation 映射 (新API去掉designation字段)
        _pid_to_desig = {}
        for p in mu.get("participants", []):
            pid = p.get("participantId", p.get("id"))
            align = p.get("alignment", "")
            if align == "home": _pid_to_desig[pid] = "home"
            elif align == "away": _pid_to_desig[pid] = "away"
            else:
                name = p.get("name", "").lower()
                if name == home.lower(): _pid_to_desig[pid] = "home"
                elif name == away.lower(): _pid_to_desig[pid] = "away"
                else: _pid_to_desig[pid] = name

        for mkt in mkt_list:
            # 跳过锁定/暂停的市场(状态非open)
            if mkt.get("status", "open") != "open":
                continue
            mtype = mkt.get("type", "")
            per = mkt.get("period", 0)
            prices = []
            for p in mkt.get("prices", []):
                desig = p.get("designation", "")  # 旧格式
                if not desig:
                    # 新格式: participantId → designation
                    pid = p.get("participantId", "")
                    desig = _pid_to_desig.get(pid, "")
                price = p.get("price")
                prices.append({
                    "designation": desig,
                    "price_decimal": us_to_decimal(price),
                    "points": p.get("points"),
                })
            if not prices:
                continue

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
            "league_id": league_id,
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
        # pnames → DC outcome labels (same order as participants)
        # pnames = ["TeamA Or Draw", "Draw Or TeamB", "TeamA Or TeamB"]
        dc_label_map = {0: "1X", 1: "2X", 2: "12"}

        dc_prices = None
        for mkt in dc_markets:
            if mkt.get("type") == "moneyline" and mkt.get("period") == 0:
                raw_prices = mkt.get("prices", [])
                if len(raw_prices) < 3:
                    continue
                prices = []
                for i, p in enumerate(raw_prices):
                    label = dc_label_map.get(i, p.get("designation", ""))
                    prices.append({
                        "designation": label,
                        "price_decimal": us_to_decimal(p.get("price")),
                        "points": p.get("points"),
                    })
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
            # pnames = ["Odd", "Even"], 用 pnames 给价格打标签
            raw_prices = mkt.get("prices", [])
            labelled = []
            for i, p in enumerate(raw_prices):
                label = pnames[i] if i < len(pnames) else p.get("designation", "")
                labelled.append({
                    "designation": label,
                    "price_decimal": us_to_decimal(p.get("price")),
                    "points": p.get("points"),
                })
            if len(labelled) >= 2:
                oe_entries.append({"period": period, "prices": labelled})
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
        # HTFT 9个价格按固定顺序对应 HTFT_KEYS(用位置, 不用pnames值)
        # Pinnacle pnames是具体队名(如\"sportivoluqueno-sportivoluqueno\"), 不是通用标签
        HTFT_POS_KEYS = ["home/home","home/draw","home/away",
                         "draw/home","draw/draw","draw/away",
                         "away/home","away/draw","away/away"]
        htft_label_map = {i: HTFT_POS_KEYS[i] for i in range(9)}

        htft_entries = []
        for mkt in htft_markets:
            if mkt.get("type") != "moneyline":
                continue
            period = mkt.get("period", 0)
            raw_prices = mkt.get("prices", [])
            if len(raw_prices) < 9:
                continue
            # 按 pnames 顺序给价格打标签
            labelled_prices = []
            for i, p in enumerate(raw_prices):
                label = htft_label_map.get(i, p.get("designation", ""))
                labelled_prices.append({
                    "designation": label,
                    "price_decimal": us_to_decimal(p.get("price")),
                    "points": p.get("points"),
                })
            htft_entries.append({"period": period, "prices": labelled_prices})
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

    # 非足球 parent-child 去重合并：同一 parent 的多个子比赛合并为一个条目
    _parent_groups = {}
    _no_parent = []
    for r in result:
        pid = r.get("matchup_id")
        # 根据 matchup_id 找到原始 matchup 的 parentId
        _mu = mu_map.get(pid, {})
        _parent_id = _mu.get("parentId") if isinstance(_mu, dict) else None
        if _parent_id:
            if _parent_id not in _parent_groups:
                _parent_groups[_parent_id] = []
            _parent_groups[_parent_id].append(r)
        else:
            _no_parent.append(r)

    if _parent_groups:
        _merged = list(_no_parent)
        for _pid, _entries in _parent_groups.items():
            _base = _entries[0]
            # 合并所有子条目的盘口
            for e in _entries[1:]:
                _base["moneyline"].extend(e.get("moneyline", []))
                _base["spread"].extend(e.get("spread", []))
                _base["total"].extend(e.get("total", []))
                _base["team_total"].extend(e.get("team_total", []))
            _merged.append(_base)
        result = _merged

    return result


def get_league_corner_markets(league_id):
    """从基础足球联赛提取 Pinnacle 角球(角球让球/角球大小)市场。

    Pinnacle 把角球作为基础联赛里的子比赛返回: 子比赛 league.name 以
    " Corners" 结尾, units == "Corners", participants 带 "(Corners)" 后缀,
    parentId 指向主比赛。直接调 /leagues/{corner_league_id}/matchups 返回 0
    (角球"联赛"ID 只出现在 /sports/{id}/matchups 里), 所以必须从基础联赛提取。

    Returns: 与 get_league_matchups_and_markets 相同结构的比赛列表,
             其中 spread=角球让球, total=角球大小, team_total=单队角球。
             角球无独赢(moneyline)盘口。
    """
    matchups = api_get(f"/leagues/{league_id}/matchups")
    if not matchups:
        return []
    markets = api_get(f"/leagues/{league_id}/markets/straight") or []

    # 识别角球子比赛: units == "Corners" (最可靠), 兜底 league.name 结尾 " Corners"
    corner_mus = {}
    for m in matchups:
        lg = m.get("league", {}) or {}
        if m.get("units") == "Corners" or lg.get("name", "").endswith(" Corners"):
            corner_mus[m["id"]] = m

    if not corner_mus:
        return []

    # 按 matchupId 分组市场, 只保留角球子比赛的
    mm = {}
    for mk in markets:
        mid = mk.get("matchupId")
        if mid in corner_mus:
            mm.setdefault(mid, []).append(mk)

    result = []
    for mid, mu in corner_mus.items():
        # 干净队名优先从 parent.participants 取 (不带 "(Corners)" 后缀)
        parent_parts = ((mu.get("parent") or {}).get("participants") or [])
        home = away = ""
        for p in parent_parts:
            if p.get("alignment") == "home":
                home = p.get("name", "")
            elif p.get("alignment") == "away":
                away = p.get("name", "")
        if not home or not away:
            # 兜底: 从自身 participants 去 "(Corners)" 后缀
            for p in mu.get("participants", []):
                nm = (p.get("name", "") or "").replace("(Corners)", "").replace(" (Corners)", "").strip()
                if p.get("alignment") == "home":
                    home = nm
                elif p.get("alignment") == "away":
                    away = nm
        if not home or not away:
            continue

        spread, total, team_total = [], [], []
        for mk in mm.get(mid, []):
            if mk.get("status", "open") != "open":
                continue
            mtype = mk.get("type", "")
            per = mk.get("period", 0)
            prices = []
            for p in mk.get("prices", []):
                prices.append({
                    "designation": p.get("designation", ""),
                    "price_decimal": us_to_decimal(p.get("price")),
                    "points": p.get("points"),
                })
            if not prices:
                continue
            entry = {"period": per, "prices": prices}
            if mtype == "spread":
                spread.append(entry)
            elif mtype == "total":
                total.append(entry)
            elif mtype == "team_total":
                entry["side"] = mk.get("side", "")
                team_total.append(entry)

        result.append({
            "matchup_id": mid,
            "league_id": league_id,
            "league_name": (mu.get("league") or {}).get("name", ""),
            "league_group": (mu.get("league") or {}).get("group", ""),
            "home": home,
            "away": away,
            "start_time": mu.get("start_time", ""),
            "moneyline": [],
            "spread": spread,
            "total": total,
            "team_total": team_total,
            "ht_moneyline": [],
            "ht_spread": [],
            "ht_total": [],
            "btts": [],
            "double_chance": [],
            "draw_no_bet": [],
        })

    return result


def get_league_special_markets(league_id):
    """提取 Pinnacle 特殊盘口(正确比分/净胜球/总进球区间/先进球)。

    这些是 matchups 里的 special 子比赛(有 parentId), 赔率在 /markets/straight
    (type=moneyline, matchupId=子比赛id)。
    返回 {parent_matchup_id: {special_key: [{name, odds}]}}
    """
    matchups = api_get(f"/leagues/{league_id}/matchups")
    if not matchups:
        return {}
    markets = api_get(f"/leagues/{league_id}/markets/straight") or []

    SPECIAL_MAP = {
        "Correct Score": "correct_score",
        "Winning Margin": "winning_margin",
        "Total Goals Range": "total_goals_range",
        "First Team To Score": "first_to_score",
    }

    mm = {}
    for mk in markets:
        mid = mk.get("matchupId")
        if mid:
            mm.setdefault(mid, []).append(mk)

    result = {}
    for mu in matchups:
        desc = (mu.get("special") or {}).get("description", "")
        key = SPECIAL_MAP.get(desc)
        if not key:
            continue
        parent_id = mu.get("parentId")
        mid = mu.get("id")
        if not parent_id or not mid:
            continue
        prices = []
        for mk in mm.get(mid, []):
            if mk.get("type") != "moneyline":
                continue
            for p in mk.get("prices", []):
                name = p.get("designation", "")
                odds = us_to_decimal(p.get("price"))
                if name and odds and odds > 1.0:
                    prices.append({"name": name, "odds": odds})
        if prices:
            result.setdefault(parent_id, {})[key] = prices
    return result
