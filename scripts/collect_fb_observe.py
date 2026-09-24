"""FB 体育独立观察库收集器 (2026-09-13)。

拉 FB 滚球 +EV 机会(FB 赔率 vs Pin 公平价) → 存 data/storage/fb_live_paper_bets.json → 结算。

与 BB 观察库(live_paper_bets.json)完全独立:
  - FB 的 match_id 与 BB 各自独立(FB 比赛用 FB 域名 api.5c4r3.com 的 getMatchDetail 结算)。
  - 观察库只收集数据; 实盘下单走 FB_RELEASE_CAPS(2026-09-15 加, 默认只释放独赢客胜1.0-2.0)。
  - 结算口径与 BB 观察库完全一致(含让球按当前比分 since-bet 结算、让球0排除)。

用法:
  .venv312/bin/python scripts/collect_fb_observe.py --once              # 跑一轮
  .venv312/bin/python scripts/collect_fb_observe.py --interval 60       # 每 60s 轮询
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FB_PAPER_FILE = ROOT / "data" / "storage" / "fb_live_paper_bets.json"

# sub + direction → designation(与 second_level_monitor 同口径)
_DESIG = {
    ("1x2", "主"): "主胜", ("1x2", "客"): "客胜", ("1x2", "和"): "和局",
    ("hc", "主"): "让球主胜", ("hc", "客"): "让球客胜",
    ("ou", "大"): "大球", ("ou", "小"): "小球",
}

# 让球0(line=0)BB会void, 不可靠结算, 不进库(与 BB 观察库同策略)
def _is_settleable(desig, line):
    if desig in ("主胜", "客胜", "和局"):
        return True
    if desig in ("让球主胜", "让球客胜"):
        return line is not None and abs(line) > 0.0001
    if desig in ("大球", "小球"):
        return line is not None
    return False


# ── FB 实盘释放(2026-09-15) ──
# FB 手动释放盘口: 独赢客胜低赔 1.0-2.0(观察库 +13.6pp, 连续3天正 edge, 见 live-clv-tracking)。
# 格式 "sport|sm|direction|interval|scope" → cap(试探 100)。与 BB observe_release_caps 同口径。
FB_RELEASE_CAPS = {
    "football|1x2|客|1.0-2.0|live": 100,
}
FB_BET_ENABLED = False   # FB 实盘下单总开关(2026-09-15 关闭: 中心钱包与BB互斥+token不稳定, 先只观察)


def _odds_interval(o):
    if o <= 1.0:
        return "?"
    if o < 2.0:
        return "1.0-2.0"
    if o < 3.0:
        return "2.0-3.0"
    if o < 5.0:
        return "3.0-5.0"
    return ">5.0"


def _fb_release_cap(o):
    """返回 FB 释放的 cap(未释放返回 None)。"""
    sub = o.get("sub")            # "1x2"/"ou"/"hc"
    dr = o.get("direction")       # "主"/"客"/"和"/"大"/"小"
    itv = _odds_interval(o.get("bb_odds", 0) or 0)
    key = f"football|{sub}|{dr}|{itv}|live"
    return FB_RELEASE_CAPS.get(key)


def _bet_score(b):
    """下注瞬间比分 [主,客] → (hb, ab)。无则 (0,0)=全场。"""
    v = b.get("bsc")
    if isinstance(v, list) and len(v) >= 2:
        try:
            return int(v[0]), int(v[1])
        except (ValueError, TypeError):
            pass
    return 0, 0


def _load():
    if FB_PAPER_FILE.exists():
        try:
            return json.loads(FB_PAPER_FILE.read_text())
        except Exception:
            return []
    return []


def _save(data):
    FB_PAPER_FILE.parent.mkdir(parents=True, exist_ok=True)
    FB_PAPER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))


# CLV 追踪(2026-09-14): 与 BB 滚球(second_level_monitor._check_clv)同口径。
# 下注 60s 后复验 Pin 公平价(复用 BB 写入的文件缓存, 零新增 Pin 请求), 算 CLV 写回 clv 字段。
_clv_track = {}  # (match_id, market_id, option_type) -> {bb_odds, ts}


def _do_clv(data, opps):
    """对 60s+ 旧的 _clv_track 记录, 用当前 opps 的 fair 算 CLV, 写回 data。

    CLV = (bb_odds - fair@T+60) / fair@T+60 * 100。正 = 抢到比市场后来定价更优的价格(真 edge);
    负 = 逆向选择。fair 来自 opps(use_file_cache, 复用 BB 公平价文件, 零 Pin 请求)。
    """
    global _clv_track
    now = time.time()
    ready = {k: v for k, v in _clv_track.items() if now - v["ts"] >= 60}
    if not ready:
        return 0
    cur_fair = {(o["bb_match_id"], o.get("market_id"), o.get("option_type")): o.get("fair")
                for o in opps if o.get("fair") and o.get("fair") > 1}
    written = 0
    for (mid, mkt, opt), v in ready.items():
        del _clv_track[(mid, mkt, opt)]
        f1 = cur_fair.get((mid, mkt, opt))
        if not f1:
            continue
        clv = (v["bb_odds"] - f1) / f1 * 100
        for b in data:
            if (str(b.get("match_id")) == str(mid)
                    and str(b.get("market_id")) == str(mkt)
                    and str(b.get("option_type")) == str(opt)):
                if "clv" not in b:
                    b["clv"] = round(clv, 2)
                    written += 1
                break
    return written


def _settle_bet(b):
    """判定单笔输赢(与 second_level_monitor._settle_paper_bets 同口径)。返回 result 或 None。"""
    from src.scrapers.bb_api_fetcher import fetch_bb_match_result
    mid = b.get("match_id")
    if not mid:
        return None
    detail = fetch_bb_match_result(mid, language_type="EN", platform="FB")
    if detail is None or not detail.get("completed"):
        return None
    hs, as_ = detail.get("home_score"), detail.get("away_score")
    if hs is None or as_ is None:
        return None
    desig = b.get("designation", "")
    line = b.get("line")
    if desig == "主胜":
        return "won" if hs > as_ else "lost"
    if desig == "客胜":
        return "won" if as_ > hs else "lost"
    if desig == "和局":
        return "won" if hs == as_ else "lost"
    if desig in ("让球主胜", "让球客胜", "大球", "小球"):
        if line is None:
            return None
        if desig == "让球主胜":
            hb, ab = _bet_score(b)
            diff = ((hs - hb) + line) - (as_ - ab)
        elif desig == "让球客胜":
            hb, ab = _bet_score(b)
            diff = ((as_ - ab) + line) - (hs - hb)
        elif desig == "大球":
            diff = (hs + as_) - line
        else:  # 小球
            diff = line - (hs + as_)
        if abs(diff) < 0.0001:
            return "push"
        return "won" if diff > 0 else "lost"
    return None


def collect_once(threshold=3.0, stake=100):
    """拉一轮 FB +EV 机会, 入库(去重) + 结算。返回 (新增数, 结算数)。"""
    from src.scrapers.pinnacle_live import fetch_live_opportunities_oa, fetch_bb_live_matches

    # 1. FB +EV 机会 + FB live 比分(bsc)。2026-09-24: 旧 Pin 15min缓存已停用,
    #    改用 Betfair 公平价(fetch_live_opportunities_oa, 与 BB 主流程同锚, 不再拉旧Pin)。
    opps = fetch_live_opportunities_oa(threshold=threshold, platform="FB")
    fb_matches = fetch_bb_live_matches(platform="FB")

    data = _load()
    existing = {(b.get("match_id"), b.get("market_id"), b.get("option_type"), b.get("sub"))
                for b in data}

    # CLV: 先处理 60s+ 旧记录(用当前 opps 的 fair 算), 再录新单
    clv_written = _do_clv(data, opps)

    added = 0
    for o in opps:
        sub = o["sub"]
        desig = _DESIG.get((sub, o["direction"]))
        if not desig or not _is_settleable(desig, o.get("line")):
            continue
        mid = o["bb_match_id"]
        key = (mid, o.get("market_id"), o.get("option_type"), sub)
        if key in existing:
            continue
        data.append({
            "ts": time.time(), "match_id": mid,
            "market_id": o.get("market_id"), "option_type": o.get("option_type"),
            "sport": o.get("sport", ""),
            "home": o.get("home", ""), "away": o.get("away", ""),
            "designation": desig, "sub": sub, "line": o.get("line"),
            "bsc": fb_matches.get(mid, {}).get("sc"),
            "bb_odds": o["bb_odds"], "fair": o["fair"], "ev": o["ev"],
            "stake": stake, "settled": False, "result": None, "profit": None,
        })
        existing.add(key)
        _clv_track[(str(mid), str(o.get("market_id")), str(o.get("option_type")))] = {
            "bb_odds": o["bb_odds"], "ts": time.time(),
        }
        added += 1

    # 1b. FB 实盘下单(释放盘口才投, 2026-09-15)
    bet_n = 0
    if FB_BET_ENABLED:
        try:
            from src.betting.bb_auto_bet import place_single_bet, _load_stake_record
        except Exception:
            place_single_bet = None
        if place_single_bet:
            rec = _load_stake_record()
            for o in opps:
                cap = _fb_release_cap(o)
                if cap is None:
                    continue
                mid = o.get("bb_match_id"); mkt = o.get("market_id"); opt = o.get("option_type")
                if not mid or not mkt or not opt:
                    continue
                # 去重: 该盘口已下过注(record_stake 记 (match_id, market_id), FB match_id 独立于 BB)
                if rec.get(str(mid), {}).get(str(mkt), 0.0) > 0:
                    continue
                stake_v = min(stake, cap)
                tag = f"{o.get('home','')} vs {o.get('away','')} {_DESIG.get((o['sub'],o['direction']),'')}"
                code, order_id, msg = place_single_bet(
                    mkt, o["bb_odds"], opt, stake=stake_v,
                    match_id=mid, check_limit=True, verify_price=True, platform="FB")
                if code == 0:
                    bet_n += 1
                    print(f"  ✅ FB 下单成功 {tag} | 注额¥{stake_v} | 订单{order_id}", flush=True)
                else:
                    print(f"  ❌ FB 下单失败({code} {msg}) {tag}", flush=True)

    # 2. 结算(已捕捉超 2h 的未结算样本)
    settled_n = 0
    now = time.time()
    for b in data:
        if b.get("settled") or now - b.get("ts", 0) < 2 * 3600:
            continue
        result = _settle_bet(b)
        if result is None:
            continue
        stake_v = float(b.get("stake", 0))
        odds = float(b.get("bb_odds", 0))
        profit = 0.0 if result == "push" else (stake_v * (odds - 1) if result == "won" else -stake_v)
        b["settled"] = True
        b["result"] = result
        b["profit"] = round(profit, 1)
        settled_n += 1

    if added or settled_n or clv_written:
        _save(data)
    return added, settled_n


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒(默认60)")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--threshold", type=float, default=3.0, help="EV 门槛(默认3%)")
    args = ap.parse_args()

    while True:
        try:
            added, settled = collect_once(threshold=args.threshold)
            n = len(_load())
            settled_total = sum(1 for b in _load() if b.get("settled"))
            print(f"[fb_observe] 新增 {added} 结算 {settled} 总 {n} (已结算 {settled_total})")
        except Exception as e:
            print(f"[fb_observe] 轮询异常: {type(e).__name__} {str(e)[:80]}")
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
