"""FB 体育独立观察库收集器 (2026-09-13)。

拉 FB 滚球 +EV 机会(FB 赔率 vs Pin 公平价) → 存 data/storage/fb_live_paper_bets.json → 结算。

与 BB 观察库(live_paper_bets.json)完全独立:
  - FB 的 match_id 与 BB 各自独立(FB 比赛用 FB 域名 api.5c4r3.com 的 getMatchDetail 结算)。
  - 只收集数据、不释放盘口、不下真单。
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
    from src.scrapers.pinnacle_live import fetch_live_opportunities, fetch_bb_live_matches

    # 1. FB +EV 机会 + FB live 比分(bsc)。use_file_cache=True: 复用 BB 进程已写入的
    #    Pin 公平价文件, 不重复拉 Pin markets(避免 FB 每 60s 加 Pin 负载/带宽争抢)。
    opps = fetch_live_opportunities(threshold=threshold, platform="FB", use_file_cache=True)
    fb_matches = fetch_bb_live_matches(platform="FB")

    data = _load()
    existing = {(b.get("match_id"), b.get("market_id"), b.get("option_type"), b.get("sub"))
                for b in data}

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
        added += 1

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

    if added or settled_n:
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
