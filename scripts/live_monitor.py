#!/usr/bin/env python3
"""+EV 实时监控 — 每 30 分钟刷新所有联赛 +EV 机会，输出简洁报告。

用法:
    python3 scripts/live_monitor.py          # 单次扫描
    python3 scripts/live_monitor.py --loop   # 持续监控（每30分钟）

推荐:
    python3 scripts/live_monitor.py --loop --dingtalk  # 持续 + 推钉钉
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.line_shopping import LineShoppingScanner
from src.core.team_names import cn_team
from config.settings import DATA_DIR, DINGTALK_WEBHOOK
from config.logging_config import get_logger, setup_logging
from config.dingtalk import send_dingtalk

setup_logging()
logger = get_logger(__name__)

OUTCOME_CN = {"home": "主胜", "draw": "平局", "away": "客胜", "yes": "双方进球", "no": "一方不进"}

REPORT_FILE = DATA_DIR / "live_report.json"

# 每日复盘跟踪
_LAST_REVIEW_DATE = None

DAILY_BUDGET = 10000.0
MAX_PER_BET = 2000.0     # 单注上限 ¥2,000
MAX_PER_MATCH = 3500.0   # 单场比赛总暴露上限 ¥3,500
MIN_EDGE = 0.03
KELLY_FRAC = 0.25        # 1/4 Kelly


def _calc_stakes(opps: list, budget: float = DAILY_BUDGET) -> list:
    """按 Kelly 归一化计算每场建议投注额（与 place_line_shops 逻辑一致）。"""
    # 扣减今日已分配
    import json
    from config.settings import DATA_DIR
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
    budget = max(0, budget - allocated_today)
    if budget < 100:
        return [{**o, "stake": 0} for o in opps]

    candidates = []
    for o in opps:
        ev = o.get("_ev", 0)
        odds = o.get("odds", 0)
        model_prob = o.get("model_prob", 0)
        if ev < MIN_EDGE or odds <= 1 or model_prob <= 0:
            candidates.append({**o, "stake": 0})
            continue
        b = odds - 1.0
        kelly = (model_prob * b - (1.0 - model_prob)) / b if b > 0 else 0
        if kelly <= 0:
            candidates.append({**o, "stake": 0})
            continue
        candidates.append({**o, "_kelly": kelly})

    # 归一化
    valid = [c for c in candidates if "_kelly" in c]
    raw = [min(c["_kelly"] * KELLY_FRAC, MAX_PER_BET / budget) for c in valid]
    total_raw = sum(raw)

    result = []
    i = 0
    for c in candidates:
        if "_kelly" in c:
            if total_raw > 0:
                stake = min(round(budget * raw[i] / total_raw, 0), MAX_PER_BET)
            else:
                stake = 0
            i += 1
            result.append({**c, "stake": int(stake)})
        else:
            result.append(c)

    # 单场比赛总暴露上限
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


def generate_report(opps: list) -> str:
    """生成包含建议投注额的投注对照报告（取前8条）。"""
    opps = _calc_stakes(opps)
    opps = opps[:12]
    total = sum(o.get("stake", 0) for o in opps)
    lines = []
    lines.append("=" * 80)
    lines.append(f"  +EV 投注推荐  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"  {'#':>2} {'比赛':<32} {'推荐':>6} {'公平价':>7} {'建议投注':>10} {'Edge':>8}")
    lines.append(f"  {'─'*2} {'─'*32} {'─'*6} {'─'*7} {'─'*10} {'─'*8}")

    for i, o in enumerate(opps, 1):
        h_cn = cn_team(o["home_team"], "football")
        a_cn = cn_team(o["away_team"], "football")
        # 显示盘口标记
        mkt = o.get("market", "1x2")
        if mkt == "over_under":
            mkt_tag = " [大小]"
        elif mkt == "btts":
            mkt_tag = " [进球]"
        else:
            mkt_tag = ""
        match_str = f"{h_cn} vs {a_cn}{mkt_tag}"
        oc = OUTCOME_CN.get(o["outcome"], o.get("outcome_label", o["outcome"]))
        fair = round(1.0 / o["model_prob"], 2)
        stake_str = f"¥{o['stake']:.0f}" if o.get("stake", 0) > 0 else " —"
        lines.append(
            f"  {i:>2} {match_str:<32} {oc:>6} {fair:>7} {stake_str:>10} +{o['edge_pct']:>5.1f}%"
        )

    lines.append("")
    lines.append("-" * 80)
    lines.append(f"  合计: ¥{total:.0f} / 日预算 ¥{DAILY_BUDGET:.0f}")
    lines.append("  操作: 打开 BB体育 → 赔率 > 公平价 = +EV 可下")
    lines.append("-" * 80)
    return "\n".join(lines)


def push_dingtalk(opps: list):
    """推送含建议投注额的报告到钉钉。"""
    if not DINGTALK_WEBHOOK:
        return

    opps = _calc_stakes(opps)
    qualified = [o for o in opps if o["edge_pct"] >= 3][:12]
    if not qualified:
        return

    lines = []
    lines.append(f"📊 投注推荐 {datetime.now().strftime('%m/%d %H:%M')}")
    lines.append("")
    for i, o in enumerate(qualified, 1):
        h_cn = cn_team(o["home_team"], "football")
        a_cn = cn_team(o["away_team"], "football")
        oc = OUTCOME_CN.get(o["outcome"], o.get("outcome_label", o["outcome"]))
        fair = round(1.0 / o["model_prob"], 2)
        stake_str = f" ¥{o['stake']:.0f}" if o.get("stake", 0) > 0 else ""
        mkt = o.get("market", "1x2")
        tag = {"over_under": "大小", "btts": "进球"}.get(mkt, "独赢")
        decay = o.get("_edge_decay")
        decay_tag = ""
        if decay and decay.get("decaying"):
            decay_tag = f" [下降{decay['change']:+.1f}%]"
        lines.append(f"{i}. {h_cn} vs {a_cn}")
        lines.append(f"   {tag} {oc} | 公平价 {fair}{stake_str} | +{o['edge_pct']}%{decay_tag}")

    # 衰退汇总
    decaying_all = [o for o in opps if o.get("_edge_decay") and o["_edge_decay"].get("decaying")]
    if decaying_all:
        lines.append(f"  共 {len(decaying_all)} 条 Edge 下降中，建议尽快下注")
        for d in decaying_all[:2]:
            h = cn_team(d["home_team"], "football")
            a = cn_team(d["away_team"], "football")
            ch = d["_edge_decay"]["change"]
            mn = d["_edge_decay"]["elapsed_min"]
            lines.append(f"   {h} vs {a} {ch:+.1f}%（{mn}分钟）")

    lines.append("")
    lines.append(f"日预算 ¥{DAILY_BUDGET:.0f} | 共 {len(opps)} 条机会")

    body = "\n".join(lines)
    send_dingtalk(body, msgtype="markdown", title=f"+EV {len(qualified)}条")


def _send_dingtalk_alert(text: str):
    """发送纯文本钉钉告警（用于故障通知）。"""
    send_dingtalk(text)


def push_daily_review():
    """每日复盘推送 — 调用 PaperTrader 生成日报并推钉钉。"""
    global _LAST_REVIEW_DATE
    today = datetime.now().strftime("%Y-%m-%d")
    if _LAST_REVIEW_DATE == today:
        return
    if not DINGTALK_WEBHOOK:
        return

    from src.betting.paper_trader import PaperTrader
    try:
        pt = PaperTrader()
        state = pt.refresh()
        report = pt.generate_dingtalk_report(state)
        title = f"📊 投注推荐复盘 — {today}"
        body = f"👉 投注推荐复盘\n\n{report}"
        send_dingtalk(body, msgtype="markdown", title=title)
        _LAST_REVIEW_DATE = today
    except Exception as e:
        logger.warning("  每日复盘推送失败: %s", e)


def main():
    parser = argparse.ArgumentParser(description="+EV 实时监控")
    parser.add_argument("--loop", action="store_true", help="持续监控，每 30 分钟刷新")
    parser.add_argument("--dingtalk", action="store_true", help="扫描后推送钉钉")
    parser.add_argument("--interval", type=int, default=1800, help="刷新间隔（秒）")
    parser.add_argument("--betting", action="store_true", help="自动执行虚拟投注 + 结算")
    args = parser.parse_args()

    if args.loop:
        logger.info("🔄 +EV 实时监控已启动，每 %d 秒刷新一次", args.interval)
        logger.info("   按 Ctrl+C 停止")

    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 3

    while True:
        print()
        scanner = LineShoppingScanner()
        try:
            opps = scanner.scan()
            scanner.save_results()
        except Exception as e:
            logger.error("  ❌ 扫描异常: %s", e, exc_info=True)
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                msg = (
                    f"⚠️ Line Shopping 连续 {consecutive_failures} 次扫描失败\n"
                    f"最后错误: {e}\n"
                    f"请检查网络 / BSD API 状态"
                )
                logger.error("  🚨 %s", msg)
                if DINGTALK_WEBHOOK:
                    _send_dingtalk_alert(msg)
            if args.loop:
                print(f"\n  下次重试: {datetime.now().strftime('%H:%M')} → {args.interval//60} 分钟后")
                try:
                    time.sleep(args.interval)
                except KeyboardInterrupt:
                    print("\n  已停止")
                    break
            continue
        consecutive_failures = 0  # 成功后重置

        # ── 自动结算（每次扫描都跑，不依赖新投注） ──
        if args.betting:
            from src.monitor.auto_settle import auto_settle
            settled = auto_settle()
            if settled:
                logger.info("  已结算 %d 笔", settled)

        # ── 虚拟投注（--betting 模式） ──
        placed = 0
        if args.betting:
            from src.betting.place_line_shops import place_line_shops
            placed = place_line_shops()

        # ── 投注后状态报告 ──
        if args.betting and (placed or settled):
            from src.betting.paper_trader import PaperTrader
            pt = PaperTrader()
            state = pt.refresh()
            rd = state.get("readiness", {})
            verdict = "GO" if rd.get("ready") else "NO-GO"
            logger.info("   余额: ¥%.0f | 已结算: %d 笔 | P&L: ¥%+.0f | %s",
                        state.get("current_bankroll", 0),
                        state.get("settled_bets", 0),
                        state.get("total_profit", 0),
                        verdict)

        if opps:
            report = generate_report(opps)
            print()
            print(report)
            print()

            # Edge 衰退提示
            decaying = [o for o in opps if o.get("_edge_decay") and o["_edge_decay"].get("decaying")]
            if decaying:
                print(f"  ⏳ {len(decaying)} 条机会 Edge 正在下降，建议尽快下注")
                for d in decaying[:3]:
                    h = cn_team(d["home_team"], "football")
                    a = cn_team(d["away_team"], "football")
                    ch = d["_edge_decay"]["change"]
                    mn = d["_edge_decay"]["elapsed_min"]
                    print(f"     {h} vs {a}: Edge {ch:+.1f}pp（{mn}分钟内）")
                print()

            # 保存报告
            REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            REPORT_FILE.write_text(report)

            if args.dingtalk:
                push_dingtalk(opps)
                push_daily_review()

            # 输出简单总结
            opps_with_stakes = _calc_stakes(opps)
            top3 = opps_with_stakes[:3]
            print("  TOP 3 速览:")
            for i, o in enumerate(top3, 1):
                h_cn = cn_team(o["home_team"], "football")
                a_cn = cn_team(o["away_team"], "football")
                oc = OUTCOME_CN.get(o["outcome"], o["outcome"])
                fair = round(1.0 / o["model_prob"], 2)
                stake_str = f"¥{o['stake']:.0f}" if o.get("stake", 0) > 0 else "—"
                decay_tag = " ⏳" if o.get("_edge_decay") and o["_edge_decay"].get("decaying") else ""
                print(f"    {i}. {h_cn} vs {a_cn} → {oc}  公平价 {fair}  投 {stake_str}  Edge +{o['edge_pct']}%{decay_tag}")

        else:
            print("  当前无 +EV 机会")
            REPORT_FILE.write_text(f"{datetime.now()} — 无 +EV 机会")

        if not args.loop:
            break

        print(f"\n  下次刷新: {datetime.now().strftime('%H:%M')} → 每 {args.interval//60} 分钟")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n  已停止")
            break


if __name__ == "__main__":
    main()
