"""OddsPortal Cloudflare 过墙 + oddswatch-data 参数抓取 (一步到位脚本)

背景:
  OddsPortal 内部 API /proxy/ajax-* 的加密已破解 (AES-CBC + PBKDF2 + gzip, 见
  op_market_probe.decrypt_payload), 但 headless Chrome 被 Cloudflare 拦 (403)。
  本脚本用【有头浏览器】让你手动过一次 Cloudflare 人机验证, 拿到 cf_clearance
  cookie 后保存, 再拦截 oddswatch-data(庄家均值盘口, 含半场) 的请求参数,
  供后续批量 HTTP 下载复用。

用法:
  .venv312/bin/python data/pinnacle_historical/op_oddswatch_crack.py [--match-url URL]

产出:
  data/storage/op_cf_clearance.json        — cf_clearance cookie + UA (供批量下载复用)
  data/logs/op_oddswatch_capture.log       — oddswatch-data 完整 URL + 解密结构
"""
import sys, re, json, time, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv312" / "lib" / "python3.12" / "site-packages"))
sys.path.insert(0, str(ROOT / "data" / "pinnacle_historical"))

from op_market_probe import decrypt_payload

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
BASE = "https://www.oddsportal.com"
PROFILE_DIR = ROOT / "data" / "pinnacle_historical" / "op_browser_profile"
COOKIE_FILE = ROOT / "data" / "storage" / "op_cf_clearance.json"
LOG_FILE = ROOT / "data" / "logs" / "op_oddswatch_capture.log"

# 盘口标签 (H2H 页上的 tab), 点击触发 oddswatch-data 请求
# 含 1st Half(半场) 及全场各盘口
TABS = [
    "1st Half", "Over/Under", "Double Chance", "Draw No Bet",
    "BTTS", "Odd/Even", "Asian Handicap", "Handicap", "Full Time",
]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def has_cf_clearance(context) -> bool:
    return any(c.get("name") == "cf_clearance" and c.get("value")
               for c in context.cookies())


def save_cookie(context):
    """把 cf_clearance + 其它 OddsPortal cookie 存盘, 供批量 HTTP 下载复用。"""
    cookies = context.cookies()
    cf = next((c for c in cookies if c.get("name") == "cf_clearance"), None)
    if not cf:
        log("⚠️ 未找到 cf_clearance cookie")
        return None
    payload = {
        "cf_clearance": cf.get("value", ""),
        "user_agent": UA,
        "domain": cf.get("domain", ""),
        "all_cookies": {c.get("name"): c.get("value") for c in cookies},
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    log(f"✅ cf_clearance 已保存 → {COOKIE_FILE}")
    return payload


def wait_for_cloudflare(page, context, timeout_sec=300):
    """等用户手动过 Cloudflare: 轮询 cf_clearance cookie。"""
    log("🔐 检查 Cloudflare...")
    if has_cf_clearance(context):
        log("✅ 已有 cf_clearance cookie (浏览器 profile 复用)")
        return True

    log(f"⚠️ 检测到 Cloudflare 人机验证, 请在浏览器里手动点过 (最多等 {timeout_sec}s)")
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        time.sleep(3)
        # 尝试点击 Cloudflare 的 iframe 复选框 (可选, 一般手动更稳)
        try:
            for f in page.frames:
                cb = f.locator("iframe[src*='challenges.cloudflare.com']")
                if cb.count() > 0:
                    page.reload(wait_until="domcontentloaded")
        except Exception:
            pass
        if has_cf_clearance(context):
            log("✅ Cloudflare 已通过")
            return True
        if int(time.time() - t0) % 30 == 0:
            log(f"   等待中... {int(time.time()-t0)}s (请在浏览器手动过验证)")
    log("❌ 超时未通过 Cloudflare")
    return False


def find_match_url(page, sport="football"):
    """从运动首页发现一个即将开赛的 H2H 链接。"""
    hubs = {"football": "/football/", "basketball": "/basketball/", "tennis": "/tennis/"}
    page.goto(BASE + hubs.get(sport, f"/{sport}/"), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    links = page.eval_on_selector_all("a[href*='h2h']", "els => els.map(e => e.href)")
    for l in links:
        if "#" in l:
            return l
    return links[0] if links else None


def capture_oddswatch(page, match_url):
    """打开比赛 H2H 页, 点盘口标签, 拦截 oddswatch-data 并解密。"""
    captured = []

    def on_response(resp):
        url = resp.url
        if "oddswatch-data" not in url and "ajax-oddswatch" not in url:
            return
        try:
            body = resp.text()
        except Exception:
            body = ""
        captured.append((url, body))

    page.on("response", on_response)
    log(f"🔍 打开比赛: {match_url}")
    page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)  # 等 JS 触发 ajax (oddswatch 有 2s 延迟)

    # 逐个点盘口标签触发 oddswatch-data
    for tab in TABS:
        try:
            loc = page.get_by_text(tab, exact=False).first
            if loc.count() > 0:
                loc.click(timeout=3000)
                log(f"   点击标签: {tab}")
                page.wait_for_timeout(2500)
        except Exception:
            pass

    # 也试点击所有可能是盘口 tab 的元素 (含中文/英文标签)
    try:
        tabs = page.locator("[role='tab'], .market-tabs a, ul li a")
        n = tabs.count()
        for i in range(min(n, 20)):
            try:
                el = tabs.nth(i)
                txt = (el.inner_text() or "").strip()
                if txt and len(txt) < 30:
                    el.click(timeout=1500)
                    page.wait_for_timeout(1500)
            except Exception:
                pass
    except Exception:
        pass

    page.wait_for_timeout(3000)
    return captured


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match-url", default=None)
    ap.add_argument("--sport", default="football")
    ap.add_argument("--save-cookie-only", action="store_true",
                    help="只过 Cloudflare 存 cookie, 不抓 oddswatch")
    args = ap.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        # 有头浏览器 + 持久化 profile (cookie 跨次运行保留)
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, executable_path=CHROME,
            user_agent=UA, viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(60000)
        # 隐藏 webdriver 标记, 降低被 Cloudflare 识别为 bot 的概率
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        # 1. 过 Cloudflare
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        if not wait_for_cloudflare(page, context):
            log("❌ 未能通过 Cloudflare, 退出")
            context.close()
            return

        save_cookie(context)

        if args.save_cookie_only:
            log("✅ 已保存 cookie (save-cookie-only 模式), 结束")
            context.close()
            return

        # 2. 抓 oddswatch 参数
        match_url = args.match_url
        if not match_url:
            match_url = find_match_url(page, args.sport)
            if not match_url:
                log("❌ 未找到 H2H 链接, 请手动传 --match-url")
                context.close()
                return

        captured = capture_oddswatch(page, match_url)
        log(f"📦 拦截到 {len(captured)} 条 oddswatch-data 请求")

        # 3. 解密并打印
        for url, body in captured:
            pt = decrypt_payload(body)
            if pt is None:
                log(f"[未解密] {url}")
                log(f"    len={len(body)} head={body[:80]!r}")
                continue
            try:
                js = json.loads(pt.decode("utf-8"))
                pretty = json.dumps(js, ensure_ascii=False, indent=2)[:2500]
            except Exception:
                pretty = pt[:2000].decode("utf-8", "replace")
            log(f"[解密成功] {url}")
            log(pretty)

        context.close()
        log(f"✅ 完成, 日志 → {LOG_FILE}")


if __name__ == "__main__":
    main()
