"""连接已运行的 Chrome，探索 BB体育 赔率。"""
import asyncio, json, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR
logger = get_logger(__name__)


async def main():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()

    # 连接到独立运行的 Chrome
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()

    logger.info("已连接到 Chrome")

    # 打开 BB体育
    logger.info("正在打开 BB体育...")
    try:
        await page.goto("https://bb60.com", timeout=120000, wait_until="domcontentloaded")
    except Exception as e:
        logger.warning("goto 超时: %s，继续...", e)

    await page.wait_for_timeout(5000)

    # 截图
    ts = int(time.time())
    screenshot_path = DATA_DIR / f"bb_{ts}.png"
    await page.screenshot(path=str(screenshot_path), full_page=True)
    logger.info(f"截图: {screenshot_path}")

    # 页面信息
    info = await page.evaluate("""() => ({
        url: window.location.href,
        title: document.title,
        body_text: (document.body?.innerText || '').substring(0, 3000),
        iframes: Array.from(document.querySelectorAll('iframe')).map(f => ({
            src: f.src.substring(0, 120),
            id: f.id, name: f.name, visible: f.offsetParent !== null
        })),
        buttons: Array.from(document.querySelectorAll('button, .tab, .nav-item')).map(el => el.innerText.trim()).filter(t => t && t.length < 30),
    })""")

    logger.info(f"URL: {info['url']}")
    logger.info(f"标题: {info['title']}")
    logger.info(f"页面文本: {info['body_text'][:500]}")
    logger.info(f"iframe: {json.dumps(info['iframes'], ensure_ascii=False)}")
    logger.info(f"按钮: {info['buttons'][:20]}")

    # 检测登录状态
    login_btn = await page.evaluate("""() => {
        const b = document.querySelector('button.el-button--primary.login');
        return b ? b.innerText.trim() : null;
    }""")
    logger.info(f"登录按钮: {login_btn}")

    # 保存 body HTML
    html_path = DATA_DIR / f"bb_{ts}_body.html"
    try:
        html = await page.evaluate("() => document.body?.innerHTML || ''")
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html[:200000])
        logger.info(f"HTML 已保存 ({len(html)//1024} KB)")
    except Exception as e:
        logger.warning(f"HTML 保存失败: {e}")

    logger.info("=" * 50)
    logger.info("浏览器保持打开，不会关闭")
    logger.info("请在浏览器中操作，需要我截图或分析就说")
    logger.info("=" * 50)

    # 保持连接，定期截图
    try:
        while True:
            await page.wait_for_timeout(60000)
            # 每60秒检查一次页面状态
            try:
                url = page.url
                logger.info(f"[存活] {url[:80]}")
            except:
                logger.info("页面连接断开")
                break
    except asyncio.CancelledError:
        pass

    await pw.stop()

asyncio.run(main())
