"""观察库纸面投注结算 — 给 validate 样本补结算结果(won/lost/void)。

背景(2026-08-29): 观察库(clv_tracking.csv source=validate)记录了所有 EV≥2% 机会,
但 stake=0、无结算结果。导致:
  1. 自有标定(compute_self_calibration)只能吃实盘(tracked_bets)那点样本, n 攒不够;
  2. 实盘样本有选择偏差(只在 BB>Pin 时投), 标定的"真实胜率"偏乐观。
本脚本把 validate 样本按每日 ¥20K 虚拟投注额分配(纸面), 再结算出 won/lost/void,
写进 paper_bets.json —— 让观察库有真实胜率, 供自有标定扩面 + 消除选择偏差。

用法:
    python -m src.monitor.paper_settle            # 结算(默认)
    python -m src.monitor.paper_settle --dry-run  # 只预览

结算源: BB getMatchDetail(按 bb_match_id 精确匹配), 拿不到再退回 void(不误判)。
"""
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

TRACKING_FILE = DATA_DIR / "clv_tracking.csv"
PAPER_FILE = DATA_DIR / "paper_bets.json"
DAILY_BUDGET = 20000.0  # 每日虚拟投注额(与实盘日预算一致)

# 赛果窗口: 开赛后至少等这么久才结算(BB 赛果有时效窗口 ~24-48h, 太早可能还没出)
SETTLE_AFTER_HOURS = 2.0
# 增量过滤(2026-08-30): 开赛超过 48h 的样本 BB getMatchDetail 返回空壳(赛果时效窗口),
# 永久跳过, 不再每次 do_settle 都重试老样本(此前 1002 条老样本每次都要调 BB, 拖 settle 到 15min)
SETTLE_MAX_AGE_HOURS = 48.0


def _key(sport, home_pin, away_pin, designation, sub_market, match_epoch):
    return f"{sport}|{home_pin}|{away_pin}|{designation}|{sub_market}|{match_epoch}"


def load_paper_bets() -> dict:
    """加载已有纸面结算结果(键 = key)。"""
    if PAPER_FILE.exists():
        try:
            d = json.loads(PAPER_FILE.read_text())
            return {b["key"]: b for b in d.get("bets", [])}
        except Exception:
            pass
    return {}


def save_paper_bets(bets: list):
    out = {"bets": bets, "generated_at": datetime.now(timezone.utc).isoformat()}
    tmp = PAPER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    tmp.replace(PAPER_FILE)


def _read_validate_rows():
    """读 clv_tracking.csv 里 source=validate 的记录, 去重(按 key)。"""
    rows = {}
    if not TRACKING_FILE.exists():
        return rows
    with open(TRACKING_FILE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("source") != "validate":
                continue
            bid = r.get("bb_match_id", "").strip()
            if not bid:
                continue  # 没有 bb_match_id 无法精确结算, 跳过(老数据)
            epoch = int(r.get("match_epoch") or 0)
            if not epoch:
                continue
            k = _key(r.get("sport"), r.get("home_pin"), r.get("away_pin"),
                     r.get("designation"), r.get("sub_market"), epoch)
            # 同 key 保留最新一条(bb_odds/fair_price 更接近结算时刻)
            rows[k] = r
    return rows


def settle_paper(dry_run: bool = False) -> dict:
    from src.scrapers.bb_api_fetcher import fetch_bb_match_result
    from src.monitor.result_fetcher import determine_result

    validate_rows = _read_validate_rows()
    settled_map = load_paper_bets()

    now = time.time()
    new_settled = 0
    settled_this_run = []

    for k, r in validate_rows.items():
        if k in settled_map:
            continue  # 已结算过
        epoch = int(r.get("match_epoch") or 0)
        if epoch > 0 and (now - epoch) < SETTLE_AFTER_HOURS * 3600:
            continue  # 还没到结算时间(开赛后 2h 才出最终比分)
        if epoch > 0 and (now - epoch) > SETTLE_MAX_AGE_HOURS * 3600:
            continue  # 增量过滤: 开赛超48h BB拿不到赛果(空壳), 永久跳过
        bid = r.get("bb_match_id", "").strip()
        sport = r.get("sport", "")
        sub_market = r.get("sub_market", "")

        # BB getMatchDetail 拿最终比分
        detail = fetch_bb_match_result(bid, language_type="EN")
        if not detail or detail.get("home_score") is None or detail.get("away_score") is None:
            continue  # BB 没返回比分(空壳/过老), 这次跳过, 下次再试
        if detail.get("sport") and sport and detail["sport"] != sport:
            continue  # 跨运动 id 冲突, 不能拿别的运动比分结算

        match_result = {
            "home_score": detail["home_score"],
            "away_score": detail["away_score"],
        }
        if detail.get("ht_home_score") is not None:
            match_result["ht_home_score"] = detail["ht_home_score"]
            match_result["ht_away_score"] = detail["ht_away_score"]
        if detail.get("games_home") is not None:
            match_result["games_home"] = detail["games_home"]
            match_result["games_away"] = detail["games_away"]

        bet = {
            "sport": sport,
            "sub_market": sub_market,
            "designation": r.get("designation", ""),
            "bb_odds": float(r.get("bb_odds") or 0),
        }
        try:
            result, hs, as_, mult = determine_result(bet, match_result)
        except Exception as e:
            logger.warning("判定失败 %s: %s", k, e)
            continue

        bb_odds = float(r.get("bb_odds") or 0)
        stake = _virtual_stake(float(r.get("fair_price") or 0), bb_odds, r.get("tier", "3"))
        if result == "won":
            profit = stake * (bb_odds - 1) * mult
        elif result in ("lost", "half_lost"):
            profit = -stake * abs(mult)
        elif result == "half_won":
            profit = stake * (bb_odds - 1) * mult
        else:  # void
            profit = 0.0

        rec = {
            "key": k,
            "sport": sport,
            "home_pin": r.get("home_pin", ""),
            "away_pin": r.get("away_pin", ""),
            "designation": r.get("designation", ""),
            "sub_market": sub_market,
            "bb_odds": bb_odds,
            "fair_price": float(r.get("fair_price") or 0),
            "ev_pct": float(r.get("ev_pct") or 0),
            "match_epoch": epoch,
            "bb_match_id": bid,
            "result": result,
            "home_score": hs,
            "away_score": as_,
            "stake": round(stake, 2),
            "profit": round(profit, 2),
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "is_paper": True,
            "source": "validate",
        }
        settled_map[k] = rec
        settled_this_run.append(rec)
        new_settled += 1

    if new_settled and not dry_run:
        save_paper_bets(list(settled_map.values()))
        logger.info("纸面结算: 新增 %d 笔, 累计 %d 笔", new_settled, len(settled_map))

    # 打印本次结算汇总
    if settled_this_run:
        by_res = {}
        for r in settled_this_run:
            by_res[r["result"]] = by_res.get(r["result"], 0) + 1
        logger.info("本次纸面结算: %s", by_res)
    return {"new_settled": new_settled, "total_paper": len(settled_map)}


def _virtual_stake(fair_price: float, bb_odds: float, tier) -> float:
    """虚拟注额 — 半凯利, 与实盘同源。fair_price 隐含胜率 × BB 赔率算 edge。

    观察库样本量远大于实盘(8-28: validate 1364 vs push 191), 单注虚拟注额天然偏小,
    这里只按半凯利给"相对权重", 真实 ¥20K/日归一化在 settle 后统一做(避免过早摊薄)。
    """
    if not fair_price or fair_price <= 1.0 or not bb_odds or bb_odds <= 1.0:
        return 0.0
    p = 1.0 / fair_price
    kelly = max(0.0, (p * bb_odds - 1.0) / (bb_odds - 1.0))
    tier_mult = 1.0 if tier in ("1", "2", 1, 2) else 0.7  # T3/T4 降权
    return round(kelly * 0.5 * tier_mult * 1000.0, 2)  # 千元基数的半凯利虚拟注额


def _normalize_daily_budget(dry_run: bool = False):
    """把每天的纸面注额归一化到 ¥20K(让纸面 ROI 与实盘可比)。

    按 _virtual_stake 的相对权重, 每天等比例缩放到总额 ¥20K。
    """
    bets = load_paper_bets()
    if not bets:
        return 0
    by_day = {}
    for b in bets.values():
        sa = (b.get("settled_at") or "")[:10]
        by_day.setdefault(sa, []).append(b)
    changed = 0
    for day, day_bets in by_day.items():
        total_w = sum(b["stake"] for b in day_bets)
        if total_w <= 0:
            continue
        scale = DAILY_BUDGET / total_w
        for b in day_bets:
            old = b["stake"]
            b["stake"] = round(old * scale, 2)
            # profit 同比例缩放(虚拟, 保持 ROI 不变)
            b["profit"] = round(b["profit"] * scale, 2)
            if abs(b["stake"] - old) > 0.01:
                changed += 1
    if changed and not dry_run:
        save_paper_bets(list(bets.values()))
    return changed


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    r = settle_paper(dry_run=dry)
    n = _normalize_daily_budget(dry_run=dry)
    print(json.dumps({"settled": r, "normalized_stakes": n}, ensure_ascii=False, indent=2))
