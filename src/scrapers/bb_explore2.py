"""连接正在运行的 Chrome，深度探索 BB体育 体育赔率。"""
import asyncio, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)


async def main():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")

    ctx = browser.contexts[0]
    page = None
    for p in ctx.pages:
        if "bbty" in p.url or "bb60" in p.url:
            page = p
            print(f"[使用现有标签页] {p.url[:80]}")
            break
    if not page:
        page = ctx.pages[0]
        print(f"[使用第一个标签页] {page.url[:80]}")

    ts = int(time.time())

    # 截图当前状态
    await page.screenshot(path=str(DATA_DIR / f"bb_{ts}_state.png"))
    print(f"[截图已保存]")

    # ===== 1. 页面结构分析 =====
    info = await page.evaluate("""() => {
        const r = {
            url: location.href, title: document.title,
            app_class: document.querySelector('.app-container')?.className || '',
            bet_count: document.querySelectorAll('.bet-item').length,
            match_count: document.querySelectorAll('.match-item').length,
            tabs: [],
            sport_content: null,
            iframes: [],
        };
        document.querySelectorAll('.tab, .left-tab, [class*="tab"]').forEach(el => {
            if (el.offsetParent !== null) {
                const t = el.innerText.trim().replace(/\\s+/g, ' ');
                if (t && t.length < 30) r.tabs.push(t);
            }
        });
        const sc = document.querySelector('.sport-match-content, .match-list, .game-list, [class*="sport"], [class*="match"]');
        if (sc) r.sport_content = { cls: sc.className, html: sc.innerHTML.substring(0, 300) };
        document.querySelectorAll('iframe').forEach(f => {
            r.iframes.push({ src: (f.src||'').substring(0,150), id: f.id, visible: f.offsetParent!==null, w: f.offsetWidth });
        });
        return r;
    }""")

    print(f"URL={info['url']}  app_class={info['app_class']}")
    print(f"bet-items={info['bet_count']}  match-items={info['match_count']}")
    print(f"tabs={info['tabs']}")
    print(f"iframes={len(info['iframes'])}")
    if info['sport_content']:
        print(f"sport_content cls={info['sport_content']['cls']}")
        print(f"sport HTML={info['sport_content']['html'][:200]}")
    for i, f in enumerate(info['iframes'][:5]):
        print(f"  iframe{i}: {f['src'][:100]} v={f['visible']} {f['w']}px")

    # ===== 2. 点击"足球"标签 =====
    print("=" * 50)
    print("点击 '足球' 标签...")

    clicked = await page.evaluate("""() => {
        const tabs = document.querySelectorAll('.tab, [class*="tab"]');
        for (const el of tabs) {
            const t = (el.innerText || el.textContent || '').trim();
            if (t === '足球' && el.offsetParent !== null) {
                el.click();
                return 'clicked 足球: ' + el.className;
            }
        }
        return '足球 tab not found';
    }""")
    print(f"点击结果: {clicked}")
    await page.wait_for_timeout(5000)

    # 截图点击后
    await page.screenshot(path=str(DATA_DIR / f"bb_{ts}_football.png"))

    # ===== 3. 点击后分析 =====
    info2 = await page.evaluate("""() => {
        const r = {
            url: location.href,
            title: document.title,
            app_class: document.querySelector('.app-container')?.className || '',
            bet_count: document.querySelectorAll('.bet-item').length,
            match_count: document.querySelectorAll('.match-item').length,
            sport_content: null,
            body_text: (document.body?.innerText || '').substring(0, 800),
        };
        const sc = document.querySelector('.sport-match-content, .match-list, .game-list, [class*="sport"], [class*="match"]');
        if (sc) r.sport_content = { cls: sc.className, html: sc.innerHTML.substring(0, 500) };
        return r;
    }""")

    print(f"点击后: bet-items={info2['bet_count']}  match-items={info2['match_count']}")
    print(f"app_class={info2['app_class']}")
    if info2['sport_content']:
        print(f"sport HTML={info2['sport_content']['html'][:300]}")
    else:
        print("无体育内容区")
    print(f"页面文本={info2['body_text'][:400]}")

    # ===== 4. 如果足球没内容，试试其他标签 =====
    if info2['match_count'] == 0 and info2['bet_count'] == 0:
        print("=" * 50)
        print("足球无内容，尝试 '主播' (可能回到首页)...")
        await page.evaluate("""() => {
            const tabs = document.querySelectorAll('.tab, [class*="tab"]');
            for (const el of tabs) {
                const t = (el.innerText || el.textContent || '').trim();
                if (t === '主播' && el.offsetParent !== null) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(DATA_DIR / f"bb_{ts}_anchor.png"))

        # 分析首页内容
        home_info = await page.evaluate("""() => ({
            app_class: document.querySelector('.app-container')?.className || '',
            bet_count: document.querySelectorAll('.bet-item').length,
            match_count: document.querySelectorAll('.match-item').length,
            body_text: (document.body?.innerText || '').substring(0, 1000),
        })""")
        print(f"首页: app_class={home_info['app_class']}")
        print(f"bet-items={home_info['bet_count']}  match-items={home_info['match_count']}")
        print(f"文本={home_info['body_text'][:500]}")

        # 看看"主播"页面有什么
        # 如果前面有 iframes，可能体育在 iframe 里
        if info['iframes']:
            print("页面有 iframe，检查 iframe 内容...")
            for i, f in enumerate(info['iframes']):
                if f['visible'] and f['w'] > 100:
                    print(f"  iframe[{i}] src={f['src'][:100]}")
                    try:
                        frame = page.frame(name=f['id']) or page.frame(url=f['src'])
                        if frame:
                            t = await frame.evaluate("() => document.body?.innerText?.substring(0,300) || 'no content'")
                            print(f"    iframe内容: {t}")
                    except Exception as e:
                        print(f"    iframe读取失败: {e}")

    # ===== 5. 保存结果 =====
    if info2['bet_count'] > 0 or info2['match_count'] > 0:
        odds = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('.bet-item')).map(el => ({
                league: el.querySelector('.league')?.innerText?.trim() || '',
                event: el.querySelector('.event-display')?.innerText?.trim() || '',
                full_text: el.innerText.trim(),
            }));
        }""")
        output = {"timestamp": time.strftime('%Y-%m-%dT%H:%M:%S'), "url": page.url, "odds": odds}
        (DATA_DIR / "bb_odds_extracted.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"保存 {len(odds)} 条赔率")
    else:
        # 保存 HTML 做深度分析
        html = await page.evaluate("() => document.body?.innerHTML || ''")
        (DATA_DIR / f"bb_{ts}_body.html").write_text(html[:200000], encoding='utf-8')
        print(f"HTML已保存 ({len(html)//1024} KB)")

        # 查所有可见短文本
        texts = await page.evaluate("""() => {
            const s = new Set();
            document.querySelectorAll('body *:not(script):not(style)').forEach(el => {
                if (el.children.length === 0) {
                    const t = (el.textContent || '').trim();
                    if (t && t.length < 40) s.add(t);
                }
            });
            return Array.from(s).slice(0, 120);
        }""")
        print(f"可见文本节点({len(texts)}): {texts}")

    print("=" * 50)
    print("完成。浏览器保持打开。")
    await pw.stop()

asyncio.run(main())
