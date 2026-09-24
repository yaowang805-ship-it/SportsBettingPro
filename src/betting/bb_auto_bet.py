"""BB 体育真实下单模块（2026-09-05 逆向完成 + 实测下单成功）。

从"BB 无下单接口"到实测 code=0 下单成功, 完整链路见记忆 [[bb-order-api-reverse-20260905]]。

用法:
    from src.betting.bb_auto_bet import place_single_bet
    code, order_id, msg = place_single_bet(market_id, odds, option_type, stake=10)

下单链路:
    getList → marketId(mg.mks.id) + odds(op.od) + optionType(op.ty)
    → 实时读 user-token → POST /v1/order/bet/singlePass

错误码: code=5(参数错) / 14010(token过期) / 3015(盘口关闭) / 0(成功)
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_DOMAIN = "https://api.x-vip8.com"
FB_DOMAIN = "https://api.c7z4.com"  # FB体育真实域名(2026-09-15 从 5c4r3 改 c7z4: 5c4r3 是FB空钱包, c7z4 走中心钱包共享BB余额)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# 单场比赛最大投注额(2026-09-06 用户要求: 400)
MAX_MATCH_STAKE = 400.0
# 单盘口(含重推)最大投注额(2026-09-06 用户要求: 含重推加一起 ≤ 400)
MAX_MARKET_STAKE = 400.0
# 投注额记录文件(按比赛+盘口维度累计, 跨扫描共享)
STAKE_RECORD_FILE = ROOT / "data" / "storage" / "bet_stake_record.json"


def _load_stake_record():
    """读投注额记录 {match_id: {market_id: 累计注额}}。"""
    if STAKE_RECORD_FILE.exists():
        try:
            return json.loads(STAKE_RECORD_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_stake_record(rec):
    STAKE_RECORD_FILE.write_text(json.dumps(rec, ensure_ascii=False))


def check_stake_limit(match_id, market_id, stake):
    """检查注额上限。返回 (是否超限, 原因)。

    单场比赛累计 ≤ 300; 单盘口(含重推)累计 ≤ 300。跨盘口可各 300。
    """
    rec = _load_stake_record()
    match_key = str(match_id)
    market_key = str(market_id)
    match_total = sum(rec.get(match_key, {}).values())
    market_total = rec.get(match_key, {}).get(market_key, 0.0)
    if match_total + stake > MAX_MATCH_STAKE:
        return False, f"单场超限(已投{match_total:.0f}+{stake:.0f}>{MAX_MATCH_STAKE:.0f})"
    if market_total + stake > MAX_MARKET_STAKE:
        return False, f"单盘口超限(已投{market_total:.0f}+{stake:.0f}>{MAX_MARKET_STAKE:.0f})"
    return True, ""


def record_stake(match_id, market_id, stake):
    """下单成功后记录投注额。"""
    rec = _load_stake_record()
    match_key = str(match_id)
    market_key = str(market_id)
    rec.setdefault(match_key, {})
    rec[match_key][market_key] = rec[match_key].get(market_key, 0.0) + stake
    _save_stake_record(rec)


# ── 同场同盘口互斥锁(2026-09-19) ──
# 根因: 早盘 auto_bet_flow 无任何去重; 滚球 market_id 去重粒度太粗(让球 home/away 同线=同一
# market_id 但不同线=不同 market_id, 拦不住不同线的对立方向)。导致「同一盘线正负都下注」
# (让球主↔客、大小 over↔under) + 「同方向多线」重复下注 → 白交双边抽水/过度暴露。
# 规则: 同一 match_id + 同一标准化盘口(sub) 只下一注, 覆盖对立方向 + 同方向多线两种情况。
SUB_RECORD_FILE = ROOT / "data" / "storage" / "bet_sub_record.json"

# 观察库命名 → BB 命名(统一 canonical sub, 早盘/滚球口径一致)
_SUB_CANON = {
    "opportunities": "1x2", "handicap": "hc", "over_under": "ou",
    "double_chance": "dc",
}


def _canonical_sub(sub):
    return _SUB_CANON.get(sub, sub)


def _load_sub_record():
    """读同场同盘口投注记录 {match_id: [sub, ...]}。"""
    try:
        if SUB_RECORD_FILE.exists():
            return json.loads(SUB_RECORD_FILE.read_text())
    except Exception:
        pass
    return {}


def check_sub_market_bet(match_id, sub_market):
    """同一 match + 同一标准化盘口是否已下过注。True=已投(跳过), False=可投。"""
    if match_id is None:
        return False
    rec = _load_sub_record()
    sub = _canonical_sub(sub_market)
    return sub in rec.get(str(match_id), [])


def record_sub_market_bet(match_id, sub_market):
    """下单成功后记录该场该盘口已投(互斥锁落盘)。"""
    if match_id is None:
        return
    rec = _load_sub_record()
    key = str(match_id)
    sub = _canonical_sub(sub_market)
    rec.setdefault(key, [])
    if sub not in rec[key]:
        rec[key].append(sub)
    try:
        SUB_RECORD_FILE.write_text(json.dumps(rec, ensure_ascii=False))
    except OSError:
        pass


# ── 全局投注冷却(跨进程共享) ──
# 早盘(auto_bet_flow) + 滚球(second_level_monitor)是两个独立进程, 各自有随机间隔但互不知晓,
# 可能"同一时间"各下一单, 像机器投注被风控识别。用共享时间戳文件强制全局随机间隔。
GLOBAL_BET_TS_FILE = ROOT / "data" / "storage" / "last_bet_ts.txt"


def global_bet_cooldown(min_s=15.0, max_s=45.0):
    """返回还需等待的秒数(全局随机间隔 min~max)。跨进程读共享时间戳:
    上次任意流程下单距今 < 随机间隔 → 返回还需等待秒数; 否则 0(可下单)。
    下单成功后必须调 record_global_bet()。
    """
    import random as _r
    last = 0.0
    try:
        if GLOBAL_BET_TS_FILE.exists():
            last = float(GLOBAL_BET_TS_FILE.read_text().strip() or 0)
    except Exception:
        pass
    if not last:
        return 0.0
    wait = _r.uniform(min_s, max_s)
    return max(0.0, wait - (time.time() - last))


def record_global_bet():
    """下单成功后记录共享时间戳(全局冷却起点)。"""
    try:
        GLOBAL_BET_TS_FILE.parent.mkdir(parents=True, exist_ok=True)
        GLOBAL_BET_TS_FILE.write_text(str(time.time()))
    except Exception:
        pass


def _read_localstorage():
    """用 applescript 读 Chrome 活动标签的 localStorage(含 user-token/st-domain)。"""
    script = ROOT / "scripts" / "get_h5_token.applescript"
    if not script.exists():
        return {}
    try:
        raw = subprocess.check_output(["osascript", str(script)], text=True, timeout=15)
        return json.loads(raw.strip())
    except Exception:
        return {}


def read_token(platform="BB"):
    """实时读 user-token(下单接口鉴权用, 有效期短, 必须下单瞬间读)。

    platform="BB": 读 .bb_token; platform="FB": 读 .fb_token(独立 token, 避免和 BB 互相覆盖)。
    顺序: token 文件(用户手动配置最新 token) > applescript(Chrome 活动标签)。
    """
    # 1. token 文件(持久化, 分平台)
    tok_file = ROOT / "data" / "storage" / (".bb_token" if platform == "BB" else ".fb_token")
    if tok_file.exists():
        tok = tok_file.read_text().strip()
        if tok and len(tok) > 30:
            return tok
    # 2. applescript 读活动标签(依赖 Chrome 活动标签是对应平台页)
    ls = _read_localstorage()
    return ls.get("user-token", "") or ls.get("st-auth", "")


def read_domain(platform="BB"):
    """读 API 域名。platform="FB" 用固定 FB 域名; BB 动态读 .bb_domain/Chrome/默认。"""
    if platform == "FB":
        return FB_DOMAIN
    dom_file = ROOT / "data" / "storage" / ".bb_domain"
    if dom_file.exists():
        dom = dom_file.read_text().strip()
        if dom:
            return dom.rstrip("/")
    ls = _read_localstorage()
    return ls.get("st-domain", "").rstrip("/") or DEFAULT_DOMAIN


_token_refresh_until = 0.0  # token 刷新冷却(避免频繁扫 LevelDB + 打 API)


def refresh_token(platform="BB"):
    """token 失效(14010)时自动重抓: 扫 Chrome LevelDB 里所有 tt_ token, 找该平台有效的写入文件。

    2026-09-15: BB/FB 的 token 都不稳定(几分钟到几小时就「账号已登出」), 下单前/后失效了要能
    自动从 Chrome 缓存里挖一个新鲜的有效 token, 否则投注一直被打断。60s 冷却防刷。
    返回新 token 或 None(没找到)。
    """
    global _token_refresh_until
    now = time.time()
    if now < _token_refresh_until:
        return None
    _token_refresh_until = now + 60
    dom = "api.infv1.com" if platform == "BB" else "api.c7z4.com"
    tok_file = ROOT / "data" / "storage" / (".bb_token" if platform == "BB" else ".fb_token")
    import re, glob, os
    db = os.path.expanduser('~/Library/Application Support/Google/Chrome/Default/Local Storage/leveldb')
    tokens = set()
    for f in sorted(glob.glob(db + '/*.ldb') + glob.glob(db + '/*.log')):
        try:
            data = open(f, 'rb').read()
        except Exception:
            continue
        for m in re.finditer(rb'tt_[A-Za-z0-9_.]{40,90}', data):
            tokens.add(m.group(0).decode())
    urllib3.disable_warnings()
    for tok in sorted(tokens):
        try:
            r = requests.post(f'https://{dom}/v1/order/new/bet/list',
                json={'languageType': 'CMN', 'isSettled': True, 'current': 1, 'size': 1},
                headers={'Content-Type': 'application/json', 'Authorization': tok, 'User-Agent': _UA},
                timeout=8, verify=False)
            if r.json().get('code') == 0:
                try:
                    tok_file.write_text(tok)
                except Exception:
                    pass
                return tok
        except Exception:
            pass
    return None


def _refresh_bb_page():
    """苹果脚本: 打开 BB 网址 + 清 st-auth + reload, 触发自动登录生成新 token。

    2026-09-24 修(用户纠正): 之前只 reload 活动标签, cookie 过期后停在登录页不会自动登录。
    正确方式 = 打开网址 vv899.bbty0vip7.com(靠持久 cookie 自动登录, 不走账号密码) + 清 st-auth
    + reload 生成新 st-auth。用 AppleScript set URL 模拟用户访问, 不用 CDP page.goto
    (后者触发网易易盾验证码, 见 [[bb-token-self-renew-20260906]])。
    """
    BB_URL = "https://vv899.bbty0vip7.com"
    script = ('tell application "Google Chrome"\n'
              '    activate\n'
              '    set t to active tab of front window\n'
              '    set URL of t to "%s"\n'
              '    delay 10\n'
              '    execute t javascript "localStorage.removeItem(\\"st-auth\\"); localStorage.removeItem(\\"user-token\\"); '
              'localStorage.removeItem(\\"h5-token\\")"\n'
              '    reload t\n'
              '    delay 12\n'
              'end tell\n' % BB_URL)
    try:
        subprocess.check_output(["osascript", "-e", script], text=True, timeout=120)
        return True
    except Exception as e:
        print(f"[auto_renew] 打开网址+清st-auth+reload 失败: {str(e)[:80]}", flush=True)
        return False


def auto_renew_token():
    """自动续期: 苹果脚本读 Chrome 活动标签 st-auth → 测下单接口 → 更新 .bb_token。

    用 osascript(读活动标签)而非 playwright CDP(9222): 后者在 asyncio 异步上下文里会报
    "Playwright Sync API inside async context"(秒级监控的 run() 是 asyncio), 且依赖 9222 端口常开。
    苹果脚本两条都不依赖, 只要 Chrome 活动标签是 BB 页即可。

    2026-09-08 用户要求: 一旦 token 过期, 自动刷新 BB 页面重新登录拿新 token(Chrome 已最小化、
    登录态持久, 刷新自动登录不需要验证码)。
    """
    tok_file = ROOT / "data" / "storage" / ".bb_token"
    dom_file = ROOT / "data" / "storage" / ".bb_domain"

    def _test_save(new_tok, new_dom):
        if not new_tok or len(new_tok) < 30:
            return False
        dom = new_dom or read_domain()
        try:
            r = _session().post(f"{dom}/v1/order/new/bet/list",
                                json={"languageType": "CMN", "isSettled": False, "current": 1, "size": 1},
                                headers={"Content-Type": "application/json", "Authorization": new_tok,
                                         "User-Agent": _UA}, timeout=15, verify=False)
        except Exception:
            return False
        if r.json().get("code") != 0:
            return False
        try:
            tok_file.write_text(new_tok)
            if new_dom:
                dom_file.write_text(new_dom)
        except Exception:
            pass
        return True

    try:
        # 1. 先读当前 st-auth(不刷新)
        ls = _read_localstorage()
        new_tok = ls.get("st-auth", "") or ls.get("user-token", "")
        new_dom = (ls.get("st-domain", "") or "").rstrip("/")
        if _test_save(new_tok, new_dom):
            return True, f"已续期 {new_tok[:20]}..."
        # 2. 失效 → 刷新 BB 页面重新登录(2026-09-08)
        _refresh_bb_page()
        time.sleep(8)  # 等重载 + 自动登录
        ls = _read_localstorage()
        new_tok = ls.get("st-auth", "") or ls.get("user-token", "")
        new_dom = (ls.get("st-domain", "") or "").rstrip("/")
        if _test_save(new_tok, new_dom):
            return True, f"已刷新续期 {new_tok[:20]}..."
        return False, "刷新后 st-auth 仍失效"
    except Exception as e:
        return False, f"续期失败: {type(e).__name__} {str(e)[:60]}"


def fetch_balance():
    """读账户余额(下单接口 Authorization 鉴权, 比 user-token 严格)。返回余额字符串或 None。"""
    tok = read_token(); dom = read_domain()
    if not tok:
        return None
    try:
        r = _session().post(f"{dom}/v1/user/base",
                            json={"languageType": "CMN"},
                            headers={"Content-Type": "application/json", "Authorization": tok,
                                     "User-Agent": _UA}, timeout=15, verify=False)
        d = r.json()
        if d.get("code") == 0:
            return str((d.get("data") or {}).get("bl", ""))
    except Exception:
        pass
    return None


BALANCE_MIN = 1000.0  # 余额低于此值暂停投注(2026-09-19 用户要求), 结算后余额恢复再投
_balance_cache = {"ts": 0.0, "value": None}
_reject_stats = {}  # 下单失败 code 统计(oddsChange=0 被拒率实测, 2026-09-19)


def check_balance_ok(min_balance=BALANCE_MIN):
    """BB 账户余额是否 >= min_balance(60s 缓存, 避免每次下单都拉余额 HTTP)。

    余额 < min_balance → 暂停投注; 读不到余额 → 放行(不误拦)。
    """
    global _balance_cache
    now = time.time()
    if now - _balance_cache["ts"] < 60 and _balance_cache["value"] is not None:
        return _balance_cache["value"] >= min_balance
    ok = True
    try:
        bal = fetch_balance()
        if bal:
            v = float(str(bal).replace(",", "").replace("¥", "").strip())
            _balance_cache = {"ts": now, "value": v}
            ok = v >= min_balance
    except (TypeError, ValueError, OSError):
        pass
    return ok


def _session():
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": "", "https": ""}
    return s


def fetch_current_odds(market_id, match_id, option_type, token=None, domain=None):
    """下注前拉最新赔率(batchBetMatchMarketOfJumpLine)。返回 (最新赔率, smin, smax) 或 None。

    用于下注前验价: 扫描赔率和下单赔率可能不同, 用最新赔率下单并校验漂移。
    """
    token = token or read_token()
    domain = domain or read_domain()
    if not token:
        return None
    body = {
        "languageType": "CMN",
        "isSelectSeries": False,
        "currencyId": 1,
        "betMatchMarketList": [{
            "marketId": market_id,
            "matchId": match_id,
            "type": option_type,
            "oddsType": 1,
        }],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": token,
        "User-Agent": _UA,
        "Origin": "https://pc.x14ff.com",
        "Referer": "https://pc.x14ff.com/",
    }
    try:
        r = _session().post(f"{domain}/v1/order/batchBetMatchMarketOfJumpLine",
                            json=body, headers=headers, timeout=15, verify=False)
        d = r.json()
        if d.get("code") == 0 and d.get("data", {}).get("bms"):
            b = d["data"]["bms"][0]
            cur_odds = b.get("op", {}).get("od")
            smin = b.get("smin")
            smax = b.get("smax")
            return cur_odds, smin, smax
    except Exception:
        pass
    return None


def place_single_bet(market_id, odds, option_type, stake=10.0, token=None, domain=None,
                     match_id=None, check_limit=True, verify_price=True, max_rise_pct=5.0,
                     platform="BB", fair_price=None, min_ev_pct=None, prefetched_odds=None):
    """单关下单。返回 (code, order_id, message)。

    code=0 成功; code=5 参数错; code=14010 token过期; code=3015 盘口关闭;
    code=-3 注额超限; code=-4 赔率逆向漂移超阈值(验价拦截, 逆向选择); code=-1 无 token; code=-2 异常。

    match_id: 比赛 id, 用于注额上限 + 验价。verify_price=False 跳过验价。
    max_rise_pct: 最新赔率比扫描赔率【升高】超过此百分比 → 放弃下单(默认5%)。
        2026-09-14 方向修正: 原逻辑是"跌超阈值放弃", 但方向反了——BB价回落(往Pin靠)=正CLV
        该投(实证 spike 回落赢 +32.5% ROI), BB价升高(背离Pin)=逆向选择该放弃。见 live-clv-tracking-20260914。
    fair_price/min_ev_pct(2026-09-15 补缺口): 传入 Pin 公平价 + EV 阈值。BB 价【回落】时(往Pin靠),
        用回落后的 final_odds 重算 edge=(final_odds-fair)/fair, 跌破 min_ev_pct 就放弃(否则会拿
        stale 价定的注额下到低 edge 上); 同时注额按新 edge 重算(Kelly stake∝edge/(odds-1))。
    platform: "BB"|"FB"(2026-09-15)。FB 用 api.5c4r3.com + .fb_token, 与 BB 独立 session 不冲突。
    """
    token = token or read_token(platform)
    domain = domain or read_domain(platform)
    if not token:
        return -1, None, "无法读取 user-token(Chrome 未登录 BB 或活动标签不对)"

    # 注额上限检查(单场 300 + 单盘口 300, 含重推)
    if check_limit and match_id is not None:
        ok, reason = check_stake_limit(match_id, market_id, stake)
        if not ok:
            return -3, None, reason

    # 下注前验价(2026-09-14 方向修正): 拉最新赔率, 升高超阈值则放弃(逆向选择)。
    # 原"跌超阈值放弃"方向反了: 回落=正CLV该投, 升高=逆向选择该放弃。
    final_odds = odds
    if verify_price and match_id is not None:
        if prefetched_odds is not None:
            cur_odds = prefetched_odds  # 已预拉的 BB 当前赔率(与 Pin 验价并行, 省 1-2s)
        else:
            cur = fetch_current_odds(market_id, match_id, option_type, token, domain)
            cur_odds = cur[0] if cur else None
        if cur_odds and float(odds) > 0:
            rise = (float(cur_odds) - float(odds)) / float(odds) * 100
            if rise > max_rise_pct:
                return -4, None, f"赔率逆向漂移{rise:.1f}%(扫描{odds}→现{cur_odds}), 放弃"
            # 2026-09-15 补缺口: BB 价回落(往 Pin 靠)时, 重算 edge + 注额。之前 stake 是按
            # stale 价定的, 下到回落后的低 edge 上会偏大; 且没重验回落后的 edge 是否还够阈值。
            if (fair_price is not None and min_ev_pct is not None
                    and float(cur_odds) < float(odds) and float(fair_price) > 1):
                edge_final = (float(cur_odds) - float(fair_price)) / float(fair_price) * 100
                if edge_final < min_ev_pct:
                    return -4, None, f"BB回落至{cur_odds}, edge降至{edge_final:.1f}%<{min_ev_pct}%, 放弃"
                edge_orig = (float(odds) - float(fair_price)) / float(fair_price) * 100
                if edge_orig > 0 and float(cur_odds) > 1:
                    stake = stake * (edge_final / edge_orig) * (float(odds) - 1) / (float(cur_odds) - 1)
                    stake = int(round(stake / 10.0) * 10)
            final_odds = cur_odds  # 用最新赔率下单

    body = {
        "languageType": "CMN",
        "singleBetList": [{
            "unitStake": stake,
            "oddsChange": 0,  # 2026-09-19 实测: 0=不接受赔率变动(下单瞬间价格变了就拒), 替代时间验价
            "betOptionList": [{
                "marketId": market_id,
                "odds": final_odds,
                "optionType": option_type,
                "oddsFormat": 1,
            }],
        }],
        "currencyId": 1,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": token,
        "User-Agent": _UA,
        "Origin": "https://pc.7y99z.com" if platform == "FB" else "https://pc.x14ff.com",
        "Referer": "https://pc.7y99z.com/" if platform == "FB" else "https://pc.x14ff.com/",
    }
    for attempt in range(3):
        try:
            r = _session().post(f"{domain}/v1/order/bet/singlePass",
                                json=body, headers=headers, timeout=15, verify=False)
            d = r.json()
            code = d.get("code", -1)
            msg = d.get("message") or ""
            if code == 14010 and attempt == 0:
                # token 失效 → 自动从 Chrome 缓存重抓一个有效的, 重试一次(2026-09-15)
                _new_tok = refresh_token(platform)
                if _new_tok:
                    token = _new_tok
                    headers["Authorization"] = _new_tok
                    continue
            # 二次验价(2026-09-19 用户要求): 下单失败(盘口关闭3015/赔率变等可重试错误,
            # 排除注额超限-3/参数错5) → 重拉实时价, edge 还在则用实时价重试一次, 否则放弃。
            if attempt == 0 and code not in (0, 14010, -3, 5) and fair_price and min_ev_pct is not None:
                _cur = fetch_current_odds(market_id, match_id, option_type, token, domain)
                _new_odds = _cur[0] if _cur else None
                if _new_odds and float(fair_price) > 1:
                    _edge = (float(_new_odds) - float(fair_price)) / float(fair_price) * 100
                    if _edge >= min_ev_pct:
                        # edge 仍在, 用实时价重试下单(二次验价后的成交价)
                        body["singleBetList"][0]["betOptionList"][0]["odds"] = _new_odds
                        final_odds = _new_odds
                        continue
            order_id = None
            if code == 0 and d.get("data"):
                # data[0].id = 订单号
                data = d.get("data") or []
                if isinstance(data, list) and data:
                    order_id = data[0].get("id")
            # 下单成功后记录投注额(供上限检查)
            if code == 0 and match_id is not None:
                record_stake(match_id, market_id, stake)
            if code != 0:
                # 2026-09-19 统计下单失败 code(oddsChange=0 被拒率实测)
                _reject_stats[code] = _reject_stats.get(code, 0) + 1
                print(f"[bb] 下单失败 code={code} msg={str(msg)[:60]} | 累计{_reject_stats}", flush=True)
            return code, order_id, msg
        except Exception as e:
            return -2, None, f"下单异常: {type(e).__name__} {e}"
    return -1, None, "下单失败(重试后仍失败)"


def find_market_from_match(match, sub_market="1x2", direction=None):
    """从 getList 的比赛 dict 里找指定盘口/方向的 marketId+odds+optionType。

    match: getList 返回的 record(dict, 含 mg[]/ts[])
    sub_market: "1x2"(独赢)/"hc"(让球)/"dc"(双重机会)/"ou"(大小球)
    direction: "主"/"客"/"和"/"大"/"小" 或 optionType 数字
    返回 (market_id, odds, option_type) 或 None
    """
    # 盘口 → (mty, pe) 码。mty=盘口类型码(语言无关), pe=period(1001全场/1002半场)。
    # 必须同时匹配 mty+pe, 否则"上半场1x2"(1005+1002)会被误当成"全场1x2"(1005+1001)。
    market_codes = {
        "1x2": [(1005, 1001)],
        "hc": [(1000, 1001)],
        "ou": [(1007, 1001)],
        "dc": [(1012, 1001)],
        "ht": [(1005, 1002)],            # 上半场独赢
        "ht_hc": [(1000, 1002)],         # 上半场让球
        "ht_ou": [(1007, 1002)],         # 上半场大小
        "ht_dc": [(1012, 1002)],         # 上半场双机会
        "htft": [(1033, 1001)],          # 半全场
        "correct_score": [(1099, 1001), (1188, 1001)],   # 正确比分(全场)
        "correct_score_ht": [(1100, 1002), (1188, 1002)],  # 上半场正确比分
        "exact_goals_ht": [(1103, 1002)],  # 上半场精确进球
        "btts": [(1027, 1001)],          # 双边进球
        "corner": [(1009, 1001), (1010, 1001), (1011, 1001)],  # 角球(独赢/大小/让球)
        "oe": [(1008, 1001)],            # 单双
        "winning_margin": [(1018, 1001)],  # 净胜球
        "total_goals": [(1101, 1001)],   # 总进球区间
    }
    target_codes = market_codes.get(sub_market)
    # 方向 → optionType
    dir_map = {
        "主": 1, "客": 2, "和": 3, "大": 4, "小": 5,
        "主/和": 50, "主/客": 51, "和局/客": 52,
    }
    target_ty = dir_map.get(direction) if direction else None

    for mg in match.get("mg", []):
        if target_codes is not None:
            if (mg.get("mty"), mg.get("pe")) not in target_codes:
                continue
        else:
            # 未支持的盘口: 退回名称匹配(兜底)
            nm = (mg.get("nm") or "").lower()
            if sub_market.lower() not in nm:
                continue
        for mk in mg.get("mks", []):
            if mk.get("ss") != 1 or not mk.get("op"):
                continue
            for op in mk.get("op", []):
                if op.get("od", 0) <= 0:
                    continue
                if target_ty is not None:
                    if int(op.get("ty")) == target_ty:
                        return int(mk["id"]), op["od"], int(op["ty"])
                else:
                    # 没指定方向, 返回第一个在售选项
                    return int(mk["id"]), op["od"], int(op["ty"])
    return None


if __name__ == "__main__":
    # 测试: 从 getList 拿一场比赛, 找让球主胜盘口, 下单 10 元
    from src.betting.bb_auto_bet import place_single_bet, find_market_from_match, read_token, read_domain
    tok = read_token()
    dom = read_domain()
    print(f"token: {tok[:40]}... domain: {dom}")
    r = _session().post(f"{dom}/v1/match/getList",
                        json={"sportId": 1, "type": 2, "current": 1, "pageSize": 5,
                              "isPC": True, "languageType": "CMN"},
                        headers={"Content-Type": "application/json", "user-token": tok,
                                 "User-Agent": _UA}, timeout=10, verify=False)
    d = r.json()
    if d.get("code") != 0:
        print("getList 失败:", d.get("code"), d.get("message"))
        sys.exit(1)
    for m in d["data"]["records"]:
        mk = find_market_from_match(m, "hc", "主")
        if mk:
            market_id, odds, opt_ty = mk
            print(f"比赛: {m['ts'][0]['na']} vs {m['ts'][1]['na']} | marketId={market_id} odds={odds} ty={opt_ty}")
            code, oid, msg = place_single_bet(market_id, odds, opt_ty, stake=10)
            print(f"下单: code={code} order_id={oid} msg={msg}")
            break
