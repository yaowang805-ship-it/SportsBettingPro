"""CLV 采集器 — 赛前拉取 Pinnacle 收盘赔率，计算真实 CLV。

每次运行时:
1. 读取 clv_tracking.csv 中所有已推送但未采集收盘价的记录
2. 对比赛开始时间在 5-120 分钟内的记录，拉取 Pinnacle 实时赔率
3. 计算真实 CLV = (推送时BB赔率 - 收盘Pinnacle公平价) / 收盘Pinnacle公平价
4. 保存到 clv_results.csv

用法: python3 -m src.monitor.clv_collector
"""
import csv, json, os, sys, time, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

TRACKING_FILE = DATA_DIR / "clv_tracking.csv"
RESULTS_FILE = DATA_DIR / "clv_results.csv"

# 采集窗口：比赛开始前 1 分钟 ~ 360 分钟拉取收盘赔率
# V5.1 修复: 下限从15→1, 捕捉开赛前最后一刻的"真收盘线"
# 开赛后 30 分钟内也采集 (兜底, 防止守护进程重启错过窗口)
CLV_WINDOW_BEFORE_MIN = 1     # 比赛前 1 分钟 (原15, 太严漏掉收盘线)
CLV_WINDOW_AFTER_MAX = 30     # 开赛后 30 分钟内兜底采集 (滚球价作参考)
CLV_WINDOW_BEFORE_MAX = 360  # 比赛前 360 分钟/6小时 (原120)
CLV_MIN_AGE_SECONDS = 300    # 至少推送后 5 分钟才采集 (避免取到同一时刻的赔率)


def _load_existing_results():
    """加载已采集的 CLV 结果，返回 {match_key: result}。"""
    existing = {}
    if RESULTS_FILE.exists():
        try:
            with open(RESULTS_FILE, newline='') as f:
                for r in csv.DictReader(f):
                    key = (r.get("home", ""), r.get("away", ""), r.get("sub_market", ""), r.get("designation", ""))
                    existing[key] = r
        except Exception:
            pass
    return existing


def _load_pending_entries():
    """从 clv_tracking.csv 加载尚未采集收盘价的记录。"""
    if not TRACKING_FILE.exists():
        return []

    existing = _load_existing_results()
    entries = []
    with open(TRACKING_FILE, newline='') as f:
        for r in csv.DictReader(f):
            # 用 BB 中文名 + Pinnacle 英文名组合做 key
            key = (r.get("home", ""), r.get("away", ""), r.get("sub_market", ""), r.get("designation", ""))
            # 也尝试用 Pinnacle 名匹配
            key_pin = (r.get("home_pin", ""), r.get("away_pin", ""), r.get("sub_market", ""), r.get("designation", ""))
            if key not in existing and key_pin not in existing:
                entries.append(r)
    return entries


def _fetch_close_odds(entries):
    """为 pending entries 拉取 Pinnacle 实时赔率作为收盘价。

    只处理比赛在 [now+5min, now+120min] 窗口内的记录。
    Returns: list of dicts with CLV results
    """
    from src.scrapers.pinnacle_markets import get_league_matchups_and_markets
    from src.scrapers.pinnacle_league_map import find_pinnacle_league_ids
    from src.scrapers.pinnacle_api import get_decimal_price

    now_epoch = time.time()
    results = []
    league_cache = {}  # Pinnacle league ID → matchups cache

    # 加载 Pinnacle 联赛结构
    ps_file = DATA_DIR / "pinnacle_league_structure.json"
    if not ps_file.exists():
        logger.warning("无 Pinnacle 联赛结构文件，跳过")
        return results
    ps = json.loads(ps_file.read_text())

    # V5: 从对比文件补全 Pinnacle 队名 (历史追踪数据缺失 home_pin/away_pin)
    _cmp_file = DATA_DIR / "bb_vs_pinnacle_comparison.json"
    _pin_names = {}
    if _cmp_file.exists():
        try:
            _cmp = json.loads(_cmp_file.read_text())
            for det in _cmp.get("details", []):
                key = (det.get("home_bb", ""), det.get("away_bb", ""), det.get("league", ""))
                _pin_names[key] = (det.get("home_pin", ""), det.get("away_pin", ""))
        except: pass
    for e in entries:
        if not e.get("home_pin") or not e.get("away_pin"):
            key = (e.get("home", ""), e.get("away", ""), e.get("league", ""))
            if key in _pin_names:
                e["home_pin"] = _pin_names[key][0]
                e["away_pin"] = _pin_names[key][1]

    # 按联赛分组，减少 API 调用
    by_league = defaultdict(list)
    for e in entries:
        match_epoch = int(e.get("match_epoch", 0))
        if not match_epoch:
            continue

        # V5.1: 采集窗口 = 开赛后30分钟 ~ 开赛前360分钟
        minutes_to_match = (match_epoch - now_epoch) / 60
        if minutes_to_match < -CLV_WINDOW_AFTER_MAX or minutes_to_match > CLV_WINDOW_BEFORE_MAX:
            continue

        # 推送时间必须在比赛前至少 5 分钟
        try:
            push_ts = datetime.fromisoformat(e["timestamp"]).timestamp()
            if now_epoch - push_ts < CLV_MIN_AGE_SECONDS:
                continue
        except (ValueError, KeyError):
            pass

        league = e.get("league", "")
        if league:
            by_league[league].append(e)

    if not by_league:
        logger.info("无比赛在采集窗口内 (%.0f ~ %.0f 分钟前)",
                    CLV_WINDOW_BEFORE_MIN, CLV_WINDOW_BEFORE_MAX)
        return results

    # 对每个联赛拉取 Pinnacle 数据
    sport_map = {"football": "⚽", "basketball": "🏀", "tennis": "🎾", "baseball": "⚾",
                 "american_football": "🏈", "mma": "🥊", "boxing": "👊", "ice_hockey": "🏒"}

    for league, league_entries in by_league.items():
        pin_ids = find_pinnacle_league_ids(league, ps)
        if not pin_ids:
            continue

        for pin_id in pin_ids:
            if pin_id in league_cache:
                matchups = league_cache[pin_id]
            else:
                try:
                    matchups = get_league_matchups_and_markets(pin_id)
                    league_cache[pin_id] = matchups
                    time.sleep(0.3)  # 限速
                except Exception as e:
                    logger.warning("拉取 Pinnacle 联赛 %s (ID=%s) 失败: %s", league, pin_id, e)
                    continue

            if not matchups:
                continue

            # 对每条 pending entry，在 Pinnacle matchup 中找对应比赛
            for e in league_entries:
                bb_home = e.get("home", "").lower().strip()
                bb_away = e.get("away", "").lower().strip()
                pin_home_name = e.get("home_pin", "").lower().strip()  # Pinnacle 英文名
                pin_away_name = e.get("away_pin", "").lower().strip()
                match_epoch = int(e.get("match_epoch", 0))
                sub_market = e.get("sub_market", "")
                designation = e.get("designation", "").lower()

                best_pin = None
                best_score = 0
                for mu in matchups:
                    mu_home = mu.get("home", "").lower().strip()
                    mu_away = mu.get("away", "").lower().strip()

                    # 优先匹配 Pinnacle 英文名（最可靠）
                    if pin_home_name and pin_away_name:
                        if pin_home_name == mu_home and pin_away_name == mu_away:
                            best_pin = mu
                            break

                    # 其次用 BB 中文名子串匹配
                    score = 0
                    if bb_home and (bb_home in mu_home or mu_home in bb_home):
                        score += 1
                    if bb_away and (bb_away in mu_away or mu_away in bb_away):
                        score += 1
                    if pin_home_name and (pin_home_name in mu_home or mu_home in pin_home_name):
                        score += 2
                    if pin_away_name and (pin_away_name in mu_away or mu_away in pin_away_name):
                        score += 2

                    if score > best_score:
                        best_score = score
                        best_pin = mu

                if not best_pin or best_score == 0:
                    continue

                # 提取对应市场的收盘赔率
                close_odds = _extract_market_odds(best_pin, sub_market, designation)
                if close_odds is None:
                    continue

                # 计算真实 CLV
                bb_odds = float(e.get("bb_odds", 0))
                fair_price = float(e.get("fair_price", 0))
                push_ev = float(e.get("ev_pct", 0))

                # 收盘公平价 = 收盘 Pin 赔率 × (1 / total_implied)
                total_implied = sum(1.0 / p for p in close_odds if p and p > 0)
                if total_implied <= 0:
                    continue
                close_fair = round(close_odds[0] * total_implied, 4)

                true_clv = round((bb_odds - close_fair) / close_fair * 100, 2)
                clv_delta = round(true_clv - push_ev, 2)  # 正=赔率朝有利方向移动

                results.append({
                    "collect_time": datetime.now(timezone.utc).isoformat(),
                    "push_time": e.get("timestamp", ""),
                    "match_key": f"{bb_home}|{bb_away}",
                    "sport": e.get("sport", ""),
                    "league": league,
                    "home": e.get("home", ""),
                    "away": e.get("away", ""),
                    "home_pin": e.get("home_pin", ""),
                    "away_pin": e.get("away_pin", ""),
                    "designation": designation,
                    "sub_market": sub_market,
                    "tier": e.get("tier", ""),
                    "bb_odds": bb_odds,
                    "push_fair_price": fair_price,
                    "push_ev_pct": push_ev,
                    "close_pin_odds": close_odds[0],
                    "close_fair_price": close_fair,
                    "close_total_implied": round(total_implied, 4),
                    "true_clv_pct": true_clv,
                    "clv_delta": clv_delta,  # + = 有利, - = 不利
                    "match_epoch": e.get("match_epoch", ""),
                    "minutes_before_match": round((int(e.get("match_epoch", 0)) - time.time()) / 60, 1),
                })

    return results


def _extract_market_odds(pin_matchup, sub_market, designation):
    """从 Pinnacle matchup 中提取对应市场的收盘赔率。
    Returns: list of decimal odds, or None if not found
    """
    if sub_market in ("1x2", "ht"):
        # 独赢/上半场 → moneyline
        for ml in pin_matchup.get("moneyline", []):
            period = ml.get("period", 0)
            if sub_market == "ht" and period != 1:
                continue
            if sub_market == "1x2" and period != 0:
                continue
            odds = []
            for p in ml.get("prices", []):
                from src.scrapers.pinnacle_api import get_decimal_price
                d = get_decimal_price(p)
                if d and 1.01 <= d <= 51.0:
                    odds.append(d)
            if len(odds) >= 2:
                return odds
    elif sub_market == "hc":
        # 让球 → spread
        for sp in pin_matchup.get("spread", []):
            if sp.get("period", 0) != 0:
                continue
            odds = []
            for p in sp.get("prices", []):
                from src.scrapers.pinnacle_api import get_decimal_price
                d = get_decimal_price(p)
                if d and 1.01 <= d <= 51.0:
                    odds.append(d)
            if len(odds) >= 2:
                return odds
    elif sub_market == "ou":
        # 大小球 → total
        for tot in pin_matchup.get("total", []):
            if tot.get("period", 0) != 0:
                continue
            odds = []
            for p in tot.get("prices", []):
                from src.scrapers.pinnacle_api import get_decimal_price
                d = get_decimal_price(p)
                if d and 1.01 <= d <= 51.0:
                    odds.append(d)
            if len(odds) >= 2:
                return odds

    return None


def _save_results(results):
    """追加保存 CLV 结果到 CSV。"""
    if not results:
        return

    fieldnames = [
        "collect_time", "push_time", "match_key", "sport", "league", "home", "away",
        "home_pin", "away_pin",
        "designation", "sub_market", "tier", "bb_odds", "push_fair_price", "push_ev_pct",
        "close_pin_odds", "close_fair_price", "close_total_implied",
        "true_clv_pct", "clv_delta", "match_epoch", "minutes_before_match",
    ]

    file_exists = RESULTS_FILE.exists()
    with open(RESULTS_FILE, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for r in results:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    # V5.1: 同时落库到 clv_data 表 (之前只写CSV, SQLite一直空)
    try:
        from src.storage.database import db
        for r in results:
            try:
                db.record_clv(
                    match_key=r.get("match_key", ""),
                    bookmaker="Pinnacle",
                    market=f"{r.get('sub_market','')}/{r.get('designation','')}",
                    opening=r.get("bb_odds", 0),     # 推送时BB赔率
                    closing=r.get("close_fair_price", 0),  # 收盘公平价
                )
            except Exception:
                pass
    except ImportError:
        pass

    logger.info("保存 %d 条 CLV 结果到 %s + clv_data表", len(results), RESULTS_FILE)


def collect():
    """主入口：采集所有 pending 比赛的收盘赔率并计算 CLV。"""
    logger.info("CLV 采集开始...")

    entries = _load_pending_entries()
    total = len(entries)

    # V5: 统计epoch质量
    valid_epoch = sum(1 for e in entries if int(e.get("match_epoch", 0) or 0) > 100000)
    no_epoch = sum(1 for e in entries if not e.get("match_epoch") or int(e.get("match_epoch", 0) or 0) == 0)
    bad_epoch = total - valid_epoch - no_epoch
    logger.info("pending: %d条 (有效epoch:%d, 无epoch:%d, 异常:%d)", total, valid_epoch, no_epoch, bad_epoch)

    if not entries:
        logger.info("无 pending 记录，跳过")
        return 0

    results = _fetch_close_odds(entries)
    logger.info("采集到 %d 条收盘赔率", len(results))

    _save_results(results)

    # 统计
    if results:
        avg_clv = sum(r["true_clv_pct"] for r in results) / len(results)
        positive = sum(1 for r in results if r["true_clv_pct"] > 0)
        logger.info("CLV 统计: 平均 %.1f%%, 正CLV率 %.0f%% (%d/%d)",
                    avg_clv, positive/len(results)*100, positive, len(results))

    return len(results)


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    collect()
