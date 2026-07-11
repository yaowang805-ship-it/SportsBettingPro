"""每日结算报告 — 上午 9:00 钉钉推送已结算/未结算详情。"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging, get_logger
from config.settings import DATA_DIR, DINGTALK_WEBHOOK
from src.core.team_names import cn_team
from src.report.validator import validate_output

logger = get_logger(__name__)

VP_FILE = DATA_DIR / "virtual_portfolio.json"

MARKET_CN = {
    "line_shopping": "综合机会",
    "home": "主胜", "away": "客胜", "draw": "平局",
    "yes": "双方进球", "no": "不进球",
    "1X": "主队不败", "X2": "客队不败",
}

def _market_cn(mt: str) -> str:
    if mt in MARKET_CN:
        return MARKET_CN[mt]
    parts = mt.split("_")
    if parts[0] == "over":
        return "大" + parts[1]
    if parts[0] == "under":
        return "小" + parts[1]
    return mt


def _build_message() -> str:
    """组装钉钉 Markdown 正文。"""
    if not VP_FILE.exists():
        return "❌ virtual_portfolio.json 不存在"

    vp = json.loads(VP_FILE.read_text())
    pending = vp.get("pending_bets", [])
    history = vp.get("history", [])
    balance = vp.get("balance", 0)

    today = datetime.now().strftime("%m/%d")
    lines = [f"📊 投注推荐 · 每日结算 {today}", ""]

    # ── 已结算 ──
    wins = [h for h in history if h.get("profit", 0) > 0]
    losses = [h for h in history if h.get("profit", 0) <= 0]
    total_profit = sum(h.get("profit", 0) for h in history)
    total_stake = sum(h.get("stake", 0) for h in history)
    roi = total_profit / total_stake * 100 if total_stake else 0
    wr = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0

    lines.append(f"【已结算】{len(history)}笔 | {len(wins)}胜 {len(losses)}负")
    lines.append(f"净利润: ¥{total_profit:+.2f} | ROI: {roi:+.2f}% | 胜率: {wr:.1f}%")
    lines.append("")

    # ── 未结算 ──
    total_exposure = sum(b.get("stake", 0) for b in pending)
    lines.append(f"【未结算】{len(pending)}笔 | 敞口 ¥{total_exposure:.0f}")
    lines.append(f"余额: ¥{balance:.2f}")
    lines.append("")

    # ── 按比赛汇总（只显示前10大持仓）──
    if pending:
        lines.append("📋 主要持仓：")
        # Group by match
        from collections import defaultdict
        pm = defaultdict(list)
        for b in pending:
            home = b.get("home_team", b.get("home_cn", ""))
            away = b.get("away_team", b.get("away_cn", ""))
            pm[(home, away)].append(b)

        now = datetime.now(timezone.utc)
        sorted_matches = sorted(pm.items(), key=lambda x: sum(b.get("stake", 0) for b in x[1]), reverse=True)

        for idx, ((home, away), bets) in enumerate(sorted_matches[:10], 1):
            match_total = sum(b.get("stake", 0) for b in bets)
            # Team name conversion
            try:
                sport = "nba" if ("nba" in bets[0].get("league", "").lower() or bets[0].get("sport") in ("nba", "basketball")) else "football"
                h_cn = cn_team(home, sport)
                a_cn = cn_team(away, sport)
            except Exception:
                h_cn, a_cn = home, away

            # Time until match — 组内找第一个有 commence_time 的
            ct = next((b.get("commence_time", "") for b in bets if b.get("commence_time")), "")
            time_str = ""
            if ct:
                try:
                    dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                    h_until = (dt - now).total_seconds() / 3600
                    if h_until > 24:
                        time_str = f" ({h_until:.0f}h后)"
                    elif h_until > 1:
                        time_str = f" ({h_until:.0f}h后)"
                    elif h_until > 0:
                        time_str = f" ({int(h_until*60)}min后)"
                    elif h_until > -3:
                        time_str = " (已开赛)"
                except Exception:
                    pass

            lines.append(f"  {idx}. {h_cn} vs {a_cn}{time_str}  ¥{match_total:.0f}")

        lines.append("")

    lines.append(f"累计: ¥{total_profit:+.2f} | ROI: {roi:+.2f}% | 余额: ¥{balance:.0f}")
    return "\n".join(lines)


def send_daily_report():
    """读取虚拟组合，推送结算概览到钉钉。"""
    setup_logging()
    if not DINGTALK_WEBHOOK:
        logger.info("未配置钉钉 Webhook，跳过推送")
        return

    try:
        body = _build_message()
        title = f"结算报告 {datetime.now().strftime('%m/%d')}"

        # 校准检查：推送前确保全部中文
        cal_issues = validate_output(body, context="钉钉结算报告")
        if cal_issues:
            logger.warning("校准发现 %d 个中文化问题，已修复: %s", len(cal_issues), cal_issues[:3])

        from config.settings import send_dingtalk
        ok = send_dingtalk(title, body)
        if ok:
            logger.info("每日结算报告推送完成")
        else:
            logger.warning("每日结算报告推送失败")
    except Exception as e:
        logger.warning("每日结算报告推送失败: %s", e)


def main():
    send_daily_report()


if __name__ == "__main__":
    main()
