#!/usr/bin/env python3
"""BB 登录态续期 + token 写盘（CDP 连 Chrome 9222，替代旧 AppleScript 默认 Chrome 路径）。

背景: BB 的 st-auth 约 11h 过期，过期后下单 API 和 push WS 订阅都失效。旧 auto_renew_token
用 AppleScript 操作「默认 Chrome 前台窗口」，但 tap 读 G04 挂的是独立 Chrome 9222(.chrome-bb)，
两个浏览器登录态不同步 —— 之前 BB WS 死就是 9222 登录态过期没人续。

本脚本统一走 CDP 连 Chrome 9222:
1. 找/开 BB 页(vv899.bbty0vip7.com)
2. 读 st-auth，测下单接口; 失效则清 st-auth + reload 触发自动登录(靠持久 cookie)
3. 读新 st-auth/st-domain 写 .bb_token / .bb_domain

用法: .venv312/bin/python scripts/renew_bb_login.py
launchd 每 6h 跑一次(StartInterval 21600)。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data" / "storage"
TOKEN_FILE = DATA / ".bb_token"
DOMAIN_FILE = DATA / ".bb_domain"
BB_URL = "https://vv899.bbty0vip7.com"
CDP_URL = "http://127.0.0.1:9222"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


def _log(msg):
    print(f"[renew_bb] {msg}", flush=True)


def _test_token(tok, dom):
    """测下单接口，code==0 表示 token 有效。"""
    import urllib3
    import requests
    urllib3.disable_warnings()
    try:
        r = requests.post(f"{dom}/v1/order/new/bet/list",
                          json={"languageType": "CMN", "isSettled": False, "current": 1, "size": 1},
                          headers={"Content-Type": "application/json", "Authorization": tok,
                                   "User-Agent": _UA},
                          timeout=15, verify=False)
        return r.json().get("code") == 0
    except Exception as e:
        _log(f"测试 token 异常: {e}")
        return False


def _read_ls(pg):
    return pg.evaluate("() => { const r={}; for(let i=0;i<localStorage.length;i++){"
                       "const k=localStorage.key(i); r[k]=localStorage.getItem(k);} return r; }")


def _get_bb_page(ctx):
    for page in ctx.pages:
        if "vv899" in page.url or "bbty" in page.url:
            return page
    return None


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            _log(f"连 Chrome 9222 失败(未启动?): {e}")
            return 1
        ctx = browser.contexts[0]
        pg = _get_bb_page(ctx)
        if not pg:
            _log("BB 页未开，新建并打开 vv899...")
            pg = ctx.new_page()
            try:
                pg.goto(BB_URL, wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass
            time.sleep(5)

        # 1. 读当前 st-auth
        ls = _read_ls(pg)
        st = ls.get("st-auth", "")
        # 2026-09-30: BB 切域名 invf1→nsvip9, 兜底从硬编码改为读上次 .bb_domain
        _last_dom = ""
        try:
            _last_dom = DOMAIN_FILE.read_text().strip().rstrip("/")
        except Exception:
            pass
        dom = (ls.get("st-domain", "") or _last_dom or "https://api.infv1.com").rstrip("/")

        # 2. 测试 token；失效则清 + reload 触发自动登录
        if st and _test_token(st, dom):
            _log(f"token 有效，直接写盘: {st[:25]}...")
        else:
            _log(f"token 失效或为空({st[:15] if st else '无'})，清 st-auth + reload 触发自动登录")
            try:
                pg.evaluate('() => { localStorage.removeItem("st-auth"); '
                            'localStorage.removeItem("user-token"); localStorage.removeItem("h5-token"); }')
                pg.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                _log(f"reload 异常(可忽略): {e}")
            # 等自动登录(靠持久 cookie)生成新 st-auth
            for _ in range(10):
                time.sleep(3)
                ls2 = _read_ls(pg)
                st2 = ls2.get("st-auth", "")
                if st2 and _test_token(st2, (ls2.get("st-domain", "") or dom).rstrip("/")):
                    st = st2
                    dom = (ls2.get("st-domain", "") or dom).rstrip("/")
                    break
            if not st or not _test_token(st, dom):
                _log("⚠️ 自动登录未成功(可能需手动登录)，保留旧 token 不覆盖")
                browser.close()
                return 1

        # 3. 写盘
        try:
            TOKEN_FILE.write_text(st)
            DOMAIN_FILE.write_text(dom)
            _log(f"✅ token 写盘成功: {st[:25]}... domain={dom}")
        except Exception as e:
            _log(f"写盘失败: {e}")
            return 1

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
