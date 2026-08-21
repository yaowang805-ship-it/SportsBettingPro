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
                if not e.partial:
                    return data  # 空 partial 无进展, 直接退出防 100% CPU 死循环
                data += e.partial
                amt -= len(e.partial)
                if amt <= 0:
                    return data
        return data

    http.client.HTTPResponse._safe_read = _safe_read_patched
    http.client.HTTPResponse._safe_read_is_patched = True  # marker on CLASS, survives reload()

# ── Session setup ──────────────────────────────────────────────────────

# V5.1: DNS 绕过 Shadowrocket VPN劫持 → 直连 Pinnacle Cloudflare IP
import urllib3.util.connection as _urllib3_conn
_PIN_REAL = ('104.18.42.200', 443)
_PIN_HOST = 'guest.api.arcadia.pinnacle.com'

_orig_create_connection = getattr(_urllib3_conn, '_orig_create_connection', _urllib3_conn.create_connection)
if not getattr(_urllib3_conn, '_pin_patched', False):
    def _patched_create_connection(address, *args, **kwargs):
        if address[0] == _PIN_HOST:
            address = (_PIN_REAL[0], _PIN_REAL[1])
        return _orig_create_connection(address, *args, **kwargs)
    _urllib3_conn.create_connection = _patched_create_connection
    _urllib3_conn._orig_create_connection = _orig_create_connection
    _urllib3_conn._pin_patched = True

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
LOW_PRIORITY_MAX_RETRIES = 2       # V5.10: 后台任务(CLV采集等)快速失败, 见 api_get 注释
RETRY_DELAY = 2.0
MAX_BACKOFF = 8.0                  # 指数退避封顶
MAX_TOTAL_WAIT = 30.0              # 累积等待超过此值放弃
JITTER = 0.25                      # ±25% 随机抖动
COOKIE_REFRESH_COOLDOWN = 900      # Cookie 刷新失败后冷却 15 分钟
MAX_COOKIE_REFRESHES = 3           # 每次扫描最多刷新 3 次 cookie
_ssl_fail_count = 0  # 全局 SSL 失败计数器（看门狗用）

_last_req_time = 0.0
_MIN_REQUEST_INTERVAL = 0.10       # V5.5: 0.10s (10 req/s, 封禁线16 req/s, 62%安全线)
_REQUEST_COUNT = 0                  # V5: 扫描内请求计数器
_SCAN_PAUSE_UNTIL = 0.0            # V5: Cloudflare 封禁后自动暂停
_REQUEST_BURST_WINDOW = 10         # V5: 10秒窗口
_REQUEST_BURST_LIMIT = 100         # V5.5: 100个/10s (10 req/s, 封禁线16 req/s的62%)
_BURST_WINDOW_START = 0.0
_BURST_COUNT = 0
_ip_ban_notify_until = 0.0         # V5.2: IP 封禁钉钉告警节流(30min 内只发一次)
_BAN_COUNT = 0                     # V5.9: 连续封禁计数 → 降级请求频率
_LAST_BAN_TIME = 0.0               # V5.9: 上次封禁时间(24h无封禁则重置计数)
_BAN_RESET_HOURS = 24              # V5.9: 24h 无封禁则重置降级

_CONSECUTIVE_FAILURES = 0          # V5.9: 连续失败计数(熔断器)
_CIRCUIT_OPEN_UNTIL = 0.0          # V5.9: 熔断打开(暂停所有请求)到期时间
_CIRCUIT_FAILURE_THRESHOLD = 10    # V5.9: 连续失败阈值
_CIRCUIT_COOLDOWN = 600            # V5.9: 熔断冷却 10 分钟
_CIRCUIT_NOTIFY_UNTIL = 0.0        # V5.9: 熔断告警节流(30min 一次)
_SHARED_BAN_CACHE = None           # V5.10: 全局封禁次数缓存(跨进程共享层读来的)
_SHARED_BAN_CACHE_AT = 0.0


def _current_min_interval():
    """封禁降级: 连续封禁越多, 请求间隔越长(10→8 req/s), 降低 Cloudflare 反复封禁概率。

    0 次封禁 → 0.10s (10 req/s, 默认安全线)
    1 次封禁 → 0.1125s (8.9 req/s)
    ≥2 次封禁 → 0.125s (8 req/s, 最低)
    24h 内无封禁 → 自动重置回 10 req/s。
    """
    global _BAN_COUNT, _SHARED_BAN_CACHE, _SHARED_BAN_CACHE_AT
    if _LAST_BAN_TIME and time.time() - _LAST_BAN_TIME > _BAN_RESET_HOURS * 3600:
        _BAN_COUNT = 0
    # V5.10: 降级档位取全局封禁次数 —— 别的进程被封了, 本进程也该降速,
    # 否则"降级"只在挨打的那个进程生效, 总速率纹丝不动。缓存 60s 免得每请求读库。
    ban = _BAN_COUNT
    now = time.time()
    if now - _SHARED_BAN_CACHE_AT > 60:
        try:
            from src.scrapers import pin_rate_state
            _SHARED_BAN_CACHE = pin_rate_state.get_ban_count()
        except Exception:
            _SHARED_BAN_CACHE = None
        _SHARED_BAN_CACHE_AT = now
    if _SHARED_BAN_CACHE is not None:
        ban = max(ban, _SHARED_BAN_CACHE)
    interval = _MIN_REQUEST_INTERVAL + 0.0125 * min(ban, 2)  # 最多降 2 档
    return min(interval, 0.125)


def _circuit_open() -> bool:
    """熔断器是否打开(暂停所有 Pin 请求)。"""
    return _CIRCUIT_OPEN_UNTIL > time.time()


def _record_pin_failure():
    """记录一次 Pin 请求失败(非200)。连续失败超阈值 → 熔断打开, 暂停所有请求。

    防风控: 正常提取是限速的, 被封几乎都是代码 bug 反复连 Pin(如404死循环)。
    熔断器在连续失败时提前暂停, 避免把 Cloudflare 触发封禁。
    """
    global _CONSECUTIVE_FAILURES, _CIRCUIT_OPEN_UNTIL
    _CONSECUTIVE_FAILURES += 1
    if _CONSECUTIVE_FAILURES >= _CIRCUIT_FAILURE_THRESHOLD:
        _CIRCUIT_OPEN_UNTIL = time.time() + _CIRCUIT_COOLDOWN
        _CONSECUTIVE_FAILURES = 0
        # V5.10: 熔断同样要全进程共享 —— 单进程熔断挡不住别的进程继续锤 Pin
        try:
            from src.scrapers import pin_rate_state
            pin_rate_state.open_circuit(_CIRCUIT_COOLDOWN)
        except Exception:
            pass
        logger.error("⚠️ 熔断器: 连续 %d 次 Pin 请求失败 → 暂停所有请求 %d 分钟 (疑似代码bug反复连Pin, 防风控)",
                     _CIRCUIT_FAILURE_THRESHOLD, _CIRCUIT_COOLDOWN // 60)
        # 文件节流(模块热重载会重置内存全局, 必须文件持久化) — 30min 内只发一条熔断告警
        _cb_file = DATA_DIR / ".pin_circuit_notify.txt"
        now = time.time()
        if _cb_file.exists():
            try:
                if now - float(_cb_file.read_text().strip()) < 1800:
                    return
            except (ValueError, OSError):
                pass
        _cb_file.write_text(str(now))
        try:
            # 走 config.settings 统一入口(自动注入关键词 + urgent 跳过每日配额)。
            # 原为裸 except: pass, 熔断告警发不出去完全静默 —— 熔断本身就是重故障, 不能哑。
            from config.settings import send_dingtalk
            _msg = "⚠️ Pin 请求熔断\n\n连续 10 次失败, 已暂停 10 分钟防风控。疑似代码 bug 反复连 Pin API, 请检查。"
            if not send_dingtalk("Pin 熔断告警", _msg, urgent=True):
                logger.warning("Pin 熔断告警未送达(钉钉返回失败)")
        except Exception as e:
            logger.warning("Pin 熔断告警推送异常: %s", e)


def _record_pin_success():
    """记录一次 Pin 请求成功 → 重置连续失败计数。"""
    global _CONSECUTIVE_FAILURES
    _CONSECUTIVE_FAILURES = 0

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


_rate_limit_lock = __import__('threading').Lock()  # V5.5: 并行拉取时保护限速计数器


_REQUEST_PRIORITY = "high"   # V5.10: high=主扫描(带宽优先), low=CLV/回填等后台任务


def set_request_priority(priority: str):
    """标记本进程的 Pinnacle 请求优先级。

    后台任务(clv_collector 等)设成 "low" 后, 在跨进程共享限速里只吃主扫描剩下的
    带宽(自身 <=2 req/s), 保证增量扫描推送不受影响(扫描推送隔离铁律)。
    """
    global _REQUEST_PRIORITY
    _REQUEST_PRIORITY = priority


def _rate_limit(bypass_pause: bool = False):
    global _last_req_time, _REQUEST_COUNT, _BURST_WINDOW_START, _BURST_COUNT, _SCAN_PAUSE_UNTIL

    # V5.10: 先走跨进程共享限速 —— 四个进程(pipeline/clv_collector/两个回填)各自
    # 持有内存限速器时, 真实总速率可达单进程的数倍, 是反复被 Cloudflare 封的根因。
    # 共享层不可用时返回 None, 直接 fail-open 落到下面的进程内限速, 绝不阻塞扫描。
    if not bypass_pause:
        try:
            from src.scrapers import pin_rate_state
            allowed, wait, reason = pin_rate_state.reserve(
                _current_min_interval(), _REQUEST_BURST_LIMIT, _REQUEST_BURST_WINDOW,
                priority=_REQUEST_PRIORITY)
            if allowed is False:
                logger.warning("Pin 全局限速: 跳过请求 (%s)", reason)
                return False
            if allowed is True:
                if wait > 0:
                    time.sleep(wait)
                with _rate_limit_lock:
                    _last_req_time = time.time()
                    _REQUEST_COUNT += 1
                    _BURST_COUNT += 1
                return True
        except Exception as e:
            logger.debug("共享限速异常, 退回进程内限速: %s", e)

    # V5.5: 加锁 — 8线程并行时全局计数器有竞争, 会超限被Cloudflare封禁(数据丢失)
    with _rate_limit_lock:
        # V5: Cloudflare 封禁后自动暂停 — 冷却中真正跳过请求(修复每60s重锤被封IP的bug)
        # bypass_pause=True 供换节点自检用: 自检必须真实请求 Pin, 不能被自己设的暂停挡住
        if not bypass_pause and _SCAN_PAUSE_UNTIL > 0:
            remaining = _SCAN_PAUSE_UNTIL - time.time()
            if remaining > 0:
                logger.warning("Cloudflare 封禁冷却中, 跳过请求 (剩余 %.0fs)", remaining)
                return False  # 冷却中立即跳过 (原 sleep60s 导致 N联赛×60s 小时级卡死)
            _SCAN_PAUSE_UNTIL = 0.0  # 到期, 清除标志

        # V5.9: 熔断器 — 连续失败超阈值时暂停所有请求(防风控: 代码bug反复连Pin)
        if not bypass_pause and _circuit_open():
            logger.warning("Pin 熔断冷却中, 跳过请求 (剩余 %.0fs)", _CIRCUIT_OPEN_UNTIL - time.time())
            return False

        # V5: 突发流量限制 — 10秒窗口内最多 N 请求
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
        _interval = _current_min_interval()  # V5.9: 连续封禁降级(10→8 req/s)
        if elapsed < _interval:
            import random
            # 微抖动 0~0.2s 打破规律节奏, 几乎不影响速度
            time.sleep(_interval - elapsed + random.uniform(0, 0.2))
        _last_req_time = time.time()
        _REQUEST_COUNT += 1
        _BURST_COUNT += 1
        return True


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


def _auto_switch_node():
    """IP 封禁后自动换 Shadowrocket 节点(换国家才换 IP), 后台线程执行, 成功后解除扫描暂停。

    V5.5 方案①: 不再要求手动换节点。封禁 → 自动换国家 → 验证出口 IP → 自检 Pin → 恢复。
    """
    import threading
    try:
        from src.scrapers.pin_proxy_pool import load_nodes, do_recover

        def _run():
            try:
                nodes = load_nodes()
                ok = do_recover(nodes)
                if ok:
                    global _SCAN_PAUSE_UNTIL
                    _SCAN_PAUSE_UNTIL = 0  # 换节点成功, 解除暂停
                    logger.info("✅ 自动换节点成功, 已解除扫描暂停")
            except Exception as e:
                logger.error("自动换节点异常: %s", e)

        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:
        logger.error("自动换节点启动失败: %s", e)


def _notify_ip_ban():
    """IP 封禁时发钉钉提醒(一条)。

    节流用文件持久化(模块热重载会重置内存全局, 导致每次 tick 重发告警)。
    30min 内只发一次封禁告警; 解禁后由 _maybe_notify_recovered 发恢复通知。
    不再自动换节点(URL scheme/写文件对 macOS Shadowrocket 都不生效, 需手动换)。
    """
    _throttle_file = DATA_DIR / ".ip_ban_notify.txt"
    now = time.time()
    if _throttle_file.exists():
        try:
            if now - float(_throttle_file.read_text().strip()) < 1800:
                return  # 30min 内已告警过, 不重复发
        except (ValueError, OSError):
            pass
    _throttle_file.write_text(str(now))
    try:
        from config.dingtalk import send_dingtalk
        msg = ("【投注推荐】⚠️ Pinnacle IP 被封禁\n\n"
               "Cloudflare 已封禁当前出口 IP, 请手动切换 Shadowrocket 节点(建议 HK/JP/TW/DE)恢复。")
        send_dingtalk(msg, msgtype="text", title="Pinnacle 封禁告警")
        logger.info("已发送钉钉 IP 封禁告警")
    except Exception as e:
        logger.error("发送 IP 封禁告警失败: %s", e)


def _maybe_notify_recovered():
    """解禁后发一条恢复通知(封禁告警发出过才发, 只发一次)。"""
    _throttle_file = DATA_DIR / ".ip_ban_notify.txt"
    _recovered_file = DATA_DIR / ".ip_ban_recovered.txt"
    if not _throttle_file.exists():
        return  # 从没发过封禁告警, 不发恢复
    if _recovered_file.exists():
        try:
            if time.time() - float(_recovered_file.read_text().strip()) < 86400:
                return  # 24h 内已发过恢复
        except (ValueError, OSError):
            pass
    try:
        # 走 config.settings 入口自动注入机器人关键词 —— 原先直连 config.dingtalk 且
        # 正文无"投注推荐", 被钉钉服务端以 errcode 310000 静默拒收, 恢复通知从未送达。
        from config.settings import send_dingtalk
        msg = "✅ Pinnacle 已恢复\n\nCloudflare 封禁已解除, 增量扫描/推送恢复正常。"
        _sent = send_dingtalk("Pinnacle 恢复通知", msg, urgent=True)
        _recovered_file.write_text(str(time.time()))
        logger.info("钉钉 Pinnacle 恢复通知: %s", "已送达" if _sent else "未送达")
        # 重置封禁节流文件, 下次封禁能立即发新告警(一个封禁/恢复周期各一条)
        try:
            _throttle_file.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as e:
        logger.error("发送恢复通知失败: %s", e)


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


def api_get(path, retry=True, bypass_pause=False):
    """调用 Pinnacle API，带自诊断和自动恢复。

    V4.5: 指数退避 + 封顶 + 抖动 + 累积超时保护。
    bypass_pause=True 时跳过 Cloudflare 封禁冷却检查(换节点自检用)。
    """
    global _ssl_fail_count
    _load_cookie()
    if not _rate_limit(bypass_pause=bypass_pause):
        return None  # Cloudflare 冷却中, 跳过本次请求
    url = f"{API_BASE}{path}"
    total_wait = 0.0

    # V5.10: 后台任务快速失败。5 次重试 × 指数退避 = 单个联赛烧 30~60 秒,
    # 而 CLV 采集窗口只有 19 分钟 —— 实测 8-18 19:50 那次窗口, 两个联赛各锤 5 次、
    # 白打 20 个请求、产出 0, 窗口就此关闭, 那批比赛的收盘价永久丢失。
    # 更要命的是这 20 个请求全打在 Cloudflare 已经开始拒绝的时候, 是把封禁坐实的行为。
    # 主扫描(high)保持 5 次不变 —— 它要的是可靠性, 且有下一轮兜底。
    _max = MAX_RETRIES if _REQUEST_PRIORITY == "high" else LOW_PRIORITY_MAX_RETRIES

    for attempt in range(_max if retry else 1):
        try:
            resp = SESSION.get(url, timeout=30)

            if resp.status_code == 429:
                slept = _backoff_sleep(attempt)
                total_wait += slept
                logger.warning("429 rate limited, retry in %.1fs (attempt %d/%d, total %.1fs)",
                               slept, attempt + 1, _max, total_wait)
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue

            if resp.status_code == 200:
                _ssl_fail_count = 0
                _record_pin_success()  # V5.9: 成功 → 重置熔断失败计数
                _maybe_notify_recovered()  # V5.9: 封禁后首次成功 → 发恢复通知(仅一次)
                return resp.json()

            # ── 自诊断 ──
            err_type = _diagnose_response(resp, url)

            if err_type == "maintenance":
                logger.warning("Pinnacle 维护中 — 跳过本次扫描")
                return None  # 不重试

            if err_type == "ip_ban":
                logger.error("Cloudflare IP 封禁 — 自动暂停扫描 30 分钟")
                global _SCAN_PAUSE_UNTIL, _BAN_COUNT, _LAST_BAN_TIME
                _SCAN_PAUSE_UNTIL = time.time() + 1800  # 30 分钟冷却
                _BAN_COUNT += 1                          # V5.9: 连续封禁计数 → 降级请求频率
                _LAST_BAN_TIME = time.time()
                # V5.10: 封禁是 IP 级的, 必须让所有进程一起停 —— 否则主扫描停了,
                # CLV 采集器还在往被封的 IP 上打, 封禁只会被坐实、延长。
                try:
                    from src.scrapers import pin_rate_state
                    pin_rate_state.set_pause(1800)
                    pin_rate_state.record_ban()
                except Exception:
                    pass
                _notify_ip_ban()  # V5.2: 钉钉提醒换节点(节流)
                return None  # 不重试，重试也没用

            if err_type == "cookie_expired":
                logger.warning("Cookie 过期 — 从 Chrome 恢复...")
                if _refresh_cookie_from_chrome():
                    continue  # 重试
                if attempt < _max - 1:
                    continue

            if err_type == "rate_limit":
                slept = _backoff_sleep(attempt, extra=5.0)
                total_wait += slept
                logger.warning("Pinnacle 限速, %.1fs 后重试 (attempt %d/%d, total %.1fs)",
                               slept, attempt + 1, _max, total_wait)
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue

            # 其他错误: 重试 (指数退避 + 抖动)
            if attempt < _max - 1:
                slept = _backoff_sleep(attempt)
                total_wait += slept
                logger.warning("HTTP %d, retrying (attempt %d/%d, total %.1fs)",
                               resp.status_code, attempt + 1, _max, total_wait)
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue

            _diagnose_pinnacle_error(url, resp.status_code)
            _record_pin_failure()  # V5.9: 失败计数 → 连续失败熔断防风控
            return None

        except requests.exceptions.SSLError as e:
            _ssl_fail_count += 1
            logger.warning("SSL error (attempt %d/%d): %s", attempt + 1, _max, e)
            if attempt < _max - 1:
                extra = 10.0 if _ssl_fail_count > 20 else 0.0
                if extra:
                    logger.warning("SSL大面积故障(已%d次), 额外冷却%.0fs", _ssl_fail_count, extra)
                slept = _backoff_sleep(attempt, extra=extra)
                total_wait += slept
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue
            logger.error("SSL handshake failed after %d retries", _max)
            return None

        except requests.exceptions.ConnectionError as e:
            logger.error("Connection failed: %s", e)
            return None

        except requests.exceptions.Timeout:
            if attempt < _max - 1:
                slept = _backoff_sleep(attempt)
                total_wait += slept
                logger.warning("timeout, retrying in %.1fs (attempt %d/%d, total %.1fs)...",
                               slept, attempt + 1, _max, total_wait)
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue
            logger.error("Pinnacle API timeout after %d retries", _max)
            return None

        except requests.exceptions.ChunkedEncodingError as e:
            logger.warning("ChunkedEncodingError (Python 3.14 bug), retrying... %s", e)
            if attempt < _max - 1:
                slept = _backoff_sleep(attempt)
                total_wait += slept
                if total_wait >= MAX_TOTAL_WAIT:
                    logger.error("Max total wait exceeded (%.0fs), giving up", MAX_TOTAL_WAIT)
                    return None
                continue
            return None

        except Exception as e:
            logger.error("Pinnacle API error (%s): %s", type(e).__name__, e)
            _record_pin_failure()  # V5.9: 失败计数 → 熔断防风控
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
