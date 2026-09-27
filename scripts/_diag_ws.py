import sys, json, time
sys.path.insert(0, '/Users/wangyao/SportsBettingPro')
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
    ctx = browser.contexts[0]
    pg = [x for x in ctx.pages if 'vv899' in x.url or 'bbty' in x.url][0]
    js = '''
    (() => {
        window.__ws_log = [];
        const ws = new WebSocket('wss://pushi32e54ih.h906qtx274jb.com/');
        window.__ws_log.push('creating');
        ws.onopen = () => {
            window.__ws_log.push('onopen readyState=' + ws.readyState);
            ws.send(JSON.stringify({cmd:'subscribe', channel:[11708, 2428], userId:2150894}));
            window.__ws_log.push('sent subscribe');
        };
        ws.onerror = () => { window.__ws_log.push('onerror'); };
        ws.onclose = (e) => { window.__ws_log.push('onclose code=' + e.code + ' reason=' + (e.reason||'')); };
        ws.onmessage = (e) => { window.__ws_log.push('onmessage: ' + e.data.slice(0,120)); };
        window.__ws = ws;
        return 'ok';
    })()
    '''
    pg.evaluate(js)
    time.sleep(8)
    log = pg.evaluate('() => window.__ws_log')
    for l in log:
        print(' ', l)
    rs = pg.evaluate('() => window.__ws ? window.__ws.readyState : "no ws"')
    print('readyState:', rs)
    browser.close()
