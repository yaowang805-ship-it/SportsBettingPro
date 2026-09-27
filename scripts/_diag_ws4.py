import sys, json, time
sys.path.insert(0, '/Users/wangyao/SportsBettingPro')
from playwright.sync_api import sync_playwright

# 从浏览器网页抓到的 subscribe channel（38 个频道 ID，不是联赛 lg.id）
CHANNEL = [401,407,408,409,410,411,412,413,414,426,430,402,403,405,406,424,431,432,
           433,434,435,436,437,438,439,440,443,444,445,451,460,462,453,452,442,441,514]

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
    ctx = browser.contexts[0]
    pg = [x for x in ctx.pages if 'vv899' in x.url or 'bbty' in x.url][0]
    js = '''
    (() => {
        window.__g04 = [];
        window.__g04_n = 0;
        window.__ws_log = [];
        const ws = new WebSocket('wss://pushi32e54ih.h906qtx274jb.com/');
        ws.onopen = () => {
            ws.send(JSON.stringify({cmd:'subscribe', channel:%s, userId:2150894}));
            window.__ws_log.push('sent subscribe');
        };
        ws.onmessage = (e) => {
            try {
                const d = JSON.parse(e.data);
                if (d.cmd === 'G04') {
                    window.__g04.push({market: d.data && d.data.market, matchId: d.data && d.data.matchId, items: d.data && d.data.items});
                    window.__g04_n++;
                } else if (d.cmd === 'hello') {
                    window.__ws_log.push('got hello');
                }
            } catch(err) {}
        };
        ws.onerror = () => { window.__ws_log.push('onerror'); };
        ws.onclose = (e) => { window.__ws_log.push('onclose ' + e.code); };
        return 'ok';
    })()
    ''' % json.dumps(CHANNEL)
    pg.evaluate(js)
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(3)
        log = pg.evaluate('() => window.__ws_log')
        n = pg.evaluate('() => window.__g04_n')
        print(f'  log={log}  G04数={n}')
        if n > 0:
            samples = pg.evaluate('() => window.__g04.slice(0,3)')
            for s in samples:
                print('    G04:', json.dumps(s, ensure_ascii=False)[:200])
            break
    browser.close()
