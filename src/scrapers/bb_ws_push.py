"""BB 赔率 WebSocket 秒级推送客户端 (2026-09-06 逆向)。

关键结论(见记忆 bb-websocket-push-reverse-20260906):
  - push 域名是每次会话动态生成的随机子域名(如 pushi32e54ih.h906qtx274jb.com),
    域名本身就是鉴权(连对即 101, 无需 token/cookie)。
  - 协议: 连接后客户端发 subscribe, 服务器推 G04(赔率变动)。
    subscribe: {"cmd":"subscribe","channel":[联赛ID整数,...],"userId":<int>}
    G04 data: {"market":"角球:大/小","id":"{marketId}-{option}","matchId":"5083832",
               "items":[{"name":"大 6","value":"1.86",...}]}  ← matchId 直接给, 无需映射
  - 传输: 用 curl_cffi(impersonate chrome)。websockets 库会卡在 Shadowrocket 的
    198.18.x.x 假 DNS 上(TCP/TLS 通但 Upgrade 挂), curl_cffi 走 libcurl 正常连。

⚠️ TLS 指纹结论(2026-09-06): 服务器对 Chrome 152(真浏览器)推数据, 对 curl_cffi(最高
   chrome150)连上 101 但静默不推任何帧(实测浏览器收 L04/P02/pong, curl_cffi 收 0)。
   握手头已逐字段对比完全一致(无 Client Hints), 差异只在 TLS 指纹(JA3/JA4)。
   → 可靠路径是 --tap(CDP 挂浏览器现有 WS 读 G04, 已通); 独立 curl_cffi 客户端留作
   待 curl_cffi 出 chrome152 后用。

用法:
    .venv312/bin/python -m src.scrapers.bb_ws_push --tap --listen 60   # 可靠, 绕指纹
    .venv312/bin/python -m src.scrapers.bb_ws_push --listen 60 --leagues 401,407,408
"""
import asyncio
import json
import sys
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

CACHE_FILE = ROOT / "data" / "storage" / ".bb_push_domain"
CACHE_TTL = 10 * 3600  # push 域名会话内稳定, 缓存 10h(对齐 token ~11h 有效期)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")


# ── 1. 动态 push 域名 ──────────────────────────────────────────────────

def get_push_domain(force=False) -> str:
    """拿 push 域名。顺序: 缓存文件(10h内) > 浏览器抓(playwright CDP)。"""
    if not force and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            if data.get("domain") and time.time() - data.get("ts", 0) < CACHE_TTL:
                return data["domain"]
        except Exception:
            pass
    dom = _bootstrap_from_browser()
    if dom:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"domain": dom, "ts": time.time()}))
    return dom or ""


def _bootstrap_from_browser() -> str:
    """从运行中的独立 Chrome(port 9222)抓 push WebSocket 域名。返回空串失败。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return _bootstrap_fallback()
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            pg = browser.contexts[0].pages[0]
            captured = {}
            def on_ws(ws):
                u = ws.url
                if "push" in u and "chat" not in u:
                    captured["url"] = u
            pg.on("websocket", on_ws)
            try:
                pg.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            for _ in range(15):
                pg.wait_for_timeout(1000)
                if captured.get("url"):
                    break
            browser.close()
            url = captured.get("url", "")
            return url.rstrip("/") if url else ""
    except Exception:
        return _bootstrap_fallback()


def _bootstrap_fallback() -> str:
    """兜底: 读缓存文件(即使过期)。"""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text()).get("domain", "")
        except Exception:
            pass
    return ""


# ── 2. 联赛 ID ────────────────────────────────────────────────────────

def get_league_ids(token=None, domain=None):
    """getList 全量拉取, 提取所有联赛 ID(用于 subscribe channel)。"""
    from src.betting.bb_auto_bet import read_token, read_domain, _session
    token = token or read_token()
    domain = domain or read_domain()
    if not token:
        return []
    s = _session()
    lids = set()
    for sport_id in (1, 3, 5, 7, 6):  # 足球/篮球/网球/棒球/美足
        page = 1
        while page <= 40:
            try:
                r = s.post(f"{domain}/v1/match/getList",
                           json={"sportId": sport_id, "type": 2, "current": page,
                                 "pageSize": 50, "isPC": True, "languageType": "CMN"},
                           headers={"Content-Type": "application/json", "user-token": token,
                                    "User-Agent": _UA}, timeout=15, verify=False)
                d = r.json()
                if d.get("code") != 0:
                    break
                data = d.get("data") or {}
                recs = data.get("records") or []
                for m in recs:
                    lid = m.get("lid") or m.get("leagueId")
                    if lid:
                        lids.add(int(lid))
                page_total = data.get("pageTotal") or 1
                if page >= page_total or not recs:
                    break
                page += 1
            except Exception:
                break
    return sorted(lids)


# ── 3. WebSocket 客户端 ────────────────────────────────────────────────

def _to_str(raw):
    if isinstance(raw, tuple):
        raw = raw[0]
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw


class BBWsPushClient:
    """连 push 域名, subscribe 联赛, 收 G04 赔率推送。回调 on_g04(data)。"""

    def __init__(self, domain, on_g04=None, user_id=2150894):
        self.domain = domain
        self.on_g04 = on_g04
        self.user_id = user_id
        self._n_g04 = 0

    async def run(self, leagues, seconds=0):
        """连接+订阅+收流。seconds>0 = 运行 N 秒(测试), 0 = 常驻。"""
        from curl_cffi.requests import AsyncSession
        url = f"wss://{self.domain}/" if not self.domain.startswith("wss://") else self.domain
        url = url if url.endswith("/") else url + "/"
        print(f"[bb_ws_push] 连接 {url} (impersonate chrome)")
        deadline = time.time() + seconds if seconds else None
        s = AsyncSession(impersonate="chrome150")
        while deadline is None or time.time() < deadline:
            try:
                ws = await s.ws_connect(url, headers={"User-Agent": _UA,
                                                      "Origin": "https://vv899.bbty0vip7.com"},
                                        timeout=12)
                print("[bb_ws_push] 已连接 (101)")
                await ws.send(json.dumps({"cmd": "subscribe", "channel": leagues,
                                          "userId": self.user_id}))
                print(f"[bb_ws_push] 已订阅 {len(leagues)} 联赛")
                while deadline is None or time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    await self._handle(raw)
                await ws.close()
                return
            except Exception as e:
                print(f"[bb_ws_push] 断开({type(e).__name__} {str(e)[:80]}), 5s 后重连")
                await asyncio.sleep(5)
                if deadline and time.time() >= deadline:
                    return
        await s.close()

    async def _handle(self, raw):
        try:
            msg = json.loads(_to_str(raw))
        except Exception:
            return
        cmd = msg.get("cmd")
        if cmd == "G04":
            self._n_g04 += 1
            data = msg.get("data") or {}
            items = "; ".join(f"{i.get('name')}={i.get('value')}" for i in data.get("items", []))
            print(f"[G04 #{self._n_g04}] matchId={data.get('matchId')} {data.get('market')} | {items}")
            if self.on_g04:
                try:
                    self.on_g04(data)
                except Exception as e:
                    print(f"[bb_ws_push] on_g04 回调异常: {e}")
        elif cmd == "hello":
            print("[bb_ws_push] hello (订阅确认)")
        # pong/L03/L04/M01 静默


# ── 4. CDP 抓浏览器现有 WS 的 G04(可靠路径, 绕 TLS 指纹) ───────────────

async def tap_browser_g04(on_g04=None, seconds=0):
    """用 CDP 抓浏览器已建立的 push WebSocket 的 G04 帧。

    浏览器(Chrome 152)的 TLS 指纹才能让服务器推数据, curl_cffi(最高 chrome150)连上但
    收不到任何帧。所以可靠路径是: 挂到浏览器现有 WS 上读 Network.webSocketFrameReceived。
    前提: 独立 Chrome(port 9222)开着且 BB 页面已连上 push WS。
    """
    import urllib.request
    import websockets
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5))
    page_ws = None
    for t in targets:
        if t.get("type") == "page" and "bbty" in t.get("url", ""):
            page_ws = t["webSocketDebuggerUrl"]
    if not page_ws:
        print("[tap] 找不到 BB 页面 target(Chrome 9222 没起? 或没开 BB 页)")
        return
    print(f"[tap] 挂到浏览器 WS: {page_ws}")
    deadline = time.time() + seconds if seconds else None
    async with websockets.connect(page_ws, max_size=2**24) as ws:
        _mid = 0
        async def send(method, params=None):
            nonlocal _mid
            _mid += 1
            await ws.send(json.dumps({"id": _mid, "method": method, "params": params or {}}))
        await send("Network.enable")
        n_g04 = 0
        while deadline is None or time.time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if msg.get("method") != "Network.webSocketFrameReceived":
                continue
            payload = msg["params"]["response"].get("payloadData", "")
            try:
                data = json.loads(payload)
            except Exception:
                continue
            if data.get("cmd") != "G04":
                continue
            n_g04 += 1
            d = data.get("data") or {}
            items = "; ".join(f"{i.get('name')}={i.get('value')}" for i in d.get("items", []))
            print(f"[G04 #{n_g04}] matchId={d.get('matchId')} {d.get('market')} | {items}")
            if on_g04:
                try:
                    on_g04(d)
                except Exception as e:
                    print(f"[tap] on_g04 回调异常: {e}")


def main():
    ap = argparse.ArgumentParser(description="BB 赔率 WebSocket 秒级推送")
    ap.add_argument("--listen", type=int, default=0, help="监听秒数(0=常驻)")
    ap.add_argument("--domain", action="store_true", help="只拿 push 域名并缓存")
    ap.add_argument("--leagues", default="", help="联赛ID逗号分隔(默认从 getList 全量提取)")
    ap.add_argument("--tap", action="store_true", help="用 CDP 抓浏览器现有 WS 的 G04(可靠, 绕 TLS 指纹)")
    args = ap.parse_args()

    if args.tap:
        try:
            asyncio.run(tap_browser_g04(seconds=args.listen))
        except KeyboardInterrupt:
            pass
        return

    if args.domain:
        dom = get_push_domain(force=True)
        print(f"push 域名: {dom or '(获取失败)'}")
        return

    dom = get_push_domain()
    if not dom:
        print("❌ 拿不到 push 域名(Chrome 9222 没起? 或缓存空)")
        sys.exit(1)
    print(f"push 域名: {dom}")

    if args.leagues:
        leagues = [int(x) for x in args.leagues.split(",") if x.strip()]
    else:
        print("从 getList 提取联赛 ID...")
        leagues = get_league_ids()
        print(f"共 {len(leagues)} 个联赛")
    if not leagues:
        print("❌ 无联赛 ID")
        sys.exit(1)

    client = BBWsPushClient(dom)
    try:
        asyncio.run(client.run(leagues, seconds=args.listen))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
