#!/usr/bin/env python3
"""CLV 归档库回捞 — 把采集窗口错过的 tracking 记录用归档库快照补回来。

背景: clv_collector 只在赛前 1~20 分钟拉 Pinnacle 实时价。窗口期一旦遇到
Pinnacle SSL/Cloudflare 抽风(实测两天 52 次 handshake failed), 这批比赛的收盘价
就永久丢失, 没有任何补救 —— 实测 175 条已开赛记录里丢了 105 条(成功率 40%)。

本模块用本地 pinnacle_odds_archive.db(375 万条扫描快照)取「赛前最后一个快照」
补算 CLV, **零 Pinnacle 请求**, 不影响增量扫描推送。

口径与 clv_collector 完全一致: 直接复用 _extract_market_odds 做去抽水,
不另写一套公式, 避免两条链路口径漂移。

收盘价来源标记(写入 clv_results.csv 的 close_source 列):
    live         — 采集器窗口内实时拉的 Pin 价(最准)
    archive      — 归档库快照, 距开赛 <= MAX_LAG_MIN 分钟, 可当收盘价用
    archive_open — 归档库快照距开赛过远, **不是收盘价**, 默认不进 CLV 统计

注意让球/大小球的归档局限: odds_archive 有 UNIQUE(matchup_id, market_type,
designation, period, points) + INSERT OR IGNORE, 同一条线只留首见价。所以线没动过
的比赛只有开盘价(会被判成 archive_open); 独赢(points 为 NULL, SQLite 的 UNIQUE
不约束 NULL)则每次扫描都插一条, 有完整时间序列, 实测单场可达 148 个快照。

用法:
    .venv312/bin/python -m src.monitor.clv_archive_recover            # 演练, 只报告
    .venv312/bin/python -m src.monitor.clv_archive_recover --write    # 写入 clv_results.csv
"""
import argparse
import csv
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

from config.settings import DATA_DIR
from config.logging_config import get_logger
from src.monitor.clv_collector import (
    RESULTS_FILE, TRACKING_FILE, _extract_market_odds, _infer_sub_market, _save_results,
)

logger = get_logger(__name__)

ARCHIVE_DB = DATA_DIR / "pinnacle_odds_archive.db"

# 快照距开赛超过这个分钟数就不算收盘价, 标 archive_open 不进统计
MAX_LAG_MIN = 60
# 只回捞已开赛的记录(留 1 分钟余量, 与采集器 CLV_WINDOW_BEFORE_MIN 对齐)
STARTED_MARGIN_MIN = 1
# 归档库开赛时间 vs tracking 开赛时间的最大容差, 超出判为上游场次错配
KICKOFF_TOLERANCE_MIN = 15


def _key(r):
    return (r.get("home", "").strip(), r.get("away", "").strip(),
            r.get("sub_market", "").strip(), r.get("designation", "").strip())


def _load_missed():
    """已开赛、但 clv_results.csv 里没有收盘价的 tracking 记录。"""
    if not TRACKING_FILE.exists():
        return []
    done = set()
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, encoding="utf-8-sig") as f:
            done = {_key(r) for r in csv.DictReader(f)}
    now = time.time()
    missed, seen = [], set()
    with open(TRACKING_FILE, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            ep = int(r.get("match_epoch") or 0)
            if not ep or (ep - now) / 60 >= STARTED_MARGIN_MIN:
                continue  # 没开赛的交给实时采集器, 别抢
            k = _key(r)
            if k in done or k in seen:
                continue
            seen.add(k)
            missed.append(r)
    return missed


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _load_snapshots(conn, matchup_id):
    """读一场比赛的全部归档快照, 按 (period, fetched_at) 归组。

    Returns: {period: {fetched_at: [row, ...]}}, kickoff_epoch
    """
    rows = conn.execute(
        "SELECT designation, period, points, price, fetched_at, match_start "
        "FROM odds_archive WHERE matchup_id = ? ORDER BY fetched_at", (matchup_id,)
    ).fetchall()
    kickoff = None
    for r in rows:
        kickoff = _parse_ts(r[5])
        if kickoff:
            break

    # 重建"开赛时刻的盘面": 每个 (period, designation, points) 取赛前最后一个价格。
    #
    # V5.10 重要变更: 归档库重建后只存**价格变化点**(scripts/rebuild_odds_archive.py),
    # 同一时刻只会写入发生变动的那条腿, 因此不再存在"某个 fetched_at 上凑齐全部腿"
    # 的完整快照 —— 原先按单一时间戳取快照的做法在重建后会取不到数据。
    # 正确做法是按 key 各自取赛前最后值来还原盘面: 价格三小时没变不代表它不是
    # 收盘价, 恰恰说明它一直有效。
    state = defaultdict(dict)   # period -> {(des, points): price}
    last_seen = None            # 赛前最后一次"观测到这场比赛"的时刻(不管价格变没变)
    for des, period, points, price, fetched_at, _ms in rows:
        ts = _parse_ts(fetched_at)
        if ts is None or (kickoff and ts > kickoff):
            continue            # 开赛后的是滚球价, 不能当收盘价
        state[period or 0][(des, points)] = price
        if last_seen is None or ts > last_seen:
            last_seen = ts
    return state, kickoff, last_seen


def _build_matchup(snap_rows):
    """把归档行拼成 _extract_market_odds 认识的 pin_matchup 结构。

    归档库 price 列存的是**十进制**赔率, 必须写进 price_decimal 键 —
    get_decimal_price 会把 price 键当美式赔率解析, 用错键会得到完全错误的赔率。
    """
    ml, spreads, totals = {}, defaultdict(dict), defaultdict(dict)
    for des, points, price in snap_rows:
        if not price or price <= 1:
            continue
        cell = {"designation": des, "points": points, "price_decimal": float(price)}
        if points is None:
            ml[des] = cell
        elif des in ("over", "under"):
            totals[points][des] = cell
        elif des in ("home", "away"):
            spreads[points][des] = cell

    mu = {}
    order = [d for d in ("home", "draw", "away") if d in ml]
    if len(order) >= 2:
        mu["moneyline"] = [{"prices": [ml[d] for d in order]}]
    mu["spread"] = [{"period": 0, "prices": [v["home"], v["away"]]}
                    for v in spreads.values() if "home" in v and "away" in v]
    mu["total"] = [{"period": 0, "prices": [v["over"], v["under"]]}
                   for v in totals.values() if "over" in v and "under" in v]
    return mu


def recover(write=False):
    if not ARCHIVE_DB.exists():
        logger.error("归档库不存在: %s", ARCHIVE_DB)
        return []
    missed = _load_missed()
    if not missed:
        logger.info("没有需要回捞的记录")
        return []

    conn = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    cache = {}
    results = []
    mismatches = []
    stats = defaultdict(int)

    for e in missed:
        mid = (e.get("pin_match_id") or "").strip()
        if not mid:
            stats["跳过_无pin_match_id"] += 1
            continue
        if mid not in cache:
            cache[mid] = _load_snapshots(conn, mid)
        by_period, kickoff = cache[mid]
        if not by_period:
            stats["跳过_归档库无此场"] += 1
            continue

        # 护栏: pin_match_id 指向的比赛开赛时间必须和 tracking 记的对得上。
        # 对不上说明上游 BB↔Pinnacle 配错了场次(实测有女双配到单打的案例),
        # 这种输入算出来的 CLV 是纯噪声(见过 +67% 的假 edge), 宁可不出数也不出假数。
        track_epoch = int(e.get("match_epoch") or 0)
        if kickoff is not None and track_epoch:
            if abs(kickoff - track_epoch) > KICKOFF_TOLERANCE_MIN * 60:
                stats["跳过_开赛时间不符(上游BB↔Pin错配)"] += 1
                mismatches.append((e, round((kickoff - track_epoch) / 60)))
                continue
        if kickoff is None:
            kickoff = track_epoch

        sub_market = _infer_sub_market(e.get("sub_market", ""), e.get("designation", ""))
        period = 1 if sub_market.startswith("ht") else 0
        snaps = by_period.get(period) or by_period.get(0) or {}
        if not snaps:
            stats["跳过_无对应半场数据"] += 1
            continue

        # 赛前最后一个能算出公平价的快照 —— 从最靠近开赛的往前退
        candidates = sorted(
            ((ts, _parse_ts(ts)) for ts in snaps), key=lambda x: x[1] or 0, reverse=True
        )
        close_data, lag = None, None
        for ts, epoch in candidates:
            if epoch is None or epoch > kickoff:
                continue  # 开赛后的快照是滚球价, 不能当收盘价
            mu = _build_matchup(snaps[ts])
            # ht_* 盘口的数据在 period=1, 这里已按 period 取好, 直接喂全场键
            probe = sub_market[3:] if sub_market.startswith("ht_") else sub_market
            probe = {"ht": "1x2"}.get(sub_market, probe)
            close_data = _extract_market_odds(mu, probe, e.get("designation", ""),
                                              sport=e.get("sport", "football"))
            if close_data:
                lag = (kickoff - epoch) / 60
                break
        if not close_data:
            stats["跳过_归档库无此盘口"] += 1
            continue

        close_pin_odds, close_fair, total_implied = close_data
        try:
            bb_odds = float(e.get("bb_odds", 0))
            push_ev = float(e.get("ev_pct", 0))
        except (TypeError, ValueError):
            stats["跳过_赔率字段异常"] += 1
            continue
        if not bb_odds or not close_fair:
            stats["跳过_赔率为零"] += 1
            continue

        true_clv = round((bb_odds - close_fair) / close_fair * 100, 2)
        src = "archive" if lag is not None and lag <= MAX_LAG_MIN else "archive_open"
        stats[f"回捞_{src}"] += 1

        results.append({
            "collect_time": datetime.now(timezone.utc).isoformat(),
            "push_time": e.get("timestamp", ""),
            "match_key": f"{e.get('home','')}|{e.get('away','')}",
            "sport": e.get("sport", ""), "league": e.get("league", ""),
            "home": e.get("home", ""), "away": e.get("away", ""),
            "home_pin": e.get("home_pin", ""), "away_pin": e.get("away_pin", ""),
            "designation": e.get("designation", ""), "sub_market": sub_market,
            "tier": e.get("tier", ""), "bb_price_source": e.get("bb_price_source", ""),
            "source": e.get("source", "push"),
            "bb_odds": bb_odds, "push_fair_price": e.get("fair_price", ""),
            "push_ev_pct": push_ev,
            "close_pin_odds": close_pin_odds, "close_fair_price": close_fair,
            "close_total_implied": round(total_implied, 4),
            "true_clv_pct": true_clv, "clv_delta": round(true_clv - push_ev, 2),
            "match_epoch": e.get("match_epoch", ""),
            "minutes_before_match": round(lag, 1) if lag is not None else "",
            "close_source": src, "close_lag_min": round(lag, 1) if lag is not None else "",
        })
    conn.close()

    logger.info("回捞候选 %d 条 → 成功 %d 条", len(missed), len(results))
    for k in sorted(stats):
        logger.info("  %s: %d", k, stats[k])

    if mismatches:
        logger.warning("⚠️ %d 条被判上游场次错配(BB↔Pinnacle 配错比赛), 这些机会的 EV 本身就是假的:",
                       len(mismatches))
        for e, diff in mismatches[:10]:
            logger.warning("   %s | BB: %s vs %s | Pin: %s vs %s | 开赛差 %+d 分 | EV %s%%",
                           e.get("league", "")[:34], e.get("home", "")[:16], e.get("away", "")[:16],
                           e.get("home_pin", "")[:16], e.get("away_pin", "")[:16], diff, e.get("ev_pct"))

    usable = [r for r in results if r["close_source"] == "archive"]
    if usable:
        clvs = [r["true_clv_pct"] for r in usable]
        logger.info("可用样本(archive) %d 条: 中位CLV %+.2f%%, 正CLV率 %.0f%%, 快照距开赛中位 %.0f 分",
                    len(usable), statistics.median(clvs),
                    sum(1 for c in clvs if c > 0) / len(clvs) * 100,
                    statistics.median([r["close_lag_min"] for r in usable]))

    if write and results:
        # 只有真收盘价(archive)进 clv_results.csv —— 这个文件的语义是"有收盘价的样本",
        # 掺进开盘价会让所有 CLV 统计失真。archive_open 单独落诊断文件, 等归档器
        # 支持让球/大小球时间序列后可以重新回捞(不写进主表就不会被 _load_missed 判成已完成)。
        _save_results(usable)
        opens = [r for r in results if r["close_source"] == "archive_open"]
        if opens:
            path = DATA_DIR / "clv_archive_open_pending.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(opens[0].keys()))
                w.writeheader()
                w.writerows(opens)
            logger.info("另有 %d 条只有开盘价(非收盘价), 未进主表, 存至 %s", len(opens), path.name)
    elif results:
        logger.info("演练模式, 未写入。加 --write 落盘。")
    return results


def main():
    from config.logging_config import setup_logging
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写入 clv_results.csv")
    recover(write=ap.parse_args().write)


if __name__ == "__main__":
    main()
