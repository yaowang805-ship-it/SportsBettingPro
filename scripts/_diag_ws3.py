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
        # reload 触发浏览器网页重新连 WS + 发 subscribe
        await send('Page.reload')
        sent = []
        received_cmds = []
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            m = msg.get('method')
            if m == 'Network.webSocketFrameSent':
                payload = msg['params']['response'].get('payloadData','')
                # 只关注 subscribe / 含 cmd 的帧
                if 'subscribe' in payload or 'cmd' in payload:
                    sent.append(payload[:300])
            elif m == 'Network.webSocketFrameReceived':
                payload = msg['params']['response'].get('payloadData','')
                try:
                    d = json.loads(payload)
                    cmd = d.get('cmd','?')
                    received_cmds.append(cmd)
                except Exception:
                    received_cmds.append('nonjson')
    print('浏览器发的帧(含 subscribe/cmd):')
    for s in sent[:5]:
        print('  SENT:', s[:200])
    from collections import Counter
    print('浏览器收到的 cmd 分布:', dict(Counter(received_cmds)))

asyncio.run(main())
