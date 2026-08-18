"""CLV 采集器 — 赛前拉取 Pinnacle 收盘赔率，计算真实 CLV。

每次运行时:
1. 读取 clv_tracking.csv 中所有已推送但未采集收盘价的记录
2. 对比赛开始时间在 5-120 分钟内的记录，拉取 Pinnacle 实时赔率
3. 计算真实 CLV = (推送时BB赔率 - 收盘Pinnacle公平价) / 收盘Pinnacle公平价
4. 保存到 clv_results.csv

用法: python3 -m src.monitor.clv_collector
"""
import csv, json, os, sys, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

TRACKING_FILE = DATA_DIR / "clv_tracking.csv"
RESULTS_FILE = DATA_DIR / "clv_results.csv"

# 采集窗口：比赛开始前 20 分钟内拉取收盘赔率 (真收盘线)
# V5.4 收窄: BEFORE_MAX 720→20, AFTER_MAX 60→0 — 用户要求开赛前20分钟,
#           只在临开赛采集 Pin 价作"真收盘线", 不再用滚球价兜底(滚球价受赛况污染)。
CLV_WINDOW_BEFORE_MIN = 1     # 比赛前 1 分钟 (原15, 太严漏掉收盘线)
CLV_WINDOW_AFTER_MAX = 0      # 开赛后不采集 (真收盘线=开赛前, 滚球价不可靠)
CLV_WINDOW_BEFORE_MAX = 20    # 比赛前 20 分钟 (原720, 收窄到真收盘线)
CLV_MIN_AGE_SECONDS = 300    # 至少推送后 5 分钟才采集 (避免取到同一时刻的赔率)


def _infer_sub_market(sub_market: str, designation: str) -> str:
    """半场盘口 sub_market 被粗标成 "ht", 从 designation 推断精确盘口。

    追踪数据里 ht_hc/ht_ou 都被标成 "ht", 采集后结果存的是推断值(ht_hc/ht_ou),
    去重 key 若用原始 "ht" 会与结果不匹配 → 重复采集。此函数统一口径:
    "让球"→ht_hc, "小球/大球"→ht_ou, 否则保持原值。
    """
    if sub_market == "ht":
        d = (designation or "").lower()
        if "让球" in d:
            return "ht_hc"
        if ("小球" in d) or ("大球" in d):
            return "ht_ou"
    return sub_market


def _load_existing_results():
    """加载已采集的 CLV 结果，返回 {match_key: result}。"""
    existing = {}
    if RESULTS_FILE.exists():
        try:
            with open(RESULTS_FILE, newline='') as f:
                for r in csv.DictReader(f):
                    key = (r.get("home", ""), r.get("away", ""), r.get("sub_market", ""), r.get("designation", ""))
                    existing[key] = r
        except Exception:
            pass
    return existing


def _load_pending_entries():
    """从 clv_tracking.csv 加载尚未采集收盘价的记录。"""
    if not TRACKING_FILE.exists():
        return []

    existing = _load_existing_results()
    entries = []
    with open(TRACKING_FILE, newline='') as f:
        for r in csv.DictReader(f):
            # sub_market 统一推断口径 (ht→ht_hc/ht_ou), 否则去重 key 与结果不匹配
            sm = _infer_sub_market(r.get("sub_market", ""), r.get("designation", ""))
            # 用 BB 中文名 + Pinnacle 英文名组合做 key
            key = (r.get("home", ""), r.get("away", ""), sm, r.get("designation", ""))
            # 也尝试用 Pinnacle 名匹配
            key_pin = (r.get("home_pin", ""), r.get("away_pin", ""), sm, r.get("designation", ""))
            if key not in existing and key_pin not in existing:
                entries.append(r)
    return entries


def _fetch_close_odds(entries):
    """为 pending entries 拉取 Pinnacle 实时赔率作为收盘价。

    只处理比赛在 [now+5min, now+120min] 窗口内的记录。
    Returns: list of dicts with CLV results
    """
    from src.scrapers.pinnacle_markets import get_league_matchups_and_markets
    from src.scrapers.pinnacle_league_map import find_pinnacle_league_ids
    from src.scrapers.pinnacle_api import get_decimal_price

    now_epoch = time.time()
    _budget_start = time.time()
    _MAX_RUNTIME = 600  # 单次采集 wall-clock 预算 10 分钟, 防卡死/无限重扫
    results = []
    league_cache = {}  # Pinnacle league ID → matchups cache

    # 加载 Pinnacle 联赛结构
    ps_file = DATA_DIR / "pinnacle_league_structure.json"
    if not ps_file.exists():
        logger.warning("无 Pinnacle 联赛结构文件，跳过")
        return results
    ps = json.loads(ps_file.read_text())

    # V5: 从对比文件补全 Pinnacle 队名 (历史追踪数据缺失 home_pin/away_pin)
    _cmp_file = DATA_DIR / "bb_vs_pinnacle_comparison.json"
    _pin_names = {}
    if _cmp_file.exists():
        try:
            _cmp = json.loads(_cmp_file.read_text())
            for det in _cmp.get("details", []):
                key = (det.get("home_bb", ""), det.get("away_bb", ""), det.get("league", ""))
                _pin_names[key] = (det.get("home_pin", ""), det.get("away_pin", ""))
        except: pass
    for e in entries:
        if not e.get("home_pin") or not e.get("away_pin"):
            key = (e.get("home", ""), e.get("away", ""), e.get("league", ""))
            if key in _pin_names:
                e["home_pin"] = _pin_names[key][0]
                e["away_pin"] = _pin_names[key][1]

    # 按联赛分组，减少 API 调用
    by_league = defaultdict(list)
    for e in entries:
        match_epoch = int(e.get("match_epoch") or 0)
        if not match_epoch:
            continue

        # 采集窗口 = 赛前 1~20 分钟 (BEFORE_MIN 之前是死常量, 实际窗口却是 [0,20])
        minutes_to_match = (match_epoch - now_epoch) / 60
        if minutes_to_match < CLV_WINDOW_BEFORE_MIN or minutes_to_match > CLV_WINDOW_BEFORE_MAX:
            continue

        # 推送时间必须在比赛前至少 5 分钟
        try:
            push_ts = datetime.fromisoformat(e["timestamp"]).timestamp()
            if now_epoch - push_ts < CLV_MIN_AGE_SECONDS:
                continue
        except (ValueError, KeyError):
            pass

        league = e.get("league", "")
        # V5.9: 优先用存储的 pin_league_id 分组(采集时按ID直拉, 免反查联赛名映射失败)
        pin_lid = e.get("pin_league_id", "")
        group_key = pin_lid if pin_lid else league
        if group_key:
            by_league[group_key].append(e)

    if not by_league:
        logger.info("无比赛在采集窗口内 (%.0f ~ %.0f 分钟前)",
                    CLV_WINDOW_BEFORE_MIN, CLV_WINDOW_BEFORE_MAX)
        return results

    # 对每个联赛拉取 Pinnacle 数据
    sport_map = {"football": "⚽", "basketball": "🏀", "tennis": "🎾", "baseball": "⚾",
                 "american_football": "🏈", "mma": "🥊", "boxing": "👊", "ice_hockey": "🏒"}

    for group_key, league_entries in by_league.items():
        if time.time() - _budget_start > _MAX_RUNTIME:
            logger.warning("CLV 采集超预算 %ds, 提前结束 (已处理 %d 条)", _MAX_RUNTIME, len(results))
            break
        league = league_entries[0].get("league", group_key)
        # V5.9: group_key 是数字=存储的 pin_league_id 直拉; 否则是联赛名, 反查
        if str(group_key).isdigit():
            pin_ids = [group_key]
        else:
            pin_ids = find_pinnacle_league_ids(group_key, ps)
        if not pin_ids:
            continue

        for pin_id in pin_ids:
            if pin_id in league_cache:
                matchups = league_cache[pin_id]
            else:
                try:
                    matchups = get_league_matchups_and_markets(pin_id)
                    league_cache[pin_id] = matchups
                    time.sleep(0.3)  # 限速
                except Exception as e:
                    logger.warning("拉取 Pinnacle 联赛 %s (ID=%s) 失败: %s", league, pin_id, e)
                    continue

            if not matchups:
                continue

            # 对每条 pending entry，在 Pinnacle matchup 中找对应比赛
            for e in league_entries:
                bb_home = e.get("home", "").lower().strip()
                bb_away = e.get("away", "").lower().strip()
                pin_home_name = e.get("home_pin", "").lower().strip()  # Pinnacle 英文名
                pin_away_name = e.get("away_pin", "").lower().strip()
                match_epoch = int(e.get("match_epoch") or 0)
                # sub_market 统一推断口径 (ht→ht_hc/ht_ou), 与去重 key 一致
                sub_market = _infer_sub_market(e.get("sub_market", ""), e.get("designation", ""))
                designation = e.get("designation", "").lower()

                best_pin = None
                best_score = 0
                for mu in matchups:
                    mu_home = mu.get("home", "").lower().strip()
                    mu_away = mu.get("away", "").lower().strip()

                    # 优先匹配 Pinnacle 英文名（最可靠）
                    if pin_home_name and pin_away_name:
                        if pin_home_name == mu_home and pin_away_name == mu_away:
                            best_pin = mu
                            best_score = 100  # 精确匹配最高分 (否则下面 best_score==0 会误丢)
                            break

                    # 其次用 BB 中文名子串匹配
                    score = 0
                    if bb_home and (bb_home in mu_home or mu_home in bb_home):
                        score += 1
                    if bb_away and (bb_away in mu_away or mu_away in bb_away):
                        score += 1
                    if pin_home_name and (pin_home_name in mu_home or mu_home in pin_home_name):
                        score += 2
                    if pin_away_name and (pin_away_name in mu_away or mu_away in pin_away_name):
                        score += 2

                    if score > best_score:
                        best_score = score
                        best_pin = mu

                if not best_pin or best_score == 0:
                    continue

                # 提取对应市场的收盘公平价 (直盘+推导盘口均支持)
                close_data = _extract_market_odds(best_pin, sub_market, designation)
                if close_data is None:
                    continue
                close_pin_odds, close_fair, total_implied = close_data

                # 计算真实 CLV
                bb_odds = float(e.get("bb_odds", 0))
                fair_price = float(e.get("fair_price", 0))
                push_ev = float(e.get("ev_pct", 0))

                true_clv = round((bb_odds - close_fair) / close_fair * 100, 2)
                clv_delta = round(true_clv - push_ev, 2)  # 正=赔率朝有利方向移动

                results.append({
                    "collect_time": datetime.now(timezone.utc).isoformat(),
                    "push_time": e.get("timestamp", ""),
                    "match_key": f"{bb_home}|{bb_away}",
                    "sport": e.get("sport", ""),
                    "league": league,
                    "home": e.get("home", ""),
                    "away": e.get("away", ""),
                    "home_pin": e.get("home_pin", ""),
                    "away_pin": e.get("away_pin", ""),
                    "designation": designation,
                    "sub_market": sub_market,
                    "tier": e.get("tier", ""),
                    "bb_price_source": e.get("bb_price_source", ""),
                    "bb_odds": bb_odds,
                    "push_fair_price": fair_price,
                    "push_ev_pct": push_ev,
                    "close_pin_odds": close_pin_odds,
                    "close_fair_price": close_fair,
                    "close_total_implied": round(total_implied, 4),
                    "true_clv_pct": true_clv,
                    "clv_delta": clv_delta,  # + = 有利, - = 不利
                    "match_epoch": e.get("match_epoch", ""),
                    "minutes_before_match": round((int(e.get("match_epoch") or 0) - time.time()) / 60, 1),
                })

    return results


def _extract_market_odds(pin_matchup, sub_market, designation):
    """从 Pinnacle matchup 提取对应市场的收盘公平价。

    Returns: (close_pin_odds, close_fair_price, total_implied) 或 None。
    - close_pin_odds: 代表性收盘赔率 (直盘=选项原始收盘价; 推导盘=组合公平价)
    - close_fair_price: 去抽水后的公平价 (proportional devig)
    - total_implied: 隐含概率和 (去抽水前)
    """
    from src.scrapers.pinnacle_api import get_decimal_price
    from src.scrapers.matching_engine import (
        get_pin_ml_sorted_from_source, get_pin_spread, get_pin_total,
    )
    des = (designation or "").lower()

    def _devig(odds, selected_idx):
        """proportional devig: 公平价 = odds[i] * sum(1/odds)。selected_idx 可为 int 或 list(组合)。"""
        total = sum(1.0 / p for p in odds if p and p > 0)
        if total <= 0:
            return None, None
        if isinstance(selected_idx, (list, tuple)):
            prob = sum(1.0 / odds[i] for i in selected_idx if 0 <= i < len(odds))
            fair = 1.0 / (prob / total) if prob > 0 else None
        else:
            fair = odds[selected_idx] * total
        return fair, total

    def _parse_line(designation):
        """从 designation 括号里解析线值, 如 (-0.5)/(2.5)/(-0/0.5)→-0.25。"""
        import re
        m = re.search(r'[（(]([^（）)]*)[）)]', designation or "")
        if not m:
            return None
        s = m.group(1).strip()
        sign = -1.0 if s.startswith('-') else 1.0
        s = s.lstrip('+-')
        if not s:
            return None
        if '/' in s:
            parts = [float(x) for x in s.split('/')]
            return sign * sum(parts) / len(parts)
        try:
            return sign * float(s)
        except ValueError:
            return None

    # ── 独赢 / 双重机会 / 平局退款 (3-way moneyline 直接或推导) ──
    if sub_market in ("1x2", "ht", "dc", "dnb", "ht_dc", "ht_dnb"):
        # HT 独赢在 ht_moneyline 独立字段, 全场在 moneyline
        src = pin_matchup.get("ht_moneyline", []) if sub_market.startswith("ht") \
            else pin_matchup.get("moneyline", [])
        odds = get_pin_ml_sorted_from_source(src, "football")  # [home, draw, away]
        if len(odds) < 3:
            return None

        if sub_market in ("1x2", "ht"):
            if "和" in des or "draw" in des or "平" in des:
                idx = 1
            elif "客" in des or "away" in des:
                idx = 2
            else:
                idx = 0
            fair, total = _devig(odds, idx)
            if fair is None:
                return None
            return round(odds[idx], 4), round(fair, 4), round(total, 4)

        elif sub_market in ("dc", "ht_dc"):
            idx = set()
            if "主" in des:
                idx.add(0)
            if "和" in des or "平" in des:
                idx.add(1)
            if "客" in des:
                idx.add(2)
            if not idx:
                return None
            fair, total = _devig(odds, sorted(idx))
            if fair is None:
                return None
            return round(fair, 4), round(fair, 4), round(total, 4)

        else:  # dnb / ht_dnb: 平局退款, 主或客
            idx = 2 if ("客" in des or "away" in des) else 0
            denom = (1.0 / odds[0] + 1.0 / odds[2])  # 排除平局
            prob = (1.0 / odds[idx]) / denom if denom > 0 else 0
            fair = 1.0 / prob if prob > 0 else None
            if fair is None:
                return None
            return round(fair, 4), round(fair, 4), round(denom, 4)

    # ── 让球 (全场/半场) ──
    elif sub_market in ("hc", "ht_hc"):
        src = pin_matchup.get("ht_spread", []) if sub_market == "ht_hc" \
            else pin_matchup.get("spread", [])
        # 让球线符号约定: get_pin_spread 按主队 points 匹配; 客胜要反转符号
        _line = _parse_line(designation)
        if _line is not None and ("客" in des or "away" in des):
            _line = -_line
        home_p, away_p, _ = get_pin_spread(pin_matchup, target_line=_line, source=src)
        if not home_p or not away_p:
            return None
        h, a = get_decimal_price(home_p), get_decimal_price(away_p)
        if not h or not a:
            return None
        idx = 1 if ("客" in des or "away" in des) else 0
        odds = [h, a]
        fair, total = _devig(odds, idx)
        if fair is None:
            return None
        return round(odds[idx], 4), round(fair, 4), round(total, 4)

    # ── 大小球 (全场/半场) ──
    elif sub_market in ("ou", "ht_ou"):
        src = pin_matchup.get("ht_total", []) if sub_market == "ht_ou" \
            else pin_matchup.get("total", [])
        over_p, under_p = get_pin_total(pin_matchup, target_line=_parse_line(designation), source=src)
        if not over_p or not under_p:
            return None
        o, u = get_decimal_price(over_p), get_decimal_price(under_p)
        if not o or not u:
            return None
        idx = 1 if ("小" in des or "under" in des) else 0
        odds = [o, u]
        fair, total = _devig(odds, idx)
        if fair is None:
            return None
        return round(odds[idx], 4), round(fair, 4), round(total, 4)

    # ── BTTS (全场) ──
    elif sub_market == "btts":
        for bt in pin_matchup.get("btts", []):
            if bt.get("period", 0) != 0:
                continue
            yes = no = None
            for p in bt.get("prices", []):
                if p.get("designation") == "yes":
                    yes = get_decimal_price(p)
                elif p.get("designation") == "no":
                    no = get_decimal_price(p)
            if yes and no:
                idx = 0 if ("是" in des or "yes" in des) else 1
                odds = [yes, no]
                fair, total = _devig(odds, idx)
                if fair is None:
                    return None
                return round(odds[idx], 4), round(fair, 4), round(total, 4)
        return None

    elif sub_market == "oe":
        # oe(单双) 在 matchup["oe"] 字段 (pinnacle_markets 已提取)
        for oe in pin_matchup.get("oe", []):
            if oe.get("period", 0) != 0:
                continue
            odd = even = None
            for p in oe.get("prices", []):
                pd = (p.get("designation") or "").lower()
                val = p.get("price_decimal") or get_decimal_price(p)
                if "odd" in pd:
                    odd = val
                elif "even" in pd:
                    even = val
            if odd and even:
                odds = [odd, even]
                idx = 0 if ("单" in des or "odd" in des) else 1
                fair, total = _devig(odds, idx)
                if fair is None:
                    return None
                return round(odds[idx], 4), round(fair, 4), round(total, 4)
        return None

    # correct_score/winning_margin/total_goals_range/first_to_score 在 get_league_special_markets
    # (matchup 里无对应字段, 需在 _fetch_close_odds 里单独拉)
    return None


def _save_results(results):
    """追加保存 CLV 结果到 CSV。"""
    if not results:
        return

    fieldnames = [
        "collect_time", "push_time", "match_key", "sport", "league", "home", "away",
        "home_pin", "away_pin",
        "designation", "sub_market", "tier", "bb_price_source", "bb_odds", "push_fair_price", "push_ev_pct",
        "close_pin_odds", "close_fair_price", "close_total_implied",
        "true_clv_pct", "clv_delta", "match_epoch", "minutes_before_match",
    ]

    file_exists = RESULTS_FILE.exists()
    with open(RESULTS_FILE, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    # V5.1: 同时落库到 clv_data 表 (之前只写CSV, SQLite一直空)
    try:
        from src.storage.database import db
        for r in results:
            try:
                db.record_clv(
                    match_key=r.get("match_key", ""),
                    bookmaker="Pinnacle",
                    market=f"{r.get('sub_market','')}/{r.get('designation','')}",
                    opening=r.get("bb_odds", 0),     # 推送时BB赔率
                    closing=r.get("close_fair_price", 0),  # 收盘公平价
                )
            except Exception:
                pass
    except ImportError:
        pass

    logger.info("保存 %d 条 CLV 结果到 %s + clv_data表", len(results), RESULTS_FILE)


def collect():
    """主入口：采集所有 pending 比赛的收盘赔率并计算 CLV。"""
    from src.storage.file_lock import task_lock
    with task_lock("clv_collector") as acquired:
        if not acquired:
            logger.info("CLV 采集已在运行，跳过本次（防 crontab/pipeline 重叠）")
            return 0
        return _collect_inner()


def _collect_inner():
    logger.info("CLV 采集开始...")

    entries = _load_pending_entries()
    total = len(entries)

    # V5: 统计epoch质量
    valid_epoch = sum(1 for e in entries if int(e.get("match_epoch", 0) or 0) > 100000)
    no_epoch = sum(1 for e in entries if not e.get("match_epoch") or int(e.get("match_epoch", 0) or 0) == 0)
    bad_epoch = total - valid_epoch - no_epoch
    logger.info("pending: %d条 (有效epoch:%d, 无epoch:%d, 异常:%d)", total, valid_epoch, no_epoch, bad_epoch)

    if not entries:
        logger.info("无 pending 记录，跳过")
        return 0

    results = _fetch_close_odds(entries)
    logger.info("采集到 %d 条收盘赔率", len(results))

    _save_results(results)

    # 统计
    if results:
        avg_clv = sum(r["true_clv_pct"] for r in results) / len(results)
        positive = sum(1 for r in results if r["true_clv_pct"] > 0)
        logger.info("CLV 统计: 平均 %.1f%%, 正CLV率 %.0f%% (%d/%d)",
                    avg_clv, positive/len(results)*100, positive, len(results))

    return len(results)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    collect()
