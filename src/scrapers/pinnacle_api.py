"""Pinnacle API 传输层 — HTTP 请求、限速、错误诊断

Python 3.14 http.client chunked encoding bug → 自动 monkey-patch
cf_clearance cookie → 绕过 Cloudflare Turnstile
DNS bypass → 绕过 Shadowrocket VPN 劫持
"""
import time
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# ── DNS 解析: V4.5 走系统自然DNS (不再硬编码IP, 换节点即换IP) ──

import requests
from config.settings import DATA_DIR

logger = logging.getLogger(__name__)

API_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
COOKIE_FILE = DATA_DIR / "pinnacle_cf_clearance.txt"


# ── Python 3.14 chunked encoding monkey-patch ──────────────────────────
import http.client

_PATCHED = getattr(http.client.HTTPResponse, "_safe_read_is_patched", False)

if not _PATCHED:
    _orig_safe_read = http.client.HTTPResponse._safe_read

    def _safe_read_patched(self, amt):
        """Retry on IncompleteRead — Python 3.14 http.client chunked bug."""
        data = b""
        while amt > 0:
            try:
                chunk = _orig_safe_read(self, amt)
                return data + chunk if chunk else data
            except http.client.IncompleteRead as e:
                data += e.partial
                amt -= len(e.partial)
                if amt <= 0:
                    return data

    http.client.HTTPResponse._safe_read = _safe_read_patched
    http.client.HTTPResponse._safe_read_is_patched = True  # marker on CLASS, survives reload()

# ── Session setup ──────────────────────────────────────────────────────

SESSION = requests.Session()
SESSION.trust_env = False
SESSION.proxies = {"http": "", "https": ""}
SESSION.headers.update({
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/150.0.0.0 Safari/537.36",
    "Referer": "https://www.pinnacle.com/",
    "Origin": "https://www.pinnacle.com",
    "X-API-Key": API_KEY,
})

MAX_RETRIES = 5
RETRY_DELAY = 2.0
MAX_BACKOFF = 8.0                  # 指数退避封顶
MAX_TOTAL_WAIT = 30.0              # 累积等待超过此值放弃
JITTER = 0.25                      # ±25% 随机抖动
COOKIE_REFRESH_COOLDOWN = 900      # Cookie 刷新失败后冷却 15 分钟
MAX_COOKIE_REFRESHES = 3           # 每次扫描最多刷新 3 次 cookie
_ssl_fail_count = 0  # 全局 SSL 失败计数器（看门狗用）

_last_req_time = 0.0
_MIN_REQUEST_INTERVAL = 0.35       # V5: 0.5→0.35s (3 workers × ~8 req/s, 封禁线16)
_REQUEST_COUNT = 0                  # V5: 扫描内请求计数器
_SCAN_PAUSE_UNTIL = 0.0            # V5: Cloudflare 封禁后自动暂停
_REQUEST_BURST_WINDOW = 10         # V5: 10秒窗口
_REQUEST_BURST_LIMIT = 15          # V5: 10秒内最多15个请求
_BURST_WINDOW_START = 0.0
_BURST_COUNT = 0

_cookie_loaded = False  # 进程级标志，只记一次日志
_cookie_refresh_count = 0          # 本次扫描 cookie 刷新次数
_last_cookie_val = ""              # 上次 cookie 值（防假刷新）
_cookie_cooldown_until = 0.0       # Cookie 冷却到期时间戳


def reset_cookie_state():
    """每次扫描开始时重置 cookie 刷新计数器。"""
    global _cookie_refresh_count, _last_cookie_val, _cookie_cooldown_until
    _cookie_refresh_count = 0
    # 注意：不重置 cooldown 和 last_val（如果正在冷却，应该保持冷却）
    if time.time() > _cookie_cooldown_until:
        _cookie_cooldown_until = 0.0


def _load_cookie():
    """从文件加载 cf_clearance cookie。

    V4.5: 循环替代递归，防止 Chrome 刷新无限递归导致 RecursionError。
    """
    global _cookie_loaded
    max_attempts = 3  # 最多尝试 3 轮（初始加载 + 2 次刷新）
    for attempt in range(max_attempts):
        if COOKIE_FILE.exists():
            val = COOKIE_FILE.read_text().strip()
            if val:
                SESSION.cookies.set("cf_clearance", val, domain=".pinnacle.com")
                if not _cookie_loaded:
                    logger.info("已加载 cf_clearance cookie (%d 字符)", len(val))
                    _cookie_loaded = True
                return True
        if attempt == 0 and not _cookie_loaded:
            logger.warning("cf_clearance cookie 文件不存在: %s", COOKIE_FILE)
        # 自动从 Chrome 浏览器恢复 cookie
        if _refresh_cookie_from_chrome():
            continue  # 刷新成功，重试加载文件
        break  # 刷新失败或冷却中，停止尝试
    _cookie_loaded = True  # 标记已尝试，不再重复
    return False


def _refresh_cookie_from_chrome():
    """直接从 Chrome 浏览器读取 cf_clearance cookie。

    防假刷新: 每次扫描最多刷新 3 次，相同 cookie 值不重试，冷却 15 分钟。
    """
    global _cookie_refresh_count, _last_cookie_val, _cookie_cooldown_until

    now = time.time()
    if now < _cookie_cooldown_until:
        remaining = int(_cookie_cooldown_until - now)
        logger.warning("Cookie 刷新冷却中 (剩余 %ds)，跳过刷新", remaining)
        return False
    if _cookie_refresh_count >= MAX_COOKIE_REFRESHES:
        _cookie_cooldown_until = now + COOKIE_REFRESH_COOLDOWN
        logger.error("Cookie 已刷新 %d 次仍失败，冷却 %d 分钟",
                     _cookie_refresh_count, COOKIE_REFRESH_COOLDOWN // 60)
        return False

    _cookie_refresh_count += 1
    try:
        import browser_cookie3
        cj = browser_cookie3.chrome(domain_name=".pinnacle.com")
        for c in cj:
            if c.name == "cf_clearance":
                val = c.value
                if val == _last_cookie_val:
                    logger.warning("Cookie 值未变化 (%d chars)，Chrome 中可能也是过期 cookie", len(val))
                    _cookie_cooldown_until = now + COOKIE_REFRESH_COOLDOWN
                    return False
                _last_cookie_val = val
                COOKIE_FILE.write_text(val)
                SESSION.cookies.set("cf_clearance", val, domain=".pinnacle.com")
                logger.info("cf_clearance refreshed from Chrome (%d chars)", len(val))
                _cookie_refresh_count = 0
                return True
        logger.warning("Chrome 中未找到 cf_clearance cookie")
    except Exception as e:
        logger.warning("从 Chrome 读取 cookie 失败: %s", e)
    return False


def _check_hosts_file():
    """检查 /etc/hosts 是否包含 pinnacle 相关域名 —— 硬编码 IP 可能导致 VPN 切换后连不上。"""
    hosts_path = "/etc/hosts"
    pinnacle_domains = ["pinnacle.com", "arcadia.pinnacle.com"]
    try:
        content = Path(hosts_path).read_text()
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for domain in pinnacle_domains:
                if domain in line:
                    msg = (
                        f"/etc/hosts 包含 pinnacle 硬编码: {line} — "
                        "VPN IP 变更后会导致连接失败，建议删除"
                    )
                    logger.info(msg)  # 硬编码是有意为之, 非错误
                    return True
    except (OSError, IOError):
        pass
    return False


def _rate_limit():
    global _last_req_time, _REQUEST_COUNT, _BURST_WINDOW_START, _BURST_COUNT, _SCAN_PAUSE_UNTIL

    # V5: Cloudflare 封禁后自动暂停
    if _SCAN_PAUSE_UNTIL > 0:
        remaining = _SCAN_PAUSE_UNTIL - time.time()
        if remaining > 0:
            logger.warning("Cloudflare 封禁冷却中, 暂停 %.0fs...", remaining)
            time.sleep(min(remaining, 60))  # 每次最多等60秒
            if time.time() < _SCAN_PAUSE_UNTIL:
                return  # 还没到时间，直接返回（让调用者决定是否继续）

    # V5: 突发流量限制 — 10秒窗口内最多 15 请求
    now = time.time()
    if now - _BURST_WINDOW_START > _REQUEST_BURST_WINDOW:
        _BURST_WINDOW_START = now
        _BURST_COUNT = 0
    if _BURST_COUNT >= _REQUEST_BURST_LIMIT:
        sleep_time = _REQUEST_BURST_WINDOW - (now - _BURST_WINDOW_START)
        if sleep_time > 0:
            time.sleep(sleep_time)
            _BURST_WINDOW_START = time.time()
            _BURST_COUNT = 0

    elapsed = time.time() - _last_req_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_req_time = time.time()
    _REQUEST_COUNT += 1
    _BURST_COUNT += 1


def _backoff_sleep(attempt: int, extra: float = 0.0) -> float:
    """指数退避 + 封顶 + 抖动。

    Args:
        attempt: 0-based 重试序号 (0, 1, 2, …)
        extra: 额外秒数，加在封顶之前

    Returns:
        实际睡眠秒数（用于累计总等待时间）
    """
    import random
    raw = RETRY_DELAY * (2 ** attempt) + extra
    capped = min(raw, MAX_BACKOFF)
    jittered = capped * (1.0 + random.uniform(-JITTER, JITTER))
    wait = max(0.1, jittered)
    time.sleep(wait)
    return wait


def _refresh_cookie_via_playwright():
    """Playwright 启动 Chrome 150 → 刷新 cf_clearance cookie。

    仅在 cookie 过期/403 时自动调用。
    """
    try:
        _refresh_cookie_fast()
        return True
    except Exception as exc:
        logger.error("Playwright refresh failed: %s", exc)
        return False


def _refresh_cookie_fast():
    """用 Playwright + 真实 Chrome 150 + 当前 cookie 刷新。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright not installed, cannot refresh cookie")
        return

    chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    old_cookie = SESSION.cookies.get("cf_clearance", domain=".pinnacle.com") or ""

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=chrome_path,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        )
        if old_cookie:
            context.add_cookies([{
                "name": "cf_clearance",
                "value": old_cookie,
                "domain": ".pinnacle.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
            }])

        page = context.new_page()
        resp = page.goto(
            f"{API_BASE}/sports",
            wait_until="domcontentloaded",
            timeout=20000,
        )

        if resp and resp.status == 200:
            cookies = context.cookies()
            for c in cookies:
                if c["name"] == "cf_clearance":
                    COOKIE_FILE.write_text(c["value"])
                    SESSION.cookies.set(
                        "cf_clearance", c["value"], domain=".pinnacle.com"
                    )
                    logger.info("cf_clearance refreshed via Playwright")
                    break
        browser.close()


def _diagnose_response(resp, url: str) -> str:
    """诊断非 200 响应，返回错误类型: cloudflare_block/ip_ban/maintenance/rate_limit/unknown"""
    try:
        body = resp.text[:500]
    except Exception:
        body = ""

    # 503 + MAINTENANCE → Pinnacle 计划内维护
    if resp.status_code == 503 and "MAINTENANCE" in body:
        return "maintenance"

    # 503 空响应 → Pinnacle 后端限速
    if resp.status_code == 503 and not body.strip():
        return "rate_limit"

    # 403 + Cloudflare 拦截页面 → IP 被风控
    if resp.status_code == 403 and ("cloudflare" in body.lower() or "Attention Required" in body):
        return "ip_ban"

    # 403 + JSON → cookie 过期
    if resp.status_code == 403 and "application/json" in resp.headers.get("content-type", ""):
        return "cookie_expired"

    # 403 其他 → 大概率 cookie
    if resp.status_code == 403:
        return "cookie_expired"

    return "unknown"


def api_get(path, retry=True):
    """调用 Pinnacle API，带自诊断和自动恢复。

    V4.5: 指数退避 + 封顶 + 抖动 + 累积超时保护。
    """
    global _ssl_fail_count
    _load_cookie()
    _rate_limit()
    url = f"{API_BASE}{path}"
    total_wait = 0.0

    for attempt in range(MAX_RETRIES if retry else 1):
        try:
            resp = SESSION.get(url, timeout=30)

            if resp.status_code == 429:
                slept = _backoff_sleep(attempt)
                total_wait += slept
                logger.warning("429 rate limited, retry in %.1fs (attempt %d/%d, total %.1fs)",
                               slept, attempt + 1, MAX_RETRIES, total_wait)
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue

            if resp.status_code == 200:
                _ssl_fail_count = 0
                return resp.json()

            # ── 自诊断 ──
            err_type = _diagnose_response(resp, url)

            if err_type == "maintenance":
                logger.warning("Pinnacle 维护中 — 跳过本次扫描")
                return None  # 不重试

            if err_type == "ip_ban":
                logger.error("Cloudflare IP 封禁 — 自动暂停扫描 30 分钟")
                global _SCAN_PAUSE_UNTIL
                _SCAN_PAUSE_UNTIL = time.time() + 1800  # 30 分钟冷却
                return None  # 不重试，重试也没用

            if err_type == "cookie_expired":
                logger.warning("Cookie 过期 — 从 Chrome 恢复...")
                if _refresh_cookie_from_chrome():
                    continue  # 重试
                if attempt < MAX_RETRIES - 1:
                    continue

            if err_type == "rate_limit":
                slept = _backoff_sleep(attempt, extra=5.0)
                total_wait += slept
                logger.warning("Pinnacle 限速, %.1fs 后重试 (attempt %d/%d, total %.1fs)",
                               slept, attempt + 1, MAX_RETRIES, total_wait)
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue

            # 其他错误: 重试 (指数退避 + 抖动)
            if attempt < MAX_RETRIES - 1:
                slept = _backoff_sleep(attempt)
                total_wait += slept
                logger.warning("HTTP %d, retrying (attempt %d/%d, total %.1fs)",
                               resp.status_code, attempt + 1, MAX_RETRIES, total_wait)
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue

            _diagnose_pinnacle_error(url, resp.status_code)
            return None

        except requests.exceptions.SSLError as e:
            _ssl_fail_count += 1
            logger.warning("SSL error (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, e)
            if attempt < MAX_RETRIES - 1:
                extra = 10.0 if _ssl_fail_count > 20 else 0.0
                if extra:
                    logger.warning("SSL大面积故障(已%d次), 额外冷却%.0fs", _ssl_fail_count, extra)
                slept = _backoff_sleep(attempt, extra=extra)
                total_wait += slept
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue
            logger.error("SSL handshake failed after %d retries", MAX_RETRIES)
            return None

        except requests.exceptions.ConnectionError as e:
            logger.error("Connection failed: %s", e)
            return None

        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                slept = _backoff_sleep(attempt)
                total_wait += slept
                logger.warning("timeout, retrying in %.1fs (attempt %d/%d, total %.1fs)...",
                               slept, attempt + 1, MAX_RETRIES, total_wait)
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue
            logger.error("Pinnacle API timeout after %d retries", MAX_RETRIES)
            return None

        except requests.exceptions.ChunkedEncodingError as e:
            logger.warning("ChunkedEncodingError (Python 3.14 bug), retrying... %s", e)
            if attempt < MAX_RETRIES - 1:
                slept = _backoff_sleep(attempt)
                total_wait += slept
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue
            return None

        except Exception as e:
            logger.error("Pinnacle API error (%s): %s", type(e).__name__, e)
            return None

    return None


def _diagnose_pinnacle_error(url, status_code):
    """Pinnacle API 非 200 响应诊断。"""
    logger.error("Pinnacle API returned %d for %s", status_code, url)
    if status_code == 403:
        logger.error("403 Forbidden — cf_clearance cookie 无效或 Cloudflare WAF 拦截")
        logger.error("运行诊断: python3 -m src.scrapers.pinnacle_api --check")
    elif status_code == 401:
        logger.error("401 Unauthorized — X-API-Key 缺失或无效")
    elif status_code == 429:
        logger.error("429 Rate Limited — 等待 1 分钟后重试")
    elif status_code == 503:
        logger.error("503 Service Unavailable — Pinnacle 服务不可用")


def us_to_decimal(us_price):
    """美式赔率 → 十进制。"""
    if us_price is None:
        return None
    if us_price > 0:
        return round(1 + us_price / 100, 4)
    else:
        return round(1 - 100 / us_price, 4)


def get_decimal_price(price_obj: dict) -> float:
    """从 Pinnacle 价格对象中提取十进制赔率。

    兼容两种 API 格式:
    - 新格式: {"price": 147} (美式赔率)
    - 旧格式: {"price_decimal": 2.47} (十进制)
    """
    # 新格式: price (US odds)
    us_price = price_obj.get("price")
    if us_price is not None and isinstance(us_price, (int, float)):
        return us_to_decimal(us_price)
    # 旧格式: price_decimal
    dec = price_obj.get("price_decimal")
    if dec is not None and isinstance(dec, (int, float)) and dec > 1:
        return float(dec)
    return None


# ── Preflight connectivity check ───────────────────────────────────────

def check_pinnacle_connectivity(verbose=True):
    """Pinnacle API 全面连通性检查。

    按顺序测试: DNS → HTTP基础连接 → cookie → 关键端点
    返回 (ok: bool, diagnostics: list)
    """
    import socket as _socket

    diag = []
    ok = True

    def _log(msg, is_error=False):
        diag.append(msg)
        if verbose:
            # CLI 模式下 logging 可能无 handler，用 print 兜底
            try:
                (logger.error if is_error else logger.info)(msg)
            except Exception:
                pass
            print(msg)

    # 0. Hosts 文件检查（仅提示，不算失败——硬编码是有意为之）
    _log("--- Pinnacle API 连通性检查 ---")
    if _check_hosts_file():
        _log("ℹ️  /etc/hosts 含 pinnacle 硬编码（已主动配置，非错误）", is_error=False)

    # 1. DNS 解析
    dns_ok = False
    for host in ["guest.api.arcadia.pinnacle.com", "www.pinnacle.com"]:
        try:
            ip = _socket.getaddrinfo(host, 443)[0][4][0]
            _log(f"✅ DNS: {host} → {ip}")
            dns_ok = True
        except _socket.gaierror as e:
            _log(f"❌ DNS 解析失败 {host}: {e}", is_error=True)
            ok = False
    if not dns_ok:
        _log("❌ DNS 完全不可用 — 检查网络/VPN", is_error=True)
        if verbose:
            print("\n❌ DNS 故障: Pinnacle 域名无法解析")
            print("   可能原因: VPN DNS 劫持 / 网络断开 / 路由器故障")
        return False, diag

    # 2. 基础 HTTP 连通性（CF 层面）
    try:
        r = SESSION.get(f"{API_BASE}/sports", timeout=15)
        if r.status_code == 200:
            _log(f"✅ /sports = 200 ({len(r.json())} sports)")
        elif r.status_code == 403:
            _log("❌ /sports = 403 — cf_clearance cookie 无效或过期", is_error=True)
            ok = False
        elif r.status_code == 503:
            _log("❌ /sports = 503 — Pinnacle 服务不可用", is_error=True)
            ok = False
        else:
            _log(f"❌ /sports = {r.status_code}", is_error=True)
            ok = False
    except requests.exceptions.SSLError as e:
        _log(f"❌ SSL 握手失败: {e}", is_error=True)
        _log("   可能原因: 代理/VPN 拦截了 Pinnacle 证书", is_error=True)
        ok = False
    except requests.exceptions.ConnectionError as e:
        _log(f"❌ 连接失败: {e}", is_error=True)
        _log("   可能原因: Cloudflare 拦截 / VPN 路由问题", is_error=True)
        ok = False
    except requests.exceptions.Timeout:
        _log("❌ 连接超时 — 网络/VPN 问题", is_error=True)
        ok = False

    # 3. Cookie 状态
    cookie = SESSION.cookies.get("cf_clearance", domain=".pinnacle.com")
    if cookie:
        _log(f"✅ cf_clearance cookie 已加载 ({len(cookie)} 字符)")
    else:
        _log("❌ 未加载 cf_clearance cookie", is_error=True)
        # 尝试从 Chrome 读取
        if _refresh_cookie_from_chrome():
            _log("✅ 已从 Chrome 自动获取 cookie")
        else:
            _log("❌ 无法从 Chrome 获取 cookie", is_error=True)
            _log("   请在 Chrome 中打开 https://www.pinnacle.com 通行人机验证", is_error=True)
            ok = False

    # 4. 带 X-API-Key 的端点测试
    try:
        r = SESSION.get(f"{API_BASE}/leagues/29/matchups", timeout=15)
        if r.status_code == 200:
            n = len(r.json()) if isinstance(r.json(), list) else 0
            _log(f"✅ matchups (含 X-API-Key) = 200 ({n} matches)")
        elif r.status_code == 401:
            _log(f"❌ matchups = 401 — X-API-Key 无效或缺失", is_error=True)
            ok = False
        elif r.status_code == 403:
            _log(f"❌ matchups = 403 — Cloudflare WAF 拦截了 X-API-Key 请求", is_error=True)
            _log("   需在 Chrome 中刷新 pinnacle.com 获取新 cookie", is_error=True)
            ok = False
        else:
            _log(f"❌ matchups = {r.status_code}", is_error=True)
            ok = False
    except Exception as e:
        _log(f"❌ matchups 异常: {e}", is_error=True)
        ok = False

    if ok:
        _log("✅ Pinnacle API 连通性正常")
    else:
        _log("❌ Pinnacle API 存在问题，部分功能不可用", is_error=True)

    return ok, diag


def main():
    """CLI: python3 -m src.scrapers.pinnacle_api --check"""
    import sys
    if "--check" in sys.argv:
        ok, diag = check_pinnacle_connectivity(verbose=True)
        sys.exit(0 if ok else 1)
    else:
        print("用法: python3 -m src.scrapers.pinnacle_api --check")
        sys.exit(1)


# ── Module init ────────────────────────────────────────────────────────
_load_cookie()
_check_hosts_file()

if __name__ == "__main__":
    main()
