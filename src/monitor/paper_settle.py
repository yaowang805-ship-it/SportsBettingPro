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
RESULTS_FILE = DATA_DIR / "clv_results.csv"
PAPER_FILE = DATA_DIR / "paper_bets.json"
DAILY_BUDGET = 20000.0  # 每日虚拟投注额 —— 已弃用(2026-10-10 注额口径修复, 不再做日归一化)
KELLY_FRACTION = 0.5   # 与 second_level_monitor 一致(半凯利)
MAX_STAKE = 300.0      # 与 second_level_monitor 一致(单盘口上限)
BANKROLL_BASE_FILE = DATA_DIR / "bankroll_base.txt"


def _bankroll():
    """本金基准(读 bankroll_base.txt, 缺省¥20000), 与 second_level_monitor._bankroll 同源。"""
    v = 20000.0
    try:
        if BANKROLL_BASE_FILE.exists():
            v = float(BANKROLL_BASE_FILE.read_text().strip() or 0) or 20000.0
    except (OSError, ValueError):
        v = 20000.0
    return v

# 赛果窗口: 开赛后至少等这么久才结算(BB 赛果有时效窗口 ~24-48h, 太早可能还没出)
SETTLE_AFTER_HOURS = 2.0
# 增量过滤(2026-08-30): 开赛超过 48h 的样本 BB getMatchDetail 返回空壳(赛果时效窗口),
# 永久跳过, 不再每次 do_settle 都重试老样本(此前 1002 条老样本每次都要调 BB, 拖 settle 到 15min)
SETTLE_MAX_AGE_HOURS = 48.0
# 退避重试(2026-08-30): BB 对低级别联赛赛果覆盖差(实测 5 条样本 4 条空壳), 空壳的样本
# 24h 内不重试, 尝试 3 次后永久放弃 — 避免每次 do_settle 对上千条空壳样本重复调 BB。
RETRY_BACKOFF_HOURS = 24.0
MAX_ATTEMPTS = 3
ATTEMPTS_FILE = DATA_DIR / ".paper_attempts.json"


def _key(sport, home_pin, away_pin, designation, sub_market, match_epoch):
    return f"{sport}|{home_pin}|{away_pin}|{designation}|{sub_market}|{match_epoch}"


def _normalize_team(name: str) -> str:
    """归一化队名(去变音符号/小写/去 fc/cf 后缀), 供模糊匹配。"""
    import re as _re
    import unicodedata
    if not isinstance(name, str):
        return ""
    name = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    name = name.strip().lower()
    name = _re.sub(r"\bfc\b", "", name)
    name = _re.sub(r"\bcf\b", "", name)
    return name.strip()


def _match_score(score_map: dict, r: dict):
    """跨运动 id 冲突时, 用 getList type=6 的 score_map 按队名匹配比分(支持主客反转)。

    score_map key = (home, away) 队名(CMN中文+EN英文), value = [hs, as]。
    候选: BB中文名(home/away) + Pin英文名(home_pin/away_pin)。
    匹配: 精确 → 归一化子串(双向, 长名≥4 防短名误匹配)。
    返回 (hs, as) 或 None。
    """
    cands = [
        ((r.get("home") or "").strip(), (r.get("away") or "").strip()),
        ((r.get("home_pin") or "").strip(), (r.get("away_pin") or "").strip()),
    ]
    # 1. 精确匹配
    for ch, ca in cands:
        if not ch or not ca:
            continue
        sc = score_map.get((ch, ca))
        if sc is not None:
            return sc[0], sc[1]
        sc = score_map.get((ca, ch))
        if sc is not None:
            return sc[1], sc[0]  # 主客反转
    # 2. 归一化子串匹配(去变音/大小写/fc后缀后双向子串, 长名≥4 防短名误匹配)
    norm_cands = [(_normalize_team(ch), _normalize_team(ca)) for ch, ca in cands if ch and ca]
    norm_map = {(_normalize_team(kh), _normalize_team(ka)): sc for (kh, ka), sc in score_map.items()}
    for ch, ca in norm_cands:
        if not ch or not ca:
            continue
        for (kh, ka), sc in norm_map.items():
            if not kh or not ka:
                continue
            if (len(ch) >= 4 and len(ca) >= 4
                    and (ch in kh or kh in ch) and (ca in ka or ka in ca)):
                return sc[0], sc[1]
            if (len(ch) >= 4 and len(ca) >= 4
                    and (ch in ka or ka in ch) and (ca in kh or kh in ca)):
                return sc[1], sc[0]  # 主客反转
    return None


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


def load_attempts() -> dict:
    """加载 BB 空壳重试状态 {key: {last_attempt, attempts}}。"""
    if ATTEMPTS_FILE.exists():
        try:
            return json.loads(ATTEMPTS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_attempts(att: dict):
    tmp = ATTEMPTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(att))
    tmp.replace(ATTEMPTS_FILE)


def _read_validate_rows():
    """读观察库 source=validate 记录, 去重(按 key)。

    两个源(2026-10-06): clv_tracking.csv(实时跟踪, 有 bb_match_id/line) +
    clv_results.csv(CLV 结果, 覆盖全运动含网球/排球/拳击, 弥补 tracking 被轮转后老记录
    丢失导致结算不到)。clv_results.csv 字段映射: push_fair_price→fair_price。
    """
    rows = {}

    def _feed(r):
        if r.get("source") != "validate":
            return
        bid = (r.get("bb_match_id") or "").strip()
        if not bid:
            return  # 没有 bb_match_id 无法精确结算, 跳过
        epoch = int(r.get("match_epoch") or 0)
        if not epoch:
            return
        k = _key(r.get("sport"), r.get("home_pin"), r.get("away_pin"),
                 r.get("designation"), r.get("sub_market"), epoch)
        if k not in rows:
            rows[k] = r  # 同 key 保留先出现的(tracking 优先, 口径更全)

    if TRACKING_FILE.exists():
        with open(TRACKING_FILE, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                _feed(r)

    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("source") != "validate":
                    continue
                if not (r.get("bb_match_id") or "").strip():
                    continue  # 老 clv_results.csv 没有 bb_match_id, 跳过(无法结算)
                mapped = dict(r)
                mapped["fair_price"] = r.get("push_fair_price", "")
                mapped["ev_pct"] = r.get("push_ev_pct", "")
                _feed(mapped)

    return rows


def settle_paper(dry_run: bool = False) -> dict:
    from src.scrapers.bb_api_fetcher import fetch_bb_match_result
    from src.monitor.result_fetcher import determine_result

    validate_rows = _read_validate_rows()
    settled_map = load_paper_bets()
    attempts = load_attempts()

    # getList type=6 比分(sportId 隔离) — 跨运动 id 冲突(足球id撞乒乓球)时的兜底
    # id_map: BB match id → 比分, 按 sportId 隔离, 不会跨运动撞车(优先精确匹配)
    from src.monitor.bb_score_settle import fetch_bb_scores
    try:
        _score_map, _id_map = fetch_bb_scores()
    except Exception:
        _score_map, _id_map = {}, {}

    now = time.time()
    new_settled = 0
    settled_this_run = []

    for k, r in validate_rows.items():
        if k in settled_map:
            continue  # 已结算过
        # 退避重试: BB 空壳样本 24h 内不重试, 尝试 3 次后永久放弃
        _att = attempts.get(k, {})
        if _att.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        _last = _att.get("last_attempt", 0)
        if _last and (now - _last) < RETRY_BACKOFF_HOURS * 3600:
            continue
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
            # BB 空壳 → 记录失败(退避重试), 避免每次 do_settle 重复调
            _att = attempts.get(k, {})
            attempts[k] = {"last_attempt": now, "attempts": _att.get("attempts", 0) + 1}
            continue
        if detail.get("sport") and sport and detail["sport"] != sport:
            # 跨运动 id 冲突(足球 id 撞乒乓球) → getList type=6(sportId 隔离) id 精确匹配优先
            _sc = _id_map.get(str(bid))
            if _sc is None:
                _sc = _match_score(_score_map, r)  # 队名匹配兜底
            if _sc is None:
                continue  # 兜底也拿不到, 跳过
            match_result = {"home_score": _sc[0], "away_score": _sc[1]}
            # 注意: getList type=6 无半场比分, ht 系列盘口会 void(保守不误判)
        else:
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
            "line": r.get("line", ""),  # 2026-10-06 补线值: 让球/大小缺 line 会全 void, 纸面ROI=0
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
            "league": r.get("league", ""),
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
            "anchor": "betfair",  # 2026-09-19 锚点口径标记: 9-18后 Betfair 公平价
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

    # 持久化退避状态(BB 空壳样本的失败记录), 下次 do_settle 跳过这些样本不再重复调 BB
    if not dry_run and attempts:
        save_attempts(attempts)
    return {"new_settled": new_settled, "total_paper": len(settled_map)}


def _virtual_stake(fair_price: float, bb_odds: float, tier) -> float:
    """虚拟注额 — 与实盘同口径(2026-10-10 修)。

    原用「半凯利 × ¥1000 基数 + tier降权 + 日归一化 ¥20K」, 纸面注额被放大到 ¥18608 且无 cap,
    与实盘(半凯利 × bankroll ¥8000, cap ¥300)不可比 → 纸面 ROI 失真(低量日单注被拉爆)。
    改成与 second_level_monitor._stake_for 同核心公式: bankroll × 半凯利 × edge/(odds-1), cap ¥300。
    (不套 eq_factor/注额抖动/回撤熔断——那些是实盘风控, 纸面只需 edge 口径一致。)
    """
    if not fair_price or fair_price <= 1.0 or not bb_odds or bb_odds <= 1.0:
        return 0.0
    p = 1.0 / fair_price
    edge = max(0.0, p * bb_odds - 1.0)
    stake = _bankroll() * KELLY_FRACTION * edge / (bb_odds - 1.0)
    return round(min(stake, MAX_STAKE), 2)


def _normalize_daily_budget(dry_run: bool = False):
    """已弃用(2026-10-10): 日归一化 ¥20K 会把低量日单注放大到 ¥18608, 与实盘 cap ¥300 不可比,
    导致纸面 ROI 失真。注额口径已改由 _virtual_stake 与实盘同源, 这里不再缩放。"""
    return 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    r = settle_paper(dry_run=dry)
    n = _normalize_daily_budget(dry_run=dry)
    print(json.dumps({"settled": r, "normalized_stakes": n}, ensure_ascii=False, indent=2))
