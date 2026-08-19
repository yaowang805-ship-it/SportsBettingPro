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
# V5.10: 20 → 45 分钟。窗口只有 19 分钟 + cron 5 分钟一跑 = 每场只有约 4 次机会,
# Pinnacle 一次 SSL 抽风就把整个窗口烧光、且窗口一过永久丢失 —— 实测 101 条历史
# 丢失里 24 条(24%)是这么没的。放宽到 45 分钟给约 9 次机会。
#
# 放宽的代价已实测(400 场稠密快照, 与赛前 1 分钟公平价比):
#     距开赛 45 分 → 偏差中位 0.00%, P90 2.51%
#     距开赛 20 分 → 偏差中位 0.00%, P90 0.89%
# 也就是绝大多数比赛赛前一小时根本不改价, 但最活跃的 10% 会偏 2.5%。
# 所以**必须配套 CLV_ALLOW_REFRESH**: 早采保覆盖, 越接近开赛越要用新价覆盖旧值,
# 只放宽不刷新会系统性拉低收盘价准确度。
CLV_WINDOW_BEFORE_MAX = 45
# 已采到的记录, 若本轮能拿到更接近开赛的价格则覆盖(以 close_lag_min 更小为准)
CLV_ALLOW_REFRESH = True
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


def _load_pending_entries(return_expired=False):
    """从 clv_tracking.csv 加载尚未采集收盘价的记录。

    V5.10: 区分「真 pending」和「已过期」。以前两者混在一起, 日志天天写
    "pending: 589条"看着像在排队, 其实一大半是比赛早就打完、窗口永久关闭、
    再也不可能采到的死记录 —— 一个假装在工作的进度条, 把真实丢失率盖住了。
    """
    if not TRACKING_FILE.exists():
        return ([], []) if return_expired else []

    existing = _load_existing_results()
    now = time.time()
    entries, expired = [], []
    with open(TRACKING_FILE, newline='') as f:
        for r in csv.DictReader(f):
            # sub_market 统一推断口径 (ht→ht_hc/ht_ou), 否则去重 key 与结果不匹配
            sm = _infer_sub_market(r.get("sub_market", ""), r.get("designation", ""))
            # 用 BB 中文名 + Pinnacle 英文名组合做 key
            key = (r.get("home", ""), r.get("away", ""), sm, r.get("designation", ""))
            # 也尝试用 Pinnacle 名匹配
            key_pin = (r.get("home_pin", ""), r.get("away_pin", ""), sm, r.get("designation", ""))
            if not ev_is_plausible(r.get("ev_pct"), r.get("bb_odds")):
                continue    # 系统自己不认的 EV 量级, 不进 CLV 样本
            prev = existing.get(key) or existing.get(key_pin)
            ep = int(r.get("match_epoch") or 0)
            if prev is not None:
                # V5.10: 已采过的, 若比赛还没开赛就允许再采一次去覆盖 —— 窗口放宽到 45
                # 分钟后, 第一次采到的可能离开赛还远(实测 P90 偏差 2.51%), 越靠近开赛
                # 的价格越接近真收盘线。不刷新的话放宽窗口反而会拉低准确度。
                if not (CLV_ALLOW_REFRESH and ep and (ep - now) / 60 >= CLV_WINDOW_BEFORE_MIN):
                    continue
                try:
                    if float(prev.get("close_lag_min") or 0) <= 1.0:
                        continue  # 已经贴着开赛采到了, 没有再刷新的价值
                except (TypeError, ValueError):
                    pass
                entries.append(r)
                continue
            if ep and (ep - now) / 60 < CLV_WINDOW_BEFORE_MIN:
                expired.append(r)   # 已开赛, 实时窗口关闭 → 只能靠归档回捞
            else:
                entries.append(r)
    return (entries, expired) if return_expired else entries


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
    _seen_results = {}  # (home,away,sub_market,designation) → (best_score, row) 本轮去重
    league_cache = {}  # Pinnacle league ID → matchups cache
    special_cache = {}  # Pinnacle league ID → special markets cache

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
                    # V5.10: 窗口内拉取失败要留痕。窗口只有 19 分钟, 一旦开赛就永久
                    # 采不到了 —— 以前这里静默 continue, 丢了多少、丢在哪一步全看不见
                    # (实测 175 条已开赛记录丢了 105 条, 日志里毫无痕迹)。
                    _record_misses(league_entries, f"联赛拉取失败:{type(e).__name__}")
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
                close_data = _extract_market_odds(best_pin, sub_market, designation,
                                                  sport=e.get("sport", "football"),
                                                  line=e.get("line", ""))
                if close_data is None and sub_market in ("correct_score", "winning_margin", "total_goals_range", "first_to_score"):
                    # V5.9: 特殊盘口在 get_league_special_markets (matchup 里没有)
                    close_data = _extract_special_market_close(pin_id, e, special_cache)
                if close_data is None:
                    continue
                close_pin_odds, close_fair, total_implied = close_data

                # 计算真实 CLV
                bb_odds = float(e.get("bb_odds", 0))
                fair_price = float(e.get("fair_price", 0))
                push_ev = float(e.get("ev_pct", 0))

                true_clv = round((bb_odds - close_fair) / close_fair * 100, 2)
                clv_delta = round(true_clv - push_ev, 2)  # 正=赔率朝有利方向移动

                # V5.10: 同一条机会在本轮里只能产出一个结果。
                # find_pinnacle_league_ids 可能返回多个 pin_id, 外层 for pin_id 会让
                # 同一场 BB 比赛在两个 Pinnacle 联赛里各配到一次 —— 实测产出过同一
                # 机会两行且 CLV 互相矛盾(24.57 vs 19.77、-14.8 vs -6.32), 其中必有
                # 一个是错配。这里按队名匹配分数保留最优的那个。
                _rkey = (e.get("home", ""), e.get("away", ""), sub_market, designation)
                _prev = _seen_results.get(_rkey)
                if _prev is not None and _prev[0] >= best_score:
                    continue
                if _prev is not None:
                    try:
                        results.remove(_prev[1])
                    except ValueError:
                        pass

                _row = {
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
                    "source": e.get("source", "push"),
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
                    "close_source": "live",
                    "close_lag_min": round((int(e.get("match_epoch") or 0) - time.time()) / 60, 1),
                }
                results.append(_row)
                _seen_results[_rkey] = (best_score, _row)

    # V5.10: 窗口即将关闭却还没采到的, 记一笔 —— 这些就是永久丢失的候选,
    # 之前它们只是悄悄消失, 日志里只有一句"采集到 0 条", 看不出丢了什么。
    got = {(r["home"], r["away"], r["sub_market"], r["designation"]) for r in results}
    dying, _now = [], time.time()
    for entries_ in by_league.values():
        for e in entries_:
            ep = int(e.get("match_epoch") or 0)
            if not ep or (ep - _now) / 60 > 6:
                continue  # 还有下一轮 cron(5min) 兜底, 不算丢
            sm = _infer_sub_market(e.get("sub_market", ""), e.get("designation", ""))
            if (e.get("home", ""), e.get("away", ""), sm,
                    e.get("designation", "").lower()) not in got:
                dying.append(e)
    if dying:
        logger.warning("⚠️ %d 条记录窗口即将关闭仍未采到收盘价(再过 6 分钟永久丢失)", len(dying))
        _record_misses(dying, "窗口关闭前未采到")

    return results


def _parse_line_str(s):
    """解析 tracking 里存的线值字符串: "0" / "2.25" / "+0.5/1" / "-1.5/2" → float。"""
    s = (s or "").strip()
    if not s:
        return None
    sign = -1.0 if s.startswith("-") else 1.0
    s = s.lstrip("+-")
    if not s:
        return None
    try:
        if "/" in s:
            parts = [float(x) for x in s.split("/")]
            return sign * sum(parts) / len(parts)
        return sign * float(s)
    except ValueError:
        return None


def _extract_market_odds(pin_matchup, sub_market, designation, sport="football", line=None):
    """从 Pinnacle matchup 提取对应市场的收盘公平价。

    sport: 用于判定 2-way(篮球/网球/棒球等, ML 只有 [home, away]) 还是 3-way。
           V5.10 修复: 原先硬编码 "football" → min_req=3, 2-way 运动的 ML 只有 2 个
           价格永远 len<3 被判 None, 导致 2-way 运动的 1x2 盘口 CLV 采集率恒为 0
           (实测 tracking 110 条 / results 0 条)。

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
        # 3-way: [home, draw, away]; 2-way(篮/网/棒等): [home, away]
        odds = get_pin_ml_sorted_from_source(src, sport)
        if len(odds) < 2:
            return None
        two_way = len(odds) == 2

        if sub_market in ("1x2", "ht"):
            if "和" in des or "draw" in des or "平" in des:
                if two_way:
                    return None  # 2-way 运动无平局选项
                idx = 1
            elif "客" in des or "away" in des:
                idx = len(odds) - 1  # 3-way→2, 2-way→1
            else:
                idx = 0
            fair, total = _devig(odds, idx)
            if fair is None:
                return None
            return round(odds[idx], 4), round(fair, 4), round(total, 4)

        # 双重机会/平局退款依赖平局腿, 2-way 运动不适用
        if two_way:
            return None

        if sub_market in ("dc", "ht_dc"):
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
        # V5.10: 优先用 tracking 显式存的 line, 回退到从中文标签正则抠(老数据没有 line 列)
        _line = _parse_line_str(line)
        if _line is None:
            _line = _parse_line(designation)
        if _line is None:
            # 不知道线值就不能比 —— 以前这里会让 get_pin_spread 拿 candidates[0],
            # 相当于随机挑一条 Pinnacle 的线, 静默产出错误 CLV。宁可不出数。
            return None
        if "客" in des or "away" in des:
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
        # V5.10: 同让球 —— 优先用显式 line, 拿不到线值就不比(避免随机挑线)
        _line = _parse_line_str(line)
        if _line is None:
            _line = _parse_line(designation)
        if _line is None:
            return None
        over_p, under_p = get_pin_total(pin_matchup, target_line=_line, source=src)
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


def _extract_special_market_close(league_id, entry, special_cache):
    """特殊盘口(正确比分/净胜球/总进球区间/先进球)收盘价 — 用 get_league_special_markets。

    Returns: (close_pin_odds, close_fair_price, total_implied) 或 None。
    """
    from src.scrapers.pinnacle_markets import get_league_special_markets
    from src.scrapers.pinnacle_opportunities import _norm_scoreline, _norm_margin_side, _norm_special_name

    lid = str(league_id)
    if lid not in special_cache:
        try:
            special_cache[lid] = get_league_special_markets(lid)
        except Exception:
            special_cache[lid] = {}
    spec_map = special_cache[lid]
    if not spec_map:
        return None

    sub = entry.get("sub_market", "")
    designation = entry.get("designation", "")
    home_pin = (entry.get("home_pin", "") or "").lower().strip()
    away_pin = (entry.get("away_pin", "") or "").lower().strip()

    # 按队名匹配 special slot (与 fetch_special_opportunities 一致)
    best_markets = None
    best_score = 0
    for pid, info in spec_map.items():
        ph = (info.get("home") or "").lower().strip()
        pa = (info.get("away") or "").lower().strip()
        if not ph or not pa:
            continue
        sc = 0
        if home_pin and ph:
            if home_pin == ph:
                sc += 1
            elif home_pin in ph or ph in home_pin:
                sc += 0.5
        if away_pin and pa:
            if away_pin == pa:
                sc += 1
            elif away_pin in pa or pa in away_pin:
                sc += 0.5
        if sc > best_score:
            best_score = sc
            best_markets = info.get("markets")
    if not best_markets or best_score < 0.5:
        return None

    prices = best_markets.get(sub, [])
    if not prices:
        return None

    # 从 designation 剥离中文 label 前缀 (如 "正确比分1-1" → "1-1")
    label = {"correct_score": "正确比分", "winning_margin": "净胜球",
             "total_goals_range": "总进球区间", "first_to_score": "先进球"}.get(sub, "")
    name_part = designation[len(label):] if label and designation.startswith(label) else designation

    if sub == "correct_score":
        norm_price = {_norm_scoreline(p["name"]): p["odds"] for p in prices}
        target = _norm_scoreline(name_part)
    elif sub == "winning_margin":
        norm_price = {_norm_margin_side(p["name"], entry.get("home_pin", ""), entry.get("away_pin", "")): p["odds"] for p in prices}
        target = name_part  # designation 已存 home_byN/away_byN
    else:
        norm_price = {_norm_special_name(p["name"]): p["odds"] for p in prices}
        target = _norm_special_name(name_part)

    close_odds = norm_price.get(target)
    if not close_odds or close_odds <= 1.0:
        return None
    all_odds = [v for v in norm_price.values() if v and v > 1.0]
    if not all_odds:
        return None
    total = sum(1.0 / o for o in all_odds)
    if total <= 0:
        return None
    fair = close_odds * total  # proportional devig
    return round(close_odds, 4), round(fair, 4), round(total, 4)


MISS_LOG_FILE = DATA_DIR / "clv_miss_log.csv"


def ev_is_plausible(ev_pct, bb_odds) -> bool:
    """推送时的 EV 是否在系统自己认可的量级内。

    投注路径有 EV 上限 max(12, (赔率-1)*20)(见 CLAUDE.md), 超过就不下注 —— 因为那种
    量级的"优势"几乎必然是上游错配/坏价, 不是真机会。但 validate 路径为了积累样本
    记录了所有 EV>=2% 的机会, **没套这个上限**, 于是把垃圾一起收了进来。
    实测 792 条 tracking 里 49 条(6%)超限, 中位 EV 34%、最高 244%, 全部是 validate
    (0 条 push, 所以没亏钱), 但它们会污染 CLV 统计 —— 归档回捞就曾把一条 EV=244%
    的棒球记录算出 +174.8% 的 CLV。

    统计口径要和投注口径一致: 投注路径不认的机会, CLV 也不该拿来当证据。
    """
    try:
        ev = float(ev_pct)
        odds = float(bb_odds)
    except (TypeError, ValueError):
        return True     # 字段异常不在这里拦, 交给各自的解析逻辑
    return ev <= max(12.0, (odds - 1) * 20)

# 增量扫描按时间窗分文件写(urgent<6h / near 6-24h / far 24-72h), MAIN 只有全量扫描才刷新。
_COMPARISON_FILES = ("bb_vs_pinnacle_comparison_urgent.json",
                     "bb_vs_pinnacle_comparison_near.json",
                     "bb_vs_pinnacle_comparison_far.json",
                     "bb_vs_pinnacle_comparison_FB.json",
                     "bb_vs_pinnacle_comparison.json")


def _load_freshest_comparison_details():
    """合并四个对比文件的 details, 同一机会取来自最新文件的那条。

    V5.10: 原先只读 MAIN(bb_vs_pinnacle_comparison.json)。但增量扫描只写
    _urgent/_near/_far, MAIN 要全量扫描才刷新 —— 实测 MAIN 陈旧 88 分钟, 而
    _FB 才 3.5 分钟。拿 88 分钟前的快照当"当前机会"记进 validate, 结果就是
    36 条记录在写入时比赛早已开赛(中位晚 22 分钟, 最长 207 分钟)。

    不能只取最新的那一个文件: 各文件覆盖不同时间窗(实测 urgent 仅 2 场 /
    near 27 场 / MAIN 216 场), 只取一个会大幅丢覆盖面。所以按新鲜度升序读、
    后读的覆盖先读的, 既保覆盖又保新鲜。
    """
    files = []
    for name in _COMPARISON_FILES:
        p = DATA_DIR / name
        if p.exists():
            files.append((p.stat().st_mtime, p))
    if not files:
        return []
    files.sort()  # 旧 → 新, 后写入的覆盖同 key
    merged = {}
    for _mtime, p in files:
        try:
            for d in json.loads(p.read_text()).get("details", []):
                key = (d.get("home_bb_cn") or d.get("home_bb", ""),
                       d.get("away_bb_cn") or d.get("away_bb", ""),
                       d.get("start_time_pin_epoch", 0))
                merged[key] = d
        except Exception:
            continue
    return list(merged.values())


def _record_misses(entries, reason):
    """记录窗口内没采到收盘价的条目 + 原因。

    只记还没开赛的(还有救)和刚开赛的(已永久丢失), 供日报统计丢失率、
    供归档回捞挑目标。写失败绝不能影响采集主流程, 全程吞异常。
    """
    if not entries:
        return
    try:
        now = time.time()
        exists = MISS_LOG_FILE.exists()
        with open(MISS_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "logged_at", "reason", "sport", "league", "home", "away",
                "sub_market", "designation", "match_epoch", "minutes_to_match",
                "source", "ev_pct"])
            if not exists:
                w.writeheader()
            for e in entries:
                ep = int(e.get("match_epoch") or 0)
                w.writerow({
                    "logged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "reason": reason, "sport": e.get("sport", ""),
                    "league": e.get("league", ""), "home": e.get("home", ""),
                    "away": e.get("away", ""), "sub_market": e.get("sub_market", ""),
                    "designation": e.get("designation", ""), "match_epoch": ep,
                    "minutes_to_match": round((ep - now) / 60, 1) if ep else "",
                    "source": e.get("source", ""), "ev_pct": e.get("ev_pct", ""),
                })
    except Exception:
        pass


def _migrate_csv_header(path, fieldnames):
    """表头新增字段时就地重写整表, 避免追加行比表头多列造成错位。

    历史上出过 CLV 表头错位 bug: 直接以新 fieldnames 追加, 表头仍是旧的,
    DictReader 会把多出来的列塞进 restkey, 后续统计全部读错。这里在写入前
    检测表头, 缺列则补空值重写一次(幂等, 表头一致时立即返回)。
    """
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
            old = list(rows[0].keys()) if rows else None
        if old is None or not set(fieldnames) - set(old):
            return  # 空表或表头已含全部字段
        tmp = path.with_suffix(path.suffix + ".migrating")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow({k: (r.get(k) if r.get(k) is not None else "") for k in fieldnames})
        tmp.replace(path)
        logger.info("%s 表头已升级: +%s (%d 行已迁移)", path.name,
                    ",".join(sorted(set(fieldnames) - set(old))), len(rows))
    except Exception as e:
        logger.warning("表头迁移失败 %s: %s (跳过, 不阻塞采集)", path.name, e)


def _migrate_results_header(fieldnames):
    _migrate_csv_header(RESULTS_FILE, fieldnames)


def _save_results(results):
    """追加保存 CLV 结果到 CSV。"""
    if not results:
        return

    fieldnames = [
        "collect_time", "push_time", "match_key", "sport", "league", "home", "away",
        "home_pin", "away_pin",
        "designation", "sub_market", "tier", "bb_price_source", "bb_odds", "push_fair_price", "push_ev_pct",
        "close_pin_odds", "close_fair_price", "close_total_implied",
        "true_clv_pct", "clv_delta", "match_epoch", "minutes_before_match", "source",
        # V5.10: 收盘价来源 — live=窗口内实时拉Pin(最准); archive=归档库赛前最后快照回捞;
        #        archive_open=归档库只有首见价(让球/大小球受 UNIQUE 约束去重, 非真收盘价)。
        "close_source", "close_lag_min",
    ]

    _migrate_results_header(fieldnames)

    def _rk(r):
        return (str(r.get("home", "")).strip(), str(r.get("away", "")).strip(),
                str(r.get("sub_market", "")).strip(), str(r.get("designation", "")).strip())

    def _lag(r):
        try:
            return float(r.get("close_lag_min") or 1e9)
        except (TypeError, ValueError):
            return 1e9

    # V5.10: 允许刷新后同一 key 会被采多次, 必须"覆盖"而不是"追加" ——
    # 追加会让同一个机会在 CSV 里出现多行, 所有 CLV 统计直接被重复计数污染。
    # 保留 close_lag_min 更小(更贴近开赛)的那条。
    existing_rows = []
    if RESULTS_FILE.exists():
        try:
            with open(RESULTS_FILE, encoding="utf-8-sig", newline="") as f:
                existing_rows = list(csv.DictReader(f))
        except Exception:
            existing_rows = []

    merged, order = {}, []
    for r in existing_rows + list(results):
        k = _rk(r)
        if k not in merged:
            merged[k] = r
            order.append(k)
        elif _lag(r) <= _lag(merged[k]):
            merged[k] = r      # 新的更贴近开赛 → 覆盖

    tmp = RESULTS_FILE.with_suffix(".csv.writing")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for k in order:
            writer.writerow({fn: merged[k].get(fn, "") for fn in fieldnames})
    tmp.replace(RESULTS_FILE)

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


def log_all_ev_opportunities(comparison_path=None, min_ev=2.0):
    """把对比文件里所有 EV>=min_ev 的机会(去重)追加进 clv_tracking.csv, 用于验证套利模型。

    source='validate', stake=0 — 只用于统计 CLV 验证「EV>2% → 正CLV」是否成立, 不下注。
    与推送的机会(source='push')分开, 统计时按 source 分组即可分别验模型/验过滤。
    """
    from config.constants import get_league_tier
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if comparison_path is None:
        details = _load_freshest_comparison_details()
        if not details:
            return 0
    else:
        comparison_path = Path(comparison_path)
        if not comparison_path.exists():
            return 0
        try:
            details = json.loads(comparison_path.read_text()).get("details", [])
        except Exception:
            return 0

    # 读现有 tracking 的 key 去重
    existing = set()
    if TRACKING_FILE.exists():
        try:
            with open(TRACKING_FILE, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    existing.add((r.get("home", ""), r.get("away", ""),
                                  r.get("sub_market", ""), r.get("designation", ""),
                                  r.get("match_epoch", "")))
        except Exception:
            pass

    _MK = {"opportunities": "1x2", "handicap": "hc", "over_under": "ou",
           "double_chance": "dc", "draw_no_bet": "dnb", "btts": "btts"}
    rows = []
    skipped_started = 0
    seen = set(existing)
    for m in details:
        sport = m.get("sport", "")
        league = m.get("league", "")
        home = m.get("home_bb_cn") or m.get("home_bb", "")
        away = m.get("away_bb_cn") or m.get("away_bb", "")
        home_pin = m.get("home_pin", "")
        away_pin = m.get("away_pin", "")
        league_cn = m.get("league_cn") or league
        epoch = m.get("start_time_pin_epoch", 0) or 0
        # V5.10: 已开赛的比赛不进 validate 样本 —— 此时 BB/Pin 两边都是滚球价,
        # 算出的"机会"不是赛前 +EV, 而且收盘价窗口早已关闭永远采不到。
        # 实测 717 条 tracking 里有 36 条是开赛后才写入的(中位晚 22 分, 最长 207 分),
        # 全部堆在 validate 一侧, 是 validate 中位CLV -3.34% 的污染源之一。
        # 推送路径本身已正确过滤, 只有这里漏了。
        if epoch and epoch <= time.time():
            skipped_started += 1
            continue
        src = m.get("bb_price_source", m.get("platform", "BB"))
        pin_lid = m.get("pin_league_id", "")
        pin_mid = m.get("pin_match_id", "")
        pin_max_stake = m.get("pin_max_stake", "")
        try:
            tier = get_league_tier(league_cn)
        except Exception:
            tier = 3
        for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet", "btts"):
            for opp in m.get(mk, []) or []:
                ev = opp.get("ev_pct", 0) or 0
                if ev < min_ev:
                    continue
                sub = opp.get("_market", "") or _MK.get(mk, "1x2")
                if sub == "main":
                    sub = _MK.get(mk, "1x2")
                desig = opp.get("designation", "")
                key = (home, away, sub, desig, str(epoch))
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "timestamp": now, "sport": sport, "league": league,
                    "home": home, "away": away, "home_pin": home_pin, "away_pin": away_pin,
                    "designation": desig, "sub_market": sub,
                    # V5.10: 显式存线值。实测 77% 的 hc/ou designation 里根本没有线值
                    # (只有"小球"/"让球客胜"), 采集器只能去中文标签里正则抠 —— 抠不到时
                    # get_pin_spread(target_line=None) 会 return candidates[0], 也就是
                    # **随便拿 Pinnacle 的一条线来比**, 静默算出错的 CLV。
                    # 对比文件的 line 字段实测 100% 存在且精确("0"/"2.25"/"+0.5/1"/"-1.5/2"),
                    # 直接存下来, 不再依赖从展示用标签反推。
                    "line": opp.get("line", ""),
                    "bb_odds": opp.get("bb_odds", 0), "pin_odds": opp.get("pin_odds", 0),
                    "fair_price": opp.get("fair_price", 0), "ev_pct": ev,
                    "stake": 0, "tier": tier, "match_epoch": epoch,
                    "bb_price_source": src, "pin_league_id": pin_lid, "pin_match_id": pin_mid,
                    "source": "validate",
                    "pin_max_stake": pin_max_stake if pin_max_stake is not None else "",
                })

    if skipped_started:
        logger.info("CLV验证入库: 跳过 %d 场已开赛比赛(滚球价, 不算赛前机会)", skipped_started)
    if not rows:
        return 0

    fieldnames = ["timestamp", "sport", "league", "home", "away", "home_pin", "away_pin",
                  "designation", "sub_market", "bb_odds", "pin_odds", "fair_price", "ev_pct",
                  "stake", "tier", "match_epoch", "bb_price_source", "pin_league_id", "pin_match_id",
                  "source",
                  # V5.10: Pinnacle 主盘口注额上限(定价信心信号), 先采集不过滤
                  "pin_max_stake",
                  # V5.10: 让球/大小球的线值, 供采集器精确匹配 Pin 同线盘口
                  "line"]
    _migrate_csv_header(TRACKING_FILE, fieldnames)
    file_exists = TRACKING_FILE.exists()
    with open(TRACKING_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    logger.info("CLV验证入库: +%d 条 EV>=%.0f%% 机会 (source=validate)", len(rows), min_ev)
    return len(rows)


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

    entries, expired = _load_pending_entries(return_expired=True)
    total = len(entries)

    # V5: 统计epoch质量
    valid_epoch = sum(1 for e in entries if int(e.get("match_epoch", 0) or 0) > 100000)
    no_epoch = sum(1 for e in entries if not e.get("match_epoch") or int(e.get("match_epoch", 0) or 0) == 0)
    bad_epoch = total - valid_epoch - no_epoch
    logger.info("pending: %d条 (有效epoch:%d, 无epoch:%d, 异常:%d)", total, valid_epoch, no_epoch, bad_epoch)

    # V5.10: 已开赛却没采到收盘价的 = 实时窗口永久丢失, 只能靠归档回捞。
    # 单独报出来, 别再混进 pending 冒充"还在排队"。
    if expired:
        done = len(_load_existing_results())
        rate = len(expired) / max(1, len(expired) + done) * 100
        logger.warning("⚠️ 已过期未采到: %d 条 (实时窗口永久关闭, 丢失率 %.0f%%) — 归档回捞: "
                       "python -m src.monitor.clv_archive_recover --write", len(expired), rate)

    if not entries:
        logger.info("无 pending 记录，跳过")
        return 0

    results = _fetch_close_odds(entries)
    logger.info("采集到 %d 条收盘赔率", len(results))

    # 静默失效监控 — 只在"确实有比赛落在采集窗口内"时才算有活可干,
    # 否则夜里空窗期会天天误报, 告警一旦变噪声就等于没有。
    _now = time.time()
    _in_window = sum(
        1 for e in entries
        if (int(e.get("match_epoch") or 0)
            and CLV_WINDOW_BEFORE_MIN <= (int(e["match_epoch"]) - _now) / 60 <= CLV_WINDOW_BEFORE_MAX))
    if _in_window:
        try:
            from src.monitor.silent_failure_watch import record_run
            record_run("clv_collector", produced=len(results), expected=_in_window,
                       detail=(f"{_in_window} 场比赛在采集窗口内却一条收盘价都没拿到。"
                               f"排查方向: Pinnacle 连通性 / 联赛ID反查 / 队名匹配。"))
        except Exception:
            pass

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
    # 后台任务, 在跨进程共享限速里只吃主扫描剩下的带宽(扫描推送隔离铁律)
    try:
        from src.scrapers.pinnacle_api import set_request_priority
        set_request_priority("low")
    except Exception:
        pass
    collect()
