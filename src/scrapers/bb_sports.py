"""BB体育 赔率抓取 — 交互式探索模式。

流程：
  1. 打开浏览器 → 你手动登录 BB体育（输账号密码+验证码）
  2. 登录完成后 → 我在终端检测到后，截图分析页面结构
  3. 找到赔率位置后，抓取所有盘口赔率
  4. 浏览器保持打开，不会自动关闭

输出: data/storage/bb_sports_odds.json
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR

logger = get_logger(__name__)

OUTPUT_FILE = DATA_DIR / "bb_sports_odds.json"


class BBSportsExplorer:
    """交互式 BB体育 赔率探索器。"""

    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None

    async def start(self):
        """启动浏览器（不关闭）。"""
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--no-proxy-server",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await self.context.add_init_script(
            'Object.defineProperty(navigator, "webdriver", { get: () => undefined })'
        )
        self.page = await self.context.new_page()
        logger.info("浏览器已打开")

    async def goto_bb(self):
        """打开 BB体育。"""
        logger.info("正在打开 BB体育...")
        try:
            await self.page.goto(
                "https://bb60.com", timeout=60000, wait_until="domcontentloaded"
            )
            await self.page.wait_for_timeout(5000)
            logger.info("页面已加载")
        except Exception as e:
            logger.warning("页面加载超时: %s，继续等待...", e)

    async def wait_for_login_and_explore(self):
        """等待登录完成，然后保持浏览器打开供交互探索。"""
        logger.info("=" * 60)
        logger.info("请在浏览器窗口中手动登录 BB体育")
        logger.info("如果 Cloudflare 验证，请手动完成")
        logger.info("登录完成后，浏览器保持打开，我来分析页面")
        logger.info("=" * 60)

        # 先等一会儿让页面加载
        await self.page.wait_for_timeout(8000)

        # 尝试检测是否可以跳过 Cloudflare
        for i in range(5):
            await self.page.wait_for_timeout(3000)
            title = await self.page.title()
            url = self.page.url
            logger.info(f"当前页面: {title[:60]} | {url[:80]}")

            # 检测是否被 Cloudflare 挡住
            try:
                body_text = await self.page.evaluate("() => document.body?.innerText?.substring(0, 200) || ''")
                if "cloudflare" in body_text.lower() or "just a moment" in body_text.lower():
                    logger.warning(f"Cloudflare 验证中 (attempt {i+1}/5)...")
                    continue
            except:
                pass

        # 检查登录状态 - 如果登录按钮还在，等待用户手动登录
        while True:
            await self.page.wait_for_timeout(1000)
            try:
                login_btn = await self.page.evaluate("""() => {
                    const b = document.querySelector('button.el-button--primary.login');
                    return b ? b.innerText.trim() : null;
                }""")
                if login_btn is None:
                    logger.info("检测到登录成功！")
                    break
            except Exception as e:
                logger.info(f"等待页面就绪... ({e})")
                await self.page.wait_for_timeout(2000)

        await self.page.wait_for_timeout(3000)

    async def explore_page(self):
        """截取当前页面快照和分析 DOM 结构。"""
        ts = int(time.time())

        # 截图
        screenshot_path = DATA_DIR / f"bb_screenshot_{ts}.png"
        await self.page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info(f"截图已保存: {screenshot_path}")

        # 获取页面关键信息
        info = await self.page.evaluate("""() => {
            const info = {
                url: window.location.href,
                title: document.title,
                iframes: [],
                buttons: [],
                links: [],
                tables: [],
                odds_elements: [],
            };
            // iframe 信息
            document.querySelectorAll('iframe').forEach(f => {
                info.iframes.push({
                    src: f.src,
                    id: f.id,
                    name: f.name,
                    width: f.offsetWidth,
                    height: f.offsetHeight,
                    visible: f.offsetParent !== null
                });
            });
            // 找数字按钮/赔率元素
            document.querySelectorAll('*').forEach(el => {
                const text = (el.innerText || '').trim();
                if (/^\\d+\\.\\d{2}$/.test(text) && el.children.length === 0) {
                    info.odds_elements.push(text);
                }
            });
            // 找可见的按钮
            document.querySelectorAll('button, a, .tab, .nav-item, .menu-item').forEach(el => {
                const text = (el.innerText || '').trim();
                if (text && text.length < 20 && el.offsetParent !== null) {
                    info.buttons.push(text);
                }
            });
            return info;
        }""")

        logger.info(f"页面URL: {info['url']}")
        logger.info(f"页面标题: {info['title']}")
        logger.info(f"iframe 数量: {len(info['iframes'])}")
        for i, f in enumerate(info['iframes'][:10]):
            logger.info(f"  iframe {i}: src={f['src'][:80]}, visible={f['visible']}, size={f['width']}x{f['height']}")
        logger.info(f"可见按钮/导航: {info['buttons'][:30]}")
        logger.info(f"赔率数字元素: {info['odds_elements'][:30]}")

        # 保存完整 DOM 到文件（html 可能很大，只存 body）
        html_path = DATA_DIR / f"bb_body_html_{ts}.html"
        try:
            body_html = await self.page.evaluate("() => document.body?.innerHTML || ''")
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(body_html[:500000])  # 前 500K
            logger.info(f"HTML 已保存: {html_path} ({(len(body_html)/1024):.0f} KB)")
        except Exception as e:
            logger.warning(f"保存 HTML 失败: {e}")

        return info

    async def keep_alive(self):
        """保持浏览器打开，直到用户手动关闭。"""
        logger.info("=" * 60)
        logger.info("浏览器保持打开中，你可以继续浏览 BB体育")
        logger.info("需要我分析什么，直接说")
        logger.info("要关闭浏览器请按 Ctrl+C")
        logger.info("=" * 60)

        # 每隔 30 秒检查一下页面状态
        try:
            while True:
                await self.page.wait_for_timeout(30000)
                try:
                    url = self.page.url
                    title = await self.page.title()
                    logger.info(f"[存活] {title[:50]} | {url[:60]}")
                except:
                    logger.info("页面似乎已关闭")
                    break
        except asyncio.CancelledError:
            pass

    async def close(self):
        """关闭浏览器（仅在用户要求时调用）。"""
        logger.info("关闭浏览器...")
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()


async def main():
    explorer = BBSportsExplorer()
    try:
        await explorer.start()
        await explorer.goto_bb()
        await explorer.wait_for_login_and_explore()

        # 登录后探索页面
        info = await explorer.explore_page()

        # 如果找到 iframe，尝试进入
        if info['iframes']:
            logger.info(f"发现 {len(info['iframes'])} 个 iframe，尝试访问...")
            for idx, f in enumerate(info['iframes']):
                if f['visible'] and f['src']:
                    logger.info(f"访问 iframe {idx}: {f['src'][:100]}")
                    try:
                        frame = await explorer.page.frame(name=f['name']) or \
                                await explorer.page.frame(url=f['src'])
                        if frame:
                            frame_html = await frame.evaluate("() => document.body?.innerHTML?.substring(0, 5000) || ''")
                            logger.info(f"  iframe 内容预览: {frame_html[:200]}")
                    except Exception as e:
                        logger.warning(f"  iframe 访问失败: {e}")

        # 保持浏览器打开
        await explorer.keep_alive()

    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.exception("异常")
    # 不主动关闭浏览器

if __name__ == "__main__":
    asyncio.run(main())
