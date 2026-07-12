"""连接正在运行的 Chrome，快照 BB体育 当前页面（不关浏览器）。"""
import asyncio, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR
logger = get_logger(__name__)


async def main():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")

    # 找 BB体育 标签页
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "bbty" in p.url or "bb60" in p.url:
                page = p
                break
        else:
            continue
        break
    else:
        page = browser.contexts[0].pages[0] if browser.contexts[0].pages else await browser.contexts[0].new_page()
        await page.goto("https://bb60.com", timeout=60000)

    logger.info(f"当前URL: {page.url}")
    logger.info(f"标题: {await page.title()}")

    # 截图
    ts = int(time.time())
    sp = DATA_DIR / f"bb_{ts}.png"
    await page.screenshot(path=str(sp))
    logger.info(f"截图: {sp}")

    # 页面结构分析
    info = await page.evaluate("""() => {
        const r = {
            url: location.href,
            login_btn: null,
            iframes: [],
            odds: [],
            buttons: [],
            tabs: [],
            body_preview: (document.body?.innerText || '').substring(0, 1000),
        };
        const lb = document.querySelector('button.el-button--primary.login');
        if (lb) r.login_btn = lb.innerText.trim();
        document.querySelectorAll('iframe').forEach(f => {
            r.iframes.push({
                src: (f.src || '').substring(0,150),
                id: f.id, name: f.name,
                w: f.offsetWidth, h: f.offsetHeight,
                visible: f.offsetParent !== null,
            });
        });
        document.querySelectorAll('*').forEach(el => {
            const t = (el.innerText||'').trim();
            if (/^\\d+\\.\\d{2}$/.test(t) && el.children.length===0)
                r.odds.push(t);
        });
        document.querySelectorAll('.tab, .left-tab .tab, .match-tab .tab').forEach(el => {
            const t = (el.innerText||'').trim();
            if (t && t.length<10) r.tabs.push(t);
        });
        document.querySelectorAll('button, .nav-item, .menu-item').forEach(el => {
            const t = (el.innerText||'').trim();
            if (t && t.length<20 && el.offsetParent!==null) r.buttons.push(t);
        });
        return r;
    }""")

    logger.info(f"登录按钮: {info['login_btn']}")
    logger.info(f"标签页: {info['tabs']}")
    logger.info(f"按钮: {info['buttons'][:30]}")
    logger.info(f"赔率数字: {info['odds'][:30]}")
    logger.info(f"iframe 数: {len(info['iframes'])}")
    for i,f in enumerate(info['iframes'][:8]):
        logger.info(f"  iframe{i}: {f['src'][:100]}")
    logger.info(f"页面文本预览: {info['body_preview'][:300]}")

    # 保存完整 HTML
    html = await page.evaluate("() => document.body?.innerHTML || ''")
    hp = DATA_DIR / f"bb_{ts}_body.html"
    with open(hp, 'w', encoding='utf-8') as f:
        f.write(html[:300000])
    logger.info(f"HTML 保存: {hp} ({len(html)//1024}KB)")

    logger.info("完成。浏览器保持打开。")
    await pw.stop()  # 只停 Playwright 客户端，不影响 Chrome

asyncio.run(main())
