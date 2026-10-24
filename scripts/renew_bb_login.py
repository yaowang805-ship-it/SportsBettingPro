#!/usr/bin/env python3
"""BB 登录态续期 + token 写盘（CDP 连 Chrome 9222，替代旧 AppleScript 默认 Chrome 路径）。

背景: BB 的 st-auth 失效真因是「域名切换」(invf1→x-vip8→nsvip9, 短期多次), 不是 11h 过期
(token 实测能撑 3.5 天+, 见记忆 token-renew-triple-domain-switch-20260930)。旧 auto_renew_token
用 AppleScript 操作「默认 Chrome 前台窗口」，但 tap 读 G04 挂的是独立 Chrome 9222(.chrome-bb)，
两个浏览器登录态不同步 —— 之前 BB WS 死就是 9222 登录态失效没人续。

本脚本统一走 CDP 连 Chrome 9222:
1. 找/开 BB 页(vv899.bbty0vip7.com)
2. 读 st-auth，测下单接口; 失效/为空则跳登录页点「登录」按钮(账号密码浏览器已预填, 2026-10-24 实测)
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
    """测下单接口。三态: True=有效(code==0), False=真失效(code!=0), None=网络异常(连不上)。

    2026-10-08 区分网络异常 vs 真失效: 网络抖动(SSLError/DNS/超时)返回 None, 不清 st-auth 不 reload。
    """
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
        _log(f"测试 token 网络异常: {e}")
        return None


# 2026-10-05: BB「线路切换」多镜像域轮换(invf1→x-vip8→nsvip9→...), st-domain 可能指向已死的旧域。
# 候选列表 + 测多域, 写 code=0 的那个(不再盲信 st-domain)。
BB_DOMAIN_CANDIDATES = [
    "https://api.x-vip8.com",
    "https://api.nsvip9.com",
    "https://api.invf1.com",
]


def _find_working_domain(tok, prefer=None):
    """测 token 在哪个域有效。返回 (domain, reachable):
    domain 非 None = 有效域名(code==0);
    domain None 且 reachable=True = 至少一个域连上但 code!=0(真失效);
    domain None 且 reachable=False = 所有域都网络异常(连不上, 不判失效)。
    """
    cands = []
    if prefer:
        p = prefer.rstrip("/")
        if p.startswith("http"):
            cands.append(p)
    for d in BB_DOMAIN_CANDIDATES:
        d = d.rstrip("/")
        if d not in cands:
            cands.append(d)
    reachable = False
    for dom in cands:
        res = _test_token(tok, dom)
        if res is True:
            return dom, True
        if res is False:
            reachable = True
    return None, reachable


def _read_ls(pg):
    return pg.evaluate("() => { const r={}; for(let i=0;i<localStorage.length;i++){"
                       "const k=localStorage.key(i); r[k]=localStorage.getItem(k);} return r; }")


def _get_bb_page(ctx):
    for page in ctx.pages:
        if "vv899" in page.url or "bbty" in page.url:
            return page
    return None


def _click_login(pg):
    """在登录页点「登录」按钮(账号密码浏览器已预填), 返回新 st-auth 或空串。

    2026-10-24 替代旧「清 st-auth + reload 靠持久 cookie 自动登录」: 实测 .chrome-bb
    独立 profile 无持久 cookie, 自动登录走不通; 但登录页账号名/密码已预填(浏览器记住),
    直接点「登录」即可。无验证码时 2s 内出 st-auth。
    """
    # 1. 若不在登录页, 跳到登录页
    if "/login" not in pg.url:
        try:
            pg.goto(BB_URL.rstrip("/") + "/login", wait_until="domcontentloaded", timeout=25000)
        except Exception:
            pass
        pg.wait_for_timeout(3000)
    # 2. 点账户登录表单里的「登录」按钮
    clicked = pg.evaluate("""() => {
        const form = document.querySelector('form.main-container.account, form.account, form.el-form');
        if (form) {
            for (const b of form.querySelectorAll('button')) {
                if (b.innerText.trim() === '登录') { b.click(); return 'account'; }
            }
        }
        for (const b of document.querySelectorAll('button')) {
            if (b.innerText.trim() === '登录') { b.click(); return 'first'; }
        }
        return 'none';
    }""")
    _log(f"点登录按钮: {clicked}")
    # 3. 等新 st-auth 出现(最多 20s)
    for _ in range(20):
        time.sleep(1)
        st = pg.evaluate("() => localStorage.getItem('st-auth') || ''")
        if st:
            return st
    return ""


def _close_chrome():
    """彻底关闭 Chrome 9222(防电脑过热)。browser.close() 是 Playwright 断连, 再加 pkill 兜底。"""
    try:
        import subprocess
        subprocess.run(["pkill", "-f", "remote-debugging-port=9222"],
                       capture_output=True, timeout=5)
    except Exception:
        pass


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
        _last_dom = ""
        try:
            _last_dom = DOMAIN_FILE.read_text().strip().rstrip("/")
        except Exception:
            pass
        prefer_dom = (ls.get("st-domain", "") or _last_dom or BB_DOMAIN_CANDIDATES[0]).rstrip("/")

        # 2. 测 token 在候选域里哪个有效(优先 st-domain)；失效/为空 → 点登录续期
        dom = None
        if st:
            dom, _reachable = _find_working_domain(st, prefer_dom)
            if dom:
                _log(f"token 有效，直接写盘: {st[:25]}... domain={dom}")
            elif not _reachable:
                _log("所有域网络异常(连不上), 不判失效不续期(可能是网络抖动), 保留旧 token")
                browser.close()
                _close_chrome()
                return 0

        if not dom:
            _log(f"token 失效或为空({st[:15] if st else '无'})，点登录按钮续期(账号密码已预填)")
            st = _click_login(pg)
            if not st:
                _log("⚠️ 点登录未成功(可能需处理验证码/手动登录)，保留旧 token 不覆盖")
                browser.close()
                _close_chrome()
                return 1
            wd, _reachable2 = _find_working_domain(st, prefer_dom)
            dom = wd or prefer_dom
            _log(f"新 token 写盘: {st[:25]}... domain={dom}")

        # 3. 写盘
        try:
            TOKEN_FILE.write_text(st)
            DOMAIN_FILE.write_text(dom)
            _log(f"✅ token 写盘成功: {st[:25]}... domain={dom}")
        except Exception as e:
            _log(f"写盘失败: {e}")
            browser.close()
            _close_chrome()
            return 1

        browser.close()
        _close_chrome()
    return 0


if __name__ == "__main__":
    sys.exit(main())
