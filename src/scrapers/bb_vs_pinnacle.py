"""BB体育 vs Pinnacle 赔率对比（改进版）
1. 从 Pinnacle API 获取所有联赛 + 赔率
2. 从 BB体育 提取赔率（只取前3个为1X2赔率）
3. 寻找重叠比赛并计算 +EV

关键改进：
- 只取 BB 前 3 个赔率作为 1X2（跳过没有 1X2 的比赛）
- 通过 full_text 检测 1X2 是否可用
- 队名映射辅助校验
- 提高匹配阈值减少误报
"""
import json, sys, time, math, re, random
import concurrent.futures
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import DATA_DIR, safe_load_json

import requests

# 从子模块导入 API 传输层
from src.scrapers.pinnacle_api import (
    API_BASE, SESSION, api_get, _rate_limit, _diagnose_pinnacle_error, us_to_decimal, get_decimal_price,
)
from src.scrapers.pinnacle_api import _rate_limit as _  # noqa: ensure rate_limit usable
from src.scrapers.devig import shin_fair_odds, devig_favorite_divergence  # Shin 法去抽水, 替代比例法

API_BASE = API_BASE  # re-export for backward compat
SESSION = SESSION

from src.scrapers.pinnacle_league_map import (
    PINNACLE_LEAGUE_FILE, CACHE_TTL_DAYS, TEAM_NAME_MAP_FILE, LEAGUE_KEYWORDS_FILE, TEAM_NAME_MAP,
    _load_league_structure, _save_league_structure,
    _load_team_name_map, _save_team_name_map,
    _load_league_keywords, _save_league_keywords,
    _auto_map_leagues, _auto_map_team_names,
    _match_pin_name, _find_best_league,
    find_pinnacle_league_id, _find_itf_league_ids, find_pinnacle_league_ids,
    discover_new_leagues,
    refresh_league_structure,
)

# 从子模块导入 BB 数据提取
from src.scrapers.bb_data import (
    SPORT_IDS, TWO_WAY_SPORTS, BB_SPORT_KEYWORDS, MARKET_LABELS,
    detect_sport, load_bb_odds, extract_bb_1x2, parse_asian_line,
    extract_bb_handicap, extract_bb_ou, extract_bb_btts,
    extract_bb_oe, extract_bb_htft,
    extract_bb_btts_ht, extract_bb_oe_ht,
)

# 导入市场获取模块
from src.scrapers.pinnacle_markets import (
    sort_ml_prices, get_league_matchups_and_markets,
)

# 对比引擎版本号：修改校准/匹配逻辑后递增，触发全量重建
COMPARISON_CODE_VERSION = 3

# 导入匹配引擎
from src.scrapers.matching_engine import (
    team_name_score, get_pin_ml_sorted, get_pin_ml_sorted_from_source,
    get_pin_spread, get_pin_total,
    _pinyin_match_names, find_pin_match_by_name,
    _bb_to_epoch, _pin_to_epoch, _odds_similarity,
    _make_bb_key, _compute_combined_score, find_matches_by_odds,
)

# 导入子市场机会模块
from src.scrapers.pinnacle_opportunities import (
    add_btts_opportunities as _add_btts_opportunities,
    add_oe_opportunities as _add_oe_opportunities,
    add_htft_opportunities as _add_htft_opportunities,
    map_htft_designations as _map_htft_designations,
    fetch_corner_opportunities as _fetch_corner_opportunities,
    fetch_booking_opportunities as _fetch_booking_opportunities,
    fetch_special_opportunities as _fetch_special_opportunities,
    HTFT_LABELS, HTFT_KEYS,
)

# Re-export for backward compat
_SPORT_IDS = SPORT_IDS
_TWO_WAY_SPORTS = TWO_WAY_SPORTS
_BB_SPORT_KEYWORDS = BB_SPORT_KEYWORDS
_MARKET_LABELS = MARKET_LABELS


# ── 新数据源(odds-api.io Betfair+Sbobet)替代 Pin 公平价(2026-09-18) ──
# BB 运动 key(下划线) → odds-api.io 内部运动 id(经 _SPORT_ID_TO_SLUG 桥接)。
# 注意 sport 在 bb_vs_pinnacle 里是下划线("american_football"), 别写成连字符。
_BB_SPORT_TO_OA_ID = {"football": 1, "basketball": 3, "tennis": 5, "baseball": 7, "american_football": 6}


def _oa_fair(entry, sport, sub, target_line=None):
    """Betfair 单锚 + SBO 置信度(2026-09-26 用户定: 早盘不用 Pinnacle, 只用 Betfair/SBO)。

    之前三锚共识(Pinnacle 0.5+Betfair 0.3+SBO 0.2)依赖 pinnapi 免费100次/天, 额度用完(429)
    就退化 Betfair 单锚。用户定: 早盘干脆不用 Pinnacle, 直接 Betfair 中间价(定价)+SBO devig(置信度)。
    sub ∈ {1x2, hc, ou, ht, ht_ou, dc, dnb, btts}; ht_hc/correct_score/oe/corner/booking 无源。
    target_line: hc/ou/ht_ou 的 BB 让球/大小线(用于在 Betfair 里选对应线)。
    """
    from src.scrapers.odds_api_io import fair_price_bb
    sid = _BB_SPORT_TO_OA_ID.get(sport, 0)
    if not sid:
        return None
    res = fair_price_bb(entry["home_bb"], entry["away_bb"], sid, sub, target_line=target_line)
    if res and res.get("fair"):
        # 存 odds-api.io 事件 id, 供 CLV 采集器读收盘价(替代 pin_match_id)
        if res.get("event_id"):
            entry.setdefault("oa_event_id", res["event_id"])
        # 存 SBO 置信度(按 sub), 供 _oa_add_markets 做同向确认(2026-09-19)
        if res.get("confidence"):
            entry.setdefault("_oa_conf", {})[sub] = res["confidence"]
        return res["fair"]
    return None


def _devig_dc(dc_odds):
    """DC(双重机会)去抽水 — 三条**非互斥**腿, 公平概率和=2(不是1)。

    之前对 [1X,2X,12] 直接用 shin_fair_odds 会强推 Σp=1, 把每条 DC 公平价
    抬高 40-90%(假+EV); 而"从 1X2 推导"又有 HT 平局高估的 21pp 偏差。
    正确做法: 比例法归一化到和=2(三条腿覆盖两个结果, 公平和=2)。

    Args:
        dc_odds: [odds_1X, odds_2X, odds_12] 十进制赔率(顺序对应 dc_labels)
    Returns:
        [fair_1X, fair_2X, fair_12] 公平十进制赔率; 无效则 None
    """
    b = [1.0 / float(o) for o in dc_odds if o and float(o) > 1.0]
    if len(b) < 3:
        return None
    s = sum(b)
    if s <= 2.0 + 1e-9:
        fair_probs = b
    else:
        fair_probs = [bi * 2.0 / s for bi in b]  # 归一化到和=2
    return [round(1.0 / fp, 4) for fp in fair_probs]


def _pin_main_max_stake(pin_match):
    """取该场 Pinnacle 主盘口(全场独赢, 非备用线)的注额上限, 取不到就退到任意盘口最大值。

    单场内不同盘口上限差异很大(实测同一联赛 $250 ~ $9,100), 用主独赢盘最能代表
    Pinnacle 对"这场比赛"整体的定价信心。
    """
    try:
        for e in pin_match.get("moneyline", []) or []:
            if e.get("period") == 0 and not e.get("is_alternate") and e.get("max_stake"):
                return e["max_stake"]
        vals = [e.get("max_stake") for k in ("moneyline", "spread", "total")
                for e in (pin_match.get(k) or []) if e.get("max_stake")]
        return max(vals) if vals else None
    except Exception:
        return None


def verify_match(bb_match, pin_match):
    """Verify a match by checking if team names correspond.
    Returns (verified: bool, note: str)."""
    bb_home = bb_match.get("home", "")
    bb_away = bb_match.get("away", "")
    pin_home = pin_match.get("home", "")
    pin_away = pin_match.get("away", "")

    # V5.10: 传 sport 以启用双打/单打结构护栏(仅个人项目生效)
    _sport = bb_match.get("sport") or ""
    if not _sport:
        try:
            _sport = detect_sport(bb_match) or ""
        except Exception:
            _sport = ""
    ts = team_name_score(bb_home, bb_away, pin_home, pin_away, sport=_sport)

    if ts >= 1.0:
        return True, "队名完全匹配"
    elif ts >= 0.6:
        return True, "队名部分匹配"
    else:
        return False, "队名无法验证（无中文→英文映射）"


def _calibrate_market_line(sport, market_type, bb_line, pin_line, pin_points, is_ht=False):
    """检查 BB 盘口线与 Pinnacle 盘口线是否一致，防止市场错配。

    market_type: "hc"(让球) 或 "ou"(大小)
    is_ht: HT(半场)市场 — 线必须完全一致，不允许近似匹配
    返回 (ok, msg)，ok=False 表示线不匹配，该机会应被过滤掉。
    """
    # BB无线 → 无法比较 (OK, 后续会跳过)
    if bb_line is None:
        return True, ""
    # BB有线但Pin无线 → 校准失败 (不能让BB线匹配到不存在的Pin线)
    if pin_line is None and pin_points is None:
        return False, "Pin无线可比(BB={})".format(bb_line)
    ref = pin_line if pin_line is not None else pin_points
    try:
        ref = float(ref)
    except (TypeError, ValueError):
        return True, ""

    diff = abs(bb_line - ref)

    if market_type == "hc":
        # 让球线: quarter-ball 0.25 一档, 必须精确匹配 (0.01 容差仅容纳浮点误差)
        # V5.5: 0.1 → 0.01, 防止 BB quarter线(-1/1.5=-1.25) 错配到 Pin 整球线(-1)
        max_diff = 0.001 if is_ht else 0.01
        if diff > max_diff:
            tag = "HT" if is_ht else ""
            return False, f"{tag}让球线不一致: BB={bb_line} vs Pinnacle={ref}"
    elif market_type == "ou":
        # 大小球线: 0.01 容差 (2.75 vs 3.0 是 0.25, 必须拒绝)
        max_diff = 0.001 if is_ht else 0.01
        if diff > max_diff:
            tag = "HT" if is_ht else ""
            return False, f"{tag}大小盘线不一致: BB={bb_line} vs Pinnacle={ref}"

    return True, ""


def _warn_suspicious(ev_pct, match_score, verified):
    """返回高 EV / 低置信度警告标记，None 表示无警告。"""
    if ev_pct > 20:
        return "⚠️ 溢价异常高(>20%)，可能是匹配错误，请核对球队"
    if ev_pct > 15:
        return "⚠️ 溢价偏高(>15%)，建议核对赔率"
    if ev_pct > 10 and match_score < 0.85:
        return f"⚠️ 匹配度偏低({match_score})，请确认球队是否正确"
    if not verified and match_score < 0.75:
        return "⚠️ 匹配度偏低，请核对球队是否正确"
    return None


def _check_pinnacle():
    """启动时检测 Pinnacle API 直连连通性(2026-08-27 加 ReadTimeout 重试)。

    之前一次 15s 超时就返回 False → 全量扫描直接取消。ReadTimeout/SSL EOF 是瞬时抖动,
    重试 2 次(间隔 3s)再判不可用。
    """
    test_url = f"{API_BASE}/sports/29/matchups"
    SESSION.proxies = {}

    for attempt in range(3):  # 最多 3 次尝试
        try:
            # 足球 matchups 端点返回 ~30MB(1万+场比赛全盘口), 慢网络下实测 26s+
            # 才下完; 15s 超时会误判"不可用"取消扫描(2026-09-08 早盘停一天根因)。
            resp = SESSION.get(test_url, timeout=45)
            if resp.status_code == 200:
                print(f"  ✅ Pinnacle API 连通正常")
                return True
            print(f"  ⚠️  Pinnacle API 返回 {resp.status_code}")
        except requests.exceptions.SSLError as e:
            print(f"  ❌ Pinnacle API SSL 失败(第{attempt+1}次): {e}")
            print(f"     → 检查系统时间 / 更新 CA 证书")
        except requests.exceptions.ConnectionError as e:
            print(f"  ❌ Pinnacle API 直连失败(第{attempt+1}次): {e}")
            print(f"     → 检查网络连接")
        except Exception as e:
            print(f"  ❌ Pinnacle API 异常 (第{attempt+1}次, {type(e).__name__}): {e}")
        if attempt < 2:
            time.sleep(3)  # 瞬时抖动, 等 3s 重试

    print(f"\n  💡 诊断: Pinnacle API 不可用")
    print(f"    可能原因: 网络连接问题 / Python 3.14 http.client chunked bug")
    return False


_EXTRACTION_META_FILE = DATA_DIR / "extraction_consistency_meta.json"


def _check_extraction_consistency(n_matches: int):
    """检查 BB 提取量是否稳定。如果比上次下降 >30%，打印醒目警告。"""
    prev = None
    if _EXTRACTION_META_FILE.exists():
        try:
            prev = json.loads(_EXTRACTION_META_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    if prev:
        prev_count = prev.get("bb_matches_total", 0)
        if prev_count > 0:
            drop = (prev_count - n_matches) / prev_count
            if drop > 0.30:
                tag = "⚠️" * 5
                print(f"\n{tag} 提取量异常下降!")
                print(f"  BB 比赛数: {prev_count} → {n_matches} ({drop*100:.0f}%)")
                print(f"  检查 bb_api_fetcher.py 是否正常返回数据\n")

    _EXTRACTION_META_FILE.write_text(json.dumps({
        "bb_matches_total": n_matches,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }))


def _preflight_check():
    """连通性预检：同时检查 BB API 和 Pinnacle API，返回是否全部正常。"""
    print("\n" + "=" * 60)
    print("🔌 连通性预检")
    print("=" * 60)

    # BB API
    bb_ok = True
    bb_path = DATA_DIR / "bb_odds_extracted.json"
    if bb_path.exists():
        age = time.time() - bb_path.stat().st_mtime
        print(f"\n📡 BB体育 数据文件: {bb_path.name}")
        print(f"   文件存在, 更新于 {age/60:.0f} 分钟前")
    else:
        print(f"\n📡 BB体育: 数据文件不存在 → 需要先运行 bb_api_fetcher")
        bb_ok = False

    # Pinnacle API
    pin_ok = _check_pinnacle()

    print()
    if bb_ok and pin_ok:
        print(f"  ✅ 全部连通正常")
    else:
        if not bb_ok:
            print(f"  ❌ BB API 异常")
        if not pin_ok:
            print(f"  ❌ Pinnacle API 异常")
    print("=" * 60)
    return bb_ok and pin_ok


def _derive_dnb_fair(ml_odds):
    """由 Pin 独赢(1X2) 推导"平局退款(DNB)"主队公平赔率, 用于校验「0」让球线。

    让球 0 本质 = 平局退款(DNB)。若让球0的公平价与 DNB 分歧大 → 让球线不自洽
    (小联赛流动性差/挂单污染, 会产出假 +EV)。返回主队 DNB 公平赔率, 或 None(无法推导)。
    """
    if len(ml_odds) < 3:
        return None
    try:
        _fairs = shin_fair_odds([ml_odds[0], ml_odds[1], ml_odds[2]])
    except Exception:
        return None
    if any(not f or f <= 0 for f in _fairs):
        return None
    p_home = 1.0 / _fairs[0]
    p_away = 1.0 / _fairs[2]
    denom = p_home + p_away
    if denom <= 0:
        return None
    return 1.0 / (p_home / denom)


_STEAM_MOVES_CACHE = {}  # steam move 比赛集合(matchup_id→1), _detect_steam_moves 更新, entry 构建标记优先比价


def _detect_steam_moves(all_pin_matches):
    """对比 Pin 价格快照, 检测 steam move(Pin 线突然变动 = sharp money 流入)。

    职业团队"chasing steam": 锐利盘(Pin)线一动, 软盘(BB)还没跟上, 抢滞后窗口可套利。
    这里检测 Pin 的 1x2 moneyline 三腿价格变动(任一腿 > 3%), 变动的比赛存到
    pin_steam_moves.json 供 bb_ev_push 标记(steam move 比赛优先比价)。

    返回 {matchup_id: 1} 本次检测到的 steam move 比赛。
    """
    import json as _json
    snap_file = DATA_DIR / "pin_price_snapshot.json"
    steam_file = DATA_DIR / "pin_steam_moves.json"
    prev = {}
    if snap_file.exists():
        try:
            prev = _json.loads(snap_file.read_text())
        except Exception:
            prev = {}
    curr = {}
    steam = {}
    for mu in all_pin_matches:
        mid = mu.get("matchup_id")
        ml = mu.get("moneyline") or []
        if not mid or not ml:
            continue
        # 取 1x2 三腿价格(prices_sorted = [home, draw, away] 十进制)
        legs = []
        for entry in ml:
            for p in (entry.get("prices_sorted") or entry.get("prices") or []):
                v = float(p.get("price_decimal", 0) or 0)
                if v > 1.0:
                    legs.append(v)
        legs = legs[:3]
        if len(legs) < 2:
            continue
        curr[str(mid)] = legs
        old = prev.get(str(mid))
        if old and len(old) == len(legs):
            for o, n in zip(old, legs):
                if o > 0 and abs(n - o) / o > 0.03:  # 任一腿变动 > 3%
                    steam[str(mid)] = 1
                    break
    try:
        snap_file.write_text(_json.dumps(curr))
    except Exception:
        pass
    if steam:
        try:
            steam_file.write_text(_json.dumps(steam))
        except Exception:
            pass
    global _STEAM_MOVES_CACHE
    _STEAM_MOVES_CACHE = steam
    return steam


def compare_bb_vs_pinnacle(bb_matches, all_pin_leagues, selected_leagues=None, save_path=None,
                           use_pin_cache=False, save_pin_cache=False):
    """核心对比逻辑：联赛映射 -> Pinnacle抓取 -> 匹配 -> EV计算 -> 输出。

    Args:
        bb_matches: 已过滤的BB比赛列表
        all_pin_leagues: Pinnacle联赛结构 dict
        selected_leagues: 可选，指定只处理这些BB联赛（None = 全量）
        save_path: 输出路径（None = 默认路径）
    Returns:
        对比结果 dict，失败返回 None
    """
    if save_path is None:
        save_path = DATA_DIR / "bb_vs_pinnacle_comparison.json"

    # 每次扫描重置 cookie 刷新计数器（防 403 死循环）
    from src.scrapers.pinnacle_api import reset_cookie_state
    reset_cookie_state()

    # 版本自检：引擎升级后增量扫描自动切换为全量重建
    if selected_leagues and save_path.exists():
        cached = safe_load_json(save_path, default={})
        if cached.get("code_version", 0) < COMPARISON_CODE_VERSION:
            print(f"  ⚡ 对比引擎升级 (v{cached.get('code_version',0)}->v{COMPARISON_CODE_VERSION})，强制全量重建")
            selected_leagues = None

    # 3. Map BB体育 leagues to Pinnacle league IDs
    bb_leagues = {}
    for m in bb_matches:
        league = m.get("league", "?")
        if league not in bb_leagues:
            bb_leagues[league] = 0
        bb_leagues[league] += 1

    # 如果指定了 selected_leagues，只处理这些联赛
    if selected_leagues is not None:
        bb_leagues = {k: v for k, v in bb_leagues.items() if k in selected_leagues}
        if not bb_leagues:
            print("\n⚠️ 指定的联赛无匹配数据")
            return None
        print(f"\n增量扫描: 只处理 {len(bb_leagues)} 个变动的联赛")

    print(f"\nBB体育联赛分布 ({len(bb_leagues)}):")
    # V5: 动态统计无 Pinnacle 联赛覆盖的运动 (无需硬编码)
    # Pinnacle 联赛 sport 字段存中文名 (来自 SPORT_IDS), BB match sport 存英文 key
    sport_cn = {"pingpong": "乒乓球", "badminton": "羽毛球", "volleyball": "排球",
                "football": "足球", "basketball": "篮球", "tennis": "网球",
                "baseball": "棒球", "american_football": "美式足球",
                "boxing": "拳击", "mma": "MMA", "ice_hockey": "冰球"}
    _cn_to_en = {v: k for k, v in sport_cn.items()}
    _pin_active_sports = set()
    for lid, info in all_pin_leagues.items():
        cn_sport = info.get("sport", "")
        en_sport = _cn_to_en.get(cn_sport, cn_sport)
        _pin_active_sports.add(en_sport)
    _missing_sports = {}
    for m in bb_matches:
        s = m.get("sport", "")
        if s and s not in _pin_active_sports and s in sport_cn:
            _missing_sports[s] = _missing_sports.get(s, 0) + 1
    if _missing_sports:
        skipped_parts = []
        for s, c in sorted(_missing_sports.items(), key=lambda x: -x[1]):
            skipped_parts.append(f"{sport_cn.get(s,s)}({c}场)")
        print(f"  ⚠️ Pinnacle 无此运动联赛: {', '.join(skipped_parts)}")
    league_pin_cache = {}
    unmatched_leagues = []
    for league, count in sorted(bb_leagues.items(), key=lambda x: -x[1]):
        pin_ids = find_pinnacle_league_ids(league, all_pin_leagues)
        league_pin_cache[league] = pin_ids
        status = f" → Pinnacle ID={pin_ids}" if pin_ids else " → ❌ 未匹配"
        print(f"  {league}: {count}场{status}")
        if not pin_ids:
            unmatched_leagues.append(league)

    # 自动全量映射：每天第一次跑数据时，所有新出现的联赛自动找 Pinnacle ID
    new_mappings = {}
    if unmatched_leagues:
        print(f"\n  🔍 自动联赛映射: 尝试为 {len(unmatched_leagues)} 个未匹配联赛发现 Pinnacle ID...")
        new_mappings = _auto_map_leagues(unmatched_leagues, all_pin_leagues) or {}
        # 自动发现：Pinnacle新增联赛，BB未映射的
        discover_new_leagues(all_pin_leagues)
        if new_mappings:
            for league in new_mappings:
                pin_ids = find_pinnacle_league_ids(league, all_pin_leagues)
                if pin_ids:
                    league_pin_cache[league] = pin_ids
                    print(f"    ✅ [{league}] → Pinnacle ID={pin_ids}")
            print()

    # 4. Get Pinnacle odds for matched leagues
    matched_leagues = {}
    for league in bb_leagues:
        pin_ids = league_pin_cache.get(league, [])
        if pin_ids:
            matched_leagues[league] = pin_ids

    if not matched_leagues:
        print("\n⚠️ 没有找到匹配的 Pinnacle 联赛")
        return

    # 5. Fetch markets for each matched league — 去重，每个 Pinnacle ID 只调用一次
    all_unique_pin_ids = set()
    for pin_ids in matched_leagues.values():
        all_unique_pin_ids.update(pin_ids)
    print(f"\n  Pinnacle 联赛去重后: {len(all_unique_pin_ids)} 个 (来自 {len(matched_leagues)} 个 BB 联赛)")

    # V4.3: 全量拉取, Pinnacle API自动过滤空联赛
    pin_ids_to_fetch = list(sorted(all_unique_pin_ids))
    # V5.8 预取缓存(save_pin_cache/--pin-cache)模式: 拉全量联赛而非只拉当前BB窗口涉及的联赛 —
    # 否则缓存残缺, 下次扫描 use_pin_cache 读到不完整缓存会丢足球/网球等主运动对比
    # (根因: 增量扫描的预取线程只缓存了变动的~73个联赛, 没缓存全量206个)
    if save_pin_cache or ("--pin-cache" in sys.argv):
        _all_pin_ids = [lid for lid, info in all_pin_leagues.items()
                        if isinstance(info, dict) and "name" in info]
        pin_ids_to_fetch = list(sorted(set(pin_ids_to_fetch) | set(_all_pin_ids)))
        print(f"\n  📦 预取缓存: 全量拉 {len(pin_ids_to_fetch)} 个联赛 "
              f"(BB窗口 {len(all_unique_pin_ids)} 个 + 补齐 {len(_all_pin_ids)} 个)")
    else:
        print(f"\n  待获取赔率的联赛: {len(pin_ids_to_fetch)} 个")

    # 并行获取（4 个线程，短延时避免 Pinnacle 限流）
    MAX_WORKERS = 3  # V5.1: 并行, 配合文件锁防冲突
    all_pin_matches = []
    _fetch_lock = __import__('threading').Lock()
    _fetch_errors = []  # V5: 跟踪获取失败的联赛

    def _fetch_one(pin_id):
        # V4.3 nested: 穿透查找 league info
        from src.scrapers.pinnacle_league_map import lookup_pin_league
        info = lookup_pin_league(all_pin_leagues, pin_id)
        time.sleep(random.uniform(0.03, 0.10))  # V5: reduced further for 3min scans
        name = info.get('name', pin_id)
        with _fetch_lock:
            print(f"\n获取 [{name}] (ID={pin_id}) 赔率...")
        first_try = get_league_matchups_and_markets(pin_id)
        retried = False
        # V5.1: 并行调用时偶发空返回, 重试一次
        if not first_try:
            time.sleep(0.5)
            first_try = get_league_matchups_and_markets(pin_id)
            retried = True
        with _fetch_lock:
            note = " (重试)" if retried else ""
            print(f"  → [{name}] {len(first_try)} 场比赛{note}")
        # V5.1: 存档赔率到本地历史数据库 (sport 统一英文名)
        if first_try:
            try:
                from src.evolve.odds_archiver import archive_matchups
                _cn2en = {"足球": "football", "篮球": "basketball", "网球": "tennis",
                          "棒球": "baseball", "美式足球": "american_football", "拳击": "boxing",
                          "MMA": "mma", "冰球": "ice_hockey", "乒乓球": "pingpong",
                          "羽毛球": "badminton", "排球": "volleyball"}
                sport = _cn2en.get(info.get('sport', ''), info.get('sport', '?'))
                archive_matchups(sport, pin_id, name, first_try, [])
            except ImportError: pass
        return first_try

    pin_cache_path = DATA_DIR / "pin_matches_cache.json"
    _use_cache = use_pin_cache or ("--use-pin-cache" in sys.argv)
    _save_cache = save_pin_cache or ("--pin-cache" in sys.argv)

    if _use_cache and pin_cache_path.exists():
        # Pin先→BB后 流程: 对比阶段从缓存加载 Pin 赔率, 不重新拉取
        try:
            all_pin_matches = json.loads(pin_cache_path.read_text())
            # 按时间窗过滤: 缓存是全量 415 联赛(含 near/far/urgent), 但本次扫描只匹配
            # 当前 BB 窗口的比赛。按 BB 比赛的起止时间过滤缓存, 减少队名匹配量
            # (near 窗口 599 场 BB 匹配全量 ~1300 场缓存, 匹配量差 3 倍 —— 2026-08-23)。
            _cache_before = len(all_pin_matches)
            _bb_bts = [int(m.get("bt", 0)) for m in bb_matches if m.get("bt")]
            if _bb_bts and all_pin_matches:
                _lo_s = (min(_bb_bts) - 2 * 3600 * 1000) / 1000.0  # ±2h 缓冲
                _hi_s = (max(_bb_bts) + 2 * 3600 * 1000) / 1000.0
                _kept = []
                for _pm in all_pin_matches:
                    _st = _pm.get("start_time") or ""
                    try:
                        _ts = datetime.fromisoformat(str(_st).replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError):
                        _kept.append(_pm)  # 解析不了时间 → 保守保留
                        continue
                    if _lo_s <= _ts <= _hi_s:
                        _kept.append(_pm)
                all_pin_matches = _kept
            print(f"  📦 使用缓存 Pin 赔率 ({len(all_pin_matches)} 场比赛"
                  + (f", 时间窗过滤 {_cache_before}→{len(all_pin_matches)}" if _bb_bts else "") + ")")
        except Exception:
            all_pin_matches = []
    else:
        # V5.5: 并行获取(ThreadPoolExecutor) — 速度优先, 空返回重试已在 _fetch_one 内处理
        # Pin 限速器是全局的(10 req/s), 并行只是隐藏 API 延迟, 不会突破限速
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as _exec:
            _futs = {_exec.submit(_fetch_one, pid): pid for pid in sorted(pin_ids_to_fetch)}
            for _fut in concurrent.futures.as_completed(_futs):
                _pid = _futs[_fut]
                try:
                    _matches = _fut.result()
                    if _matches:
                        all_pin_matches.extend(_matches)
                except Exception as e:
                    from src.scrapers.pinnacle_league_map import lookup_pin_league
                    info = lookup_pin_league(all_pin_leagues, _pid)
                    league_name = info.get('name', str(_pid))
                    sport = info.get('sport', '?')
                    error_msg = f"获取联赛失败 [{league_name}] (ID={_pid}, sport={sport}): {e}"
                    print(f"  ❌ {error_msg}")
                    _fetch_errors.append(error_msg)
        # steam move 检测(2026-09-11): Pin 线变动 = sharp money 流入, BB 未跟上的滞后窗口
        try:
            _steam = _detect_steam_moves(all_pin_matches)
            if _steam:
                print(f"  ⚡ steam move 检测: {len(_steam)} 场 Pin 线变动(优先比价)")
        except Exception:
            pass
        if _save_cache:
            # Pin先→BB后 流程: 只拉 Pin 并缓存, 对比由后续 do_full_scan 的 BB 重拉后完成
            try:
                # 原子写: tmp → rename, 防止读端读到写了一半的坏 JSON
                _tmp = pin_cache_path.with_suffix(".tmp")
                _tmp.write_text(json.dumps(all_pin_matches, ensure_ascii=False))
                _tmp.replace(pin_cache_path)
                print(f"  💾 已缓存 Pin 赔率 ({len(all_pin_matches)} 场), 跳过对比")
            except Exception as e:
                print(f"  ⚠️ 缓存失败: {e}")
            return None

    # 6. Group Pinnacle matches by BB league name for matching
    # V4.5: 优先按 league_id 匹配（比 league_name 字符串匹配更可靠）
    # 网球/棒球/冰球等运动的 Pinnacle API 返回的 match league_name 可能与
    # 联赛结构缓存中的 name 不一致（如 API 返回 "ATP Montreal"，缓存中是
    # "ATP Montreal - R1"），导致按名称过滤全部丢弃。
    pin_by_bb_league = {}
    for bb_league in matched_leagues:
        pin_ids = matched_leagues[bb_league]
        pin_league_names = set()
        pin_league_id_set = set()
        for pid in pin_ids:
            pin_league_id_set.add(str(pid))
            # 同时保留 league_name 作为 fallback
            from src.scrapers.pinnacle_league_map import lookup_pin_league
            info = lookup_pin_league(all_pin_leagues, pid)
            name = info.get("name", "")
            if name:
                pin_league_names.add(name)
        # league_id 匹配（首选） + league_name fallback（league_id 为空时）
        id_matches = [m for m in all_pin_matches if str(m.get("league_id", "")) in pin_league_id_set]
        name_matches = [m for m in all_pin_matches if m["league_name"] in pin_league_names and m not in id_matches]
        pin_by_bb_league[bb_league] = id_matches + name_matches

    # 7. Find overlapping matches by odds pattern matching
    # V5.1: debug — log non-football leagues in pin_by_bb_league
    _non_fb_count = sum(1 for league in pin_by_bb_league if any(
        kw in league for kw in ('WNBA','NBA','ATP','WTA','MLB','NFL','UFC','拳击')))
    _non_fb_matches = sum(len(v) for k, v in pin_by_bb_league.items() if any(
        kw in k for kw in ('WNBA','NBA','ATP','WTA','MLB','NFL','UFC','拳击')))
    if _non_fb_matches > 0:
        print(f"  🔍 pin_by_bb_league: {_non_fb_count} 非足球联赛, {_non_fb_matches} Pinnacle场次")
    matched = find_matches_by_odds(bb_matches, pin_by_bb_league)

    # V4.5: Union-Find 去重 — 跨联赛同比赛只保留最高分
    from src.scrapers.matching_engine import dedup_cross_league, try_date_independent_match
    if matched:
        before = len(matched)
        matched = dedup_cross_league(matched)
        if len(matched) < before:
            print(f"  🔗 Union-Find去重: {before}→{len(matched)} (移除{before-len(matched)}个重复)")

    # V4.5: 日期无关匹配 — 网球/拳击/MMA (拼音过滤防错配)
    for sport_name in ("tennis", "boxing", "mma"):
        sport_matches = try_date_independent_match(bb_matches, pin_by_bb_league, sport=sport_name)
        if sport_matches:
            # 去重: 避免与已有匹配重复
            used_bb = {_make_bb_key(m["bb"]) for m in matched}
            used_pin = {m["pin"].get("matchup_id", id(m["pin"])) for m in matched}
            new = [m for m in sport_matches
                   if _make_bb_key(m["bb"]) not in used_bb
                   and m["pin"].get("matchup_id", id(m["pin"])) not in used_pin]
            if new:
                matched.extend(new)
                print(f"  📅 日期无关匹配 [{sport_name}]: +{len(new)} 场")

    # 自动队名映射：从高置信度匹配中提取中文→英文队名
    if matched:
        _auto_map_team_names(matched)

    # 7a. 网球占位符过滤：Pinnacle 有时返回 "Qualifier vs Qualifier" 等占位比赛
    _PLACEHOLDER_NAMES = {"qualifier", "tbd", "bye", "player", "winner", "alternate",
                          "qualifying", "unknown", "placeholder", "待定", "资格赛选手"}
    placeholder_count = 0
    for m in matched:
        if m.get("sport") == "tennis":
            home = (m.get("pin") or {}).get("home", "").strip().lower()
            away = (m.get("pin") or {}).get("away", "").strip().lower()
            if home in _PLACEHOLDER_NAMES or away in _PLACEHOLDER_NAMES:
                m["_placeholder"] = True
                placeholder_count += 1
            # 双打比赛中 "Unknown/Unknown vs Unknown/Unknown"
            elif all(part.strip().lower() in _PLACEHOLDER_NAMES or not part.strip()
                     for part in re.split(r'\s*/\s*', home + "/" + away)):
                m["_placeholder"] = True
                placeholder_count += 1
    if placeholder_count:
        print(f"  ⚠️ 占位符过滤: {placeholder_count} 场网球比赛含 Qualifier/TBD 等占位名")

    # 7b. 球员冲突检测：同一联赛同一球员出现在多场Pinnacle比赛 → 可疑数据
    player_conflicts = set()
    for league, pin_list in pin_by_bb_league.items():
        # 收集该联赛下所有Pinnacle比赛中的球员名
        # 先拆出所有独立球员名（单人/双打/团队都拆分）
        pin_players = defaultdict(list)  # player -> [(match_id, home/away)]
        for pin in pin_list:
            pid = id(pin)
            # 用 / 或 vs 拆分可能的双打/团队名
            for side, key in [("home", "home"), ("away", "away")]:
                name = pin.get(key, "").strip()
                if not name:
                    continue
                parts = re.split(r'\s*/\s*|\s+vs\s+', name)
                for part in parts:
                    part = part.strip().lower()
                    if part:
                        pin_players[part].append(pid)
        # 如果某个球员出现在多场比赛中，记录冲突
        for player, pids in pin_players.items():
            unique_pids = set(pids)
            if len(unique_pids) > 1:
                for pid in unique_pids:
                    player_conflicts.add(pid)
    # 标记冲突的matched条目
    conflict_count = 0
    for m in matched:
        pin_id = id(m["pin"])
        if pin_id in player_conflicts:
            m["_player_conflict"] = True
            conflict_count += 1
    if conflict_count:
        print(f"  ⚠️ 球员冲突: {conflict_count} 场比赛同一球员出现在多个对战中（可能是过期数据）")

    name_matches = [m for m in matched if m.get("match_type") == "name"]
    other_matches = [m for m in matched if m.get("match_type") != "name"]

    print(f"\n\n匹配比赛: {len(matched)} 场")
    print(f"  队名: {len(name_matches)} | 时间+赔率: {len(other_matches)}")

    # 验证匹配的比赛：检查球队名是否一致
    verified_count = 0
    for m in matched:
        verified, note = verify_match(m["bb"], m["pin"])
        m["verified"] = verified
        m["verify_note"] = note
        if verified:
            verified_count += 1
    print(f"  队名验证: {verified_count}/{len(matched)} 可确认球队一致")

    # 校准计数器
    cal_blocked_hc = 0
    cal_blocked_ou = 0

    # For +EV calculation
    valid_matches = matched
    if conflict_count:
        print(f"  ⚠️ {conflict_count} 场有球员冲突（已在详情中标记），推送到期后人工确认")

    if not matched:
        print("\n⚠️ 联赛匹配成功但没有找到相同比赛")
        return

    # 8. Compare all markets (1X2, Handicap, O/U)
    opportunities = []
    time_skip_count = 0
    for m in valid_matches:
        bb = m["bb"]
        pin = m["pin"]
        sport = m.get("sport", "football")

        # V5: 时间匹配假阳性防护 — 非队名匹配且双方队名都不同时跳过
        match_type = m.get("match_type", "")
        match_score = m.get("match_score", 0)
        if match_type != "name":
            # 检查是否有任何队名相似性
            bb_home = (bb.get("home") or "").lower()
            bb_away = (bb.get("away") or "").lower()
            pin_home = (pin.get("home") or "").lower()
            pin_away = (pin.get("away") or "").lower()
            # 至少有一队名包含对方(时间匹配必须队名重叠, 不允许高分放行)
            has_name_overlap = (
                (bb_home and (bb_home in pin_home or pin_home in bb_home)) or
                (bb_away and (bb_away in pin_away or pin_away in bb_away))
            )
            if not has_name_overlap:
                time_skip_count += 1
                continue
        mlabels = MARKET_LABELS.get(sport, MARKET_LABELS["football"])
        bb_ml = m.get("bb_1x2", [])
        pin_ml = m.get("pin_1x2", [])
        # V4.5: 对齐数组长度 — tennis/boxing等2way运动pin_ml可能≠bb_ml长度
        # V5.8: 2-way运动的 bb_ml 可能混入第3个赔率(如篮球带平局)导致 n_ml>len(ml), 越界崩溃
        n_ml = min(len(bb_ml), len(mlabels["ml"]))
        if len(pin_ml) < n_ml:
            pin_ml = list(pin_ml) + [0] * (n_ml - len(pin_ml))
        elif len(pin_ml) > n_ml:
            pin_ml = pin_ml[:n_ml]

        # 开赛时间（北京时间）
        bb_period = bb.get("period", "")
        bb_time = bb.get("time", "")
        bb_bt = bb.get("bt")
        bb_epoch = None  # 2026-09-08: 供开赛时间一致性校验
        if bb_bt:
            try:
                bb_epoch = int(int(bb_bt) / 1000)
                bb_dt = datetime.fromtimestamp(bb_epoch, tz=timezone.utc)
                bb_bj = bb_dt.astimezone(timezone(timedelta(hours=8)))
                bb_start = bb_bj.strftime("%m/%d %H:%M")
            except (ValueError, TypeError, OSError):
                bb_start = ""
        else:
            bb_start = f"{bb_period} {bb_time}".strip()
        pin_start_raw = pin.get("start_time", "")
        # Convert Pinnacle UTC to epoch for display
        pin_epoch = _pin_to_epoch(pin)

        # 2026-09-08 彻底解决 BB↔Pin 场次错配: 开赛时间差 > 60 分钟 = 配错比赛(队名相似但不同场),
        # 直接跳过不进库。归档回捞实测 8 条错配(开赛差 -2895min~+120min), 这种 EV 全是假的。
        if bb_epoch and pin_epoch and abs(bb_epoch - pin_epoch) > 60 * 60:
            time_skip_count += 1
            continue

        entry = {
            "league": m["league"],
            "match_type": m.get("match_type", "?"),
            "bb_match_id": bb.get("id", ""),  # BB 比赛ID, 结算时按ID精确匹配(免队名错配)
            "home_bb": bb.get("home", "?"),
            "away_bb": bb.get("away", "?"),
            # V5.5: 中文名(展示用) — 从双语言拉取携带
            "home_bb_cn": bb.get("home_cn") or bb.get("home", ""),
            "_dbg_home_cn": bb.get("home_cn"),
            "away_bb_cn": bb.get("away_cn", bb.get("away", "")),
            "league_cn": bb.get("league_cn", m.get("league", "")),
            "home_pin": pin.get("home", "?"),
            "away_pin": pin.get("away", "?"),
            # V5.9: 存 Pinnacle 联赛/比赛 ID, 供 CLV 采集器按 ID 直拉(免反查联赛名映射)
            "pin_league_id": str(pin.get("league_id", "") or ""),
            "pin_match_id": str(pin.get("matchup_id", "") or ""),
            # steam move 标记(2026-09-11): Pin 线刚变动的比赛, BB 可能未跟上(滞后窗口, 推送端优先)
            "_steam_move": (str(pin.get("matchup_id", "")) in _STEAM_MOVES_CACHE),
            # 2026-09-07: 存 Pin 全场 1X2 原始 3-way, 供 Betfair 双锚交叉验证(检测 Pin 主/平/客偏差)
            "_pin_ml": pin_ml,
            # V5.10: Pinnacle 主盘口注额上限 = 它对自己定价的信心。上限低 = 它没把握,
            # 我们拿它的去抽水价当"公平价"标尺就不可靠, 算出的 EV 更可能是噪声。
            # 实测 NBA 中位 $750 vs 乌拉圭女足 $50(15倍), 与 CLV 分档吻合(低上限的
            # T3/T4 正是中位 CLV 最差的档)。**现阶段只采集入库不做过滤** —— 门槛要等
            # 真实 CLV 数据验证「上限低→CLV 差」成立后再定, 别拿未验证的假设砍机会。
            "pin_max_stake": _pin_main_max_stake(pin),
            "match_score": m["match_score"],
            "sport": sport,
            "flags": [],
            "start_time_bb": bb_start,
            "start_time_pin": pin_start_raw,
            "start_time_pin_epoch": pin_epoch,
            "_bb_view": bb.get("_bb_view", "main"),
            "platform_sources": bb.get("platform_sources", {}),
            "bb_price_source": bb.get("platform", "BB"),
            "opportunities": [],
            "handicap": [],
            "over_under": [],
            "double_chance": [],
            "draw_no_bet": [],
        }

        # Pinnacle 队名含 G1/G2/Game 前缀 → 双赛其中一场，与 BB 单场比赛可能不匹配
        for pname in (entry["home_pin"], entry["away_pin"]):
            if re.search(r'\b[Gg](?:ame)?\s*\d+\b', pname):
                entry["flags"].append(f"Pinnacle含比赛序号前缀({pname})，可能是多赛之一，对比不可靠")

        pin_ml_source = pin.get("moneyline", [])
        pin_hc_source = pin.get("spread", [])
        pin_ou_source = pin.get("total", [])

        # 2-way运动独赢校验: BB和Pin价格不应差>2x (否则可能是让分线混入)
        # V5: AF低比分运动, BB独赢常为空或用错盘口线, 收紧到1.5x
        _ml_ratio_limit = 1.5 if sport == "american_football" else 2.0
        if n_ml == 2 and len(pin_ml) >= 2:
            for i in range(2):
                if bb_ml[i] and pin_ml[i]:
                    ratio = max(bb_ml[i], pin_ml[i]) / min(bb_ml[i], pin_ml[i])
                    if ratio > _ml_ratio_limit:
                        entry["flags"].append(f"⚠️ {mlabels['ml'][i]}独赢价格异常(BB={bb_ml[i]:.2f} Pin={pin_ml[i]:.2f}),可能市场错配")

        # Sanity check: flag if moneyline odds differ by > 3x
        for i in range(n_ml):
            if bb_ml[i] and pin_ml[i]:
                ratio = max(bb_ml[i], pin_ml[i]) / min(bb_ml[i], pin_ml[i])
                if ratio > 3.0:
                    entry["flags"].append(f"{mlabels['ml'][i]}差异{ratio:.1f}x")
                    break

        # --- 独赢 (Moneyline) 带去抽水 ---
        # 检测主客反转：BB 和 Pinnacle 对主客队的标注可能相反
        # 用两种方式检测：1) 队名交叉匹配  2) 赔率差异最小化
        _ml_swapped = False

        # 方式1：队名映射交叉匹配
        _bb_home_mapped = entry["home_bb"] in TEAM_NAME_MAP
        _bb_away_mapped = entry["away_bb"] in TEAM_NAME_MAP
        if _bb_home_mapped and _bb_away_mapped:
            _bb_he = TEAM_NAME_MAP.get(entry["home_bb"], "").lower()
            _bb_ae = TEAM_NAME_MAP.get(entry["away_bb"], "").lower()
            _ph = entry["home_pin"].lower()
            _pa = entry["away_pin"].lower()
            _direct = (1 if _bb_he == _ph else 0) + (1 if _bb_ae == _pa else 0)
            _cross = (1 if _bb_he == _pa else 0) + (1 if _bb_ae == _ph else 0)
            if _cross > _direct:
                _ml_swapped = True

        # 方式2：赔率模式检测 (适用于无队名映射的运动，如拳击/MMA/网球/篮球)
        # V5.10 修复: 2-way 运动禁用方式2。
        # 实测 NFL 公羊/钢人两场主客本就对齐, 但 BB 对热门队定价偏高一点, direct 与
        # cross 的差值就跌破 0.85 阈值触发反转 —— 把冷门价硬对到热门 fair, 产出方向
        # 完全相反的假 +EV(本应 -30% 变成 +14%)。2-way 只有两条腿、无 draw 锚点,
        # 赔率模式对"轻微定价偏差 vs 真反转"不可分, 纯靠赔率猜主客太危险。
        # 方式1(TEAM_NAME_MAP 交叉匹配)是可靠信号, 2-way 反转只信它。
        if not _ml_swapped and len(bb_ml) >= 2 and len(pin_ml) >= 2 and n_ml != 2:
            _direct_diff = 0.0
            _cross_diff = 0.0
            _n_compare = min(len(bb_ml), len(pin_ml))  # V4.5: 防数组不等长
            for i in range(_n_compare):
                if bb_ml[i] and pin_ml[i]:
                    _direct_diff += abs(bb_ml[i] - pin_ml[i])
            _pin_cross = list(pin_ml)
            _pin_cross[0], _pin_cross[-1] = _pin_cross[-1], _pin_cross[0]
            _n = min(len(bb_ml), len(pin_ml))
            for i in range(_n):
                if bb_ml[i] and _pin_cross[i]:
                    _cross_diff += abs(bb_ml[i] - _pin_cross[i])
            # V4.2: 放宽阈值 — 2-way sport (篮球等) 赔率接近, 需要更敏感
            is_two_way = n_ml == 2
            swap_ratio_threshold = 0.85 if is_two_way else 0.7
            swap_abs_threshold = 0.25 if is_two_way else 0.5
            if _cross_diff < _direct_diff * swap_ratio_threshold and _direct_diff > swap_abs_threshold:
                _ml_swapped = True
                entry["flags"].append(f"赔率模式检测到主客反转 (direct={_direct_diff:.2f} cross={_cross_diff:.2f})")

        if _ml_swapped:
            entry["flags"].append("已校准: BB主客反转(Pin主=BB客, Pin客=BB主)")

        # 2026-09-18: 公平价改用 odds-api.io Betfair Exchange 中间价(替代 Pin devig)。
        # 主客反转由 odds-api.io 的 match_event_orient 内部处理(swapped 时 home/away 交换)。
        _oa_ml = _oa_fair(entry, sport, "1x2")
        if _oa_ml:
            _ml_keys = ["home", "draw", "away"] if n_ml == 3 else ["home", "away"]
            for i in range(n_ml):
                bb_o = bb_ml[i]
                fair_price = _oa_ml.get(_ml_keys[i])
                if bb_o and fair_price and fair_price > 1:
                    ev = (bb_o - fair_price) / fair_price * 100
                    if ev > 1:
                        entry["opportunities"].append({
                            "designation": mlabels["ml"][i],
                            "bb_odds": bb_o,
                            "pin_odds": 0,  # 新数据源无 Pin 赔率, 用 0 占位
                            "fair_price": round(fair_price, 4),
                            "ev_pct": round(ev, 2),
                        })

        # 网球双打 vs 单打不匹配时跳过让球和大小盘
        _is_doubles_mismatch = (
            sport == "tennis"
            and (" / " in entry.get("home_bb", "") or " / " in entry.get("away_bb", ""))
            and (" / " not in entry.get("home_pin", "") and " / " not in entry.get("away_pin", ""))
        )
        if _is_doubles_mismatch:
            entry["flags"].append("网球双打 vs 单打: 非独赢市场可能来自错误的单打比赛")

        # --- 让球/让分 (Handicap/Spread) ---
        hc_candidates = []
        bb_hc = extract_bb_handicap(bb, sport) if not _is_doubles_mismatch else None
        if bb_hc:
            hc_candidates.append(("main", bb_hc))
        # 备用让球盘线加入对比（BB/FB API 可能有 alternate_handicaps/alternate_handicap）
        alt_hcs = bb.get("odds_ft", {}).get("alternate_handicaps") or bb.get("odds_ft", {}).get("alternate_handicap", [])
        if isinstance(alt_hcs, list):
            for alt in alt_hcs:
                if isinstance(alt, dict) and alt.get("home_odds") and alt.get("away_odds") and (alt.get("home_line") is not None or alt.get("away_line") is not None):
                    hc_candidates.append(("alt", alt))

        for hc_tag, hc_dict in hc_candidates:
            bb_hl = hc_dict.get("home_line") if hc_dict.get("home_line") is not None else hc_dict.get("away_line")
            if bb_hl is None:
                continue
            # 2026-09-18: 公平价改用 Betfair Spread(替代 Pin get_pin_spread+devig)。
            # 主客反转/线取反由 odds-api.io 的 match_event_orient 内部处理。
            _oa_hc = _oa_fair(entry, sport, "hc", target_line=bb_hl)
            if not _oa_hc or not _oa_hc.get("home") or not _oa_hc.get("away"):
                continue
            # 线匹配校验: BB 让球线须 ≈ Betfair 线(否则比的是不同线的价, 假EV)
            _bf_line = _oa_hc.get("line")
            if _bf_line is None or abs(float(bb_hl) - float(_bf_line)) > 0.25:
                if hc_tag == "main":
                    cal_blocked_hc += 1
                continue
            home_fair = _oa_hc["home"]
            away_fair = _oa_hc["away"]
            bb_home_odds = hc_dict["home_odds"]
            bb_away_odds = hc_dict["away_odds"]
            ev_h = (bb_home_odds - home_fair) / home_fair * 100 if home_fair > 0 else 0
            ev_a = (bb_away_odds - away_fair) / away_fair * 100 if away_fair > 0 else 0
            line_info = f"[备{bb_hl}]" if hc_tag == "alt" else ""
            if ev_h > 1:
                opp = {
                    "designation": mlabels["hc_home"],
                    "line": hc_dict.get("home_line_str", ""),
                    "bb_odds": bb_home_odds,
                    "pin_odds": 0,  # 新数据源无 Pin 赔率
                    "fair_price": round(home_fair, 4),
                    "ev_pct": round(ev_h, 2),
                }
                if line_info:
                    opp.setdefault("tags", []).append(line_info)
                entry["handicap"].append(opp)
            if ev_a > 1:
                opp = {
                    "designation": mlabels["hc_away"],
                    "line": hc_dict.get("away_line_str", ""),
                    "bb_odds": bb_away_odds,
                    "pin_odds": 0,
                    "fair_price": round(away_fair, 4),
                    "ev_pct": round(ev_a, 2),
                }
                if line_info:
                    opp.setdefault("tags", []).append(line_info)
                entry["handicap"].append(opp)

        # 2026-09-18: 网球让局(hc_games) 已禁用 —— Betfair 网球 Spread 无法区分让盘/让局, 不再用 Pin 生成假溢价。

        # --- 大小 (Over/Under) 带去抽水 ---
        ou_candidates = []
        bb_ou = extract_bb_ou(bb, sport) if not _is_doubles_mismatch else None
        if bb_ou:
            ou_candidates.append(("main", bb_ou))
        # 备用大小盘线加入对比（BB/FB API 可能有 alternate_totals/alternate_total）
        alt_totals = bb.get("odds_ft", {}).get("alternate_totals") or bb.get("odds_ft", {}).get("alternate_total", [])
        if isinstance(alt_totals, list):
            for alt in alt_totals:
                if isinstance(alt, dict) and alt.get("line") is not None and alt.get("over_odds") and alt.get("under_odds"):
                    ou_candidates.append(("alt", alt))

        for ou_tag, bb_ou in ou_candidates:
            bb_line = bb_ou.get("line")
            if bb_line is None:
                continue
            # 2026-09-18: 公平价改用 Betfair Totals(替代 Pin get_pin_total+devig)。
            _oa_ou = _oa_fair(entry, sport, "ou", target_line=bb_line)
            if not _oa_ou or not _oa_ou.get("over") or not _oa_ou.get("under"):
                continue
            # 线匹配校验: BB 大小线须 ≈ Betfair 线(否则比的是不同线的价, 假EV)
            _bf_line = _oa_ou.get("line")
            if _bf_line is None or abs(float(bb_line) - float(_bf_line)) > 0.25:
                if ou_tag == "main":
                    cal_blocked_ou += 1
                continue
            over_fair = _oa_ou["over"]
            under_fair = _oa_ou["under"]
            ev_o = (bb_ou["over_odds"] - over_fair) / over_fair * 100 if over_fair > 0 else 0
            ev_u = (bb_ou["under_odds"] - under_fair) / under_fair * 100 if under_fair > 0 else 0
            if ev_o > 1:
                entry["over_under"].append({
                    "designation": mlabels["over"],
                    "line": str(bb_line),
                    "bb_odds": bb_ou["over_odds"],
                    "pin_odds": 0,  # 新数据源无 Pin 赔率
                    "fair_price": round(over_fair, 4),
                    "ev_pct": round(ev_o, 2),
                })
            if ev_u > 1:
                entry["over_under"].append({
                    "designation": mlabels["under"],
                    "line": str(bb_line),
                    "bb_odds": bb_ou["under_odds"],
                    "pin_odds": 0,
                    "fair_price": round(under_fair, 4),
                    "ev_pct": round(ev_u, 2),
                })

        # --- 上半场 (HT) 对比：从 DOM odds_ht 读，与 Pinnacle period=1 对比 ---
        bb_ht = bb.get("odds_ht", {})
        if bb_ht and bb_ht.get("ml"):
            # V5.11: period 名称按运动区分 — 足球/篮球/美式足球是"半场", 网球/排球是"第1盘", 冰球是"第1节"
            _ht_period = {"football": "上半场", "basketball": "上半场", "american_football": "上半场",
                          "tennis": "第1盘", "volleyball": "第1盘", "ice_hockey": "第1节"}.get(sport, "上半场")
            ht_labels = {
                "ml": [f"{_ht_period}主胜", f"{_ht_period}和局", f"{_ht_period}客胜"] if sport == "football" else [f"{_ht_period}主胜", f"{_ht_period}客胜"],
                "hc_home": f"{_ht_period}让球主胜", "hc_away": f"{_ht_period}让球客胜",
                "over": f"{_ht_period}大球", "under": f"{_ht_period}小球",
            }
            # HT 独赢 (2026-09-18 全面替代 Pin): 用 Betfair ML HT 中间价当公平价
            bb_ht_ml = bb_ht["ml"]
            if bb_ht_ml:
                _oa_ht = _oa_fair(entry, sport, "ht")
                if _oa_ht:
                    _keys = ["home", "draw", "away"] if sport == "football" else ["home", "away"]
                    for i, label in enumerate(ht_labels["ml"]):
                        if i >= len(bb_ht_ml) or i >= len(_keys):
                            break
                        bb_o = bb_ht_ml[i]
                        fair_price = _oa_ht.get(_keys[i])
                        if bb_o and fair_price and fair_price > 1:
                            ev = (bb_o - fair_price) / fair_price * 100
                            if ev > 1:
                                entry["opportunities"].append({
                                    "designation": label,
                                    "bb_odds": bb_o,
                                    "pin_odds": 0,  # 新数据源无 Pin 赔率, 用 0 占位
                                    "fair_price": round(fair_price, 4),
                                    "ev_pct": round(ev, 2),
                                    "_market": "ht",
                                })

            # HT 让球 (2026-09-18): Betfair 无 Spread HT 市场(仅 Sbobet 有, 但 Sbobet 是置信度
            # 不进定价), 无 Betfair 公平价 → 按「无覆盖=无机会」跳过, 不再用 Pin 生成 ht_hc 假溢价。

            # HT 大小 (2026-09-18): 用 Betfair Totals HT 中间价替代 Pin
            bb_ht_ou = bb_ht.get("total")
            if bb_ht_ou:
                bb_line = bb_ht_ou.get("line")
                if bb_line is not None:
                    _oa_htou = _oa_fair(entry, sport, "ht_ou", target_line=bb_line)
                    if _oa_htou and _oa_htou.get("over") and _oa_htou.get("under"):
                        _bf_line = _oa_htou.get("line")
                        if _bf_line is not None and abs(float(bb_line) - float(_bf_line)) <= 0.25:
                            over_fair = _oa_htou["over"]
                            under_fair = _oa_htou["under"]
                            ev_o = (bb_ht_ou["over_odds"] - over_fair) / over_fair * 100 if over_fair > 0 else 0
                            ev_u = (bb_ht_ou["under_odds"] - under_fair) / under_fair * 100 if under_fair > 0 else 0
                            if ev_o > 1:
                                entry["over_under"].append({
                                    "designation": ht_labels["over"],
                                    "line": str(bb_line),
                                    "bb_odds": bb_ht_ou["over_odds"],
                                    "pin_odds": 0,
                                    "fair_price": round(over_fair, 4),
                                    "ev_pct": round(ev_o, 2),
                                    "_market": "ht_ou",
                                })
                            if ev_u > 1:
                                entry["over_under"].append({
                                    "designation": ht_labels["under"],
                                    "line": str(bb_line),
                                    "bb_odds": bb_ht_ou["under_odds"],
                                    "pin_odds": 0,
                                    "fair_price": round(under_fair, 4),
                                    "ev_pct": round(ev_u, 2),
                                    "_market": "ht_ou",
                                })

        # 2026-09-18: 棒球 F5(前5局大小) 已禁用 —— Betfair 棒球覆盖 0%, 不再用 Pin 生成假溢价。

        # --- 双重机会 (Double Chance) FT ---
        bb_dc = bb.get("odds_dc", [])
        dc_labels = ["双重机会-主/和局", "双重机会-和局/客", "双重机会-主/客"]
        dc_desig_map = {"1X": 0, "2X": 1, "12": 2}
        if len(bb_dc) >= 3 and n_ml == 3:
            # 安全校验：BB DC 赔率是否与主1X2相同（侧边栏点击失败时会出现）
            dc_first3 = [round(float(x), 2) for x in bb_dc[:3]]
            ml_first3 = [round(x, 2) for x in bb_ml[:3]]
            if dc_first3 == ml_first3:
                bb_dc = []

        if len(bb_dc) >= 3 and n_ml == 3:
            # 2026-09-18: 公平价改用 Betfair Double Chance(替代 Pin _devig_dc)。
            # Betfair 键是 1X/12/X2(交易所只给 back, 无 lay), BB 顺序是 [1X(主/和), 2X(和/客)=X2, 12(主/客)]。
            _oa_dc = _oa_fair(entry, sport, "dc")
            if _oa_dc:
                _bb_dc_keys = ["1X", "X2", "12"]
                dc_pair_indices = [(0, 1), (1, 2), (0, 2)]
                for i in range(3):
                    bb_dc_val = float(bb_dc[i]) if isinstance(bb_dc[i], str) else bb_dc[i]
                    fp = _oa_dc.get(_bb_dc_keys[i])
                    if not (bb_dc_val and fp and fp > 0):
                        continue
                    # 安全校验：DC赔率必须低于两个组成赛果的1X2赔率
                    idx1, idx2 = dc_pair_indices[i]
                    if bb_ml[idx1] and bb_ml[idx2] and bb_dc_val >= min(bb_ml[idx1], bb_ml[idx2]):
                        continue
                    ev = (bb_dc_val - fp) / fp * 100
                    if ev > 1:
                        entry["double_chance"].append({
                            "designation": dc_labels[i],
                            "bb_odds": bb_dc_val,
                            "pin_odds": 0,  # 新数据源无 Pin 赔率
                            "fair_price": round(fp, 4),
                            "ev_pct": round(ev, 2),
                            "_market": "dc",
                        })

        # 2026-09-18: HT DC 已禁用 —— Betfair 无 Double Chance HT 市场(仅 Sbobet 有, 但不进定价),
        # 按「无覆盖=无机会」不再用 Pin 生成 ht_dc 假溢价。

        # 2026-09-18: HT BTTS / HT OE 已禁用 —— Betfair 无 BTTS HT / OE HT 市场, 不再用 Pin 生成假溢价。

        # --- 平局退款 (Draw No Bet) FT ---
        bb_dnb = bb.get("odds_dnb", [])
        if len(bb_dnb) >= 2 and n_ml == 3:
            # 安全校验：DNB赔率必须小于对应独赢赔率（退款盘更安全→赔率更低）
            bb_dnb_h = float(bb_dnb[0]) if isinstance(bb_dnb[0], str) else bb_dnb[0]
            bb_dnb_a = float(bb_dnb[1]) if isinstance(bb_dnb[1], str) else bb_dnb[1]
            if bb_dnb_h >= bb_ml[0] * 0.99 or bb_dnb_a >= bb_ml[-1] * 0.99:
                bb_dnb = []
        if len(bb_dnb) >= 2 and n_ml == 3:
            # 2026-09-18: 公平价改用 Betfair Draw No Bet(替代 Pin draw_no_bet+shin)。
            _oa_dnb = _oa_fair(entry, sport, "dnb")
            if _oa_dnb and _oa_dnb.get("home") and _oa_dnb.get("away"):
                dnb_labels = ["平局退款-主", "平局退款-客"]
                dnb_fair = [_oa_dnb["home"], _oa_dnb["away"]]
                for i in range(2):
                    bb_dnb_val = float(bb_dnb[i]) if isinstance(bb_dnb[i], str) else bb_dnb[i]
                    if bb_dnb_val and dnb_fair[i] > 0:
                        ev = (bb_dnb_val - dnb_fair[i]) / dnb_fair[i] * 100
                        if 1 < ev <= 20:
                            entry["draw_no_bet"].append({
                                "designation": dnb_labels[i],
                                "bb_odds": bb_dnb_val,
                                "pin_odds": 0,  # 新数据源无 Pin 赔率
                                "fair_price": round(dnb_fair[i], 4),
                                "ev_pct": round(ev, 2),
                                "_market": "dnb",
                            })

        # --- 双边进球 (BTTS) FT ---
        bb_btts_yes, bb_btts_no = extract_bb_btts(bb)
        if bb_btts_yes and bb_btts_no:
            # 2026-09-18: 公平价改用 Betfair Both Teams To Score(替代 Pin btts+shin)。
            _oa_btts = _oa_fair(entry, sport, "btts")
            if _oa_btts and _oa_btts.get("yes") and _oa_btts.get("no"):
                _add_btts_opportunities(entry, bb_btts_yes, bb_btts_no,
                                        _oa_btts["yes"], _oa_btts["no"])

        # 2026-09-18: 单双(oe) / 半全场(htft) 已禁用 —— Betfair 无 OE 市场; htft 是「放弃」特殊盘口
        # (margin 15%+ 收盘线不 sharp), 不再用 Pin 生成假溢价。

        # 2026-09-18: HT DNB(上半场平局退款) 已禁用 —— Betfair 无 Draw No Bet HT 市场, 不再用 Pin 生成假溢价。

        # 同一市场只保留溢价最高的选项（FT + HT + DC + DNB + HT_DNB + BTTS + OE + HT/FT 各自保留）
        for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
            if entry[mk]:
                ft_entries = [x for x in entry[mk] if x.get("_market") in (None, "", "main")]
                ht_entries = [x for x in entry[mk] if x.get("_market") == "ht"]
                dc_entries = [x for x in entry[mk] if x.get("_market") == "dc"]
                btts_entries = [x for x in entry[mk] if x.get("_market") == "btts"]
                oe_entries = [x for x in entry[mk] if x.get("_market") == "oe"]
                htft_entries = [x for x in entry[mk] if x.get("_market") == "htft"]
                best = []
                if ft_entries:
                    best.append(max(ft_entries, key=lambda x: x["ev_pct"]))
                if ht_entries:
                    best.append(max(ht_entries, key=lambda x: x["ev_pct"]))
                if dc_entries:
                    best.append(max(dc_entries, key=lambda x: x["ev_pct"]))
                ht_dc_entries = [x for x in entry[mk] if x.get("_market") == "ht_dc"]
                if ht_dc_entries:
                    best.append(max(ht_dc_entries, key=lambda x: x["ev_pct"]))
                dnb_entries = [x for x in entry[mk] if x.get("_market") == "dnb"]
                ht_dnb_entries = [x for x in entry[mk] if x.get("_market") == "ht_dnb"]
                if dnb_entries:
                    best.append(max(dnb_entries, key=lambda x: x["ev_pct"]))
                if ht_dnb_entries:
                    best.append(max(ht_dnb_entries, key=lambda x: x["ev_pct"]))
                if btts_entries:
                    best.append(max(btts_entries, key=lambda x: x["ev_pct"]))
                if oe_entries:
                    best.append(max(oe_entries, key=lambda x: x["ev_pct"]))
                if htft_entries:
                    best.append(max(htft_entries, key=lambda x: x["ev_pct"]))
                entry[mk] = best

        if entry["opportunities"] or entry["handicap"] or entry["over_under"] or entry["double_chance"] or entry["draw_no_bet"]:
            # 可疑 EV / 低置信度警告
            for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
                for o in entry.get(mk, []):
                    w = _warn_suspicious(o["ev_pct"], entry["match_score"], m.get("verified", False))
                    if w:
                        o["_warn"] = w
                        if w not in entry["flags"]:
                            entry["flags"].append(w)
            # 低匹配度 + 不可验证 → 标记
            ms = entry["match_score"]
            if not m.get("verified", False) and ms < 0.85:
                entry["flags"].append(f"球队待确认(匹配度{ms})")
            # 球员冲突标记
            if m.get("_player_conflict"):
                entry["flags"].append("球员冲突:同一人出现在多场比赛(可能是过期数据)")
            # 网球占位符标记
            if m.get("_placeholder"):
                entry["flags"].append("网球占位赛:对手为Qualifier/TBD等占位名")
            opportunities.append(entry)

    total_opps_1x2 = sum(len(o["opportunities"]) for o in opportunities)
    total_hc = sum(len(o.get("handicap", [])) for o in opportunities)
    total_ou = sum(len(o.get("over_under", [])) for o in opportunities)
    total_dc = sum(len(o.get("double_chance", [])) for o in opportunities)
    total_dnb = sum(len(o.get("draw_no_bet", [])) for o in opportunities)
    total_btts = sum(1 for o in opportunities for x in o["opportunities"] if x.get("_market") == "btts")
    total_oe = sum(1 for o in opportunities for x in o["opportunities"] if x.get("_market") == "oe")
    total_htft = sum(1 for o in opportunities for x in o["opportunities"] if x.get("_market") == "htft")
    total_1x2_only = total_opps_1x2 - total_btts - total_oe - total_htft
    total_all = total_opps_1x2 + total_hc + total_ou + total_dc + total_dnb

    # 2026-09-18: 角球/罚牌/特殊盘口 已禁用 —— Betfair 无 corner/booking/correct_score 等特殊盘口覆盖,
    # 且这些是「放弃」特殊盘口(margin 15%+ 收盘线不 sharp), 不再用 Pin 生成假溢价。
    corner_entries = []
    booking_entries = []
    special_entries = []
    total_corner = 0
    total_booking = 0
    total_special = 0

    print(f"\n{'='*60}")
    if _fetch_errors:
        print(f"🔴 联赛获取失败: {len(_fetch_errors)} 个 — 这些运动的数据可能不完整")
        for err in _fetch_errors:
            print(f"   {err[:120]}")
    if time_skip_count:
        print(f"🛡️ 时间匹配跳过: {time_skip_count} 场 (无队名重叠)")
    print(f"匹配: {len(matched)} | +EV 独赢: {total_1x2_only} | 让球: {total_hc} | 大小: {total_ou} | 双重机会: {total_dc} | 平局退款: {total_dnb} | 双边进球: {total_btts} | 单/双: {total_oe} | 半全场: {total_htft} | 角球: {total_corner} | 总计: {total_all}")
    print(f"{'='*60}")
    # 校准报告
    if cal_blocked_hc or cal_blocked_ou:
        print(f"\n  🔒 校准拦截: 让球{cal_blocked_hc}个 | 大小{cal_blocked_ou}个 (盘口线不匹配)")
    else:
        print("\n  ✅ 校准全部通过 (所有让球/大小盘口线一致)")
    print()
    for entry in opportunities:
        flag_txt = ""
        sport_tag = {"football":"⚽","basketball":"🏀","tennis":"🎾","baseball":"⚾","american_football":"🏈",
                       "pingpong":"🏓","boxing":"👊","mma":"🥊","badminton":"🏸","ice_hockey":"🏒","volleyball":"🏐"}.get(entry.get("sport", ""), "")
        if entry.get("flags"):
            flag_txt = " ⚠️ " + ", ".join(entry["flags"])
        print(f"\n  [{entry['league']}]{flag_txt}")
        print(f"  BB: {entry['home_bb']} vs {entry['away_bb']}  [{sport_tag}]")
        print(f"  Pin: {entry['home_pin']} vs {entry['away_pin']}")
        print(f"  score={entry['match_score']} | type={entry['match_type']}")
        for o in entry["opportunities"]:
            print(f"    ✅ +EV {o['ev_pct']}%: {o['designation']} (BB={o['bb_odds']} Pin={o['pin_odds']})")
        for o in entry.get("handicap", []):
            print(f"    ✅ +EV {o['ev_pct']}%: {o['line']} {o['designation']} (BB={o['bb_odds']} Pin={o['pin_odds']})")
        for o in entry.get("over_under", []):
            print(f"    ✅ +EV {o['ev_pct']}%: {o['designation']}({o['line']}) (BB={o['bb_odds']} Pin={o['pin_odds']})")
        for o in entry.get("double_chance", []):
            print(f"    ✅ +EV {o['ev_pct']}%: {o['designation']} (BB={o['bb_odds']} Fair={o['fair_price']})")
        for o in entry.get("draw_no_bet", []):
            print(f"    ✅ +EV {o['ev_pct']}%: {o['designation']} (BB={o['bb_odds']} Fair={o['fair_price']})")

    # 提取量一致性检查：如果 BB 比赛数比上次骤降 >30%，打印醒目警告
    _check_extraction_consistency(len(bb_matches))

    # Save
    timestamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    # Sport → name mapping for output
    sport_name_map = {"football":"足球","basketball":"篮球","tennis":"网球","baseball":"棒球","american_football":"美式足球",
                          "pingpong":"乒乓球","boxing":"拳击","mma":"MMA","badminton":"羽毛球","ice_hockey":"冰球","volleyball":"排球"}
    # Per-sport breakdown for consistency tracking
    sport_counts = {}
    sport_opp_counts = {}
    for entry in opportunities:
        s = entry.get("sport", "unknown")
        sport_counts[s] = sport_counts.get(s, 0) + 1
        n_opps = (len(entry.get("opportunities", [])) + len(entry.get("handicap", []))
                   + len(entry.get("over_under", [])) + len(entry.get("double_chance", []))
                   + len(entry.get("draw_no_bet", [])))
        sport_opp_counts[s] = sport_opp_counts.get(s, 0) + n_opps

    # V5: 检测运动级无 +EV (有BB数据但无 +EV 机会)
    # 注意: 不是"对比=0", 而是"匹配了但 BB 赔率≤Pin 公平价, 无 +EV"(2026-08-18 排查确认)
    _bb_sports_with_data = set()
    for m in bb_matches:
        _s = m.get("sport", "")
        if _s:
            _bb_sports_with_data.add(_s)
    _cmp_sports_with_matches = set(sport_counts.keys())
    _lost_sports = _bb_sports_with_data - _cmp_sports_with_matches - {"pingpong", "badminton"}
    if _lost_sports:
        print(f"ℹ️ 运动无+EV: {', '.join(sorted(_lost_sports))} — BB有数据但无+EV机会 (BB赔率≤Pin公平价, 非对比失败)")

    # ── 防御性护栏: 去抽水后公平价必须 >= Pin 原始赔率 ──
    # fair_price < pin_odds 说明 devig 方向错误 (热门公平价被压低), 会制造假 +EV。
    # 见 devig-shin-bug-20260814: 这类假机会曾一天推送 500+ 条。这里兜底剔除。
    _fair_guard_removed = 0
    for _e in opportunities:
        for _mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
            _kept = []
            for _o in _e.get(_mk, []):
                _fp = _o.get("fair_price")
                _po = _o.get("pin_odds")
                if (isinstance(_fp, (int, float)) and isinstance(_po, (int, float))
                        and _po > 1.0 and _fp + 0.005 < _po):
                    _fair_guard_removed += 1
                    continue
                _kept.append(_o)
            _e[_mk] = _kept
    if _fair_guard_removed:
        print(f"  🛡️ 公平价护栏: 剔除 {_fair_guard_removed} 个 fair<Pin 异常机会 (去抽水方向错误)")
        total_opps_1x2 = sum(len(o["opportunities"]) for o in opportunities)
        total_hc = sum(len(o.get("handicap", [])) for o in opportunities)
        total_ou = sum(len(o.get("over_under", [])) for o in opportunities)
        total_dc = sum(len(o.get("double_chance", [])) for o in opportunities)
        total_dnb = sum(len(o.get("draw_no_bet", [])) for o in opportunities)
        total_btts = sum(1 for o in opportunities for x in o["opportunities"] if x.get("_market") == "btts")
        total_oe = sum(1 for o in opportunities for x in o["opportunities"] if x.get("_market") == "oe")
        total_htft = sum(1 for o in opportunities for x in o["opportunities"] if x.get("_market") == "htft")
        total_1x2_only = total_opps_1x2 - total_btts - total_oe - total_htft
        total_all = total_opps_1x2 + total_hc + total_ou + total_dc + total_dnb

    output = {
        "version": "2.0",
        "code_version": 3,  # 对比引擎版本号，升级后强制全量重建
        "parameters": {
            "phase2_threshold_default": 0.70,
            "phase2_threshold_tennis": 0.75,
            "ev_cap_pct": 20,
            "min_ev_pct": 1,
        },
        "timestamp": timestamp,
        "bb_matches_total": len(bb_matches),
        "pinnacle_leagues_found": len(matched_leagues),
        "matched_matches": len(matched),
        "time_match_skipped": time_skip_count,
        "matches_with_ev": len(opportunities),
        "per_sport_matched": {k: v for k, v in sorted(sport_counts.items())},
        "per_sport_opportunities": {k: v for k, v in sorted(sport_opp_counts.items())},
        "fetch_errors": _fetch_errors,  # V5: 联赛获取失败记录
        "opportunities_1x2": total_opps_1x2,
        "opportunities_handicap": total_hc,
        "opportunities_over_under": total_ou,
        "opportunities_double_chance": total_dc,
        "opportunities_draw_no_bet": total_dnb,
        "opportunities_btts": total_btts,
        "opportunities_corner": total_corner,
        "opportunities_total": total_all,
        "calibration_blocked_hc": cal_blocked_hc,
        "calibration_blocked_ou": cal_blocked_ou,
        "details": opportunities,
    }
    # 2026-08-30: Betfair 双锚交叉验证 — 检测 Pin ht 平局偏差, 标记 flags + 降权 ht 机会。
    # 只对有 _pin_ht_ml 且联赛可映射的主流联赛检查, 免费配额内(500次/天)批量查询。
    try:
        from src.scrapers.oddsapi_anchor import cross_validate_ht_entries
        opportunities = cross_validate_ht_entries(opportunities)
        output["details"] = opportunities
    except Exception as _e:
        print(f"  ⚠️ Betfair 双锚交叉验证跳过: {_e}")
    # ---- 映射汇总 ----
    _n_mapped = len(matched_leagues)
    _n_unmapped = len(unmatched_leagues)
    _n_team = len(TEAM_NAME_MAP) - 1  # V4.5: exclude _meta key
    _n_league_total = _n_mapped + _n_unmapped
    print(f"\n{'='*60}")
    print(f"📊 映射汇总")
    print(f"{'='*60}")
    print(f"  联赛: {_n_mapped}/{_n_league_total} 已匹配", end="")
    if _n_unmapped:
        print(f" | ❌ {_n_unmapped} 未匹配 (Pinnacle 无覆盖)", end="")
    print()
    print(f"  队名映射表: {_n_team} 条 (team_name_map.json)")
    if new_mappings:
        print(f"  ✅ 本轮新增联赛映射: {len(new_mappings)} 个")
        for _l in new_mappings:
            print(f"    · {_l}")
    print(f"{'='*60}")

    save_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    print(f"\n已保存到 {save_path}")
    # V5.9: 全量 EV>=2% 机会入库验证(只统计不下注, source=validate), 加速 CLV 样本积累
    try:
        from src.monitor.clv_collector import log_all_ev_opportunities
        _added = log_all_ev_opportunities(save_path)
        if _added:
            print(f"  📊 CLV验证入库: +{_added} 条 EV>=2% 机会")
    except Exception as _e:
        print(f"  ⚠️ CLV验证入库失败: {_e}")
    return output


# ── 早盘 Betfair 直接匹配(2026-09-18 替代 Pin 匹配) ──

def _build_oa_entry(m, sport):
    """BB 比赛 → 早盘 entry(纯 BB 字段 + Betfair 直接匹配, 无 Pin)。"""
    bt = m.get("bt")
    epoch = float(bt) / 1000.0 if bt else 0.0
    return {
        "home_bb": m.get("home", ""), "away_bb": m.get("away", ""),
        "home_bb_cn": m.get("home_cn") or m.get("home", ""),
        "away_bb_cn": m.get("away_cn") or m.get("away", ""),
        "league": m.get("league", ""), "league_cn": m.get("league_cn", ""),
        "sport": sport,
        "start_time_bb": str(bt or ""), "start_time_pin_epoch": epoch,
        "match_type": "name", "match_score": 0.95,  # 队名直接匹配(高置信)
        "bb_price_source": m.get("platform", "BB"),
        "platform_sources": m.get("platform_sources", {}),
        "flags": [],
        "opportunities": [], "handicap": [], "over_under": [],
        "double_chance": [], "draw_no_bet": [],
    }


def _oa_add_markets(entry, bb, sport):
    """给 entry 加各盘口的 Betfair 公平价机会(替代 Pin 比价)。只产 ev>1 的机会。"""
    mlabels = MARKET_LABELS.get(sport, MARKET_LABELS["football"])
    n_ml = 3 if sport not in TWO_WAY_SPORTS else 2

    # --- 1x2 (全场独赢) ---
    bb_ml, valid = extract_bb_1x2(bb, sport)
    if valid:
        oa_ml = _oa_fair(entry, sport, "1x2")
        if oa_ml:
            keys = ["home", "draw", "away"] if n_ml == 3 else ["home", "away"]
            for i in range(n_ml):
                bb_o = bb_ml[i]
                fair = oa_ml.get(keys[i])
                if bb_o and fair and fair > 1:
                    ev = (bb_o - fair) / fair * 100
                    # 2026-09-19 SBO 同向确认: BB 和 SBO 都偏离 Betfair 同向才采信(过滤 Betfair 单边噪音假高溢价)
                    _conf = (entry.get("_oa_conf") or {}).get("1x2", {})
                    _conf_p = _conf.get(keys[i])
                    if _conf_p and _conf_p <= fair:
                        continue  # SBO 不同向 → 跳过
                    if ev > 1:
                        entry["opportunities"].append({
                            "designation": mlabels["ml"][i], "bb_odds": bb_o,
                            "pin_odds": 0, "fair_price": round(fair, 4), "ev_pct": round(ev, 2),
                            "spread": oa_ml.get("spread"),  # 2026-09-19 流动性门槛
                        })

    # --- hc (让球) ---
    bb_hc = extract_bb_handicap(bb, sport)
    if bb_hc:
        bb_hl = bb_hc.get("home_line") if bb_hc.get("home_line") is not None else bb_hc.get("away_line")
        if bb_hl is not None:
            oa_hc = _oa_fair(entry, sport, "hc", target_line=bb_hl)
            if oa_hc and oa_hc.get("home") and oa_hc.get("away"):
                bf_line = oa_hc.get("line")
                if bf_line is not None and abs(float(bb_hl) - float(bf_line)) <= 0.25:
                    ev_h = (bb_hc["home_odds"] - oa_hc["home"]) / oa_hc["home"] * 100
                    ev_a = (bb_hc["away_odds"] - oa_hc["away"]) / oa_hc["away"] * 100
                    # EV>20% = BB 让球盘与独赢盘数据自相矛盾(线错配), 丢弃(对齐 bb_ev_push 的 EV cap)
                    # 2026-09-19 SBO 同向确认: BB 和 SBO 都偏离 Betfair 同向才采信
                    _conf = (entry.get("_oa_conf") or {}).get("hc", {})
                    if 1 < ev_h <= 20 and (not _conf.get("home") or _conf["home"] > oa_hc["home"]):
                        entry["handicap"].append({
                            "designation": mlabels["hc_home"], "line": bb_hc.get("home_line_str", ""),
                            "bb_odds": bb_hc["home_odds"], "pin_odds": 0,
                            "fair_price": round(oa_hc["home"], 4), "ev_pct": round(ev_h, 2),
                            "spread": oa_hc.get("spread"),
                        })
                    if 1 < ev_a <= 20 and (not _conf.get("away") or _conf["away"] > oa_hc["away"]):
                        entry["handicap"].append({
                            "designation": mlabels["hc_away"], "line": bb_hc.get("away_line_str", ""),
                            "bb_odds": bb_hc["away_odds"], "pin_odds": 0,
                            "fair_price": round(oa_hc["away"], 4), "ev_pct": round(ev_a, 2),
                            "spread": oa_hc.get("spread"),
                        })

    # --- ou (大小球) ---
    bb_ou = extract_bb_ou(bb, sport)
    if bb_ou and bb_ou.get("line") is not None:
        oa_ou = _oa_fair(entry, sport, "ou", target_line=bb_ou["line"])
        if oa_ou and oa_ou.get("over") and oa_ou.get("under"):
            bf_line = oa_ou.get("line")
            if bf_line is not None and abs(float(bb_ou["line"]) - float(bf_line)) <= 0.25:
                ev_o = (bb_ou["over_odds"] - oa_ou["over"]) / oa_ou["over"] * 100
                ev_u = (bb_ou["under_odds"] - oa_ou["under"]) / oa_ou["under"] * 100
                # EV>20% = 大小球线与 Betfair 线错配, 丢弃
                # 2026-09-19 SBO 同向确认: BB 和 SBO 都偏离 Betfair 同向才采信
                _conf = (entry.get("_oa_conf") or {}).get("ou", {})
                if 1 < ev_o <= 20 and (not _conf.get("over") or _conf["over"] > oa_ou["over"]):
                    entry["over_under"].append({
                        "designation": mlabels["over"], "line": str(bb_ou["line"]),
                        "bb_odds": bb_ou["over_odds"], "pin_odds": 0,
                        "fair_price": round(oa_ou["over"], 4), "ev_pct": round(ev_o, 2),
                        "spread": oa_ou.get("spread"),
                    })
                if 1 < ev_u <= 20 and (not _conf.get("under") or _conf["under"] > oa_ou["under"]):
                    entry["over_under"].append({
                        "designation": mlabels["under"], "line": str(bb_ou["line"]),
                        "bb_odds": bb_ou["under_odds"], "pin_odds": 0,
                        "fair_price": round(oa_ou["under"], 4), "ev_pct": round(ev_u, 2),
                        "spread": oa_ou.get("spread"),
                    })

    # --- ht (上半场独赢) ---
    bb_ht = bb.get("odds_ht", {})
    ht_ml = bb_ht.get("ml")
    if ht_ml:
        oa_ht = _oa_fair(entry, sport, "ht")
        if oa_ht:
            keys = ["home", "draw", "away"] if sport == "football" else ["home", "away"]
            ht_labels = ([f"上半场主胜", f"上半场和局", f"上半场客胜"] if sport == "football"
                         else [f"上半场主胜", f"上半场客胜"])
            for i in range(min(len(ht_ml), len(keys))):
                bb_o = ht_ml[i]
                fair = oa_ht.get(keys[i])
                if bb_o and fair and fair > 1:
                    ev = (bb_o - fair) / fair * 100
                    if ev > 1:
                        entry["opportunities"].append({
                            "designation": ht_labels[i], "bb_odds": bb_o,
                            "pin_odds": 0, "fair_price": round(fair, 4), "ev_pct": round(ev, 2),
                            "_market": "ht", "spread": oa_ht.get("spread"),
                        })

    # --- ht_ou (上半场大小) ---
    ht_ou = bb_ht.get("total")
    if ht_ou and ht_ou.get("line") is not None:
        oa_htou = _oa_fair(entry, sport, "ht_ou", target_line=ht_ou["line"])
        if oa_htou and oa_htou.get("over") and oa_htou.get("under"):
            bf_line = oa_htou.get("line")
            if bf_line is not None and abs(float(ht_ou["line"]) - float(bf_line)) <= 0.25:
                ev_o = (ht_ou["over_odds"] - oa_htou["over"]) / oa_htou["over"] * 100
                ev_u = (ht_ou["under_odds"] - oa_htou["under"]) / oa_htou["under"] * 100
                if 1 < ev_o <= 20:
                    entry["over_under"].append({
                        "designation": "上半场大球", "line": str(ht_ou["line"]),
                        "bb_odds": ht_ou["over_odds"], "pin_odds": 0,
                        "fair_price": round(oa_htou["over"], 4), "ev_pct": round(ev_o, 2), "_market": "ht_ou",
                    })
                if 1 < ev_u <= 20:
                    entry["over_under"].append({
                        "designation": "上半场小球", "line": str(ht_ou["line"]),
                        "bb_odds": ht_ou["under_odds"], "pin_odds": 0,
                        "fair_price": round(oa_htou["under"], 4), "ev_pct": round(ev_u, 2), "_market": "ht_ou",
                    })

    # --- dc (双重机会) ---
    bb_dc = bb.get("odds_dc", [])
    if len(bb_dc) >= 3 and n_ml == 3:
        oa_dc = _oa_fair(entry, sport, "dc")
        if oa_dc:
            dc_labels = ["双重机会-主/和局", "双重机会-和局/客", "双重机会-主/客"]
            dc_keys = ["1X", "X2", "12"]  # BB 顺序 [1X(主/和), 2X(和/客)=Betfair X2, 12(主/客)]
            for i in range(3):
                bb_val = float(bb_dc[i]) if isinstance(bb_dc[i], str) else bb_dc[i]
                fair = oa_dc.get(dc_keys[i])
                if bb_val and fair and fair > 0:
                    ev = (bb_val - fair) / fair * 100
                    if ev > 1:
                        entry["double_chance"].append({
                            "designation": dc_labels[i], "bb_odds": bb_val,
                            "pin_odds": 0, "fair_price": round(fair, 4), "ev_pct": round(ev, 2), "_market": "dc",
                        })

    # --- dnb (平局退款) ---
    bb_dnb = bb.get("odds_dnb", [])
    if len(bb_dnb) >= 2 and n_ml == 3:
        oa_dnb = _oa_fair(entry, sport, "dnb")
        if oa_dnb and oa_dnb.get("home") and oa_dnb.get("away"):
            dnb_labels = ["平局退款-主", "平局退款-客"]
            dnb_fair = [oa_dnb["home"], oa_dnb["away"]]
            for i in range(2):
                bb_val = float(bb_dnb[i]) if isinstance(bb_dnb[i], str) else bb_dnb[i]
                if bb_val and dnb_fair[i] > 0:
                    ev = (bb_val - dnb_fair[i]) / dnb_fair[i] * 100
                    if 1 < ev <= 20:
                        entry["draw_no_bet"].append({
                            "designation": dnb_labels[i], "bb_odds": bb_val,
                            "pin_odds": 0, "fair_price": round(dnb_fair[i], 4), "ev_pct": round(ev, 2), "_market": "dnb",
                        })

    # --- btts (双边进球) ---
    bb_btts_yes, bb_btts_no = extract_bb_btts(bb)
    if bb_btts_yes and bb_btts_no:
        oa_btts = _oa_fair(entry, sport, "btts")
        if oa_btts and oa_btts.get("yes") and oa_btts.get("no"):
            _add_btts_opportunities(entry, bb_btts_yes, bb_btts_no, oa_btts["yes"], oa_btts["no"])


def compare_bb_vs_oa(bb_matches, save_path=None):
    """早盘 BB vs odds-api.io(Betfair) 直接匹配(2026-09-18 替代 Pin 匹配)。

    遍历所有 BB 比赛, match_event_orient 直接匹配 odds-api.io 事件, Betfair 中间价当公平价
    算 EV。不拉 Pin(联赛结构/匹配引擎全跳过)。输出结构对齐 compare_bb_vs_pinnacle。
    """
    from datetime import datetime, timezone
    if save_path is None:
        save_path = DATA_DIR / "bb_vs_pinnacle_comparison.json"

    entries = []
    sport_counts = {}
    sport_opp_counts = {}
    for m in bb_matches:
        sport = m.get("sport", "football")
        home, away = m.get("home", ""), m.get("away", "")
        if not home or not away:
            continue
        entry = _build_oa_entry(m, sport)
        _oa_add_markets(entry, m, sport)
        has_opp = bool(entry["opportunities"] or entry["handicap"] or entry["over_under"]
                       or entry["double_chance"] or entry["draw_no_bet"])
        sport_counts[sport] = sport_counts.get(sport, 0) + 1
        if has_opp:
            sport_opp_counts[sport] = sport_opp_counts.get(sport, 0) + 1
            entries.append(entry)

    # 2026-09-19 早盘 persistence: 只保留连续 2 轮扫描都 +EV 的机会(过滤单次扫描的赔率错误)
    # 冷启动(无历史)第一轮全保留, 之后每轮只保留「上一轮也 +EV」的。
    _ev_hist_file = DATA_DIR / "early_ev_history.json"
    _prev_ev = set()
    try:
        if _ev_hist_file.exists():
            _prev_ev = set(json.loads(_ev_hist_file.read_text()))
    except Exception:
        pass
    _cur_ev = set()

    def _opp_key(e, group, o):
        return f"{e.get('home_bb', '')}|{e.get('away_bb', '')}|{group}|{o.get('designation', '')}"

    _grps = ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet")
    if _prev_ev:
        for e in entries:
            for g in _grps:
                e[g] = [o for o in e.get(g, []) if _opp_key(e, g, o) in _prev_ev]
    for e in entries:
        for g in _grps:
            for o in e.get(g, []):
                _cur_ev.add(_opp_key(e, g, o))
    # 过滤空 entry(persistence 过滤后所有盘口都被过滤掉的)
    entries = [e for e in entries if any(e.get(g) for g in _grps)]
    try:
        _ev_hist_file.write_text(json.dumps(list(_cur_ev), ensure_ascii=False))
    except OSError:
        pass

    # 汇总
    total_opps_1x2 = sum(1 for e in entries for o in e["opportunities"] if o.get("_market", "") != "ht")
    total_hc = sum(len(e["handicap"]) for e in entries)
    total_ou = sum(1 for e in entries for o in e["over_under"] if o.get("_market", "") != "ht_ou")
    total_dc = sum(len(e["double_chance"]) for e in entries)
    total_dnb = sum(len(e["draw_no_bet"]) for e in entries)
    total_btts = sum(1 for e in entries for o in e["opportunities"] if o.get("_market", "") == "btts")
    total_all = sum(1 for e in entries for o in (e["opportunities"] + e["handicap"] + e["over_under"]
                                                  + e["double_chance"] + e["draw_no_bet"]))

    output = {
        "version": "2.0",
        "code_version": COMPARISON_CODE_VERSION + 10,  # 强制全量重建(Betfair 直接匹配新引擎)
        "parameters": {"min_ev_pct": 1, "ev_cap_pct": 20},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bb_matches_total": len(bb_matches),
        "pinnacle_leagues_found": 0,  # 无 Pin
        "matched_matches": len(entries),
        "time_match_skipped": 0,
        "matches_with_ev": len(entries),
        "per_sport_matched": {k: v for k, v in sorted(sport_counts.items())},
        "per_sport_opportunities": {k: v for k, v in sorted(sport_opp_counts.items())},
        "fetch_errors": [],
        "opportunities_1x2": total_opps_1x2,
        "opportunities_handicap": total_hc,
        "opportunities_over_under": total_ou,
        "opportunities_double_chance": total_dc,
        "opportunities_draw_no_bet": total_dnb,
        "opportunities_btts": total_btts,
        "opportunities_corner": 0,
        "opportunities_total": total_all,
        "calibration_blocked_hc": 0,
        "calibration_blocked_ou": 0,
        "details": entries,
    }
    try:
        _tmp = save_path.with_suffix(".tmp")
        _tmp.write_text(json.dumps(output, ensure_ascii=False))
        _tmp.replace(save_path)
    except OSError as e:
        print(f"  ⚠️ 写对比文件失败: {e}")
    print(f"匹配(Betfair直接): {len(entries)} 场有 +EV | 总计 {total_all} 机会 | 独赢 {total_opps_1x2} | 让球 {total_hc} | 大小 {total_ou}")
    return output


def main():
    """全量对比入口。"""
    print("=" * 60)
    print("BB体育 vs Pinnacle 完整赔率对比 v2")
    print("=" * 60)

    if "--check" in sys.argv:
        _preflight_check()
        return

    # 自定义输入/输出文件（用于FB独立对比等场景）
    input_path = None
    output_path = None
    for arg in sys.argv:
        if arg.startswith("--input="):
            input_path = DATA_DIR / arg.split("=", 1)[1]
        elif arg.startswith("--output="):
            output_path = DATA_DIR / arg.split("=", 1)[1]

    bb_matches = load_bb_odds(path=input_path)
    _now_ts = int(time.time() * 1000)
    _before = len(bb_matches)
    bb_matches = [m for m in bb_matches if not m.get("bt") or int(m["bt"]) > _now_ts]
    _filtered = _before - len(bb_matches)
    if _filtered:
        print(f"  🕐 已过滤 {_filtered} 场已开赛的比赛")

    # V5: 网球只保留ATP/WTA正赛 (挑战赛/ITF/双打错配率极高, Pinnacle覆盖差)
    _tennis_before = sum(1 for m in bb_matches if m.get("sport") == "tennis")
    bb_matches = [m for m in bb_matches if not (
        m.get("sport") == "tennis" and
        any(kw in str(m.get("league","")) for kw in ("ITF", "Challenger", "挑战赛", "W15", "M15", "W25", "M25", "W35", "W50", "W75", "双打", "Doubles"))
    )]
    _tennis_filtered = _tennis_before - sum(1 for m in bb_matches if m.get("sport") == "tennis")
    if _tennis_filtered:
        print(f"  🎾 网球仅保留ATP/WTA正赛, 过滤 {_tennis_filtered} 场低级别赛事")

    # 过滤禁区联赛（中国足球等），在对比层就跳过
    _banned_file = DATA_DIR / "banned_leagues.json"
    if _banned_file.exists():
        _banned = json.loads(_banned_file.read_text())
        _before_ban = len(bb_matches)
        bb_matches = [m for m in bb_matches
                       if not any(b in (m.get("league") or "") or b in (m.get("league_cn") or "") for b in _banned)]
        _banned_filtered = _before_ban - len(bb_matches)
        if _banned_filtered:
            print(f"  🚫 已过滤 {_banned_filtered} 场禁区联赛比赛")

    print(f"\nBB体育: {len(bb_matches)} 场比赛 (已过滤已开赛)")

    valid_1x2 = 0
    valid_2way = 0
    for m in bb_matches:
        sport = detect_sport(m)
        _, valid = extract_bb_1x2(m, sport)
        if valid:
            if sport in TWO_WAY_SPORTS:
                valid_2way += 1
            else:
                valid_1x2 += 1
    print(f"  有独赢赔率: {valid_1x2} 场足球 + {valid_2way} 场其他 = {valid_1x2 + valid_2way}")

    # 2026-09-18: 早盘改用 Betfair 直接匹配(compare_bb_vs_oa), 不再拉 Pin(联赛结构/匹配引擎全跳过)。
    compare_bb_vs_oa(bb_matches, save_path=output_path)


if __name__ == "__main__":
    main()
