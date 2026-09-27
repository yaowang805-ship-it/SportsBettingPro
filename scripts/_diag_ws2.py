import sys, json, time, urllib.request, asyncio
sys.path.insert(0, '/Users/wangyao/SportsBettingPro')
import websockets

async def main():
    targets = json.load(urllib.request.urlopen('http://127.0.0.1:9222/json', timeout=5))
    page_ws = None
    for t in targets:
        if t.get('type') == 'page' and ('vv899' in t.get('url','') or 'bbty' in t.get('url','')):
            page_ws = t['webSocketDebuggerUrl']
    async with websockets.connect(page_ws, max_size=2**24) as ws:
        mid = 0
        async def send(m, p=None):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({'id': mid, 'method': m, 'params': p or {}}))
        await send('Network.enable')
        # 触发浏览器网页重新连 WS（reload 会触发新的 WS 连接）
        # 先列出已创建的 WS
        created = []
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            m = msg.get('method')
            if m == 'Network.webSocketCreated':
                u = msg['params']['url']
                created.append(u)
                print('WS created:', u)
    print('共', len(created), '个 WS')

asyncio.run(main())
