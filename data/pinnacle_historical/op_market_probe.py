"""OddsPortal 逐场盘口 API 探测 + 解密 (HC/OU/DC/DNB/BTTS/OE 收盘价抓取基础)。

背景: OddsPortal 改版后, 逐场赔率页链接变 H2H, 老路子断了。但内部 API
`/proxy/ajax-*` 的响应是加密的 (AES-CBC + PBKDF2 + gzip, 密钥硬编码 JS),
本脚本用 Playwright 拦截这些加密响应并解密, 打印明文 JSON, 用于逆向盘口参数。

用法:
    .venv312/bin/python data/pinnacle_historical/op_market_probe.py \
        --sport football [--match-url https://.../h2h/.../#xxx]

不传 --match-url 时, 自动从该运动即将开赛的联赛页发现第一个 H2H 链接。
"""
import sys, re, json, base64, gzip, hashlib, argparse, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".venv312" / "lib" / "python3.12" / "site-packages"))

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
BASE = "https://www.oddsportal.com"

# 密钥 (硬编码在 OddsPortal JS, 见记忆 oddsportal-api-decrypt-20260814)
PASS = "J*8sQ!p$7aD_fR2yW@gHn*3bVp#sAdLd_k"
SALT_STR = "5b9a8f2c3e6d1a4b7c8e9d0f1a2b3c4d"


def decrypt_payload(payload: str):
    """解密 OddsPortal /proxy/ajax-* 响应 → 明文 bytes (gzip 已解压)。

    实际格式 (双重 base64): body = base64( base64(ciphertext) + ":" + iv_hex )。
    即外层 base64 解码 → "base64(ct):32位hexIV" 字符串, 再按冒号拆开。
    PBKDF2-HMAC-SHA256 (1000 iter, 32B key) → AES-256-CBC(iv) → PKCS7 unpad → gzip。
    salt 有两种可能: 32 字节 ASCII 字符串 vs 16 字节 hex 解码, 用 gzip magic 自动判定。
    """
    if not payload:
        return None
    try:
        raw = base64.b64decode(payload)
    except Exception:
        return None
    idx = raw.rfind(b":")
    if idx < 0:
        return None
    ct_b64 = raw[:idx]
    iv_hex = raw[idx + 1:].decode("ascii", "replace")
    try:
        ct = base64.b64decode(ct_b64)
        iv = bytes.fromhex(iv_hex)
    except Exception:
        return None
    for salt_bytes in (bytes.fromhex(SALT_STR), SALT_STR.encode("latin1")):
        try:
            key = hashlib.pbkdf2_hmac("sha256", PASS.encode("latin1"), salt_bytes, 1000, 32)
        except Exception:
            continue
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_CBC, iv)
        pt = cipher.decrypt(ct)
        if not pt:
            continue
        pad = pt[-1]
        if 0 < pad <= 16:
            pt = pt[:-pad]
        # gzip magic = 0x1f 0x8b
        if len(pt) >= 2 and pt[0] == 0x1f and pt[1] == 0x8b:
            try:
                return gzip.decompress(pt)
            except Exception:
                continue
    return None


def find_first_match_url(page, sport):
    """从运动的联赛列表页发现第一个即将开赛的 H2H 链接。"""
    # 常见联赛首页 (有 upcoming 比赛)
    hubs = {
        "football": "/football/",
        "basketball": "/basketball/",
        "tennis": "/tennis/",
        "baseball": "/baseball/",
        "american-football": "/american-football/",
        "ice-hockey": "/ice-hockey/",
    }
    page.goto(BASE + hubs.get(sport, f"/{sport}/"), wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    links = page.eval_on_selector_all(
        "a[href*='h2h']", "els => els.map(e => e.href)")
    # 只保留带 matchId hash 的 (形如 .../h2h/...#8位hash)
    for l in links:
        if "#" in l:
            return l
    # 兜底: 取任意 h2h 链接
    return links[0] if links else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="football")
    ap.add_argument("--match-url", default=None)
    args = ap.parse_args()

    captured = []  # (url, payload)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROME,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.set_default_timeout(60000)

        def on_response(resp):
            url = resp.url
            if "/proxy/ajax-" not in url:
                return
            try:
                body = resp.text()
            except Exception:
                body = ""
            captured.append((url, body))

        page.on("response", on_response)

        match_url = args.match_url
        if not match_url:
            match_url = find_first_match_url(page, args.sport)
            if not match_url:
                print("未找到 H2H 链接, 请手动传 --match-url")
                return
        print(f"[match] {match_url}", flush=True)
        page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)  # 等 JS 触发 ajax (oddswatch 有 2s 延迟)

        # 点击盘口标签触发 oddswatch-data (OU/DC/DNB/BTTS/OE)
        for tab in ["Over/Under", "Double Chance", "Draw No Bet", "BTTS", "Odd/Even",
                    "Asian Handicap", "Handicap"]:
            try:
                loc = page.locator("text=" + tab).first
                if loc.count() > 0:
                    loc.click(timeout=3000)
                    page.wait_for_timeout(2500)
            except Exception:
                pass

        browser.close()

    # 解密并打印
    for url, body in captured:
        pt = decrypt_payload(body)
        if pt is None:
            print(f"\n=== [未解密] {url}\n    len={len(body)} has_colon={':' in body}\n"
                  f"    head={body[:80]!r}\n    tail={body[-80:]!r}", flush=True)
            continue
        try:
            js = json.loads(pt.decode("utf-8"))
            pretty = json.dumps(js, ensure_ascii=False, indent=2)[:3000]
        except Exception:
            pretty = pt[:2000]
        print(f"\n=== [解密成功] {url}\n{pretty}", flush=True)


if __name__ == "__main__":
    main()
