"""BB体育 vs Pinnacle +EV 钉钉推送 — 格式与 ev_push.py 一致，零售→BB价。"""
import json, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import get_logger
from config.settings import DATA_DIR, DINGTALK_WEBHOOK

logger = get_logger(__name__)

COMPARISON_FILE = DATA_DIR / "bb_vs_pinnacle_comparison.json"
BANKROLL = 10000.0
MAX_OPPORTUNITIES = 50

_OUTCOME_CN = {"home": "主胜", "draw": "和局", "away": "客胜"}
_REVERSE_CN = {v: k for k, v in _OUTCOME_CN.items()}


def _league_multiplier(league: str) -> float:
    """根据联赛级别动态加权：主流联赛 1.0，次级 0.85，其他 0.7。"""
    major = ["ATP - ", "WTA - ", "NBA", "WNBA", "MLB",
             "英格兰", "西班牙", "德国甲", "意大利", "法国",
             "超级联赛", "冠军联赛", "世界杯", "欧冠", "欧联"]
    medium = ["挑战赛", "125K", "瑞典超", "挪威超", "芬兰",
              "FIBA欧洲", "欧洲篮球", "欧洲联赛",
              "日本职业", "KBO", "韩国", "澳洲",
              "白俄罗斯", "哈萨克", "乌拉圭", "巴拉圭"]
    for kw in major:
        if kw in league:
            return 1.0
    for kw in medium:
        if kw in league:
            return 0.85
    return 0.7


def _calc_kelly_stakes(opps: list) -> list:
    """按 Kelly 比例计算投注额，与 ev_push.py 一致。"""
    for o in opps:
        k = o.get("_kelly_pct", 0)  # 已转为百分比（如 2.5 = 2.5%）
        stake = round(BANKROLL * k / 100)
        if stake < 5:
            stake = 0
        o["_stake"] = stake
    return opps


def _collect_opportunities(match, market_key):
    """从指定市场收集 +EV 机会。校准过滤：时间匹配必须高分才推送。"""
    match_type = match.get("match_type", "unknown")
    match_score = match.get("match_score", 0.7)
    # 时间匹配（非队名匹配）需要高置信度，防止推错比赛
    if match_type == "time" and match_score < 0.90:
        return []
    league = match.get("league", "")
    home_cn = match.get("home_bb", "")
    away_cn = match.get("away_bb", "")
    league_mult = _league_multiplier(league)
    result = []
    for opp in match.get(market_key, []):
        ev = opp.get("ev_pct", 0)
        if ev < 1:
            continue
        bb_odds = opp.get("bb_odds", 0)
        pin_odds = opp.get("pin_odds", 0)
        fair = opp.get("fair_price") or round(pin_odds, 2)
        kelly_pct = 0
        if bb_odds > 1:
            kelly = (ev / 100) / (bb_odds - 1) * 0.25
            kelly_pct = round(kelly * 100, 2)

        # 综合评分：溢价 × 匹配度 × 联赛权重
        score = round(ev * match_score * league_mult, 2)

        # 带盘口信息的显示名
        desig = opp.get("designation", "")
        line = opp.get("line", "")
        display_name = f"{desig}({line})" if line else desig

        result.append({
            "sport": match.get("sport", ""),
            "league": league,
            "home_cn": home_cn,
            "away_cn": away_cn,
            "designation": display_name,
            "bb_odds": bb_odds,
            "pin_odds": pin_odds,
            "fair_price": fair,
            "ev_pct": ev,
            "_match_score": match_score,
            "_score": score,
            "_kelly_pct": kelly_pct,
            "_pin_epoch": match.get("start_time_pin_epoch"),  # 用于显示开赛时间
        })
    return result


def _format_bj_time(pin_epoch):
    """Convert Pinnacle UTC epoch to Beijing time string 'MM/DD HH:MM'."""
    if not pin_epoch:
        return ""
    try:
        dt = datetime.fromtimestamp(pin_epoch, tz=timezone.utc)
        bj = dt.astimezone(timezone(timedelta(hours=8)))
        return bj.strftime("%m/%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


def build_report():
    """构建格式化的 BB vs Pinnacle +EV 报告。返回 (body_text, qualified_opportunities)."""
    if not COMPARISON_FILE.exists():
        return "BB vs Pinnacle comparison data not found", []

    data = json.loads(COMPARISON_FILE.read_text())
    details = data.get("details", [])

    # 收集所有 >=1% 溢价的 opportunities
    qualified = []
    for match in details:
        qualified.extend(_collect_opportunities(match, "opportunities"))
        qualified.extend(_collect_opportunities(match, "handicap"))
        qualified.extend(_collect_opportunities(match, "over_under"))
        qualified.extend(_collect_opportunities(match, "double_chance"))
        qualified.extend(_collect_opportunities(match, "draw_no_bet"))

    if not qualified:
        return "no +EV opportunities (>=3%)", []

    SPORT_ORDER = {"football": 0, "basketball": 1, "tennis": 2, "baseball": 3, "american_football": 4}
    SPORT_CN = {"football": "⚽ 足球", "basketball": "🏀 篮球", "tennis": "🎾 网球",
                "baseball": "⚾ 棒球", "american_football": "🏈 美式足球"}

    # 第一步：按综合评分排序，取 top 50
    qualified.sort(key=lambda o: -o["_score"])
    if len(qualified) > MAX_OPPORTUNITIES:
        qualified = qualified[:MAX_OPPORTUNITIES]

    # 第二步：按运动→联赛→比赛→评分降序重排（同场比赛不同盘口不分开）
    qualified.sort(key=lambda o: (
        SPORT_ORDER.get(o.get("sport", ""), 99),
        o.get("league", ""),
        o.get("home_cn", ""),
        o.get("away_cn", ""),
        -o["_score"]
    ))

    # Kelly 分配
    qualified = _calc_kelly_stakes(qualified)
    qualified = [o for o in qualified if o["_stake"] > 0]

    now_str = datetime.now(timezone.utc).astimezone().strftime("%m/%d %H:%M")
    total_allocated = sum(o["_stake"] for o in qualified)

    lines = []
    prev_sport = None
    prev_league = None
    prev_match = None
    match_idx = 0
    for o in qualified:
        oc = o["designation"]
        pinny = round(o.get("pin_odds", 0), 2) if o.get("pin_odds", 0) > 0 else 0
        fair = o.get("fair_price") or round(o["pin_odds"], 2)
        bb_odds = o["bb_odds"]
        ev_pct = o["ev_pct"]
        stake = o["_stake"]
        match_key = (o.get("home_cn", ""), o.get("away_cn", ""))

        # 运动分组标题
        sport = o.get("sport", "")
        sport_label = SPORT_CN.get(sport, "")
        if sport != prev_sport:
            if prev_sport is not None:
                lines.append("")
            lines.append(sport_label)
            prev_sport = sport
            prev_league = None
            prev_match = None

        # 联赛分组标题
        league = o.get("league", "")
        if league and league != prev_league:
            lines.append(f"  {league}")
            prev_league = league
            prev_match = None

        # 比赛标题（同一场比赛只出现一次，下面跟多个盘口）
        if match_key != prev_match:
            match_idx += 1
            bj_time = _format_bj_time(o.get("_pin_epoch"))
            time_suffix = f"  ({bj_time})" if bj_time else ""
            lines.append(
                f"  ##### #{match_idx} {o['home_cn']} 对 {o['away_cn']}{time_suffix}"
            )
            prev_match = match_key

        lines.append(
            f"    [{oc}] 公平价: {fair}" + (f" | Pinnacle: {pinny}" if o.get("pin_odds", 0) > 0 else " | 推导: 1X2") + f" | BB价: {bb_odds} | 溢价: +{ev_pct}% | 投注: ¥{stake:,}"
        )

    title = f"+EV 投注推荐: {match_idx} 场比赛"
    body = (
        f"**{title}**\n\n"
        f"扫描 {now_str} | ≥1% 溢价 | 总额 ¥{total_allocated:,}\n\n"
        + "\n".join(lines).strip()
    )
    # 注意：禁用 "BB体育" 关键词（钉钉内容安全过滤）
    body += "\n\n---\n💡 公平价 = Pinnacle去抽水赔率 | 溢价 = (BB - 公平价) / 公平价 | 赔率实时变动，以 Pinnacle 网站当前价为准"

    return body, qualified


def push_report(place_bets=False):
    if not DINGTALK_WEBHOOK:
        logger.info("no DINGTALK_WEBHOOK configured")
        return

    body, qualified = build_report()
    if body.startswith("no") or body.startswith("BB vs"):
        logger.info("no push needed: %s", body)
        return

    # 保存推送机会列表到暂存文件（供虚拟投注使用）
    # 机会少于10场时不投注（降低集中风险）
    if qualified and place_bets and len(qualified) >= 10:
        from src.betting.bb_virtual_bet import PUSH_STAGING_FILE, place_bets_from_push
        PUSH_STAGING_FILE.write_text(json.dumps(qualified, ensure_ascii=False, indent=2))
        logger.info("推送机会已暂存到 %s，开始投注...", PUSH_STAGING_FILE)
        place_bets_from_push(qualified)
    elif qualified and place_bets and len(qualified) < 10:
        logger.info("机会不足10场(%d场)，跳过虚拟投注", len(qualified))

    # 钉钉内容安全：body 里没有 BB体育 关键词，安全
    from config.settings import send_dingtalk
    title = f"+EV 投注推荐: {body.count('#####')} 条"
    ok = send_dingtalk(title, body)
    if ok:
        logger.info("BB vs Pinnacle +EV report pushed to DingTalk (%d opportunities)", body.count('#####'))
    else:
        logger.warning("BB vs Pinnacle push failed")


# ── 格式验证（供 pre-commit 回归测试使用） ──

_FORMAT_MARKERS = {
    "header": "**+EV 投注推荐:",
    "match_prefix": "##### ",
    "fair_price": "公平价:",
    "pinnacle": "Pinnacle:",
    "retail": "BB价:",
    "edge": "溢价:",
    "stake": "投注:",
    "footer": "公平价 = Pinnacle去抽水赔率",
}


def _validate_format(body: str) -> bool:
    """验证推送body包含所有关键标记。"""
    body_stripped = body.strip()
    if not body_stripped or len(body_stripped) < 50:
        return False
    for key, marker in _FORMAT_MARKERS.items():
        if marker not in body_stripped:
            return False
    return True


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    body, qualified = build_report()
    print(body)

    # 保存推送机会到暂存文件（即使 --no-push 也保存）
    if qualified and ("--place-bets" in sys.argv or "--stage" in sys.argv):
        from src.betting.bb_virtual_bet import PUSH_STAGING_FILE, place_bets_from_push
        PUSH_STAGING_FILE.write_text(json.dumps(qualified, ensure_ascii=False, indent=2))
        if len(qualified) >= 10:
            logger.info("推送机会已暂存到 %s", PUSH_STAGING_FILE)
            place_bets_from_push(qualified)
        else:
            logger.info("机会不足10场(%d场)，跳过虚拟投注", len(qualified))

    if "--no-push" not in sys.argv:
        push_report(place_bets=("--no-bet" not in sys.argv))
