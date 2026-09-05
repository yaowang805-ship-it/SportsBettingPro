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
    """读 API 域名(动态, 每次登录可能变)。"""
    ls = _read_localstorage()
    return ls.get("st-domain", "").rstrip("/") or DEFAULT_DOMAIN


def _session():
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": "", "https": ""}
    return s


def place_single_bet(market_id, odds, option_type, stake=10.0, token=None, domain=None):
    """单关下单。返回 (code, order_id, message)。

    code=0 成功; code=5 参数错; code=14010 token过期; code=3015 盘口关闭。
    """
    token = token or read_token()
    domain = domain or read_domain()
    if not token:
        return -1, None, "无法读取 user-token(Chrome 未登录 BB 或活动标签不对)"

    body = {
        "languageType": "CMN",
        "singleBetList": [{
            "unitStake": stake,
            "oddsChange": 1,
            "betOptionList": [{
                "marketId": market_id,
                "odds": odds,
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
