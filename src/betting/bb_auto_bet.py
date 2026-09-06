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
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_DOMAIN = "https://api.x-vip8.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# 单场比赛最大投注额(2026-09-05 用户要求)
MAX_MATCH_STAKE = 300.0
# 单盘口(含重推)最大投注额
MAX_MARKET_STAKE = 300.0
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


def read_token():
    """实时读 user-token(下单接口鉴权用, 有效期短, 必须下单瞬间读)。

    顺序: .bb_token 文件(用户手动配置最新 token) > applescript(Chrome 活动标签是 BB 页)。
    """
    # 1. .bb_token 文件(持久化, 用户登录后手动更新)
    tok_file = ROOT / "data" / "storage" / ".bb_token"
    if tok_file.exists():
        tok = tok_file.read_text().strip()
        if tok and len(tok) > 30:
            return tok
    # 2. applescript 读活动标签(依赖 Chrome 活动标签是 BB 页)
    ls = _read_localstorage()
    return ls.get("user-token", "") or ls.get("st-auth", "")


def read_domain():
    """读 API 域名(动态, 每次登录可能变)。顺序: .bb_domain 文件 > applescript > 默认。"""
    dom_file = ROOT / "data" / "storage" / ".bb_domain"
    if dom_file.exists():
        dom = dom_file.read_text().strip()
        if dom:
            return dom.rstrip("/")
    ls = _read_localstorage()
    return ls.get("st-domain", "").rstrip("/") or DEFAULT_DOMAIN


def auto_renew_token():
    """自动续期(2026-09-06): playwright 读浏览器 st-auth → 测下单接口 → 更新 .bb_token/.bb_domain。

    返回 (bool, msg)。token 有效期约 11h, 下单返回 14010 时调用。需要独立 Chrome(9222)开着。
    """
    tok_file = ROOT / "data" / "storage" / ".bb_token"
    dom_file = ROOT / "data" / "storage" / ".bb_domain"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            pg = browser.contexts[0].pages[0]
            ls = pg.evaluate("() => { const o={}; for(let i=0;i<localStorage.length;i++)"
                             "{const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }")
            browser.close()
        new_tok = ls.get("st-auth", "") or ls.get("user-token", "")
        new_dom = ls.get("st-domain", "").rstrip("/") or ""
        if not new_tok or len(new_tok) < 30:
            return False, "浏览器 localStorage 无有效 st-auth"
        # 测下单接口(Authorization 严格, code=0 才有效)
        dom = new_dom or read_domain()
        r = _session().post(f"{dom}/v1/order/new/bet/list",
                            json={"languageType": "CMN", "isSettled": False, "current": 1, "size": 1},
                            headers={"Content-Type": "application/json", "Authorization": new_tok,
                                     "User-Agent": _UA}, timeout=15, verify=False)
        if r.json().get("code") != 0:
            return False, "浏览器 st-auth 也失效(code!=0)"
        tok_file.write_text(new_tok)
        if new_dom:
            dom_file.write_text(new_dom)
        return True, f"已续期 {new_tok[:20]}..."
    except Exception as e:
        return False, f"续期失败: {type(e).__name__} {str(e)[:60]}"


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
                     match_id=None, check_limit=True, verify_price=True, max_drop_pct=5.0):
    """单关下单。返回 (code, order_id, message)。

    code=0 成功; code=5 参数错; code=14010 token过期; code=3015 盘口关闭;
    code=-3 注额超限; code=-4 赔率漂移超阈值(验价拦截); code=-1 无 token; code=-2 异常。

    match_id: 比赛 id, 用于注额上限 + 验价。verify_price=False 跳过验价。
    max_drop_pct: 最新赔率比扫描赔率跌超过此百分比 → 放弃下单(默认5%)。
    """
    token = token or read_token()
    domain = domain or read_domain()
    if not token:
        return -1, None, "无法读取 user-token(Chrome 未登录 BB 或活动标签不对)"

    # 注额上限检查(单场 300 + 单盘口 300, 含重推)
    if check_limit and match_id is not None:
        ok, reason = check_stake_limit(match_id, market_id, stake)
        if not ok:
            return -3, None, reason

    # 下注前验价(2026-09-05): 拉最新赔率, 跌超阈值则放弃(临时高价假机会)
    final_odds = odds
    if verify_price and match_id is not None:
        cur = fetch_current_odds(market_id, match_id, option_type, token, domain)
        if cur:
            cur_odds = cur[0]
            if cur_odds and float(odds) > 0:
                drop = (float(odds) - float(cur_odds)) / float(odds) * 100
                if drop > max_drop_pct:
                    return -4, None, f"赔率漂移{drop:.1f}%(扫描{odds}→现{cur_odds}), 放弃"
                final_odds = cur_odds  # 用最新赔率下单

    body = {
        "languageType": "CMN",
        "singleBetList": [{
            "unitStake": stake,
            "oddsChange": 1,
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
        "Origin": "https://pc.x14ff.com",
        "Referer": "https://pc.x14ff.com/",
    }
    try:
        r = _session().post(f"{domain}/v1/order/bet/singlePass",
                            json=body, headers=headers, timeout=15, verify=False)
        d = r.json()
        code = d.get("code", -1)
        msg = d.get("message") or ""
        order_id = None
        if code == 0 and d.get("data"):
            # data[0].id = 订单号
            data = d.get("data") or []
            if isinstance(data, list) and data:
                order_id = data[0].get("id")
        # 下单成功后记录投注额(供上限检查)
        if code == 0 and match_id is not None:
            record_stake(match_id, market_id, stake)
        return code, order_id, msg
    except Exception as e:
        return -2, None, f"下单异常: {type(e).__name__} {e}"


def find_market_from_match(match, sub_market="1x2", direction=None):
    """从 getList 的比赛 dict 里找指定盘口/方向的 marketId+odds+optionType。

    match: getList 返回的 record(dict, 含 mg[]/ts[])
    sub_market: "1x2"(独赢)/"hc"(让球)/"dc"(双重机会)/"ou"(大小球)
    direction: "主"/"客"/"和"/"大"/"小" 或 optionType 数字
    返回 (market_id, odds, option_type) 或 None
    """
    # 盘口名 → 匹配关键词
    name_map = {
        "1x2": ("独赢", "1x2", "胜平负", "输赢"),
        "hc": ("让球", "让分", "handicap"),
        "ou": ("大小", "大/小", "进球数"),
        "dc": ("双重机会", "double chance"),
    }
    keywords = name_map.get(sub_market, (sub_market,))
    # 方向 → optionType
    dir_map = {
        "主": 1, "客": 2, "和": 3, "大": 4, "小": 5,
        "主/和": 50, "主/客": 51, "和局/客": 52,
    }
    target_ty = dir_map.get(direction) if direction else None

    for mg in match.get("mg", []):
        nm = (mg.get("nm") or "").lower()
        if not any(k.lower() in nm for k in keywords):
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
