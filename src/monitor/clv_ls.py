"""Line Shopping CLV 追踪 — 记录 Pinnacle 赔率快照，计算收盘价值。

CLV 是验证 edge 是否真实的核心指标：
  - Pinnacle 收盘价向我们方向移动 → edge 被市场验证
  - Pinnacle 收盘价背向我们移动 → edge 可能是噪音

数据流:
  1. ev_monitor 每次扫描 → track_snapshot() 记录当前 Pinnacle 赔率
  2. 比赛结束 → send_clv_report() 计算 CLV → 钉钉推送
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent

from config.logging_config import get_logger
from config.settings import DATA_DIR, DINGTALK_WEBHOOK

logger = get_logger(__name__)

CLV_FILE = DATA_DIR / "clv_tracking.json"


def _load_clv() -> dict:
    if CLV_FILE.exists():
        try:
            return json.loads(CLV_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_clv(data: dict):
    CLV_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLV_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def track_snapshot(opp: dict):
    """记录一次 Pinnacle 赔率快照。

    Args:
        opp: line_shopping 扫描返回的一条机会
    """
    match_key = f"{opp['home_team']} vs {opp['away_team']}"
    outcome = opp["outcome"]
    bet_id = f"{match_key}|{outcome}"

    # 该结果对应的 Pinnacle 赔率
    pinny_map = {
        "home": opp.get("pinny_home_odds"),
        "draw": opp.get("pinny_draw_odds"),
        "away": opp.get("pinny_away_odds"),
    }
    pinny_odds = pinny_map.get(outcome)
    if not pinny_odds:
        return

    data = _load_clv()
    now = datetime.now(timezone.utc).isoformat()

    if bet_id not in data:
        data[bet_id] = {
            "match_key": match_key,
            "outcome": outcome,
            "league": opp.get("league", ""),
            "home_team": opp["home_team"],
            "away_team": opp["away_team"],
            "retail_odds": opp.get("odds", opp.get("retail_odds")),
            "retail_bookmaker": opp.get("retail_bookmaker", ""),
            "initial_edge": opp.get("edge_pct", 0),
            "snapshots": [],
            "settled": False,
        }

    data[bet_id]["snapshots"].append({
        "time": now,
        "pinnacle_odds": float(pinny_odds),
        "pinnacle_home": float(opp.get("pinny_home_odds", 0)),
        "pinnacle_draw": float(opp.get("pinny_draw_odds", 0)),
        "pinnacle_away": float(opp.get("pinny_away_odds", 0)),
    })

    _save_clv(data)


def track_pending_snapshots(opportunities: list):
    """为所有待结算投注对应的机会记录 Pinnacle 快照。

    从 line_shopping_results 读取当前扫描结果，
    与 virtual_portfolio 中的 pending 投注做匹配。

    Args:
        opportunities: line_shopping 扫描结果列表
    """
    if not opportunities:
        return

    # 读取 pending 投注
    vp_file = DATA_DIR / "virtual_portfolio.json"
    if not vp_file.exists():
        return
    try:
        vp = json.loads(vp_file.read_text())
    except Exception:
        return

    pending = vp.get("pending_bets", [])
    if not pending:
        return

    # 为每条 pending 投注找对应的扫描结果
    for bet in pending:
        bt_home = bet.get("home_team", "").strip().lower()
        bt_away = bet.get("away_team", "").strip().lower()
        bt_outcome = bet.get("market_type", "")

        for opp in opportunities:
            opp_home = opp.get("home_team", "").strip().lower()
            opp_away = opp.get("away_team", "").strip().lower()
            if bt_home == opp_home and bt_away == opp_away:
                track_snapshot(opp)
                break


def _calc_clv(tracking: dict) -> Optional[dict]:
    """计算一条追踪记录的 CLV。

    CLV = (retail_odds - last_pinnacle_odds) / last_pinnacle_odds
    正 CLV = 我们的赔率优于 Pinnacle 收盘价 = edge 真实
    """
    snapshots = tracking.get("snapshots", [])
    if len(snapshots) < 1:
        return None

    first = snapshots[0]
    last = snapshots[-1]
    retail_odds = tracking.get("retail_odds", 0)

    if not retail_odds or not last.get("pinnacle_odds"):
        return None

    opening_pinny = float(first["pinnacle_odds"])
    closing_pinny = float(last["pinnacle_odds"])
    clv = (retail_odds - closing_pinny) / closing_pinny * 100 if closing_pinny > 0 else 0
    pinny_move = (closing_pinny - opening_pinny) / opening_pinny * 100 if opening_pinny > 0 else 0

    return {
        "clv_pct": round(clv, 2),
        "pinny_move_pct": round(pinny_move, 2),
        "opening_pinny": opening_pinny,
        "closing_pinny": closing_pinny,
        "retail_odds": retail_odds,
        "snapshot_count": len(snapshots),
        "hours_tracked": round(
            (datetime.fromisoformat(last["time"].replace("Z", "+00:00")) -
             datetime.fromisoformat(first["time"].replace("Z", "+00:00"))).total_seconds() / 3600, 1
        ) if len(snapshots) > 1 else 0,
    }


def send_clv_report():
    """检查已结算投注的 CLV，推送钉钉。

    两类推送:
      1. 已结算投注 → 最终 CLV + 盈亏（每次结算推一次）
      2. 追踪中投注 → Pinnacle 赔率走势（有 2+ 快照且未报告过）
    """
    if not DINGTALK_WEBHOOK:
        logger.info("  未配置钉钉 Webhook，跳过 CLV 推送")
        return

    data = _load_clv()
    if not data:
        logger.info("  无 CLV 追踪数据")
        return

    vp_file = DATA_DIR / "virtual_portfolio.json"
    if not vp_file.exists():
        return

    try:
        vp = json.loads(vp_file.read_text())
    except Exception:
        return

    history = vp.get("history", [])
    pending = vp.get("pending_bets", [])
    to_report = []

    # ── 1. 已结算投注的 CLV ──
    for h in history:
        h_id = h.get("id", "").lower()
        for bet_id, tracking in data.items():
            if tracking.get("settled"):
                continue
            t_home = tracking.get("home_team", "").strip().lower()
            t_away = tracking.get("away_team", "").strip().lower()
            if t_home in h_id and t_away in h_id:
                to_report.append({
                    "tracking": tracking,
                    "profit": h.get("profit", 0),
                    "stake": h.get("stake", 0),
                    "result": "won" if h.get("profit", 0) > 0 else "lost",
                    "type": "settlement",
                })
                data[bet_id]["settled"] = True
                break

    # ── 2. 未结算但已有追踪数据的 ──
    for bet_id, tracking in data.items():
        if tracking.get("settled"):
            continue
        if len(tracking.get("snapshots", [])) >= 2 and not tracking.get("status_reported"):
            to_report.append({
                "tracking": tracking,
                "profit": None,
                "stake": None,
                "result": "pending",
                "type": "tracking",
            })
            data[bet_id]["status_reported"] = True

    lines = []
    for info in to_report:
        t = info["tracking"]
        clv_info = _calc_clv(t)
        if not clv_info:
            continue

        outcome_cn = {"home": "主胜", "draw": "平局", "away": "客胜"}.get(t.get("outcome", ""), t.get("outcome", ""))

        result_emoji = {"won": "✅", "lost": "❌", "pending": "⏳"}
        result_text = {"won": "赢", "lost": "输", "pending": "待结算"}
        emoji = result_emoji.get(info["result"], "⏳")
        rt = result_text.get(info["result"], "待结算")

        clv_str = f"**+{clv_info['clv_pct']}%**" if clv_info['clv_pct'] > 0 else f"**{clv_info['clv_pct']}%**"
        pinny_str = f"**{clv_info['pinny_move_pct']:+.1f}%**" if clv_info['pinny_move_pct'] != 0 else "0%"

        line = (
            f"##### {t['match_key']} [{outcome_cn}] {emoji}{rt}\n"
            f"> 下注赔率: {t['retail_odds']:.2f} @ {t['retail_bookmaker']}\n"
            f"> Pinnacle: {clv_info['opening_pinny']:.2f} → {clv_info['closing_pinny']:.2f} ({pinny_str})\n"
            f"> CLV: {clv_str}\n"
            f"> 扫描: {clv_info['snapshot_count']} 次 / {clv_info['hours_tracked']} 小时"
        )
        if info["profit"] is not None:
            profit_str = f"+¥{info['profit']:.0f}" if info['profit'] > 0 else f"¥{info['profit']:.0f}"
            line += f"\n> 盈亏: {profit_str}"

        lines.append(line)

        # 标记已报告
        t["clv_reported"] = True
        if info["result"] != "pending":
            t["settled"] = True
        t["clv_status_reported"] = True

    if not lines:
        logger.info("  ⏭️ CLV: 无可格式化的报告数据")
        return

    _save_clv(data)

    total = len(lines)
    positive = sum(1 for l in lines if "CLV: **+" in l)
    title = f"📊 CLV 投注推荐: {total} 条"
    body = f"**{title}**\n\n正 CLV {positive}/{total} 条：\n\n" + "\n\n".join(lines)

    from config.settings import send_dingtalk
    ok = send_dingtalk(title, body)
    if ok:
        logger.info("  CLV 钉钉推送完成")
    else:
        logger.warning("  CLV 钉钉推送失败")

    return total


if __name__ == "__main__":
    send_clv_report()
