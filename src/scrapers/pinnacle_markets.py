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


def _market_entry(period, prices, mkt):
    """构造盘口 entry, 附带此前一直被丢弃的 Pinnacle 元数据。

    这些字段本来就在每次 /markets/straight 响应里, 采集它们**零额外请求**:

    max_stake (limits[].maxRiskStake)
        Pinnacle 自己声明的定价信心。实测分布 $100~$10,500(中位 $250),
        NBA 中位 $750 vs 乌拉圭女足 $50 —— 15 倍差距。上限低 = Pin 自己没把握
        = 我们拿它去抽水当公平价标尺不可靠 → 算出的 EV 大概率是噪声而非价值。
        (与 CLV 实测吻合: 低上限的 T3/T4 正是中位 CLV 最差的档)

    is_alternate
        是否备用线。实测让球/大小球候选里 **82% 是备用线**, 而 get_pin_spread /
        get_pin_total 只按线值就近(±0.5)挑, 根本不知道挑中的是主线还是备用线。
        备用线抽水 5.48% vs 主线 4.71% → 挑中备用线时公平价系统性偏差约 0.39%。

    cutoff_at
        真实投注截止时间, 比 match_epoch 准, 可用于校正 CLV 采集窗口。
    """
    max_stake = None
    for l in (mkt.get("limits") or []):
        if l.get("type") == "maxRiskStake" and l.get("amount"):
            max_stake = l["amount"]
            break
    return {
        "period": period,
        "prices": prices,
        "max_stake": max_stake,
        "is_alternate": bool(mkt.get("isAlternate")),
        "cutoff_at": mkt.get("cutoffAt"),
    }


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

            entry = _market_entry(per, prices, mkt)
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

    # --- 第二阶段：从子比赛识别 Pinnacle 特殊市场(2026-08-28 重写) ---
    # 之前按参与者名(" Or Draw"/"Yes No")+只认全场 period=0, 实测漏掉 Double Chance/BTTS/DNB/单双
    # 及其半场变体(全返回 0 条)。改用 special.description 精确识别, 全场+半场都接。
    _SPECIAL_DESC = {
        "Double Chance": ("double_chance", 0),
        "Double Chance 1st Half": ("double_chance", 1),
        "Both Teams To Score?": ("btts", 0),
        "Both Teams To Score? 1st Half": ("btts", 1),
        "Draw No Bet": ("draw_no_bet", 0),
        "Draw No Bet 1st Half": ("draw_no_bet", 1),
        "Total Goals Odd/Even": ("oe", 0),
        "Total Goals Odd/Even 1st Half": ("oe", 1),
        "Half-Time/Full-Time": ("htft", 0),
    }
    for mu in matchups:
        _spec = mu.get("special") or {}
        _desc = _spec.get("description", "")
        if _desc not in _SPECIAL_DESC:
            continue
        _key, _period = _SPECIAL_DESC[_desc]
        _pid_to_name = {str(p.get("id")): (p.get("name") or "").strip()
                        for p in mu.get("participants", [])}
        _mkts = mm.get(mu.get("id"), [])
        _entries = []
        for mkt in _mkts:
            if mkt.get("type") != "moneyline":
                continue
            _prices = []
            for p in mkt.get("prices", []):
                _name = _pid_to_name.get(str(p.get("participantId", "")), "")
                _desig = p.get("designation", "")
                if not _desig or _desig == "None":
                    _desig = _name
                _prices.append({
                    "designation": _desig,
                    "price_decimal": us_to_decimal(p.get("price")),
                    "points": p.get("points"),
                })
            if _prices:
                _entries.append({"period": _period, "prices": _prices})
        if not _entries:
            continue
        _parent_id = mu.get("parentId")
        for entry in result:
            if str(entry.get("matchup_id")) != str(_parent_id):
                continue
            _existing = entry.get(_key, [])
            if not _existing:
                entry[_key] = _entries
            else:
                _seen = {e.get("period") for e in _existing if isinstance(e, dict)}
                for _e in _entries:
                    if _e["period"] not in _seen:
                        _existing.append(_e)
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

        moneyline, spread, total, team_total = [], [], [], []
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
            entry = _market_entry(per, prices, mk)
            if mtype == "moneyline":
                # V5.9: 角球独赢(Pinnacle 偶尔有 moneyline 角球盘, 之前硬编码 [] 漏掉)
                entry["prices_sorted"] = sort_ml_prices(prices)  # 与主市场一致, 排成 [home, draw, away]
                moneyline.append(entry)
            elif mtype == "spread":
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
            "start_time": mu.get("startTime", ""),
            "moneyline": moneyline,
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
    返回 {parent_matchup_id: {"home": str, "away": str, "markets": {special_key: [{name, odds}]}}}
    home/away 是父比赛(parent matchup)的队名, 供调用方按队名匹配 BB(不再用"第一个联赛")。
    """
    matchups = api_get(f"/leagues/{league_id}/matchups")
    if not matchups:
        return {}
    markets = api_get(f"/leagues/{league_id}/markets/straight") or []

    SPECIAL_MAP = {
        "Correct Score": "correct_score",
        "Correct Score 1st Half": "correct_score_ht",
        "Winning Margin": "winning_margin",
        "Winning Margin 1st Half": "winning_margin_ht",
        "Total Goals Range": "total_goals_range",
        "Total Goals Range 1st Half": "total_goals_range_ht",
        "First Team To Score": "first_to_score",
        "First Team To Score 1st Half": "first_to_score_ht",
    }

    mm = {}
    for mk in markets:
        mid = mk.get("matchupId")
        if mid:
            mm.setdefault(mid, []).append(mk)

    # id → matchup 映射, 用于解析父比赛队名
    mu_by_id = {mu.get("id"): mu for mu in matchups if mu.get("id")}

    def _teams_of(mu):
        """从 matchup 提取主客队名(优先 parent.participants, 与角球函数一致)。"""
        parent_parts = ((mu.get("parent") or {}).get("participants") or [])
        home = away = ""
        for p in parent_parts:
            if p.get("alignment") == "home":
                home = p.get("name", "")
            elif p.get("alignment") == "away":
                away = p.get("name", "")
        if not home or not away:
            for p in mu.get("participants", []):
                if p.get("alignment") == "home":
                    home = p.get("name", "")
                elif p.get("alignment") == "away":
                    away = p.get("name", "")
        return home, away

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
        # 特殊盘口 prices 无 designation, 只有 participantId; 按 participantId 匹配
        # (prices 顺序可能与 participants 不一致, 之前 zip 按顺序对导致盘口/赔率错位)
        _parts = mu.get("participants", [])
        _ml_prices = []
        for mk in mm.get(mid, []):
            if mk.get("type") == "moneyline":
                _ml_prices = mk.get("prices", [])
                break
        _price_by_id = {p.get("participantId"): p.get("price") for p in _ml_prices}
        prices = []
        for _p in _parts:
            _name = (_p.get("name") or "").strip()
            _pr = _price_by_id.get(_p.get("id"))
            _odds = us_to_decimal(_pr) if _pr is not None else None
            if _name and _odds and _odds > 1.0:
                prices.append({"name": _name, "odds": _odds})
        if prices:
            slot = result.setdefault(parent_id, {"home": "", "away": "", "markets": {}})
            # 队名优先从 special 子比赛 parent 字段, 兜底查父比赛 matchup
            home, away = _teams_of(mu)
            if not home or not away:
                home, away = _teams_of(mu_by_id.get(parent_id, {}))
            slot["home"] = slot["home"] or home
            slot["away"] = slot["away"] or away
            slot["markets"][key] = prices
    return result
