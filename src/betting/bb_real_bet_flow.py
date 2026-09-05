"""BB 全自动下单流程(2026-09-05)。

从 +EV 机会列表 → 匹配 getList 比赛 → 找 marketId → 真实下单 → 钉钉通知。

机会对象字段(来自 bb_ev_push / comparison):
    home_cn / away_cn: 中文队名
    sub_market: 盘口(1x2/hc/ou/dc/ht...)
    designation: 方向(主胜/客胜/和局/大球/让球主胜...)
    _stake: 注额
    bb_odds: 推送时的赔率

用法:
    from src.betting.bb_real_bet_flow import auto_bet_flow
    results = auto_bet_flow(opportunities)
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.bb_auto_bet import (
    read_token, read_domain, place_single_bet, find_market_from_match,
    MAX_MATCH_STAKE, MAX_MARKET_STAKE,
)
from config.settings import DATA_DIR


def _get_all_matches(token, domain):
    """getList 拉全量比赛, 返回 list[record]。"""
    import requests
    import urllib3
    urllib3.disable_warnings()
    s = requests.Session(); s.trust_env = False
    s.proxies = {"http": "", "https": ""}
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36")
    all_recs = []
    for sport_id in (1, 3, 5, 7, 6):  # 足球/篮球/网球/棒球/美足
        try:
            r = s.post(f"{domain}/v1/match/getList",
                       json={"sportId": sport_id, "type": 2, "current": 1, "pageSize": 100,
                             "isPC": True, "languageType": "CMN"},
                       headers={"Content-Type": "application/json", "user-token": token,
                                "User-Agent": UA}, timeout=15, verify=False)
            d = r.json()
            if d.get("code") == 0:
                all_recs.extend(d["data"]["records"])
        except Exception:
            continue
    return all_recs


def _find_match(records, home_cn, away_cn):
    """按中文队名匹配 getList 比赛。返回 record 或 None。"""
    def norm(n):
        return (n or "").strip().replace(" ", "").lower()
    h = norm(home_cn); a = norm(away_cn)
    for m in records:
        ts = m.get("ts") or []
        if len(ts) < 2:
            continue
        mh = norm(ts[0].get("na", "")); ma = norm(ts[1].get("na", ""))
        if (h and a) and (h == mh and a == ma):
            return m
        # 队名可能只给主队名或客队名, 宽松匹配
        if h and (h in mh or mh in h) and a and (a in ma or ma in a):
            return m
    return None


def _sub_market_to_key(sub_market):
    """comparison 的 sub_market → find_market_from_match 的盘口 key。"""
    m = {
        "1x2": "1x2", "ml": "1x2",
        "hc": "hc", "handicap": "hc",
        "ou": "ou", "over_under": "ou",
        "dc": "dc", "double_chance": "dc",
    }
    return m.get(sub_market, sub_market)


def _designation_to_dir(sub_market, designation):
    """designation → find_market_from_match 的方向 key。"""
    d = (designation or "")
    if sub_market == "dc":
        if "和局/客" in d or "客/和" in d:
            return "和局/客"
        if "主/和" in d or "和局/主" in d:
            return "主/和"
        if "主/客" in d:
            return "主/客"
    if "大" in d:
        return "大"
    if "小" in d:
        return "小"
    if "和" in d or "平" in d:
        return "和"
    if "客" in d:
        return "客"
    if "主" in d:
        return "主"
    return None


def auto_bet_flow(opportunities, token=None, domain=None):
    """全自动下单。返回 {成功: [...], 失败: [...]}。"""
    token = token or read_token()
    domain = domain or read_domain()
    if not token:
        return {"success": [], "failed": [], "error": "无 token"}

    records = _get_all_matches(token, domain)
    success, failed = [], []
    sent_dingtalk = []

    for opp in opportunities:
        home = opp.get("home_cn") or opp.get("home", "")
        away = opp.get("away_cn") or opp.get("away", "")
        sub = opp.get("sub_market") or opp.get("_sub_market") or "1x2"
        desig = opp.get("designation", "")
        stake = float(opp.get("_stake") or opp.get("stake") or 10)

        # 匹配比赛
        match = _find_match(records, home, away)
        if not match:
            failed.append({"home": home, "away": away, "reason": "getList 未匹配到比赛"})
            continue
        match_id = match.get("id")

        # 找 marketId
        mk = find_market_from_match(match, _sub_market_to_key(sub), _designation_to_dir(sub, desig))
        if not mk:
            failed.append({"home": home, "away": away, "reason": f"未找到盘口 {sub}/{desig}"})
            continue
        market_id, odds, option_type = mk

        # 下单(含注额上限检查)
        code, order_id, msg = place_single_bet(
            market_id, odds, option_type, stake, token=token, domain=domain, match_id=match_id)

        # 防风控(2026-09-05): 每注之间随机间隔 20~90 秒, 模拟真人看盘思考, 避免秒下多注被风控识别
        import random as _random
        time.sleep(_random.uniform(20, 90))

        if code == 0:
            success.append({
                "home": home, "away": away, "sub_market": sub, "designation": desig,
                "odds": odds, "stake": stake, "order_id": order_id,
            })
            sent_dingtalk.append(f"✅ {home} vs {away} | {desig} @{odds} | 注额¥{stake:.0f} | 订单{order_id}")
        else:
            failed.append({"home": home, "away": away, "reason": f"code={code} {msg}"})

    # 下单成功后发钉钉
    if sent_dingtalk:
        try:
            from config.settings import send_dingtalk
            body = "**已投注比赛**\n\n" + "\n".join(sent_dingtalk)
            send_dingtalk(f"🟦 BB 自动投注 {len(sent_dingtalk)} 注", body)
        except Exception as e:
            pass

    return {"success": success, "failed": failed}


if __name__ == "__main__":
    # 测试: 从 comparison 读 +EV 机会, 自动下单
    import sys
    comp = DATA_DIR / "bb_vs_pinnacle_comparison.json"
    if not comp.exists():
        print("无 comparison 文件"); sys.exit(1)
    data = json.loads(comp.read_text())
    opps = []
    for m in data.get("details", []):
        home = m.get("home_bb_cn") or m.get("home_bb", "")
        away = m.get("away_bb_cn") or m.get("away_bb", "")
        for opp in m.get("opportunities", [])[:2]:  # 只取前2个机会测试
            opps.append({**opp, "home_cn": home, "away_cn": away,
                         "sub_market": "1x2", "_stake": 10})
    print(f"测试 {len(opps)} 个机会")
    res = auto_bet_flow(opps)
    print("成功:", len(res["success"]), "失败:", len(res["failed"]))
    for f in res["failed"][:5]:
        print("  失败:", f)
