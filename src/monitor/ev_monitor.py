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
from src.monitor.clv_ls import track_pending_snapshots, send_clv_report

logger = get_logger(__name__)

SEEN_FILE = DATA_DIR / "ev_seen.json"

OUTCOME_CN = {"home": "🏠 主胜", "draw": "⚖️ 平局", "away": "✈️ 客胜"}


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


def _send_dingtalk(opps: List[dict]):
    if not DINGTALK_WEBHOOK:
        logger.info("  未配置钉钉 Webhook，跳过推送")
        return

    lines = []
    for opp in opps[:5]:
        outcome_cn = OUTCOME_CN.get(opp["outcome"], opp["outcome"])
        commence = opp.get("commence_time", "")
        try:
            dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            commence_cn = dt.strftime("%m/%d %H:%M")
        except (ValueError, TypeError):
            commence_cn = commence

        lines.append(
            f"##### {opp['home_team']} vs {opp['away_team']}\n"
            f"> 结果: {outcome_cn} | 时间: {commence_cn}\n"
            f"> Edge: **+{opp['edge_pct']}%** | 赔率: {opp['retail_odds']:.2f} @ {opp['retail_bookmaker']}\n"
            f"> Pinnacle: {opp['pinny_home_odds']:.2f}/{opp['pinny_draw_odds']:.2f}/{opp['pinny_away_odds']:.2f}"
        )

    total = len(opps)
    title = f"⚽ +EV 投注推荐: {total} 条"
    body = f"**{title}**\n\n发现 {total} 条 Line Shopping 机会：\n\n" + "\n\n".join(lines)
    if total > 5:
        body += f"\n\n...及另外 {total - 5} 条，详见 line_shopping_results.json"

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
