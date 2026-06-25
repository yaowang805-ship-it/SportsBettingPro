"""+EV 机会监控器 — 定时扫描 Line Shopping 机会，发现即推送钉钉。

两种模式:
  单次扫描:  python3 -m src.monitor.ev_monitor
  持续监控:  python3 -m src.monitor.ev_monitor --loop --interval 1800

集成进 main.py:
    from src.monitor.ev_monitor import scan_and_notify
    n = scan_and_notify()
"""
import json
import time
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR, DINGTALK_WEBHOOK
from src.betting.line_shopping import LineShoppingScanner
from src.core.team_names import cn_team
from src.monitor.clv_ls import track_pending_snapshots, send_clv_report

logger = get_logger(__name__)

SEEN_FILE = DATA_DIR / "ev_seen.json"

OUTCOME_CN = {"home": "🏠 主胜", "draw": "⚖️ 平局", "away": "✈️ 客胜",
               "over": "大", "under": "小",
               "yes": "双方进球", "no": "不进球"}


def _load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except Exception:
            pass
    return {"seen": {}, "notified_count": 0}


def _save_seen(seen: dict):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2))


def _fingerprint(opp: dict) -> str:
    return f"{opp.get('league', '')}|{opp['home_team']}|{opp['away_team']}|{opp['outcome']}"


MARKET_TAG = {"1x2": "独赢", "over_under": "大小", "btts": "进球",
               "double_chance": "双边", "draw_no_bet": "无平", "corners_1x2": "角球"}

OUTCOME_CN_MAP = {"home": "主胜", "draw": "平局", "away": "客胜",
                   "over": "大", "under": "小",
                   "yes": "双方进球", "no": "不进球"}

DAILY_BUDGET = 10000.0
MAX_PER_BET = 2000.0
MAX_PER_MATCH = 3500.0


def _outcome_label(opp: dict) -> str:
    """获取中文结果标签。"""
    label = opp.get("outcome_label", "")
    if label:
        return label
    return OUTCOME_CN_MAP.get(opp.get("outcome", ""), opp.get("outcome", ""))


def _calc_stakes(opps: List[dict]) -> List[dict]:
    """估算每笔投注的 ¥ 金额（与 place_line_shops 逻辑一致）。"""
    vp_file = DATA_DIR / "virtual_portfolio.json"
    today_str = datetime.now().strftime("%Y-%m-%d")
    allocated_today = 0.0
    if vp_file.exists():
        try:
            vp = json.loads(vp_file.read_text())
            for b in vp.get("pending_bets", []):
                ct = b.get("created_at", "")
                if today_str in ct:
                    allocated_today += b.get("stake", 0)
        except Exception:
            pass
    budget = max(0, DAILY_BUDGET - allocated_today)
    if budget < 100:
        return [{**o, "stake": 0} for o in opps]

    scan_budget = round(budget * 0.30, 0)
    scan_budget = max(scan_budget, 100)
    scan_budget = min(scan_budget, budget)

    candidates = []
    for o in opps:
        ev = o.get("_ev", 0)
        odds = o.get("odds", 0)
        model_prob = o.get("model_prob", 0)
        if ev < 0.03 or odds <= 1 or model_prob <= 0:
            candidates.append({**o, "stake": 0})
            continue
        b_val = odds - 1.0
        kelly = (model_prob * b_val - (1.0 - model_prob)) / b_val if b_val > 0 else 0
        if kelly <= 0:
            candidates.append({**o, "stake": 0})
            continue
        candidates.append({**o, "_kelly": kelly})

    valid = [c for c in candidates if "_kelly" in c]
    raw = [min(c["_kelly"] * 0.25, MAX_PER_BET / scan_budget) for c in valid]
    total_raw = sum(raw)

    result = []
    i = 0
    for c in candidates:
        if "_kelly" in c:
            if total_raw > 0:
                stake = min(round(scan_budget * raw[i] / total_raw, 0), MAX_PER_BET)
            else:
                stake = 0
            i += 1
            result.append({**c, "stake": int(stake)})
        else:
            result.append(c)

    match_total = {}
    for r in result:
        key = f"{r.get('home_team', '')}_{r.get('away_team', '')}"
        match_total[key] = match_total.get(key, 0) + r.get("stake", 0)
    for r in result:
        key = f"{r.get('home_team', '')}_{r.get('away_team', '')}"
        if match_total.get(key, 0) > MAX_PER_MATCH:
            ratio = MAX_PER_MATCH / match_total[key]
            new_stake = round(r["stake"] * ratio, 0)
            match_total[key] -= r["stake"] - new_stake
            r["stake"] = int(new_stake)

    return result


def _build_dingtalk_body(opps: List[dict]) -> str:
    """构建钉钉推送的 Markdown 正文（纯函数，不含网络调用）。"""
    opps = _calc_stakes(opps)
    now = datetime.now(timezone.utc)
    lines = []
    lines.append(f"📊 投注推荐 {now.strftime('%m/%d %H:%M')}")
    lines.append("")

    for i, opp in enumerate(opps[:20], 1):
        oc = _outcome_label(opp)
        _sport = "nba" if opp.get("sport") in ("nba", "basketball") or "nba" in opp.get("league", "").lower() else "football"
        h_cn = cn_team(opp['home_team'], _sport)
        a_cn = cn_team(opp['away_team'], _sport)
        fair = round(1.0 / opp['model_prob'], 2)
        tag = MARKET_TAG.get(opp.get("market", "1x2"), opp.get("market", ""))

        ct = opp.get("commence_time", "")
        time_tag = ""
        if ct:
            try:
                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                hours = (dt - now).total_seconds() / 3600
                if hours > 48:
                    time_tag = f" [{hours:.0f}h后]"
                elif hours > 24:
                    time_tag = f" [{hours:.0f}h后]"
                elif hours > 1:
                    time_tag = f" [{hours:.0f}h后]"
                elif hours > 0:
                    time_tag = f" [{int(hours*60)}分钟后]"
                else:
                    time_tag = " [已开赛]"
            except Exception:
                pass

        stake_str = f" ¥{opp['stake']:.0f}" if opp.get("stake", 0) > 0 else ""

        lines.append(f"{i}. {h_cn} vs {a_cn}{time_tag}")
        lines.append(f"   {tag} {oc} | 公平价 {fair}{stake_str} | +{opp['edge_pct']}%")

    waiting = 0
    for opp in opps:
        ct = opp.get("commence_time", "")
        if ct:
            try:
                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if (dt - now).total_seconds() / 3600 > 30:
                    waiting += 1
            except Exception:
                pass

    lines.append("")
    lines.append(f"日预算 ¥{DAILY_BUDGET:.0f} | 共 {len(opps)} 条机会")
    if waiting:
        lines.append(f"⏳ {waiting} 条 >30h 两段式等待中")

    return "\n".join(lines)


def _send_dingtalk(opps: List[dict]):
    if not DINGTALK_WEBHOOK:
        logger.info("  未配置钉钉 Webhook，跳过推送")
        return

    body = _build_dingtalk_body(opps)
    title = f"+EV {len(opps)}条"

    try:
        import requests
        resp = requests.post(DINGTALK_WEBHOOK, json={
            "msgtype": "markdown",
            "markdown": {"title": title, "text": body},
        }, timeout=10)
        logger.info("  钉钉推送完成: %s", resp.status_code)
    except Exception as e:
        logger.warning("  钉钉推送失败: %s", e)


def scan_and_notify(force_notify: bool = False) -> int:
    """执行单次扫描并推送新发现的 +EV 机会。

    Args:
        force_notify: 强制推送所有机会（忽略已通知记录）

    Returns:
        新增的机会数量
    """
    scanner = LineShoppingScanner()
    opps = scanner.scan()

    if not opps:
        logger.info("  ⏭️ 无 +EV 机会")
        return 0

    scanner.save_results()

    # 记录 Pinnacle 赔率快照（用于 CLV 追踪）
    try:
        track_pending_snapshots(opps)
    except Exception as e:
        logger.warning("  CLV 快照记录失败: %s", e)

    seen = _load_seen()

    if force_notify:
        _send_dingtalk(opps)
        for opp in opps:
            seen["seen"][_fingerprint(opp)] = {
                "edge": opp["edge_pct"],
                "notified_at": datetime.now(timezone.utc).isoformat(),
            }
        seen["notified_count"] = len(opps)
        _save_seen(seen)
        return len(opps)

    new_opps = []
    for opp in opps:
        fp = _fingerprint(opp)
        if fp not in seen["seen"]:
            new_opps.append(opp)

    if not new_opps:
        logger.info("  无新 +EV 机会（全部已通知过）")
        return 0

    logger.info("  新机会: %d 条 (共 %d 条)", len(new_opps), len(opps))
    _send_dingtalk(new_opps)

    for opp in new_opps:
        seen["seen"][_fingerprint(opp)] = {
            "edge": opp["edge_pct"],
            "notified_at": datetime.now(timezone.utc).isoformat(),
        }
    seen["notified_count"] = seen.get("notified_count", 0) + len(new_opps)
    _save_seen(seen)

    return len(new_opps)


def run_loop(interval: int = 1800):
    logger.info("=" * 60)
    logger.info("+EV 持续监控启动 — 每 %d 秒扫描一次", interval)
    logger.info("按 Ctrl+C 停止")
    logger.info("=" * 60)

    running = True

    def _handle_signal(sig, frame):
        nonlocal running
        logger.info("收到停止信号，正在退出...")
        running = False

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while running:
        logger.info("")
        logger.info("--- 扫描 %s ---", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        try:
            n = scan_and_notify()
            if n > 0:
                logger.info("  ✅ 发现并推送 %d 条新机会", n)
        except Exception as e:
            logger.error("扫描异常: %s", e)

        for _ in range(interval):
            if not running:
                break
            time.sleep(1)

    logger.info("监控已停止")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="+EV 机会监控器")
    parser.add_argument("--loop", action="store_true", help="持续监控模式")
    parser.add_argument("--interval", type=int, default=1800, help="扫描间隔（秒）")
    parser.add_argument("--force", action="store_true", help="强制推送所有机会")
    args = parser.parse_args()

    if args.loop:
        run_loop(interval=args.interval)
    else:
        n = scan_and_notify(force_notify=args.force)
        if n:
            print(f"发现 {n} 条新 +EV 机会，已推送钉钉")
        else:
            print("无新 +EV 机会")


if __name__ == "__main__":
    main()
