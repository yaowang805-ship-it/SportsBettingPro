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
    fetch_corner_opportunities as _fetch_corner_opportunities,
    HTFT_LABELS, HTFT_KEYS,
)

# Re-export for backward compat
_SPORT_IDS = SPORT_IDS
_TWO_WAY_SPORTS = TWO_WAY_SPORTS
_BB_SPORT_KEYWORDS = BB_SPORT_KEYWORDS
_MARKET_LABELS = MARKET_LABELS


def _derive_btts_from_team_total(team_total_entries):
    """从 Pinnacle team_total 0.5 盘口推导 BTTS 公平价（去抽水）。"""
    home_prob = away_prob = None
    for tt in team_total_entries:
        if tt.get("period", 0) != 0:
            continue
        side = tt.get("side", "")
        prices = tt.get("prices", [])
        over_dec = under_dec = None
        for p in prices:
            des = p.get("designation", "").lower()
            dec = get_decimal_price(p) or 0
            if p.get("points") == 0.5:
                if des == "over" and dec > 1:
                    over_dec = dec
                elif des == "under" and dec > 1:
                    under_dec = dec
        if over_dec and under_dec:
            _tt_fairs = shin_fair_odds([over_dec, under_dec])
            prob_over = 1.0 / _tt_fairs[0] if _tt_fairs[0] > 0 else 0
            if side == "home":
                home_prob = prob_over
            elif side == "away":
                away_prob = prob_over
    if not home_prob or not away_prob:
        return None, None
    btts_yes = home_prob * away_prob
    if btts_yes <= 0.001:
        return None, None
    return round(1.0 / btts_yes, 4), round(1.0 / (1.0 - btts_yes), 4)


def verify_match(bb_match, pin_match):
    """Verify a match by checking if team names correspond.
    Returns (verified: bool, note: str)."""
    bb_home = bb_match.get("home", "")
    bb_away = bb_match.get("away", "")
    pin_home = pin_match.get("home", "")
    pin_away = pin_match.get("away", "")

    ts = team_name_score(bb_home, bb_away, pin_home, pin_away)

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
    """启动时检测 Pinnacle API 直连连通性。"""
    test_url = f"{API_BASE}/sports/29/matchups"
    SESSION.proxies = {}

    try:
        resp = SESSION.get(test_url, timeout=15)
        if resp.status_code == 200:
            print(f"  ✅ Pinnacle API 连通正常")
            return True
        print(f"  ⚠️  Pinnacle API 返回 {resp.status_code}")
    except requests.exceptions.SSLError as e:
        print(f"  ❌ Pinnacle API SSL 失败: {e}")
        print(f"     → 检查系统时间 / 更新 CA 证书")
    except requests.exceptions.ConnectionError as e:
        print(f"  ❌ Pinnacle API 直连失败: {e}")
        print(f"     → 检查网络连接")
    except Exception as e:
        print(f"  ❌ Pinnacle API 异常 ({type(e).__name__}): {e}")

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


def compare_bb_vs_pinnacle(bb_matches, all_pin_leagues, selected_leagues=None, save_path=None):
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
    _use_cache = "--use-pin-cache" in sys.argv
    _save_cache = "--pin-cache" in sys.argv

    if _use_cache and pin_cache_path.exists():
        # Pin先→BB后 流程: 对比阶段从缓存加载 Pin 赔率, 不重新拉取
        try:
            all_pin_matches = json.loads(pin_cache_path.read_text())
            print(f"  📦 使用缓存 Pin 赔率 ({len(all_pin_matches)} 场比赛)")
        except Exception:
            all_pin_matches = []
    else:
        # V5.1: 串行获取 (并行偶发联赛数据丢失 → 篮球/网球=0)
        for pid in sorted(pin_ids_to_fetch):
            try:
                matches = _fetch_one(pid)
                all_pin_matches.extend(matches)
            except Exception as e:
                from src.scrapers.pinnacle_league_map import lookup_pin_league
                info = lookup_pin_league(all_pin_leagues, pid)
                league_name = info.get('name', str(pid))
                sport = info.get('sport', '?')
                error_msg = f"获取联赛失败 [{league_name}] (ID={pid}, sport={sport}): {e}"
                print(f"  ❌ {error_msg}")
                _fetch_errors.append(error_msg)
        if _save_cache:
            # Pin先→BB后 流程: 只拉 Pin 并缓存, 对比由后续 do_full_scan 的 BB 重拉后完成
            try:
                pin_cache_path.write_text(json.dumps(all_pin_matches, ensure_ascii=False))
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
        n_ml = len(bb_ml)
        if len(pin_ml) < n_ml:
            pin_ml = list(pin_ml) + [0] * (n_ml - len(pin_ml))
        elif len(pin_ml) > n_ml:
            pin_ml = pin_ml[:n_ml]

        # 开赛时间（北京时间）
        bb_period = bb.get("period", "")
        bb_time = bb.get("time", "")
        bb_bt = bb.get("bt")
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

        entry = {
            "league": m["league"],
            "match_type": m.get("match_type", "?"),
            "home_bb": bb.get("home", "?"),
            "away_bb": bb.get("away", "?"),
            # V5.5: 中文名(展示用) — 从双语言拉取携带
            "home_bb_cn": bb.get("home_cn", bb.get("home", "")),
            "away_bb_cn": bb.get("away_cn", bb.get("away", "")),
            "league_cn": bb.get("league_cn", m.get("league", "")),
            "home_pin": pin.get("home", "?"),
            "away_pin": pin.get("away", "?"),
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
        if not _ml_swapped and len(bb_ml) >= 2 and len(pin_ml) >= 2:
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

        fair_ml = shin_fair_odds(pin_ml)  # Shin 去抽水公平价(整组一次算, 修正 favorite-longshot 偏差)

        # V5.5 热门 devig 交叉验证: Shin vs 比例法在热门端分歧>3% → 标记(推送端提门槛)
        # 实证(2931场Pin收盘): 热门端 Shin 公平赔率系统性比比例法低~2%(中位),
        # 分歧>3%的占22.6% — 此时 Shin 可能高估热门 EV, 用 flag 让推送端保守。
        _fav_div = devig_favorite_divergence(pin_ml)
        if _fav_div < -3.0:
            entry["flags"].append(f"热门devig分歧{abs(_fav_div):.1f}%")
        for i in range(n_ml):
            bb_o = bb_ml[i]
            # 反转时交换 Pinnacle 赔率: 2-way → 交换 0↔1, 3-way → 交换 0↔2
            if _ml_swapped:
                _pin_i = (n_ml - 1 - i) if i in (0, n_ml - 1) else i
            else:
                _pin_i = i
            pin_o = pin_ml[_pin_i]
            if pin_o and pin_o > 0:
                fair_price = fair_ml[_pin_i]
                ev = (bb_o - fair_price) / fair_price * 100 if fair_price > 0 else 0
                if ev > 1:
                    entry["opportunities"].append({
                        "designation": mlabels["ml"][i],
                        "bb_odds": bb_o,
                        "pin_odds": pin_o,
                        "fair_price": fair_price,
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
            home_sp, away_sp, sp_is_alt = get_pin_spread(pin, target_line=bb_hl)
            if not (home_sp and away_sp and get_decimal_price(home_sp) and get_decimal_price(away_sp)):
                continue

            pin_home_odds = get_decimal_price(home_sp)
            pin_away_odds = get_decimal_price(away_sp)
            bb_home_odds = hc_dict["home_odds"]
            bb_away_odds = hc_dict["away_odds"]

            new_flags = []
            if sp_is_alt:
                main_spreads = pin.get("spread", [])
                if main_spreads:
                    mp = main_spreads[0].get("prices", [])
                    mp_line = next((p.get("points","?") for p in mp if p.get("designation")=="home"), "?")
                    mp_odds = next((get_decimal_price(p) or "?" for p in mp if p.get("designation")=="home"), "?")
                    new_flags.append(f"备用盘口: Pin主线={mp_line}@{mp_odds}")

            # 校准：检查让球线是否对得上
            pin_hc_line = home_sp.get("points")
            bb_hc_line_val = hc_dict.get("home_line") if hc_dict.get("home_line") is not None else hc_dict.get("away_line")
            cal_ok, cal_msg = _calibrate_market_line(sport, "hc", bb_hc_line_val, pin_hc_line, None)
            if cal_ok:
                # 二次校验：同时检查 home 和 away 两条线的一致性
                bb_hl_inner = hc_dict.get("home_line")
                bb_al = hc_dict.get("away_line")
                pin_hl_inner = home_sp.get("points")
                pin_al = away_sp.get("points")
                home_ok = (bb_hl_inner is not None and pin_hl_inner is not None
                           and abs(bb_hl_inner - pin_hl_inner) <= 0.01)
                away_ok = (bb_al is not None and pin_al is not None
                           and abs(bb_al - pin_al) <= 0.01)
                if (bb_hl_inner is not None or bb_al is not None) and not (home_ok or away_ok):
                    cal_ok = False
                    cal_msg = f"让球线错配: BB=[{bb_hl_inner},{bb_al}] vs Pin=[{pin_hl_inner},{pin_al}]"
            if not cal_ok:
                if cal_msg not in entry["flags"]:
                    entry["flags"].append(cal_msg)
                if hc_tag == "main":
                    cal_blocked_hc += 1
                continue

            # 线校验通过 → 计算EV
            for f in new_flags:
                if f not in entry["flags"]:
                    entry["flags"].append(f)

            # 通过盘口线（points）对齐：BB 的哪条线匹配 Pinnacle 的主/客
            pin_hl = home_sp.get("points")
            pin_al = away_sp.get("points")
            swapped = False
            if bb_hl is not None and bb_al is not None and pin_hl is not None and pin_al is not None:
                bb_hl = hc_dict.get("home_line")
                bb_al = hc_dict.get("away_line")
                home_diff = abs(bb_hl - pin_hl) if bb_hl is not None else 999
                away_diff = abs(bb_al - pin_al) if bb_al is not None else 999
                cross_home = abs(bb_al - pin_hl) if bb_al is not None else 999
                cross_away = abs(bb_hl - pin_al) if bb_hl is not None else 999
                if cross_home + cross_away < home_diff + away_diff - 0.01:
                    swapped = True

            if swapped:
                bb_hc_odds_for_pin_home = bb_away_odds
                bb_hc_odds_for_pin_away = bb_home_odds
                hc_home_desig = hc_dict.get("away_line_str", "")
                hc_away_desig = hc_dict.get("home_line_str", "")
            else:
                bb_hc_odds_for_pin_home = bb_home_odds
                bb_hc_odds_for_pin_away = bb_away_odds
                hc_home_desig = hc_dict.get("home_line_str", "")
                hc_away_desig = hc_dict.get("away_line_str", "")

            # 去抽水公平价
            _hc_fairs = shin_fair_odds([pin_home_odds, pin_away_odds])
            pin_home_fair = _hc_fairs[0]
            pin_away_fair = _hc_fairs[1]

            ev_h = (bb_hc_odds_for_pin_home - pin_home_fair) / pin_home_fair * 100 if pin_home_fair > 0 else 0
            ev_a = (bb_hc_odds_for_pin_away - pin_away_fair) / pin_away_fair * 100 if pin_away_fair > 0 else 0

            if hc_tag == "alt":
                line_info = f"[备{bb_hl}]" if bb_hl is not None else "[备]"
            else:
                line_info = ""

            if ev_h > 1:
                opp = {
                    "designation": mlabels["hc_home"],
                    "line": hc_home_desig,
                    "bb_odds": bb_hc_odds_for_pin_home,
                    "pin_odds": pin_home_odds,
                    "fair_price": pin_home_fair,
                    "ev_pct": round(ev_h, 2),
                }
                if line_info:
                    opp.setdefault("tags", []).append(line_info)
                entry["handicap"].append(opp)
            if ev_a > 1:
                opp = {
                    "designation": mlabels["hc_away"],
                    "line": hc_away_desig,
                    "bb_odds": bb_hc_odds_for_pin_away,
                    "pin_odds": pin_away_odds,
                    "fair_price": pin_away_fair,
                    "ev_pct": round(ev_a, 2),
                }
                if line_info:
                    opp.setdefault("tags", []).append(line_info)
                entry["handicap"].append(opp)

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
            # 网球：BB 大小线 > 10 表示局数大小，用 games_total
            bb_line = bb_ou.get("line")
            if sport == "tennis" and bb_line is not None and bb_line > 10:
                gt = pin.get("games_total")
                over_p, under_p = get_pin_total({"total": gt}, target_line=bb_line) if gt else (None, None)
            else:
                # 找线值最接近的 Pinnacle 大小盘（可能有多个大小线）
                over_p, under_p = get_pin_total(pin, target_line=bb_line)
            if not over_p or not under_p:
                continue

            _ou_fairs = shin_fair_odds([get_decimal_price(over_p), get_decimal_price(under_p)])
            over_fair = _ou_fairs[0]
            under_fair = _ou_fairs[1]

            # 校准：检查大小盘线是否对得上
            pin_ou_line = over_p.get("points")
            cal_ok, cal_msg = _calibrate_market_line(sport, "ou", bb_ou["line"], pin_ou_line, None)
            if not cal_ok:
                if cal_msg not in entry["flags"]:
                    entry["flags"].append(cal_msg)
                if ou_tag == "main":
                    cal_blocked_ou += 1
                continue

            if get_decimal_price(over_p) and get_decimal_price(over_p) > 0:
                ev_o = (bb_ou["over_odds"] - over_fair) / over_fair * 100
                if ev_o > 1:
                    entry["over_under"].append({
                        "designation": mlabels["over"],
                        "line": str(bb_ou["line"]),
                        "bb_odds": bb_ou["over_odds"],
                        "pin_odds": get_decimal_price(over_p),
                        "fair_price": over_fair,
                        "ev_pct": round(ev_o, 2),
                    })
            if get_decimal_price(under_p) and get_decimal_price(under_p) > 0:
                ev_u = (bb_ou["under_odds"] - under_fair) / under_fair * 100
                if ev_u > 1:
                    entry["over_under"].append({
                        "designation": mlabels["under"],
                        "line": str(bb_ou["line"]),
                        "bb_odds": bb_ou["under_odds"],
                        "pin_odds": get_decimal_price(under_p),
                        "fair_price": under_fair,
                        "ev_pct": round(ev_u, 2),
                    })

        # --- 上半场 (HT) 对比：从 DOM odds_ht 读，与 Pinnacle period=1 对比 ---
        bb_ht = bb.get("odds_ht", {})
        if bb_ht and bb_ht.get("ml"):
            ht_labels = {
                "ml": ["上半场主胜", "上半场和局", "上半场客胜"] if sport == "football" else ["上半场主胜", "上半场客胜"],
                "hc_home": "上半场让球主胜", "hc_away": "上半场让球客胜",
                "over": "上半场大球", "under": "上半场小球",
            }
            # HT 独赢
            pin_ht_ml = get_pin_ml_sorted_from_source(pin.get("ht_moneyline", []), sport)
            if pin_ht_ml and len(pin_ht_ml) >= 2:
                n_ht_ml = min(len(pin_ht_ml), len(ht_labels["ml"]))  # cap to available labels
                bb_ht_ml = bb_ht["ml"]
                if len(bb_ht_ml) >= n_ht_ml:
                    fair_ht_ml = shin_fair_odds(pin_ht_ml)
                    for i in range(n_ht_ml):
                        bb_o = bb_ht_ml[i]
                        pin_o = pin_ht_ml[i]
                        if pin_o and pin_o > 0:
                            fair_price = fair_ht_ml[i]
                            ev = (bb_o - fair_price) / fair_price * 100 if fair_price > 0 else 0
                            if ev > 1:
                                entry["opportunities"].append({
                                    "designation": ht_labels["ml"][i],
                                    "bb_odds": bb_o,
                                    "pin_odds": pin_o,
                                    "fair_price": fair_price,
                                    "ev_pct": round(ev, 2),
                                    "_market": "ht",
                                })

            # HT 让球
            bb_ht_hc = bb_ht.get("handicap")
            if bb_ht_hc:
                bb_hl = bb_ht_hc.get("home_line") if bb_ht_hc.get("home_line") is not None else bb_ht_hc.get("away_line")
                home_sp, away_sp, sp_is_alt = get_pin_spread(pin, target_line=bb_hl, source=pin.get("ht_spread", []))
                if home_sp and away_sp and get_decimal_price(home_sp) and get_decimal_price(away_sp):
                    pin_home_odds = get_decimal_price(home_sp)
                    pin_away_odds = get_decimal_price(away_sp)
                    # 校准：HT 让球线必须精确一致
                    pin_hc_line = home_sp.get("points")
                    bb_hc_line_val = bb_ht_hc.get("home_line")
                    cal_ok, _ = _calibrate_market_line(sport, "hc", bb_hc_line_val, pin_hc_line, None, is_ht=True)
                    if sp_is_alt:
                        ht_spreads = pin.get("ht_spread", [])
                        if ht_spreads:
                            mp = ht_spreads[0].get("prices", [])
                            mp_line = next((p.get("points", "?") for p in mp if p.get("designation") == "home"), "?")
                            mp_odds = next((get_decimal_price(p) or "?" for p in mp if p.get("designation") == "home"), "?")
                            entry["flags"].append(f"备用盘口: Pin主线={mp_line}@{mp_odds}")
                    if cal_ok:
                        _hc_fairs = shin_fair_odds([pin_home_odds, pin_away_odds])
                        home_fair = _hc_fairs[0]
                        away_fair = _hc_fairs[1]
                        ev_h = (bb_ht_hc["home_odds"] - home_fair) / home_fair * 100 if home_fair > 0 else 0
                        ev_a = (bb_ht_hc["away_odds"] - away_fair) / away_fair * 100 if away_fair > 0 else 0
                        if ev_h > 1:
                            entry["handicap"].append({
                                "designation": ht_labels["hc_home"],
                                "line": bb_ht_hc.get("home_line_str", ""),
                                "bb_odds": bb_ht_hc["home_odds"],
                                "pin_odds": pin_home_odds,
                                "fair_price": home_fair,
                                "ev_pct": round(ev_h, 2),
                                "_market": "ht_hc",
                            })
                        if ev_a > 1:
                            entry["handicap"].append({
                                "designation": ht_labels["hc_away"],
                                "line": bb_ht_hc.get("away_line_str", ""),
                                "bb_odds": bb_ht_hc["away_odds"],
                                "pin_odds": pin_away_odds,
                                "fair_price": away_fair,
                                "ev_pct": round(ev_a, 2),
                                "_market": "ht_hc",
                            })

            # HT 大小
            bb_ht_ou = bb_ht.get("total")
            if bb_ht_ou:
                bb_line = bb_ht_ou.get("line")
                over_p, under_p = get_pin_total(pin, target_line=bb_line, source=pin.get("ht_total", []))
                if over_p and under_p:
                    pin_ou_line = over_p.get("points")
                    cal_ok, _ = _calibrate_market_line(sport, "ou", bb_ht_ou["line"], pin_ou_line, None, is_ht=True)
                    if cal_ok:
                        _ou_fairs = shin_fair_odds([get_decimal_price(over_p), get_decimal_price(under_p)])
                        over_fair = _ou_fairs[0]
                        under_fair = _ou_fairs[1]
                        if get_decimal_price(over_p) and get_decimal_price(over_p) > 0:
                            ev_o = (bb_ht_ou["over_odds"] - over_fair) / over_fair * 100
                            if ev_o > 1:
                                entry["over_under"].append({
                                    "designation": ht_labels["over"],
                                    "line": str(bb_ht_ou["line"]),
                                    "bb_odds": bb_ht_ou["over_odds"],
                                    "pin_odds": get_decimal_price(over_p),
                                    "fair_price": over_fair,
                                    "ev_pct": round(ev_o, 2),
                                    "_market": "ht_ou",
                                })
                        if get_decimal_price(under_p) and get_decimal_price(under_p) > 0:
                            ev_u = (bb_ht_ou["under_odds"] - under_fair) / under_fair * 100
                            if ev_u > 1:
                                entry["over_under"].append({
                                    "designation": ht_labels["under"],
                                    "line": str(bb_ht_ou["line"]),
                                    "bb_odds": bb_ht_ou["under_odds"],
                                    "pin_odds": get_decimal_price(under_p),
                                    "fair_price": under_fair,
                                    "ev_pct": round(ev_u, 2),
                                    "_market": "ht_ou",
                                })

        # --- 棒球 F5 (First 5 Innings) 对比：BB odds_f5 vs Pinnacle period=3 ---
        if sport == "baseball":
            bb_f5 = bb.get("odds_f5", {})
            if bb_f5:
                f5_labels = {"over": "F5大球", "under": "F5小球"}
                bb_f5_ou = bb_f5.get("total")
                if bb_f5_ou:
                    bb_line = bb_f5_ou.get("line")
                    # Pinnacle period 3 = F5 innings
                    total_source = pin.get("total", [])
                    f5_totals = [t for t in total_source if t.get("period") == 3]
                    over_p = under_p = None
                    best_diff = float("inf")
                    for t in f5_totals:
                        prices = t.get("prices", [])
                        for p in prices:
                            pts = p.get("points")
                            if pts is not None and abs(pts - (bb_line or 0)) < best_diff:
                                if p.get("designation") == "over":
                                    over_p = p
                                elif p.get("designation") == "under":
                                    under_p = p
                        if over_p and under_p:
                            best_diff = abs((over_p.get("points") or 0) - (bb_line or 0))
                    if over_p and under_p and get_decimal_price(over_p) and get_decimal_price(under_p):
                        _ou_fairs = shin_fair_odds([get_decimal_price(over_p), get_decimal_price(under_p)])
                        over_fair = _ou_fairs[0]
                        under_fair = _ou_fairs[1]
                        ev_o = (bb_f5_ou["over_odds"] - over_fair) / over_fair * 100 if over_fair > 0 else 0
                        ev_u = (bb_f5_ou["under_odds"] - under_fair) / under_fair * 100 if under_fair > 0 else 0
                        if ev_o > 1:
                            entry["over_under"].append({
                                "designation": f5_labels["over"],
                                "line": str(bb_f5_ou.get("line", "")),
                                "bb_odds": bb_f5_ou["over_odds"],
                                "pin_odds": get_decimal_price(over_p),
                                "fair_price": over_fair,
                                "ev_pct": round(ev_o, 2),
                                "_market": "f5",
                            })
                        if ev_u > 1:
                            entry["over_under"].append({
                                "designation": f5_labels["under"],
                                "line": str(bb_f5_ou.get("line", "")),
                                "bb_odds": bb_f5_ou["under_odds"],
                                "pin_odds": get_decimal_price(under_p),
                                "fair_price": under_fair,
                                "ev_pct": round(ev_u, 2),
                                "_market": "f5",
                            })

        # --- 双重机会 (Double Chance) FT ---
        bb_dc = bb.get("odds_dc", [])
        dc_labels = ["双重机会-主/和局", "双重机会-和局/客", "双重机会-主/客"]
        dc_desig_map = {"1X": 0, "2X": 1, "12": 2}
        # dc_fair[i] = fair price for comparison, dc_pin_raw[i] = Pinnacle raw price (for display)
        dc_fair = None
        dc_pin_raw = None

        if len(bb_dc) >= 3 and n_ml == 3:
            # 安全校验：BB DC 赔率是否与主1X2相同（侧边栏点击失败时会出现）
            dc_first3 = [round(float(x), 2) for x in bb_dc[:3]]
            ml_first3 = [round(x, 2) for x in bb_ml[:3]]
            if dc_first3 == ml_first3:
                bb_dc = []

        if len(bb_dc) >= 3 and n_ml == 3:
            # 路径A：Pinnacle 有 double_chance 市场 → 用 Pinnacle 零售价做基准
            pin_dc = pin.get("double_chance", [])
            for dc_market in pin_dc:
                if dc_market.get("period", 0) != 0:
                    continue
                prices = dc_market.get("prices", [])
                if len(prices) >= 3:
                    dc_raw = [None, None, None]
                    for i, p in enumerate(prices):
                        des = p.get("designation", "")
                        val = get_decimal_price(p) or 0
                        idx = dc_desig_map.get(des)
                        if idx is None and i < 3:
                            # pnames 已打标签(pinnacle_markets.py)，此处 fallback 仅用于旧缓存数据
                            idx_map = {0: "1X", 1: "2X", 2: "12"}
                            idx = dc_desig_map.get(idx_map.get(i, ""))
                        if idx is not None and val > 0:
                            dc_raw[idx] = val
                    if all(x and x > 0 for x in dc_raw):
                        # V4.4: Pinnacle DC 价格 < 1.2 → 数据异常, 回退到1X2推导
                        if any(v < 1.2 for v in dc_raw):
                            dc_fair = None  # 触发 Path B
                        else:
                            # 去抽水 (multiplicative proportional method)
                            dc_fair = shin_fair_odds(dc_raw)
                            dc_pin_raw = dc_raw
                    break

            # 路径B：Pinnacle 无 DC 市场 → 从 1X2 推导公平价（去抽水后合并概率）
            if dc_fair is None:
                h, d, a = pin_ml
                if all(x and x > 0 for x in [h, d, a]):
                    imp = 1/h + 1/d + 1/a
                    p_h, p_d, p_a = (1/h)/imp, (1/d)/imp, (1/a)/imp
                    dc_fair = [round(1/(p_h+p_d), 4), round(1/(p_d+p_a), 4), round(1/(p_h+p_a), 4)]
                    # 推导无原始 Pinnacle 价格，dc_pin_raw 保持 None → 推送会显示"推导: 1X2"

            if dc_fair:
                dc_pair_indices = [(0,1), (1,2), (0,2)]
                for i in range(3):
                    bb_dc_val = float(bb_dc[i]) if isinstance(bb_dc[i], str) else bb_dc[i]
                    fp = dc_fair[i]
                    if not (bb_dc_val and fp and fp > 0):
                        continue
                    # 安全校验：DC赔率必须低于两个组成赛果的1X2赔率
                    idx1, idx2 = dc_pair_indices[i]
                    if bb_ml[idx1] and bb_ml[idx2] and bb_dc_val >= min(bb_ml[idx1], bb_ml[idx2]):
                        continue
                    ev = (bb_dc_val - fp) / fp * 100
                    if ev > 1:
                        pin_raw_val = round(dc_pin_raw[i], 4) if dc_pin_raw else 0
                        entry["double_chance"].append({
                            "designation": dc_labels[i],
                            "bb_odds": bb_dc_val,
                            "pin_odds": pin_raw_val,
                            "fair_price": round(fp, 4),
                            "ev_pct": round(ev, 2),
                            "_market": "dc",
                        })

        # --- 上半场双重机会 (HT DC) ---
        bb_ht_dc = bb.get("odds_ht", {}).get("dc", [])
        if len(bb_ht_dc) >= 3 and n_ml == 3:
            # 安全校验：HT DC 赔率是否与 HT 独赢相同
            bb_ht_ml_check = bb.get("odds_ht", {}).get("ml", [])
            if len(bb_ht_ml_check) >= 3:
                dc_first3 = [round(float(x), 2) for x in bb_ht_dc[:3]]
                ht_ml_first3 = [round(x, 2) for x in bb_ht_ml_check[:3]]
                if dc_first3 == ht_ml_first3:
                    bb_ht_dc = []
        if len(bb_ht_dc) >= 3 and n_ml == 3:
            ht_dc_fair = None
            ht_dc_pin_raw = None
            # 路径A: Pinnacle 有 HT DC 子比赛
            pin_dc = pin.get("double_chance", [])
            for dc_market in pin_dc:
                if dc_market.get("period", 0) != 1:
                    continue  # HT DC = period 1
                prices = dc_market.get("prices", [])
                if len(prices) >= 3:
                    dc_desig_map = {"1X": 0, "2X": 1, "12": 2}
                    dc_raw = [None, None, None]
                    for p in prices:
                        des = p.get("designation", "")
                        val = get_decimal_price(p) or 0
                        idx = dc_desig_map.get(des)
                        if idx is None and len(prices) == 3:
                            idx_map = {0: "1X", 1: "2X", 2: "12"}
                            idx = dc_desig_map.get(idx_map.get(prices.index(p), ""))
                        if idx is not None and val > 0:
                            dc_raw[idx] = val
                    if all(x and x > 0 for x in dc_raw):
                        if any(v < 1.2 for v in dc_raw):
                            ht_dc_fair = None
                        else:
                            ht_dc_fair = shin_fair_odds(dc_raw)
                            ht_dc_pin_raw = dc_raw
                    break
            # 路径B: 从 HT 1X2 推导
            if ht_dc_fair is None:
                pin_ht_ml_for_dc = get_pin_ml_sorted_from_source(pin.get("ht_moneyline", []), sport)
                if len(pin_ht_ml_for_dc) == 3:
                    hh, dd, aa = pin_ht_ml_for_dc
                    if all(x and x > 0 for x in [hh, dd, aa]):
                        imp = 1/hh + 1/dd + 1/aa
                        p_h, p_d, p_a = (1/hh)/imp, (1/dd)/imp, (1/aa)/imp
                        ht_dc_fair = [round(1/(p_h+p_d), 4), round(1/(p_d+p_a), 4), round(1/(p_h+p_a), 4)]
            if ht_dc_fair:
                ht_dc_labels = ["上半场双重机会-主/和局", "上半场双重机会-和局/客", "上半场双重机会-主/客"]
                ht_dc_pair_indices = [(0,1), (1,2), (0,2)]
                for i in range(3):
                    bb_val = float(bb_ht_dc[i]) if isinstance(bb_ht_dc[i], str) else bb_ht_dc[i]
                    fp = ht_dc_fair[i]
                    if not (bb_val and fp and fp > 0):
                        continue
                    idx1, idx2 = ht_dc_pair_indices[i]
                    if len(bb_ht_ml_check) >= 3 and bb_val >= min(bb_ht_ml_check[idx1], bb_ht_ml_check[idx2]):
                        continue
                    ev = (bb_val - fp) / fp * 100
                    if ev > 1:
                        pin_raw_val = round(ht_dc_pin_raw[i], 4) if ht_dc_pin_raw else 0
                        entry["double_chance"].append({
                            "designation": ht_dc_labels[i],
                            "bb_odds": bb_val,
                            "pin_odds": pin_raw_val,
                            "fair_price": round(fp, 4),
                            "ev_pct": round(ev, 2),
                            "_market": "ht_dc",
                        })

        # --- HT 双边进球 (HT BTTS) ---
        bb_ht_btts_yes, bb_ht_btts_no = extract_bb_btts_ht(bb)
        if bb_ht_btts_yes and bb_ht_btts_no:
            pin_btts = pin.get("btts", [])
            if pin_btts:
                yes_price = no_price = None
                for btts_entry in pin_btts:
                    if btts_entry.get("period", 0) != 1:  # HT period
                        continue
                    prices = btts_entry.get("prices", [])
                    for p in prices:
                        if p.get("designation") == "yes":
                            yes_price = get_decimal_price(p)
                        elif p.get("designation") == "no":
                            no_price = get_decimal_price(p)
                    if yes_price and no_price:
                        _btts_fairs = shin_fair_odds([yes_price, no_price])
                        yes_fair = _btts_fairs[0]
                        no_fair = _btts_fairs[1]
                        _add_btts_opportunities(entry, bb_ht_btts_yes, bb_ht_btts_no,
                                                yes_fair, no_fair,
                                                pin_yes=yes_price, pin_no=no_price,
                                                prefix="上半场")
                        break

        # --- HT 单/双 (HT OE) ---
        bb_ht_oe_odd, bb_ht_oe_even = extract_bb_oe_ht(bb)
        if bb_ht_oe_odd and bb_ht_oe_even:
            pin_oe = pin.get("oe", [])  # V5 fix: was "odd_even" (key mismatch)
            if pin_oe:
                odd_price = even_price = None
                for oe_entry in pin_oe:
                    if oe_entry.get("period", 0) != 1:  # HT period
                        continue
                    prices = oe_entry.get("prices", [])
                    for p in prices:
                        if p.get("designation") == "odd":
                            odd_price = get_decimal_price(p)
                        elif p.get("designation") == "even":
                            even_price = get_decimal_price(p)
                    if odd_price and even_price:
                        _oe_fairs = shin_fair_odds([odd_price, even_price])
                        odd_fair = _oe_fairs[0]
                        even_fair = _oe_fairs[1]
                        _add_oe_opportunities(entry, bb_ht_oe_odd, bb_ht_oe_even,
                                              odd_fair, even_fair,
                                              pin_odd=odd_price, pin_even=even_price,
                                              prefix="上半场")
                        break

        # --- 平局退款 (Draw No Bet) FT ---
        bb_dnb = bb.get("odds_dnb", [])
        if len(bb_dnb) >= 2 and n_ml == 3:
            # 安全校验：DNB赔率必须小于对应独赢赔率（退款盘更安全→赔率更低）
            bb_dnb_h = float(bb_dnb[0]) if isinstance(bb_dnb[0], str) else bb_dnb[0]
            bb_dnb_a = float(bb_dnb[1]) if isinstance(bb_dnb[1], str) else bb_dnb[1]
            if bb_dnb_h >= bb_ml[0] * 0.99 or bb_dnb_a >= bb_ml[-1] * 0.99:
                bb_dnb = []
        if len(bb_dnb) >= 2 and n_ml == 3:
            dnb_fair = None
            dnb_pin_raw = None
            # 路径A: Pinnacle 直接提供 DNB 市场
            pin_dnb = pin.get("draw_no_bet", [])
            for dnb_entry in pin_dnb:
                if dnb_entry.get("period", 0) != 0:
                    continue
                prices = dnb_entry.get("prices", [])
                if len(prices) >= 2:
                    h_price = a_price = None
                    for p in prices:
                        des = p.get("designation", "").lower()
                        val = get_decimal_price(p) or p.get("price_decimal", 0)
                        if "home" in des or "主" in des:
                            h_price = val
                        elif "away" in des or "客" in des:
                            a_price = val
                    # 通过 participantId 映射
                    if not h_price or not a_price:
                        if len(prices) >= 2:
                            h_price = prices[0].get("price_decimal", 0)
                            a_price = prices[1].get("price_decimal", 0)
                    if h_price and a_price and h_price > 1 and a_price > 1:
                        dnb_fair = shin_fair_odds([h_price, a_price])
                        dnb_pin_raw = [h_price, a_price]
                        break
            if not dnb_fair:
                # 路径B: 从 1X2 推导
                h, d, a = pin_ml
                if all(x and x > 0 for x in [h, d, a]):
                    imp = 1/h + 1/d + 1/a
                    p_h, p_d, p_a = (1/h)/imp, (1/d)/imp, (1/a)/imp
                    dnb_fair = [round(1/(p_h/(1-p_d)), 4), round(1/(p_a/(1-p_d)), 4)]
            if dnb_fair:
                dnb_labels = ["平局退款-主", "平局退款-客"]
                for i in range(2):
                    bb_dnb_val = float(bb_dnb[i]) if isinstance(bb_dnb[i], str) else bb_dnb[i]
                    if bb_dnb_val and dnb_fair[i] > 0:
                        ev = (bb_dnb_val - dnb_fair[i]) / dnb_fair[i] * 100
                        if 1 < ev <= 20:
                            pin_raw = round(dnb_pin_raw[i], 4) if dnb_pin_raw else 0
                            entry["draw_no_bet"].append({
                                "designation": dnb_labels[i],
                                "bb_odds": bb_dnb_val,
                                "pin_odds": pin_raw,
                                "fair_price": round(dnb_fair[i], 4),
                                "ev_pct": round(ev, 2),
                                "_market": "dnb",
                            })

        # --- 双边进球 (BTTS) FT：从 Pinnacle both_to_score 市场或 team_total 0.5 推导 ---
        bb_btts_yes, bb_btts_no = extract_bb_btts(bb)
        if bb_btts_yes and bb_btts_no:
            pin_btts = pin.get("btts", [])
            if pin_btts:
                # 路径A：Pinnacle 直接提供 both_to_score 市场
                for btts_entry in pin_btts:
                    if btts_entry.get("period", 0) != 0:
                        continue
                    prices = btts_entry.get("prices", [])
                    yes_price = no_price = None
                    for p in prices:
                        des = p.get("designation", "").lower()
                        val = get_decimal_price(p) or 0
                        if val <= 0:
                            continue
                        if des in ("yes", "both", "是"):
                            yes_price = val
                        elif des in ("no", "否"):
                            no_price = val
                    # BTTS子比赛: 通过participantId已映射到正确Yes/No标签
                    if not yes_price or not no_price:
                        if len(prices) >= 2:
                            yes_price = prices[0].get("price_decimal", 0)
                            no_price = prices[1].get("price_decimal", 0)
                    if not yes_price or not no_price:
                        continue
                    if not yes_price or not no_price:
                        continue
                    _btts_fairs = shin_fair_odds([yes_price, no_price])
                    yes_fair = _btts_fairs[0]
                    no_fair = _btts_fairs[1]
                    _add_btts_opportunities(entry, bb_btts_yes, bb_btts_no, yes_fair, no_fair, pin_yes=yes_price, pin_no=no_price)
                    break
            else:
                # 路径B: 从 team_total 0.5 推导 BTTS
                # P(两队均进球) = P(主队>0.5) × P(客队>0.5)
                # 进球相关性<0.1, 推导误差~2-3%, 需BB溢价覆盖
                yes_fair, no_fair = _derive_btts_from_team_total(pin.get("team_total", []))
                if yes_fair and no_fair:
                    _add_btts_opportunities(entry, bb_btts_yes, bb_btts_no, yes_fair, no_fair,
                                            source="derived")

        # --- 单/双 (Odd/Even) FT：从 Pinnacle Total Goals Odd/Even 市场 ---
        bb_oe_odd, bb_oe_even = extract_bb_oe(bb)
        if bb_oe_odd and bb_oe_even:
            pin_oe = pin.get("oe", [])
            if pin_oe:
                for oe_entry in pin_oe:
                    if oe_entry.get("period", 0) != 0:
                        continue
                    prices = oe_entry.get("prices", [])
                    if len(prices) < 2:
                        continue
                    # 用 designation 匹配 Odd/Even (不再靠位置 [0]=Odd [1]=Even)
                    odd_price = even_price = 0
                    for p in prices:
                        des = p.get("designation", "").lower()
                        val = p.get("price_decimal", 0) or get_decimal_price(p) or 0
                        if des == "odd" and val > 0: odd_price = val
                        elif des == "even" and val > 0: even_price = val
                    if odd_price <= 0 or even_price <= 0:
                        continue
                    _oe_fairs = shin_fair_odds([odd_price, even_price])
                    odd_fair = _oe_fairs[0]
                    even_fair = _oe_fairs[1]
                    _add_oe_opportunities(entry, bb_oe_odd, bb_oe_even, odd_fair, even_fair, pin_odd=odd_price, pin_even=even_price)
                    break

        # --- 半全场 (HT/FT)：BB/Pin 定义不一致(含不含加时), 对比层禁用 ---
        # 历史: 曾用 extract_bb_htft + _add_htft_opportunities 算 HTFT 机会,
        # 但 BB 半全场(9结果) vs Pinnacle(3结果) 市场错配 → EV 虚高(实测 3228%),
        # 推送端虽 ev>30 封杀, 但 153 个假阳性仍白占去重/过滤算力, 故提前禁用。

        # --- 上半场平局退款 (HT DNB)：从 Pinnacle HT 1X2 推导公平价 ---
        if len(bb_dnb) >= 4 and n_ml == 3:
            # 安全校验：HT DNB赔率必须小于HT独赢赔率
            bb_ht_ml = bb.get("odds_ht", {}).get("ml", [])
            if len(bb_ht_ml) >= 2:
                ht_h = float(bb_dnb[2]) if isinstance(bb_dnb[2], str) else bb_dnb[2]
                ht_a = float(bb_dnb[3]) if isinstance(bb_dnb[3], str) else bb_dnb[3]
                if ht_h >= bb_ht_ml[0] * 0.99 or ht_a >= bb_ht_ml[-1] * 0.99:
                    bb_dnb = bb_dnb[:2]  # 保留FT DNB，清除HT DNB
            if len(bb_dnb) >= 4:  # HT DNB 有效时才继续
                pin_ht_ml = get_pin_ml_sorted_from_source(pin.get("ht_moneyline", []), sport)
                if len(pin_ht_ml) == 3:
                    hh, dd, aa = pin_ht_ml
                    if all(x and x > 0 for x in [hh, dd, aa]):
                        imp = 1/hh + 1/dd + 1/aa
                        p_h, p_d, p_a = (1/hh)/imp, (1/dd)/imp, (1/aa)/imp
                        dnb_fair = [round(1/(p_h/(1-p_d)), 4), round(1/(p_a/(1-p_d)), 4)]
                        dnb_labels = ["上半场平局退款-主", "上半场平局退款-客"]
                        for i in range(2):
                            bb_dnb_val = float(bb_dnb[2+i]) if isinstance(bb_dnb[2+i], str) else bb_dnb[2+i]
                            if bb_dnb_val and dnb_fair[i] > 0:
                                ev = (bb_dnb_val - dnb_fair[i]) / dnb_fair[i] * 100
                                if 1 < ev <= 20:
                                    entry["draw_no_bet"].append({
                                        "designation": dnb_labels[i],
                                        "bb_odds": bb_dnb_val,
                                        "fair_price": round(dnb_fair[i], 4),
                                        "ev_pct": round(ev, 2),
                                        "_market": "ht_dnb",
                                    })

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

    # 角球对比（在总计数之后合并，不污染已有统计）
    corner_start = time.time()
    corner_entries = _fetch_corner_opportunities(bb_matches, all_pin_leagues, matched_leagues)
    total_corner = 0
    if corner_entries:
        for ce in corner_entries:
            total_corner += len(ce.get("opportunities", [])) + len(ce.get("handicap", [])) + len(ce.get("over_under", []))
        opportunities.extend(corner_entries)
        total_all += total_corner
    if total_corner:
        print(f"  ⏱ 角球用时: {time.time()-corner_start:.0f}s")

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

    # V5: 检测运动级数据丢失 (有BB数据但对比结果为0)
    _bb_sports_with_data = set()
    for m in bb_matches:
        _s = m.get("sport", "")
        if _s:
            _bb_sports_with_data.add(_s)
    _cmp_sports_with_matches = set(sport_counts.keys())
    _lost_sports = _bb_sports_with_data - _cmp_sports_with_matches - {"pingpong", "badminton"}
    if _lost_sports:
        print(f"⚠️ 运动数据丢失: {', '.join(sorted(_lost_sports))} — BB有数据但对比=0 (Pinnacle获取失败?)")

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
                       if not any(b in (m.get("league") or "") for b in _banned)]
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

    if not _check_pinnacle():
        msg = "Pinnacle API 不可用 — 取消扫描（不使用缓存，确保赔率真实）"
        print(f"\n⚠️ {msg}。解决办法：")
        print("  1. 检查网络连接")
        print("  2. 切换代理节点后重试")
        raise RuntimeError(msg)

    force_refresh = "--refresh-leagues" in sys.argv
    if force_refresh:
        print("  🔄 收到 --refresh-leagues 标志，强制刷新联赛结构...")
    all_pin_leagues = refresh_league_structure(force_refresh=force_refresh)
    if not all_pin_leagues:
        print("  ⚠️  Pinnacle 联赛结构为空，跳过扫描")
        return
    print(f"  📂 Pinnacle 联赛结构: {len(all_pin_leagues)} 个联赛")

    compare_bb_vs_pinnacle(bb_matches, all_pin_leagues, save_path=output_path)


if __name__ == "__main__":
    main()
