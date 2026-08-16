"""BB体育 增量扫描器.

全量扫描 (08:00/20:00):       bb_api_fetcher → bb_vs_pinnacle → bb_ev_push
增量扫描 (每15-30分钟):       bb_api_fetcher → 检测赔率变动 → 只扫变动联赛 → 合并结果 → 推送

增量扫描策略：
1. BB API 一次请求 (轻量)
2. 对比上次快照，找出赔率变动的比赛
3. 只抓取变动联赛的 Pinnacle 数据
4. 合并到已有对比结果中
5. 新+EV机会 → 钉钉推送
"""

import json
import sys
import time
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "storage"

BB_EXTRACTED = DATA_DIR / "bb_odds_extracted.json"
BB_SNAPSHOT = DATA_DIR / "bb_odds_snapshot.json"
BB_SNAPSHOT_NEAR = DATA_DIR / "bb_odds_snapshot_near.json"   # 24h内独立快照
BB_SNAPSHOT_FAR = DATA_DIR / "bb_odds_snapshot_far.json"      # 24-72h独立快照
COMPARISON_FILE = DATA_DIR / "bb_vs_pinnacle_comparison.json"
COMPARISON_FILE_NEAR = DATA_DIR / "bb_vs_pinnacle_comparison_near.json"    # 24h内
COMPARISON_FILE_FAR = DATA_DIR / "bb_vs_pinnacle_comparison_far.json"      # 24-72h
PIN_LEAGUE_STRUCTURE = DATA_DIR / "pinnacle_league_structure.json"

# FB 独立对比通道
FB_EXTRACTED = DATA_DIR / "bb_odds_extracted_FB.json"
FB_COMPARISON_FILE = DATA_DIR / "bb_vs_pinnacle_comparison_FB.json"

from scrapers.bb_api_fetcher import main as fetch_bb
from scrapers.bb_vs_pinnacle import (
    compare_bb_vs_pinnacle,
    _load_league_structure,
    detect_sport,
    extract_bb_1x2,
)


def _odds_key(match):
    """从BB比赛数据中提取赔率特征，用于变动检测。"""
    of = match.get("odds_ft", {}) or {}
    ml = of.get("ml", [])
    hc = of.get("handicap", {}) or {}
    ou = of.get("ou", {}) or {}
    return {
        "ml": [round(x, 4) for x in ml] if ml else [],
        "hc_home": hc.get("home_odds"),
        "hc_away": hc.get("away_odds"),
        "hc_line": hc.get("home_line") or hc.get("away_line"),
        "ou_line": ou.get("line"),
        "ou_over": ou.get("over_odds"),
        "ou_under": ou.get("under_odds"),
    }


def _odds_changed(old, new):
    """判断赔率是否有实质变化（变动 > 1%）。"""
    for key in ("ml", "hc_home", "hc_away", "hc_line", "ou_line", "ou_over", "ou_under"):
        ov = old.get(key)
        nv = new.get(key)
        if ov is None and nv is None:
            continue
        if ov is None or nv is None:
            return True
        # list comparison (ml)
        if isinstance(ov, list) and isinstance(nv, list):
            if len(ov) != len(nv):
                return True
            for a, b in zip(ov, nv):
                if abs(a - b) > max(0.01, 0.01 * abs(a)):
                    return True
        # scalar comparison
        elif isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
            if abs(ov - nv) > max(0.01, 0.01 * abs(ov)):
                return True
        elif ov != nv:
            return True
    return False


def load_snapshot():
    """加载赔率快照，文件损坏时自动恢复为空快照。"""
    try:
        if BB_SNAPSHOT.exists():
            return json.loads(BB_SNAPSHOT.read_text())
    except (json.JSONDecodeError, OSError):
        print(f"  ⚠️ 快照文件损坏，重置为空快照")
    return {"timestamp": "", "matches": {}}


def save_snapshot(bb_matches, snap_file=None):
    """原子写入当前BB赔率为新快照。near/far 各自独立。"""
    if snap_file is None:
        snap_file = BB_SNAPSHOT
    snapshot = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "matches": {}}
    now_ts = int(time.time() * 1000)
    for m in bb_matches:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        bt = m.get("bt")
        if bt and int(bt) < now_ts:
            continue
        snapshot["matches"][mid] = {
            "league": m.get("league", ""),
            "sport": m.get("sport", ""),
            "bt": bt,
            **_odds_key(m),
        }
    tmp = snap_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False))
    tmp.replace(snap_file)
    return snapshot


def _detect_pin_changes(bb_matches, all_pin_leagues, active_leagues, time_window):
    """拉取 Pinnacle 数据并与上次快照对比，检测 Pin 侧赔率变动。

    Pinnacle API 是联赛级接口，无法单场查询。对所有活跃联赛拉取数据
    （约 50-100 个联赛，每联赛一个 API 调用，0.25s间隔 → ~25s），
    然后与上次 Pinnacle 快照对比，找出 Pin 侧变动。
    """
    from src.scrapers.pinnacle_league_map import find_pinnacle_league_ids
    from src.scrapers.pinnacle_api import api_get as pin_get

    pin_snap_path = DATA_DIR / f".pin_snapshot_{time_window}.json"
    old_pin = {}
    if pin_snap_path.exists():
        try:
            old_pin = json.loads(pin_snap_path.read_text())
        except:
            pass

    new_pin = {}
    pin_changed_leagues = set()

    league_count = 0
    for bb_league in active_leagues:
        pin_ids = find_pinnacle_league_ids(bb_league, all_pin_leagues)
        if not pin_ids:
            continue
        league_count += 1
        league_changed = False

        for pid in pin_ids[:3]:
            if league_changed:
                break
            try:
                matchups = pin_get(f'/leagues/{pid}/matchups')  # api_get 已自动加 /0.1 前缀
                if not matchups:
                    continue
                if isinstance(matchups, dict):
                    matchups = matchups.get('matchups', matchups.get('data', []))
                if not isinstance(matchups, list):
                    continue

                for mu in matchups:
                    if not isinstance(mu, dict):
                        continue
                    mu_id = mu.get('matchup_id', mu.get('id', ''))
                    if not mu_id:
                        continue

                    # 提取赔率指纹: ML + Spread + Total (三者任一变动都触发)
                    odds_fp = []
                    for mkt_type in ['moneyline', 'money_line', 'spread', 'total']:
                        for mkt in mu.get(mkt_type, []):
                            if isinstance(mkt, dict) and mkt.get('period', 0) == 0:
                                for p in mkt.get('prices', []):
                                    price = p.get('price', p.get('decimal', 0))
                                    pts = p.get('points', p.get('handicap', ''))
                                    if price:
                                        odds_fp.append(round(float(price), 4))
                                    if pts:
                                        odds_fp.append(round(float(pts), 4))

                    odds_key = tuple(odds_fp) if odds_fp else None
                    if odds_key:
                        new_pin[str(mu_id)] = odds_key
                        old_key = old_pin.get(str(mu_id))
                        if old_key != odds_key:
                            league_changed = True
                            pin_changed_leagues.add(bb_league)
            except Exception:
                pass

    # 保存新快照
    try:
        pin_snap_path.write_text(json.dumps(new_pin, ensure_ascii=False))
    except:
        pass

    # V5: 检测显著变动 (Pin赔率跌>=2% → 聪明钱涌入)
    _significant = set()
    for _lg in pin_changed_leagues:
        # 简单判断: 该联赛有变动就标为显著 (可后续细化阈值)
        _significant.add(_lg)

    print(f"  Pin侧: {league_count}个联赛, {len(new_pin)}场, 变动{len(pin_changed_leagues)}个, 显著{len(_significant)}个")
    return pin_changed_leagues, _significant


def detect_changes(new_matches, snapshot):
    """对比新老数据，返回 (changed_ids, new_ids, changed_leagues)。"""
    old = snapshot.get("matches", {})
    changed_ids = set()
    new_ids = set()
    changed_leagues = set()

    for m in new_matches:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        new_key = _odds_key(m)
        if mid in old:
            if _odds_changed(old[mid], new_key):
                changed_ids.add(mid)
                changed_leagues.add(m.get("league", ""))
        else:
            new_ids.add(mid)
            changed_leagues.add(m.get("league", ""))

    return changed_ids, new_ids, changed_leagues


def load_current_comparison():
    """加载已有的对比结果，文件损坏时返回 None。"""
    try:
        if COMPARISON_FILE.exists():
            return json.loads(COMPARISON_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        print("  ⚠️ 对比文件损坏，重新生成")
    return None


def merge_comparison(existing, new_result, changed_leagues):
    """合并增量扫描结果到已有对比中。

    1. 移除 changed_leagues 的旧条目（已过时）
    2. 加入新条目
    3. 过滤已开赛条目
    """
    if existing is None:
        return new_result

    now_ts = int(time.time() * 1000)

    # 保留未变动联赛的旧条目
    kept_details = []
    for entry in existing.get("details", []):
        if entry.get("league") in changed_leagues:
            continue  # 这个联赛的旧数据过时了
        # 检查是否已开赛
        bb_start = entry.get("start_time_bb", "")
        # 没有简单的方式过滤已开赛，但保留也不会出错
        kept_details.append(entry)

    # 加入新条目
    new_details = new_result.get("details", []) if new_result else []
    merged_details = kept_details + new_details

    # 重新统计
    return _rebuild_output(merged_details, existing, new_result)


def _rebuild_output(details, existing, new_result):
    """从合并后的 details 重建完整的 output dict。"""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    sport_counts = {}
    sport_opp_counts = {}
    total_1x2 = total_hc = total_ou = total_dc = total_dnb = 0

    for entry in details:
        s = entry.get("sport", "unknown")
        sport_counts[s] = sport_counts.get(s, 0) + 1
        n = (len(entry.get("opportunities", [])) + len(entry.get("handicap", []))
             + len(entry.get("over_under", [])) + len(entry.get("double_chance", []))
             + len(entry.get("draw_no_bet", [])))
        sport_opp_counts[s] = sport_opp_counts.get(s, 0) + n
        total_1x2 += len(entry.get("opportunities", []))
        total_hc += len(entry.get("handicap", []))
        total_ou += len(entry.get("over_under", []))
        total_dc += len(entry.get("double_chance", []))
        total_dnb += len(entry.get("draw_no_bet", []))

    return {
        "version": "2.1",
        "parameters": (new_result or existing or {}).get("parameters", {}),
        "timestamp": timestamp,
        "bb_matches_total": (new_result or existing or {}).get("bb_matches_total", 0),
        "pinnacle_leagues_found": (new_result or existing or {}).get("pinnacle_leagues_found", 0),
        "matched_matches": len(details),
        "matches_with_ev": len(details),
        "per_sport_matched": {k: v for k, v in sorted(sport_counts.items())},
        "per_sport_opportunities": {k: v for k, v in sorted(sport_opp_counts.items())},
        "opportunities_1x2": total_1x2,
        "opportunities_handicap": total_hc,
        "opportunities_over_under": total_ou,
        "opportunities_double_chance": total_dc,
        "opportunities_draw_no_bet": total_dnb,
        "opportunities_total": total_1x2 + total_hc + total_ou + total_dc + total_dnb,
        "calibration_blocked_hc": (new_result or existing or {}).get("calibration_blocked_hc", 0),
        "calibration_blocked_ou": (new_result or existing or {}).get("calibration_blocked_ou", 0),
        "details": details,
    }


def run_incremental(time_window: str = "all"):
    """增量扫描入口。time_window: near=24h内, far=24-72h, all=全部"""
    import sys
    sys.stdout.reconfigure(line_buffering=True)

    from datetime import datetime, timezone, timedelta
    _now_hour = datetime.now().hour
    if _now_hour < 7 or _now_hour >= 22:
        print(f"  ⏭️ 不在扫描时段，跳过")
        return

    labels = {"urgent": "<6h临场", "near": "6-24h近场", "far": "24-72h早盘", "all": "增量扫描"}
    label = labels.get(time_window, "增量扫描")

    # 设置当前扫描的快照/对比文件（urgent/near/far 独立，互不污染）
    if time_window == "urgent":
        _current_snap = BB_SNAPSHOT_NEAR  # urgent 和 near 共享 near 快照
    elif time_window == "near":
        _current_snap = BB_SNAPSHOT_NEAR
    else:
        _current_snap = BB_SNAPSHOT_FAR

    print("=" * 60)
    print(f"BB体育 增量扫描 [{label}]")
    print("=" * 60)

    # 1. 获取最新BB数据
    print("\n📡 获取BB数据...")
    bb_matches = _fetch_bb_data(time_window)
    if not bb_matches:
        print("  ❌ 获取BB数据失败")
        return

    # 过滤已开赛 + 时间窗口
    now_ms = int(time.time() * 1000)
    h24_ms = 24 * 3600 * 1000
    h72_ms = 72 * 3600 * 1000

    bb_matches = [m for m in bb_matches if not m.get("bt") or int(m["bt"]) > now_ms]

    h6_ms = 6 * 3600 * 1000

    if time_window == "all" or time_window not in ("urgent", "near", "far"):
        # all: 72h内所有比赛
        bb_matches = [m for m in bb_matches if int(m.get("bt", 0)) - now_ms <= h72_ms]
    elif time_window == "urgent":
        bb_matches = [m for m in bb_matches if int(m.get("bt", 0)) - now_ms <= h6_ms]
    elif time_window == "near":
        bb_matches = [m for m in bb_matches if h6_ms < int(m.get("bt", 0)) - now_ms <= h24_ms]
    elif time_window == "far":
        bb_matches = [m for m in bb_matches if h24_ms < int(m.get("bt", 0)) - now_ms <= h72_ms]

    print(f"  [{label}]: {len(bb_matches)} 场")

    # 2. FB 独立数据刷新 + 对比 — 只在 near/far 扫描做, urgent 临场扫描跳过以提速到 <1min
    #    (FB 机会不抹杀: near 每5min 仍会跑 FB 独立对比, 只是临场 urgent 不再等它)
    all_pin_leagues = _load_league_structure()
    fb_had_new = False
    if time_window != "urgent":
        print(f"\n📡 检查FB数据新鲜度...")
        _refresh_fb_data()
        if all_pin_leagues:
            fb_had_new = _run_fb_comparison(all_pin_leagues)

    # 3. 加载快照（near/far 各自独立）
    snapshot = {"timestamp": "", "matches": {}}
    if _current_snap.exists():
        try: snapshot = json.loads(_current_snap.read_text())
        except: pass

    # 4. 双向变动检测: BB快照 + Pin快照, 任一方变动都触发对比
    #    先拉取双方数据, 再对比快照, 只对变动联赛跑昂贵的对比逻辑
    if not all_pin_leagues:
        all_pin_leagues = _load_league_structure()
        if not all_pin_leagues:
            print("  ❌ 无 Pinnacle 联赛结构数据")
            save_snapshot(bb_matches, _current_snap)
            return

    # V5: Pinnacle变动驱动 — Pin是信号源, Pin变了才拉BB
    # 4a. Pin侧: 拉取活跃联赛数据→与上次Pin快照对比 (~25s)
    active_leagues = {m.get("league", "") for m in bb_matches if m.get("league")}
    pin_changed_leagues, pin_significant = _detect_pin_changes(bb_matches, all_pin_leagues, active_leagues, time_window)

    # 4b. BB侧: 本地快照对比 (毫秒) — 辅助确认
    changed_ids, new_ids, bb_changed_leagues = detect_changes(bb_matches, snapshot)

    # 4c. Pin变优先: Pin变了 → 必须对比; BB新比赛 → 补充对比
    all_changed = pin_changed_leagues | bb_changed_leagues
    total_changed = len(pin_changed_leagues) + len(bb_changed_leagues)
    if pin_changed_leagues:
        print(f"  🔔 Pin变动: {len(pin_changed_leagues)}个联赛 → 立即对比")
    if pin_significant:
        # V5: 聪明钱信号防抖 — 5分钟内最多触发一次
        _smart_money_file = DATA_DIR / ".last_smart_money_push"
        _last_smart = float(_smart_money_file.read_text().strip()) if _smart_money_file.exists() else 0
        if time.time() - _last_smart > 300:
            print(f"  🚨 聪明钱信号: {len(pin_significant)}个联赛Pin大幅变动 → 立即推送!")
            _smart_money_file.write_text(str(time.time()))
            _push_throttle_file = DATA_DIR / ".last_push_time"
            _push_throttle_file.write_text("0")  # 清除节流
        else:
            print(f"  🔔 Pin变动: {len(pin_significant)}个联赛 (上次聪明钱推送{time.time()-_last_smart:.0f}s前, 冷却中)")
    if not pin_changed_leagues and bb_changed_leagues:
        print(f"  📝 BB变动: {len(bb_changed_leagues)}个联赛 (Pin未动)")

    if total_changed == 0:
        # 即无变动，也要检查是否需要强制刷新：
        # 1. Pin 数据 >2h 未刷新 → 强制对比
        # 2. BB 数据比 Pin 新 >5min → BB 已更新但 Pin 未跟上 → 强制对比
        window_file = COMPARISON_FILE_NEAR if time_window in ("near", "urgent") else COMPARISON_FILE_FAR
        force_refresh = False
        try:
            if not window_file.exists():
                force_refresh = True
            else:
                pin_age_min = (time.time() - window_file.stat().st_mtime) / 60
                # 检查 BB 数据是否比 Pin 新
                bb_file = DATA_DIR / "bb_odds_extracted.json"
                bb_fresher = False
                if bb_file.exists():
                    bb_mtime = bb_file.stat().st_mtime
                    pin_mtime = window_file.stat().st_mtime
                    bb_fresher = (bb_mtime - pin_mtime) > 300  # BB 比 Pin 新 >5min
                if pin_age_min > 120:
                    print(f"\n⏰ Pin数据 {pin_age_min:.0f}min 未刷新，强制全量对比")
                    force_refresh = True
                elif bb_fresher:
                    print(f"\n⚠️ BB数据比Pin新 {((bb_mtime-pin_mtime)/60):.0f}min，强制全量对比（BB已更新Pin未跟上）")
                    force_refresh = True
                else:
                    print(f"\n✅ BB+Pin均无变动，跳过对比 (Pin数据 {pin_age_min:.0f}min前)")
        except OSError:
            pass
        if not force_refresh:
            save_snapshot(bb_matches, _current_snap)
            return
        # 强制刷新: 全量拉取对比
        total_changed = 1  # force comparison below

    print(f"\n📊 双向变动: BB {len(bb_changed_leagues)}个联赛, Pin {len(pin_changed_leagues)}个联赛 → 合并 {len(all_changed)}个")
    print(f"\n🔄 实时全量对比 (拉取最新BB+Pin, ~2min)...")
    window_file = COMPARISON_FILE_NEAR if time_window in ("near", "urgent") else COMPARISON_FILE_FAR
    new_result = compare_bb_vs_pinnacle(
        bb_matches,
        all_pin_leagues,
        selected_leagues=None,  # V4.5: 全量拉取最新BB+Pin, 不用增量
        save_path=window_file,
    )

    if new_result is None:
        print("  ⚠️ 增量对比无结果 — 仍然指纹本次扫描的比赛(防止无限重复推送)")
        save_snapshot(bb_matches, _current_snap)
        # 即使对比无结果, 也要指纹标记已处理过 (防止每次扫描都推同样的比赛)
        _save_scan_fingerprints({"details": []})
        return

    print(f"\n✅ 已保存实时结果 → {window_file.name} ({len(new_result.get('details', []))} 条+EV)")

    # 8. 保存新快照 (near/far 各自独立)
    save_snapshot(bb_matches, _current_snap)

    # 9. 推送新机会 (V5: 扫到就推, 扫描频次本身就是节流)
    push_ok = True
    _push_throttle_file = DATA_DIR / ".last_push_time"

    if new_result.get("details") or fb_had_new:
        # 聪明钱信号已清除节流; 直接推
        print(f"\n📣 新+EV机会 → 运行推送 [{label}]...")
        push_ok = _run_push(label)
        _push_throttle_file.write_text(str(time.time()))
        _save_scan_fingerprints(new_result)
    else:
        print("\n📭 无新+EV机会")
        _save_scan_fingerprints(new_result)

    return new_result


def run_full():
    """全量扫描入口 (保留现有流程)。"""
    print("=" * 60)
    print("BB体育 全量扫描")
    print("=" * 60)

    bb_matches = _fetch_bb_data("all")
    if not bb_matches:
        return

    _now_ts = int(time.time() * 1000)
    bb_matches = [m for m in bb_matches if not m.get("bt") or int(m["bt"]) > _now_ts]

    refresh_needed = False
    if BB_EXTRACTED.exists():
        age_m = (time.time() - BB_EXTRACTED.stat().st_mtime) / 60
        if age_m > 30:
            refresh_needed = True
    else:
        refresh_needed = True

    if refresh_needed:
        print("  ⚠️ BB数据已过时，重新抓取...")
        _run_fetcher()

    # 更新快照
    bb_after = _fetch_bb_data("all")
    if bb_after:
        bb_after = [m for m in bb_after if not m.get("bt") or int(m["bt"]) > _now_ts]
        new_result = compare_bb_vs_pinnacle(bb_after, _load_league_structure())
        if new_result:
            save_snapshot(bb_after)
            _run_push("全量扫描")
    else:
        # 回退到直接调 bb_vs_pinnacle 的 main()
        print("  ⚠️ 直接调用 bb_vs_pinnacle.main()")
        from scrapers.bb_vs_pinnacle import main as vs_main
        vs_main()


def _fetch_bb_data(time_window: str = "all"):
    """从BB提取文件中读取数据。near扫描阈值5分钟，far扫描15分钟。"""
    if not BB_EXTRACTED.exists():
        print("  ❌ 无BB数据，先运行 bb_api_fetcher")
        return None
    raw = json.loads(BB_EXTRACTED.read_text())
    matches = raw.get("matches", [])
    # 每次增量扫描都强制重新抓取 BB 实时数据
    age_m = (time.time() - BB_EXTRACTED.stat().st_mtime) / 60
    if age_m > 0:  # 总是实时抓取
        print(f"  ⚠️ BB数据 {age_m:.0f} 分钟前，重新抓取...")
        if not _run_fetcher():
            print("  ❌ 重新抓取失败，继续使用旧数据")
        else:
            raw = json.loads(BB_EXTRACTED.read_text())
            matches = raw.get("matches", [])
    return matches


def _run_fetcher():
    """运行BB API抓取。"""
    import subprocess
    import sys
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.scrapers.bb_api_fetcher", "--all-sports"],
            capture_output=True, text=True, cwd=SRC_DIR.parent,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"  ❌ bb_api_fetcher 失败: {result.stderr[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("  ❌ bb_api_fetcher 超时 (120s)")
        return False


def _refresh_fb_data():
    """检查 FB 提取数据是否过时，过时则重新抓取。"""
    if not FB_EXTRACTED.exists():
        print("  📥 FB 数据不存在，开始抓取...")
        return _fetch_fb_only()

    # FB 数据也每次增量扫描都实时抓取, 与 BB 保持一致
    age_m = (time.time() - FB_EXTRACTED.stat().st_mtime) / 60 if FB_EXTRACTED.exists() else 999
    if age_m > 0:  # 总是重新抓取
        print(f"  📥 FB 数据 {age_m:.0f} 分钟前，重新抓取...")
        return _fetch_fb_only()
    return True


def _fetch_fb_only():
    """仅抓取 FB 平台数据。"""
    import subprocess
    import sys
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.scrapers.bb_api_fetcher", "--platform=FB"],
            capture_output=True, text=True, cwd=SRC_DIR.parent,
            timeout=180,
        )
        for line in (result.stdout or "").splitlines()[-10:]:
            print(f"    {line}")
        if result.returncode != 0:
            print(f"  ❌ FB 抓取失败: {result.stderr[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("  ❌ FB 抓取超时 (180s)")
        return False


def _run_fb_comparison(all_pin_leagues):
    """对 FB 数据进行独立对比，更新 FB 对比文件。

    Returns:
        bool: True 如果发现新的 +EV 机会
    """
    if not FB_EXTRACTED.exists():
        print("  ⏭️ 无 FB 数据，跳过 FB 对比")
        return False

    age_m = (time.time() - FB_EXTRACTED.stat().st_mtime) / 60
    if age_m > 150:
        print(f"  ⏭️ FB 数据仍过时 ({age_m:.0f} 分钟)，跳过 FB 对比")
        return False

    print(f"\n🔍 FB 独立对比...")
    raw = json.loads(FB_EXTRACTED.read_text())
    fb_matches = raw.get("matches", [])
    _now_ts = int(time.time() * 1000)
    fb_matches = [m for m in fb_matches if not m.get("bt") or int(m["bt"]) > _now_ts]

    if not fb_matches:
        print("  ⏭️ 无未开赛 FB 比赛")
        return False

    print(f"  FB 比赛数: {len(fb_matches)}")

    from scrapers.bb_vs_pinnacle import compare_bb_vs_pinnacle
    fb_result = compare_bb_vs_pinnacle(
        fb_matches,
        all_pin_leagues,
        save_path=FB_COMPARISON_FILE,
    )

    if fb_result is None:
        print("  ⚠️ FB 对比无结果")
        return False

    n_fb = len(fb_result.get("details", []))
    print(f"  ✅ FB 对比完成: {n_fb} 条")
    return n_fb > 0


def _save_scan_fingerprints(scan_result: dict):
    """保存本次扫描所有对比结果的指纹 (防增量重复推送)。

    在推送前保存，确保即使推送被V4过滤掉，指纹仍然存在，
    下次扫描不会重复处理同一批比赛。
    """
    try:
        from config.database import load_fingerprints, save_fingerprints
        from src.report.bb_ev_push import _make_fingerprint
        import time as _time

        existing = load_fingerprints()
        new_count = 0
        now_ts = _time.time()

        for detail in scan_result.get("details", []):
            # 🔒 只指纹72h内的比赛 (>72h的提前锁死→进入窗口后推不了)
            pin_epoch = detail.get("start_time_pin_epoch")
            if pin_epoch and pin_epoch > now_ts + 72 * 3600:
                continue  # >72h, 不存指纹, 等进入窗口再说

            sport = detail.get("sport", "")
            league = detail.get("league", "")
            home = detail.get("home_bb", "").strip()
            away = detail.get("away_bb", "").strip()

            for mk in ["opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"]:
                for opp in detail.get(mk, []):
                    ev = opp.get("ev_pct", 0)
                    if ev < 1:  # Only fingerprint opportunities with some EV
                        continue

                    # 归一化 _sub_market: 与 push 侧 _make_fingerprint 一致
                    raw_mk = opp.get("_market", "")
                    if raw_mk in ("", "opportunities", "1x2"):
                        norm_mk = "1x2"
                    elif raw_mk in ("hc", "handicap"):
                        norm_mk = "hc"
                    elif raw_mk in ("ou", "over_under"):
                        norm_mk = "ou"
                    else:
                        norm_mk = raw_mk  # ht, btts, dc, dnb 等
                    o = {
                        "sport": sport, "league": league,
                        "home_cn": home, "away_cn": away,
                        "designation": opp.get("designation", ""),
                        "_sub_market": norm_mk,
                        "bb_odds": opp.get("bb_odds", 0),
                        "ev_pct": ev,
                        "_pin_epoch": detail.get("start_time_pin_epoch"),
                    }
                    fp = _make_fingerprint(o)
                    if fp not in existing:
                        existing[fp] = {"ev": ev, "ts": _time.time()}
                        new_count += 1

        if new_count > 0:
            save_fingerprints(existing)
            print(f"  🔒 扫描指纹: 新增 {new_count} 条")
    except Exception as e:
        print(f"  ⚠️ 指纹保存失败: {e}")


def _run_push(label: str = ""):
    """运行推送。label通过环境变量传递(进程隔离,无并发问题)。"""
    import subprocess, os, shutil
    # V4.5: 推送前清理 __pycache__，防止子进程加载旧 .pyc 导致去重失效
    for pyc in SRC_DIR.rglob("__pycache__"):
        try: shutil.rmtree(pyc)
        except: pass
    env = os.environ.copy()
    if label:
        env["PUSH_LABEL"] = label
    try:
        push_args = [sys.executable, "-m", "src.report.bb_ev_push"]
        if label != "全量扫描":  # 增量扫描才传 --incremental
            push_args.append("--incremental")
        result = subprocess.run(
            push_args,
            capture_output=True, text=True, cwd=SRC_DIR.parent,
            timeout=300, env=env,
        )
        if result.returncode != 0:
            print(f"  ❌ bb_ev_push 失败 (exit={result.returncode}):")
            stderr_text = (result.stderr or "")
            # 打印完整错误 (之前截断到10行, 丢失关键traceback)
            for line in stderr_text.splitlines():
                print(f"    {line}")
            # 也打印 stdout 最后几行 (可能有上下文)
            stdout_lines = (result.stdout or "").splitlines()
            if stdout_lines:
                print(f"  --- stdout (last 5) ---")
                for line in stdout_lines[-5:]:
                    print(f"    {line}")
            return False
        for line in (result.stdout or "").splitlines():
            print(f"  {line}")
        for line in (result.stderr or "").splitlines():
            print(f"  [stderr] {line}")
        return True
    except subprocess.TimeoutExpired:
        print("  ❌ bb_ev_push 超时 (120s)")
        return False


def main():
    if "--full" in sys.argv:
        run_full()
    else:
        run_incremental()


if __name__ == "__main__":
    main()
