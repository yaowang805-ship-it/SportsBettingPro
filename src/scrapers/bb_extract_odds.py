"""BB体育 赔率提取 —— 通过 AppleScript 从 pc.x14ff.com (Chrome) 提取"""
import subprocess, json, time, sys, re, random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

DATA_DIR.mkdir(parents=True, exist_ok=True)

# pc.x14ff.com SPA 运动配置（sportId 来自 URL: ?type=3&sportId=N）
# type=3 = 早盘（Early Market）
SPORT_CONFIG = [
    ("soccer",           "足球", 1),
    ("basketball",       "篮球", 3),
    ("tennis",           "网球", 5),
    ("baseball",         "棒球", 7),
    ("american_football","美式足球", 6),
]

X14FF_BASE = "https://pc.x14ff.com"
X14FF_EARLY = X14FF_BASE + "/index.html#/?type=3&sportId=1&marketType=DEFAULT&showMenu=1"


def _escape_as(s):
    """Escape for AppleScript variable string."""
    # AppleScript's `\` is an escape char for `"` and `\`.
    # We must escape both: `\` → `\\`, `"` → `\"`
    return s.replace("\\", "\\\\").replace('"', '\\"')


def run_js(js_code, timeout=15):
    """Execute JS in last tab of Chrome window 1"""
    as_script = f'''
        tell application "Google Chrome"
            set tabCount to count of tabs of window 1
            set result to execute tab tabCount of window 1 javascript "{_escape_as(js_code)}"
            return result
        end tell
    '''
    tmp = "/tmp/bb_exec.applescript"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(as_script)
    try:
        r = subprocess.run(["osascript", tmp], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return ""
        out = r.stdout.strip()
        # AppleScript 返回 "missing value" 表示 JS 返回了无法转换的值
        if out == "missing value" or out == "":
            return ""
        return out
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""


def navigate_tab(url, wait_after=1):
    """Navigate Chrome tab to a URL."""
    as_script = f'''
        tell application "Google Chrome"
            set tabCount to count of tabs of window 1
            set URL of tab tabCount of window 1 to "{_escape_as(url)}"
        end tell
    '''
    tmp = "/tmp/bb_nav.applescript"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(as_script)
    subprocess.run(["osascript", tmp], capture_output=True, text=True, timeout=15)
    if wait_after:
        time.sleep(wait_after)


def wait_for_content(check_js, timeout=15, interval=1):
    """Poll JS until it returns a truthy value or timeout.
    check_js should be an expression that returns a useful value.
    We wrap in JSON.stringify to ensure AppleScript can read the result.
    """
    wrapped = f"JSON.stringify({check_js})"
    for _ in range(int(timeout / interval)):
        try:
            val = run_js(wrapped, timeout=10).strip()
            if val and val != "0" and val != "[]" and val != "{}" and val != '""' and val != "null" and val != "missing value":
                return val
        except RuntimeError:
            pass
        time.sleep(interval)
    return None


# 加载 pc.x14ff.com 专用提取 JS
_X14FF_EXTRACT_JS = (Path(__file__).parent / "bb_extract_x14ff.js").read_text(encoding="utf-8")
# DOM 提取器（按 class name 读取，可提取 FT + HT 盘口）
_DOM_EXTRACT_JS = (Path(__file__).parent / "bb_extract_dom.js").read_text(encoding="utf-8")


def click_text(text, timeout=10):
    """在页面中查找可见文本并点击（用于 SPA 筛选按钮）。"""
    js = f"""
    (function() {{
        var all = document.querySelectorAll('span,div,a,button');
        for (var i = 0; i < all.length; i++) {{
            var t = all[i].innerText.trim();
            if (t === '{text}' && all[i].offsetParent !== null) {{
                all[i].click();
                return 'clicked';
            }}
        }}
        return 'not found';
    }})();
    """
    return run_js(js, timeout=timeout)


def navigate_sport(sport_id):
    """用 URL 导航切换运动，比点击选项卡更可靠。"""
    url = f"{X14FF_BASE}/index.html#/?type=3&sportId={sport_id}&marketType=DEFAULT&showMenu=1"
    navigate_tab(url, wait_after=0)
    human_delay(2.0, 4.0)  # 等待 SPA 切换加载


def sport_matches_extracted(sport_key, matches):
    """快速验证提取到的比赛是否确实属于该运动。

    某些运动无数据时 SPA 不会切换，导致提取到上一运动的比赛。
    通过联赛关键词校验避免分类错误。
    """
    if not matches:
        return False
    indicators = {
        "soccer": ["联赛", "杯", "FC", "超级", "英超", "西甲", "德甲"],
        "basketball": ["NBA", "篮球", "WNBA", "NBL"],
        "tennis": ["ATP", "WTA", "公开赛", "挑战赛"],
        "baseball": ["MLB", "NPB", "棒球", "巨人", "阪神", "野球"],
        "american_football": ["NFL", "美式"],
    }
    kws = indicators.get(sport_key, [])
    if not kws:
        return True
    sample = matches[:5]
    matched = sum(1 for m in sample if any(kw in m.get("league", "") for kw in kws))
    return matched >= max(1, len(sample) * 0.4)


def scroll_jitter():
    """随机上下滚动，模拟人类浏览行为。"""
    dy = random.randint(-200, 250)
    run_js(f"(function(){{window.scrollBy({{top:{dy},behavior:'smooth'}});return dy;}})()")


def human_delay(lo=1.0, hi=3.0):
    """随机延迟，模拟人类操作间隔。"""
    time.sleep(round(random.uniform(lo, hi), 2))


def click_sidebar_item(text):
    """Click item in the sports sidebar (left panel), not per-match tabs.

    Uses the correct selector for the side menu structure:
    li.sport-menu-li.active .sub-menu-list .sum-menu-item span
    """
    js = f"""
(function() {{
    var sidebar = document.querySelector('li.sport-menu-li.active');
    if (!sidebar) return 'no sidebar found';
    var items = sidebar.querySelectorAll('.sum-menu-item');
    for (var i = 0; i < items.length; i++) {{
        var spans = items[i].querySelectorAll('span');
        for (var j = 0; j < spans.length; j++) {{
            if (spans[j].innerText.trim() === '{text}') {{
                items[i].click();
                return 'clicked: ' + spans[j].innerText.trim();
            }}
        }}
    }}
    return 'not found: ' + '{text}';
}})();
"""
    return run_js(js, timeout=10)


_SIDEBAR_EXTRACT_JS_SIMPLE = """
(function() {
    var items = document.querySelectorAll('.home-match-list__item');
    var results = [];
    items.forEach(function(item) {
        try {
            var home = '', away = '', league = '';
            var tns = item.querySelectorAll('.team-name');
            if (tns.length >= 1) home = tns[0].innerText.trim();
            if (tns.length >= 2) away = tns[1].innerText.trim();
            if (!home || !away) return;

            var group = item.closest('.group-matches');
            if (group) {
                var ln = group.querySelector('.league-name');
                if (ln) league = ln.innerText.trim();
            }

            // Collect all decimal odds from this match container
            var odds = [];
            var all = item.querySelectorAll('*');
            all.forEach(function(el) {
                if (el.children.length === 0) {
                    var t = el.innerText.trim();
                    if (/^\\d+\\.\\d{2}$/.test(t)) odds.push(t);
                }
            });

            results.push({league: league, home: home, away: away, odds_values: odds});
        } catch(e) {}
    });
    return JSON.stringify(results);
})();
"""

# Map sidebar view names to output field names
_SIDEBAR_FIELDS = {
    "双重机会": "odds_dc",
    "半场/全场": "odds_htft",
    "角球": "odds_corner",
    "单/双": "odds_oe",
}

# Odds counts per view for truncation during merge
_SIDEBAR_ODDS_COUNT = {
    "odds_dc": 9,       # FT(3) + HT(3) + 2H(3) 双重机会
    "odds_htft": 9,     # 主/主, 主/和, 主/客, 和/主, 和/和, 和/客, 客/主, 客/和, 客/客
    "odds_corner": 7,   # 主, 和, 客, 让球主, 让球客, 大, 小
    "odds_oe": 6,       # FT单, FT双, HT单, HT双, 2H单, 2H双
}


def extract_sidebar_view(view_name):
    """Click a sidebar view and extract odds from match containers via DOM.

    Returns list of dicts: {home, away, league, odds_values}
    """
    print(f"    (点击 '{view_name}'...)")
    result = click_sidebar_item(view_name)
    print(f"    → {result}")
    human_delay(3.0, 5.0)

    raw = run_js(_SIDEBAR_EXTRACT_JS_SIMPLE, timeout=15)
    if not raw:
        print(f"    ⚠️  JS 提取无返回")
        return []
    try:
        matches = json.loads(raw)
    except json.JSONDecodeError:
        print(f"    ⚠️  JS 返回非 JSON")
        return []

    print(f"    → DOM 提取到 {len(matches)} 场比赛")
    return matches


def extract_football_sidebar_markets():
    """Extract all sidebar markets for football.

    First resets Chrome by navigating to about:blank and back to football
    to ensure reliable JS execution, then extracts each sidebar view.

    Returns dict: {market_name: [match_list]} for each sidebar view.
    """
    SIDEBAR_VIEWS = ["双重机会", "半场/全场", "角球", "单/双"]
    results = {}

    # Reset Chrome to ensure reliable JS execution
    print("  重置 Chrome 标签页...")
    navigate_tab("about:blank", wait_after=0.5)
    human_delay(0.5, 1.0)
    navigate_sport(1)
    wait_for_content(
        "(function(){var m=document.body.innerText.match(/\\d+\\.\\d{2,4}/g);var c=m?m.length:0;return c>=20?c:0})()",
        timeout=20, interval=2.0
    )

    for view_name in SIDEBAR_VIEWS:
        print(f"  侧边栏 '{view_name}' 提取中...")
        matches = extract_sidebar_view(view_name)
        for m in matches:
            m["_bb_view"] = view_name
        results[view_name] = matches
        print(f"    → {len(matches)} 场比赛")

        # Reset to default view before next
        navigate_sport(1)
        human_delay(2.0, 3.0)

    return results


def merge_sidebar_markets(all_matches, sidebar_data):
    """Merge sidebar market data into the all_matches list.

    Sidebar matches are matched to main-view matches by (home, away, league).
    Extra odds are added as fields (odds_dc, odds_htft, odds_corner, odds_oe).
    """
    for market_name, market_matches in sidebar_data.items():
        field = _SIDEBAR_FIELDS.get(market_name)
        if not field:
            continue

        # Build index by (home, away, league)
        idx = {}
        for sm in market_matches:
            key = (sm.get("home", ""), sm.get("away", ""), sm.get("league", ""))
            idx[key] = sm.get("odds_values", [])

        merged = 0
        for m in all_matches:
            key = (m.get("home", ""), m.get("away", ""), m.get("league", ""))
            vals = idx.get(key)
            if vals:
                n = _SIDEBAR_ODDS_COUNT.get(field, len(vals))
                m[field] = vals[:n]
                merged += 1

        logger.info("侧边栏 '%s' 合并: %d/%d 匹配成功", market_name, merged, len(all_matches))

    return all_matches


def merge_dom_odds(matches):
    """对提取到的比赛列表，运行 DOM 提取器并合并 odds_ft/odds_ht。

    用 (home, away) 匹配文本提取结果和 DOM 提取结果。
    matches 的每个元素会被注入 odds_ft 和 odds_ht 字段。
    """
    dom_raw = run_js(_DOM_EXTRACT_JS, timeout=30)
    if not dom_raw:
        logger.warning("DOM 提取器无返回，跳过 HT 盘口合并")
        return matches
    try:
        dom_matches = json.loads(dom_raw)
    except json.JSONDecodeError:
        logger.warning("DOM 提取器返回非 JSON，跳过")
        return matches

    # 建立 DOM key 索引
    dom_idx = {}
    for dm in dom_matches:
        key = (dm.get("home", "").strip(), dm.get("away", "").strip())
        dom_idx[key] = dm

    merged = 0
    for m in matches:
        key = (m.get("home", "").strip(), m.get("away", "").strip())
        dm = dom_idx.get(key)
        if dm:
            m["odds_ft"] = dm.get("odds_ft", {})
            m["odds_ht"] = dm.get("odds_ht", {})
            merged += 1

    logger.info("DOM 盘口合并: %d/%d 场比赛匹配成功", merged, len(matches))
    return matches


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="BB体育 赔率提取")
    parser.add_argument("--all-sports", action="store_true", help="提取所有运动")
    args = parser.parse_args()

    print("=" * 50)
    print("BB体育 (SBObet/IBC via pc.x14ff.com) 赔率提取")
    print("=" * 50)

    if args.all_sports:
        # --- 多运动提取模式（pc.x14ff.com SPORTSBOOK） ---
        # 禁止的中国足球联赛（用户明确要求）
        BANNED_LEAGUES = ["中国甲级联赛", "中国超级联赛", "中国乙级联赛", "中超"]

        all_matches = []
        sport_counts = {}

        # 1. 直接导航到早盘页面（足球默认，sportId=1）
        print(f"\n导航到: {X14FF_EARLY}")
        navigate_tab(X14FF_EARLY, wait_after=0)
        human_delay(1.5, 3.0)  # 等待页面加载

        # 2. 等待 SPA 加载（首次需要更长时间，至少50个赔率才算加载完成）
        content = wait_for_content(
            "(function(){var m=document.body.innerText.match(/\\d+\\.\\d{2,4}/g);var c=m?m.length:0;return c>=50?c:0})()",
            timeout=40, interval=2.0
        )
        if content is None:
            print(f"⚠️  pc.x14ff.com 加载超时")
            sys.exit(1)

        for sport_key, sport_cn, sport_id in SPORT_CONFIG:
            print(f"\n--- {sport_cn} ({sport_key}) ---")

            # 用 URL 导航切换运动（比点击选项卡更可靠）
            if sport_id != 1:
                print(f"  导航到: sportId={sport_id}")
                scroll_jitter()
                navigate_sport(sport_id)
            else:
                # 足球已在页面上，加点拟人延迟
                scroll_jitter()
                human_delay(0.5, 1.5)

            # 等待赔率出现
            content = wait_for_content(
                "(function(){var m=document.body.innerText.match(/\\d+\\.\\d{2,4}/g);var c=m?m.length:0;return c>=10?c:0})()",
                timeout=20, interval=2.0
            )

            if content is None:
                print(f"  ⚠️  {sport_cn} 无赔率内容，跳过")
                continue

            try:
                content_val = int(content)
            except (ValueError, TypeError):
                print(f"  ⚠️  {sport_cn} 数据异常({content[:40]})，跳过")
                continue

            if content_val < 10:
                print(f"  ⚠️  {sport_cn} 赔率不足({content_val})，跳过")
                continue

            print(f"  赔率数: {content_val}，提取中...")
            scroll_jitter()
            human_delay(0.8, 2.0)

            try:
                raw = run_js(_X14FF_EXTRACT_JS, timeout=30)
                matches = json.loads(raw)
            except Exception as e:
                print(f"  ⚠️  {sport_cn} 提取失败: {e}")
                continue

            # 验证提取到的比赛确实属于该运动（防止 SPA 未正确切换）
            if not sport_matches_extracted(sport_key, matches):
                print(f"  ⚠️  {sport_cn} 内容不匹配（SPA 未正确切换），跳过")
                continue

            # 添加运动标签、去重、过滤中国足球
            seen = set()
            unique = []
            for m in matches:
                key = (m.get("home", ""), m.get("away", ""))
                if key in seen or not key[0] or not key[1]:
                    continue
                league = m.get("league", "")
                # 过滤中国足球联赛
                banned = False
                for b in BANNED_LEAGUES:
                    if b in league:
                        banned = True
                        break
                if banned:
                    continue
                seen.add(key)
                m["sport"] = sport_key
                m["sport_cn"] = sport_cn
                unique.append(m)

            # 给主视图比赛打标签
            for um in unique:
                um["_bb_view"] = "main"

            all_matches.extend(unique)
            sport_counts[sport_cn] = len(unique)
            print(f"  → {len(unique)} 场比赛")

        total = len(all_matches)
        print(f"\n{'=' * 50}")
        print(f"全部运动提取完成: {total} 场比赛")
        for name, count in sorted(sport_counts.items(), key=lambda x: -x[1]):
            print(f"  {name}: {count}")

        # 合并 DOM 提取的 FT + HT 盘口
        if total > 0:
            print("\n合并 DOM 盘口数据...")
            all_matches = merge_dom_odds(all_matches)

        if total == 0:
            print("⚠️  未提取到任何比赛！")
            sys.exit(1)

        # 足球侧边栏市场提取
        print("\n=== 提取足球侧边栏市场 ===")
        sidebar_data = extract_football_sidebar_markets()
        all_matches = merge_sidebar_markets(all_matches, sidebar_data)

        # 保存
        timestamp = time.strftime('%Y-%m-%dT%H:%M:%S')
        output = {
            "timestamp": timestamp,
            "source": "BB体育 (SBObet/IBC via pc.x14ff.com) - 全运动",
            "match_count": total,
            "sport_counts": sport_counts,
            "matches": all_matches,
        }
        out_path = DATA_DIR / "bb_odds_extracted.json"
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"\n已保存到 {out_path}")

        # 提取完成，回到足球首页，不关页面
        print("提取完成，页面保持打开...")
        navigate_tab(X14FF_EARLY, wait_after=0)

    else:
        # --- 单运动提取模式（原有逻辑） ---

        # 获取页面信息
        js_info = """
    (function() {
        return JSON.stringify({
            url: location.href,
            title: document.title,
            match_count: document.querySelectorAll('.home-match-list__item').length,
            league_count: document.querySelectorAll('.matches-group-league').length,
        });
    })()
    """
        info = json.loads(run_js(js_info))
        print(f"URL: {info['url']}")
        print(f"比赛数: {info['match_count']}")
        print(f"联赛数: {info['league_count']}")

        # 提取完整比赛+赔率数据
        js_extract = """
    (function() {
        var items = document.querySelectorAll('.home-match-list__item');
        var results = [];

        items.forEach(function(item) {
            try {
                var m = {};

                // 联赛名
                var group = item.closest('.group-matches');
                if (group) {
                    var ln = group.querySelector('.league-name');
                    if (ln) m.league = ln.innerText.trim();
                }

                // 时间和状态
                var lt = item.querySelector('.match-left-text');
                if (lt) m.period = lt.innerText.trim();
                var ltime = item.querySelector('.match-left-time');
                if (ltime) m.time = ltime.innerText.trim();

                // 队名
                var tns = item.querySelectorAll('.team-name');
                var names = [];
                tns.forEach(function(tn) { names.push(tn.innerText.trim()); });
                if (names.length >= 2) {
                    m.home = names[0];
                    m.away = names[1];
                }

                // 比分
                var scs = [];
                var scoreEls = item.querySelectorAll('.match-score span');
                scoreEls.forEach(function(s) { scs.push(s.innerText.trim()); });
                if (scs.length >= 2) m.score = scs[0] + '-' + scs[1];

                // 提取所有可见赔率数值（小数点格式）
                var oddsVals = [];
                var all = item.querySelectorAll('*');
                all.forEach(function(el) {
                    if (el.children.length === 0) {
                        var t = el.innerText.trim();
                        if (/^\\d+\\.\\d{2}$/.test(t)) {
                            oddsVals.push(t);
                        }
                    }
                });
                m.odds_values = oddsVals;

                // 完整原始文本（含盘口标识如 -0/0.5、大1.5 等）
                m.full_text = item.innerText.trim();

                results.push(m);
            } catch(e) {
                results.push({error: e.message});
            }
        });
        return JSON.stringify(results);
    })()
    """

        try:
            raw = run_js(js_extract)
            matches = json.loads(raw)
        except Exception as e:
            print(f"JS 提取错误: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

        print(f"\n提取到 {len(matches)} 场比赛")

        # 显示前3场完整信息
        for i, m in enumerate(matches[:3]):
            league = m.get('league', '?')
            home = m.get('home', '?')
            away = m.get('away', '?')
            score = m.get('score', '?')
            period = m.get('period', '')
            time_val = m.get('time', '')
            odds = m.get('odds_values', [])
            print(f"\n  [{league}]")
            print(f"  {home} vs {away}")
            print(f"  比分: {score} | {period} {time_val}")
            if odds:
                print(f"  赔率数值: {' | '.join(odds)}")
            ft = m.get('full_text', '')
            if ft:
                # 只显示前3行
                lines = ft.split('\\n')[:6]
                print(f"  原始文本:")
                for line in lines:
                    print(f"    {line.strip()}")

        # 保存
        timestamp = time.strftime('%Y-%m-%dT%H:%M:%S')
        output = {
            "timestamp": timestamp,
            "source": "BB体育 (SBObet/IBC via pc.x14ff.com)",
            "url": info['url'],
            "match_count": len(matches),
            "league_count": info['league_count'],
            "matches": matches,
        }
        out_path = DATA_DIR / "bb_odds_extracted.json"
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"\n已保存到 {out_path}")

        # 提取完成，不关页面
        print("提取完成，页面保持打开...")
        navigate_tab(X14FF_EARLY, wait_after=0)  # 回到首页

        # 统计
        sports = {}
        for m in matches:
            league = m.get('league', '未知')
            sports[league] = sports.get(league, 0) + 1
        print(f"\n联赛分布 ({len(sports)}):")
        for league, count in sorted(sports.items(), key=lambda x: -x[1])[:20]:
            print(f"  {league}: {count}场")
