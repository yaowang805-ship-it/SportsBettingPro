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
COMPARISON_FILE = DATA_DIR / "bb_vs_pinnacle_comparison.json"
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
    """加载赔率快照。"""
    if BB_SNAPSHOT.exists():
        return json.loads(BB_SNAPSHOT.read_text())
    return {"timestamp": "", "matches": {}}


def save_snapshot(bb_matches):
    """保存当前BB赔率为新快照。"""
    snapshot = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "matches": {}}
    now_ts = int(time.time() * 1000)
    for m in bb_matches:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        bt = m.get("bt")
        if bt and int(bt) < now_ts:
            continue  # 已开赛不保存
        snapshot["matches"][mid] = {
            "league": m.get("league", ""),
            "sport": m.get("sport", ""),
            "bt": bt,
            **_odds_key(m),
        }
    BB_SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False))
    return snapshot


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
    """加载已有的对比结果。"""
    if COMPARISON_FILE.exists():
        return json.loads(COMPARISON_FILE.read_text())
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


def run_incremental():
    """增量扫描入口。"""
    import sys
    sys.stdout.reconfigure(line_buffering=True)

    # 扫描时段检查：只在 08:00~22:00 运行
    from datetime import datetime
    _now_hour = datetime.now().hour
    if _now_hour < 8 or _now_hour >= 22:
        print(f"  ⏭️ 当前时间 {_now_hour}:00 不在扫描时段 (08:00~22:00)，跳过增量扫描")
        return

    print("=" * 60)
    print("BB体育 增量扫描 (变动检测 → 定向对比)")
    print("=" * 60)

    # 1. 获取最新BB数据
    print("\n📡 获取BB数据...")
    bb_matches = _fetch_bb_data()
    if not bb_matches:
        print("  ❌ 获取BB数据失败")
        return

    # 过滤已开赛
    _now_ts = int(time.time() * 1000)
    bb_matches = [m for m in bb_matches if not m.get("bt") or int(m["bt"]) > _now_ts]

    # 2. FB 独立数据刷新 + 对比（每次增量扫描都检查，独立于 BB 变动检测）
    print(f"\n📡 检查FB数据新鲜度...")
    _refresh_fb_data()
    all_pin_leagues = _load_league_structure()
    fb_had_new = False
    if all_pin_leagues:
        fb_had_new = _run_fb_comparison(all_pin_leagues)

    # 3. 加载快照
    snapshot = load_snapshot()

    # 4. 检测变动
    changed_ids, new_ids, changed_leagues = detect_changes(bb_matches, snapshot)
    n_changed = len(changed_ids) + len(new_ids)

    if n_changed == 0:
        print("\n✅ 无赔率变动，跳过扫描")
        # 仍然更新快照(可能有比赛已开赛)
        save_snapshot(bb_matches)
        # FB 可能有新机会，单独触发推送
        if fb_had_new:
            print(f"\n📣 FB 新+EV机会 → 运行推送...")
            _run_push()
        return

    print(f"\n📊 变动检测:")
    print(f"  赔率变动: {len(changed_ids)} 场")
    print(f"  新增比赛: {len(new_ids)} 场")
    print(f"  涉及联赛: {len(changed_leagues)} 个")
    for lg in sorted(changed_leagues):
        print(f"    {lg}")

    # 5. 加载 Pinnacle 联赛结构
    if not all_pin_leagues:
        all_pin_leagues = _load_league_structure()
        if not all_pin_leagues:
            print("  ❌ 无 Pinnacle 联赛结构数据，跳过增量扫描")
            save_snapshot(bb_matches)
            return

    # 5. 只对变动联赛做对比
    print(f"\n🔍 增量对比 (只扫 {len(changed_leagues)} 个变动联赛)...")
    new_result = compare_bb_vs_pinnacle(
        bb_matches,
        all_pin_leagues,
        selected_leagues=changed_leagues,
        save_path=None,  # 返回 dict，不直接保存
    )

    if new_result is None:
        print("  ⚠️ 增量对比无结果")
        # 仍然保存快照
        save_snapshot(bb_matches)
        return

    # 6. 合并到已有结果
    existing = load_current_comparison()
    merged = merge_comparison(existing, new_result, changed_leagues)

    # 7. 保存合并结果
    COMPARISON_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2, default=str))
    print(f"\n✅ 已保存合并结果 ({len(merged['details'])} 条+EV)")

    # 8. 保存新快照
    save_snapshot(bb_matches)

    # 9. 推送新机会
    if new_result.get("details") or fb_had_new:
        print(f"\n📣 新+EV机会 → 运行推送...")
        _run_push()
    else:
        print("\n📭 无新+EV机会")

    return merged


def run_full():
    """全量扫描入口 (保留现有流程)。"""
    print("=" * 60)
    print("BB体育 全量扫描")
    print("=" * 60)

    bb_matches = _fetch_bb_data()
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
    bb_after = _fetch_bb_data()
    if bb_after:
        bb_after = [m for m in bb_after if not m.get("bt") or int(m["bt"]) > _now_ts]
        new_result = compare_bb_vs_pinnacle(bb_after, _load_league_structure())
        if new_result:
            save_snapshot(bb_after)
            _run_push()
    else:
        # 回退到直接调 bb_vs_pinnacle 的 main()
        print("  ⚠️ 直接调用 bb_vs_pinnacle.main()")
        from scrapers.bb_vs_pinnacle import main as vs_main
        vs_main()


def _fetch_bb_data():
    """从BB提取文件中读取数据。"""
    if not BB_EXTRACTED.exists():
        print("  ❌ 无BB数据，先运行 bb_api_fetcher")
        return None
    raw = json.loads(BB_EXTRACTED.read_text())
    matches = raw.get("matches", [])
    # 如果数据太旧，重新抓取
    age_m = (time.time() - BB_EXTRACTED.stat().st_mtime) / 60
    if age_m > 15:
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
    result = subprocess.run(
        [sys.executable, "-m", "src.scrapers.bb_api_fetcher", "--all-sports"],
        capture_output=True, text=True, cwd=SRC_DIR.parent,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"  ❌ bb_api_fetcher 失败: {result.stderr[:200]}")
        return False
    return True


def _refresh_fb_data():
    """检查 FB 提取数据是否过时，过时则重新抓取。"""
    if not FB_EXTRACTED.exists():
        print("  📥 FB 数据不存在，开始抓取...")
        return _fetch_fb_only()

    age_m = (time.time() - FB_EXTRACTED.stat().st_mtime) / 60
    # FB 数据超过 2 小时，或比 BB 合并数据更旧，则刷新
    bb_age_m = (time.time() - BB_EXTRACTED.stat().st_mtime) / 60 if BB_EXTRACTED.exists() else 999

    if age_m > 120 or age_m > bb_age_m + 30:
        print(f"  📥 FB 数据 {age_m:.0f} 分钟前 (BB数据 {bb_age_m:.0f} 分钟前)，重新抓取...")
        return _fetch_fb_only()

    print(f"  ✅ FB 数据新鲜 ({age_m:.0f} 分钟前)")
    return True


def _fetch_fb_only():
    """仅抓取 FB 平台数据。"""
    import subprocess
    import sys
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


def _run_push():
    """运行推送。"""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "src.report.bb_ev_push", "--incremental"],
        capture_output=True, text=True, cwd=SRC_DIR.parent,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"  ❌ bb_ev_push 失败 (exit={result.returncode}):")
        for line in (result.stderr or "").splitlines()[:10]:
            print(f"    {line}")
        return False
    for line in (result.stdout or "").splitlines()[:20]:
        print(f"  {line}")
    return True


def main():
    if "--full" in sys.argv:
        run_full()
    else:
        run_incremental()


if __name__ == "__main__":
    main()
