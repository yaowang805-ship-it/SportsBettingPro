"""BB体育 赔率提取（只读，不下注，不关浏览器）。"""
import asyncio, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from config.logging_config import get_logger
from config.settings import DATA_DIR
logger = get_logger(__name__)


async def extract_odds(page) -> list:
    """从页面提取所有 bet-item 赔率数据。"""
    return await page.evaluate("""() => {
        const items = document.querySelectorAll('.bet-item');
        return Array.from(items).map(el => {
            const league = el.querySelector('.league');
            const event = el.querySelector('.event-display');
            const texts = el.querySelectorAll('div');
            const parts = Array.from(texts).map(d => d.innerText.trim()).filter(Boolean);
            return {
                league: league?.innerText?.trim() || '',
                event: event?.innerText?.trim() || '',
                parts: parts,
                full_text: el.innerText.trim(),
                html: el.innerHTML.substring(0, 300),
            };
        });
    }""")


async def extract_matches(page) -> list:
    """提取所有 match-item 比赛卡片。"""
    return await page.evaluate("""() => {
        const items = document.querySelectorAll('.match-item');
        return Array.from(items).map(el => ({
            text: el.innerText.trim().substring(0, 300),
        }));
    }""")


async def main():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()

    # 连接到已运行的 Chrome（如果可用）
    try:
        browser = await pw.chromium.connect_over_cdp('http://127.0.0.1:9222')
        logger.info("已连接到现有 Chrome")
        ctx = browser.contexts[0]
    except:
        # 启动新浏览器
        logger.info("启动新浏览器...")
        browser = await pw.chromium.launch(headless=False, args=[
            "--no-sandbox", "--no-proxy-server", "--disable-blink-features=AutomationControlled",
        ])
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        await ctx.add_init_script('Object.defineProperty(navigator, "webdriver", { get: () => undefined })')

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    # 去 BB体育
    if "bbty" not in page.url and "bb60" not in page.url:
        logger.info("打开 BB体育...")
        await page.goto("https://bb60.com", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

    logger.info(f"当前: {page.url}")

    # 检测登录
    login_btn = await page.evaluate("() => document.querySelector('button.el-button--primary.login')?.innerText || null")
    if login_btn:
        logger.info("需要登录！请在浏览器中手动登录。")
        logger.info("登录完成后按 Enter 继续...")
        # 轮询等待登录
        while True:
            await page.wait_for_timeout(2000)
            btn = await page.evaluate("() => document.querySelector('button.el-button--primary.login')?.innerText || null")
            if btn is None:
                logger.info("检测到登录成功！")
                break
    else:
        logger.info("已登录")

    # 截图当前状态
    await page.screenshot(path=str(DATA_DIR / "bb_state.png"))

    # 提取当前页面的赔率
    odds = await extract_odds(page)
    matches = await extract_matches(page)

    logger.info(f"比赛卡片: {len(matches)}, 赔率项: {len(odds)}")

    # 输出示例
    for o in odds[:5]:
        logger.info(f"  赔率: {o['full_text'][:100]}")

    # 保存
    output = {
        "timestamp": __import__('datetime').datetime.now().isoformat(),
        "url": page.url,
        "matches": matches,
        "odds": odds,
    }
    (DATA_DIR / "bb_odds.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
    logger.info(f"已保存到 data/storage/bb_odds.json")

    logger.info("=" * 50)
    logger.info("浏览器保持打开，不会关闭！")
    logger.info("可以继续浏览，需要我做什么就说")
    logger.info("=" * 50)

    # 保持运行
    try:
        while True:
            await page.wait_for_timeout(60000)
            logger.info(f"[存活] {page.url[:70]}")
    except asyncio.CancelledError:
        pass

asyncio.run(main())
