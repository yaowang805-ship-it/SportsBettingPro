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
FINGERPRINT_FILE = DATA_DIR / "pushed_fingerprints.json"
BANKROLL = 50000.0
MAX_OPPORTUNITIES = 100

_OUTCOME_CN = {"home": "主胜", "draw": "和局", "away": "客胜"}
_REVERSE_CN = {v: k for k, v in _OUTCOME_CN.items()}

# ── 一致性追踪 ──
# 保存每次推送的 per-sport 机会数，用于检测异常波动
PUSH_META_FILE = DATA_DIR / "push_consistency_meta.json"
# 一个 sport 的机会数比前次下降超过此比例时，在推送中发出警告
CONSISTENCY_WARN_THRESHOLD = 0.20

# 不靠谱联赛 — 匹配质量差、假阳性多，直接屏蔽
_BANNED_LEAGUES = [
    "马里篮球甲级联赛",
    "球会友谊赛",  # Pinnacle 覆盖不全，无法准确匹配
]

# EV 上限 — EV > 此值几乎全是假阳性（队名匹配到错误比赛）
EV_CAP = 15


def _check_sport_consistency(opportunities: list) -> list:
    """对比上次推送的 per-sport 机会数，有显著下降时返回警告列表。"""
    counts = {}
    for o in opportunities:
        s = o.get("sport", "unknown")
        counts[s] = counts.get(s, 0) + 1

    prev = None
    if PUSH_META_FILE.exists():
        try:
            prev = json.loads(PUSH_META_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    warnings = []
    if prev:
        prev_counts = prev.get("per_sport_counts", {})
        for sport, count in counts.items():
            prev_count = prev_counts.get(sport, 0)
            if prev_count > 0:
                change = (count - prev_count) / prev_count
                if change < -CONSISTENCY_WARN_THRESHOLD:
                    warnings.append(
                        f"⚠️ {sport} 推送数锐减 {abs(change)*100:.0f}% "
                        f"({prev_count}→{count})，请确认匹配是否正常"
                    )
                elif change > CONSISTENCY_WARN_THRESHOLD * 2:
                    warnings.append(
                        f"📈 {sport} 推送数激增 {change*100:.0f}% "
                        f"({prev_count}→{count})，请确认是否混入异常机会"
                    )

    # 保存当前数据供下次对比
    PUSH_META_FILE.write_text(json.dumps({
        "per_sport_counts": counts,
        "total": len(opportunities),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))

    return warnings


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
    """按 Kelly 比例计算投注额，加单注上限 2% + 单场上限 3%。"""
    MAX_STAKE_PCT = 0.02  # 单注 ≤ 2% bankroll
    PER_MATCH_CAP_PCT = 0.03  # 同一场比赛总投注 ≤ 3%

    # 第一遍：计算毛 Kelly 投注额
    for o in opps:
        k = o.get("_kelly_pct", 0)
        stake = round(BANKROLL * k / 100)
        max_stake = BANKROLL * MAX_STAKE_PCT
        stake = int(min(stake, max_stake))
        if stake < 5:
            stake = 0
        o["_stake"] = stake

    # 第二遍：同一比赛多盘口 → 按比例压缩到单场上限
    from collections import defaultdict
    match_groups = defaultdict(list)
    for o in opps:
        key = (o.get("home_cn", ""), o.get("away_cn", ""))
        match_groups[key].append(o)

    per_match_max = BANKROLL * PER_MATCH_CAP_PCT
    for key, group in match_groups.items():
        total = sum(o["_stake"] for o in group)
        if total > per_match_max:
            ratio = per_match_max / total
            for o in group:
                o["_stake"] = max(0, round(o["_stake"] * ratio))

    return opps


def _collect_opportunities(match, market_key):
    """从指定市场收集 +EV 机会。校准过滤：时间匹配必须高分才推送。"""
    match_type = match.get("match_type", "unknown")
    match_score = match.get("match_score", 0.7)
    # 时间匹配（非队名匹配）需要高置信度，防止推错比赛。
    # 门限必须与 bb_vs_pinnacle.py Phase 2 保持一致：
    #   网球 0.75，其他 0.70
    if match_type == "time":
        sport = match.get("sport", "")
        min_ok = 0.75 if sport == "tennis" else 0.70
        if match_score < min_ok:
            return []
    league = match.get("league", "")
    home_cn = match.get("home_bb", "")
    away_cn = match.get("away_bb", "")
    league_mult = _league_multiplier(league)

    # 屏蔽不靠谱联赛
    for banned in _BANNED_LEAGUES:
        if banned in league:
            return []

    result = []
    for opp in match.get(market_key, []):
        ev = opp.get("ev_pct", 0)
        if ev < 2:  # 指挥官模式：低于2%溢价不值得追
            continue
        bb_odds = opp.get("bb_odds", 0)
        pin_odds = opp.get("pin_odds", 0)

        # EV 上限过滤：EV > 15% 几乎全是假阳性（中文队名匹配到错误的英文队名）
        if ev > EV_CAP:
            continue

        # 超高赔率过滤：BB 赔率 > 15.0 且不是主流联赛 → 跳过
        # （小联赛弱队不可能有真实 15+ 赔率，通常是匹配错误）
        if bb_odds > 15.0 and league_mult < 1.0:
            continue
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


def _collect_opportunities_from_file():
    """从对比文件收集所有 +EV 机会，返回 raw qualified list（未排序/未 Kelly）。"""
    if not COMPARISON_FILE.exists():
        return []
    data = json.loads(COMPARISON_FILE.read_text())
    details = data.get("details", [])
    qualified = []
    for match in details:
        for mk in ("opportunities", "handicap", "over_under", "double_chance", "draw_no_bet"):
            qualified.extend(_collect_opportunities(match, mk))
    return qualified


def _diversify_and_rank(qualified: list) -> list:
    """多样性选择 + 排序 + Kelly 分配。"""
    if not qualified:
        return []

    SPORT_ORDER = {"football": 0, "basketball": 1, "tennis": 2, "baseball": 3, "american_football": 4}

    # 各运动至少保留 1 条
    selected = []
    selected_ids = set()
    for sport in ("football", "basketball", "tennis", "baseball", "american_football"):
        sport_opps = [o for o in qualified if o.get("sport") == sport]
        if sport_opps:
            best = max(sport_opps, key=lambda x: x["_score"])
            selected.append(best)
            selected_ids.add(id(best))

    remaining = [o for o in qualified if id(o) not in selected_ids]
    remaining.sort(key=lambda o: -o["_score"])
    max_remaining = MAX_OPPORTUNITIES - len(selected)
    selected.extend(remaining[:max_remaining])
    qualified = selected

    # 排序：运动→联赛→开赛时间
    qualified.sort(key=lambda o: (
        SPORT_ORDER.get(o.get("sport", ""), 99),
        o.get("league", ""),
        o.get("home_cn", ""),
        o.get("away_cn", ""),
        o.get("_pin_epoch") if o.get("_pin_epoch") else 9999999999,
    ))

    # Kelly 分配
    qualified = _calc_kelly_stakes(qualified)
    qualified = [o for o in qualified if o["_stake"] > 0]
    return qualified


def _format_body(qualified: list, warnings: list | None = None) -> str:
    """将 qualified 机会列表格式化为钉钉推送文本。"""
    if not qualified:
        return ""

    SPORT_CN = {"football": "⚽ 足球", "basketball": "🏀 篮球", "tennis": "🎾 网球",
                "baseball": "⚾ 棒球", "american_football": "🏈 美式足球"}

    now_str = datetime.now(timezone.utc).astimezone().strftime("%m/%d %H:%M")
    total_allocated = sum(o["_stake"] for o in qualified)

    # 一致性警告 — 显示在正文最前面
    warning_lines = []
    if warnings:
        for w in warnings:
            warning_lines.append(f"{w}")
        warning_lines.append("")  # 空行隔开

    lines = list(warning_lines)
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

        sport = o.get("sport", "")
        sport_label = SPORT_CN.get(sport, "")
        if sport != prev_sport:
            if prev_sport is not None:
                lines.append("")
            lines.append(sport_label)
            prev_sport = sport
            prev_league = None
            prev_match = None

        league = o.get("league", "")
        if league and league != prev_league:
            lines.append(f"  {league}")
            prev_league = league
            prev_match = None

        if match_key != prev_match:
            match_idx += 1
            bj_time = _format_bj_time(o.get("_pin_epoch"))
            time_suffix = f"  ({bj_time})" if bj_time else ""
            lines.append(
                f"  ##### #{match_idx} {o['home_cn']} 对 {o['away_cn']}{time_suffix}"
            )
            prev_match = match_key

        confidence = "✓" if o.get("_match_score", 0) >= 0.95 else "◷"
        lines.append(
            f"    [{oc}] {confidence} 公平价: {fair}" + (f" | Pinnacle: {pinny}" if o.get("pin_odds", 0) > 0 else " | 推导: 1X2") + f" | BB价: {bb_odds} | 溢价: +{ev_pct}% | 投注: ¥{stake:,}"
        )

    title = f"+EV 投注推荐: {match_idx} 场比赛"
    body = (
        f"**{title}**\n\n"
        f"扫描 {now_str} | ≥2% 溢价 | 总额 ¥{total_allocated:,}\n\n"
        + "\n".join(lines).strip()
    )
    body += "\n\n---\n💡 公平价 = Pinnacle去抽水赔率 | 溢价 = (BB - 公平价) / 公平价 | 赔率实时变动，以 Pinnacle 网站当前价为准"
    return body


def build_report():
    """构建格式化的 BB vs Pinnacle +EV 报告。返回 (body_text, qualified_opportunities)."""
    qualified = _collect_opportunities_from_file()
    if not qualified:
        return "no +EV opportunities (>=2%)", []
    qualified = _diversify_and_rank(qualified)
    if not qualified:
        return "no +EV opportunities after filtering", []
    # 一致性检查：对比上次各运动推送数，异常时追加警告
    warnings = _check_sport_consistency(qualified)
    body = _format_body(qualified, warnings)
    return body, qualified


# ── 推送去重 ──

def _make_fingerprint(o: dict) -> str:
    """为一条机会生成唯一指纹：sport|home|away|盘口|比赛日期"""
    match_date = ""
    ep = o.get("_pin_epoch")
    if ep:
        try:
            dt = datetime.fromtimestamp(ep, tz=timezone.utc)
            match_date = dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass
    return f"{o.get('sport','')}|{o.get('home_cn','')}|{o.get('away_cn','')}|{o.get('designation','')}|{match_date}"


def _load_fingerprints() -> set:
    if FINGERPRINT_FILE.exists():
        try:
            data = json.loads(FINGERPRINT_FILE.read_text())
            if isinstance(data, list):
                return set(data)
        except (json.JSONDecodeError, ValueError):
            pass
    return set()


def _save_fingerprints(fps: set):
    FINGERPRINT_FILE.write_text(
        json.dumps(sorted(fps), ensure_ascii=False, indent=2)
    )


def _filter_pushed(qualified: list) -> list:
    """过滤掉指纹已存在于 pushed_fingerprints.json 的机会。"""
    existing = _load_fingerprints()
    if not existing:
        return qualified
    new = []
    skipped = 0
    for o in qualified:
        fp = _make_fingerprint(o)
        if fp in existing:
            skipped += 1
        else:
            new.append(o)
    if skipped:
        logger.info("去重过滤: 跳过 %d 条已推送机会", skipped)
    return new


def push_report(place_bets=False):
    if not DINGTALK_WEBHOOK:
        logger.info("no DINGTALK_WEBHOOK configured")
        return

    # 收集 → 去重 → 格式化 → 推送
    qualified = _collect_opportunities_from_file()
    if not qualified:
        logger.info("no +EV opportunities found")
        return
    qualified = _diversify_and_rank(qualified)
    if not qualified:
        logger.info("no +EV opportunities after filtering")
        return
    qualified = _filter_pushed(qualified)
    if not qualified:
        logger.info("所有机会均已推送过，跳过")
        return

    # 一致性检查：对比上次各运动推送数，异常时追加警告
    warnings = _check_sport_consistency(qualified)
    body = _format_body(qualified, warnings)
    if not body:
        logger.info("empty body, skip")
        return

    # 保存推送机会列表到暂存文件
    if place_bets and len(qualified) >= 10:
        from src.betting.bb_virtual_bet import PUSH_STAGING_FILE, place_bets_from_push
        PUSH_STAGING_FILE.write_text(json.dumps(qualified, ensure_ascii=False, indent=2))
        logger.info("推送机会已暂存到 %s，开始投注...", PUSH_STAGING_FILE)
        place_bets_from_push(qualified)
    elif place_bets and len(qualified) < 10:
        logger.info("机会不足10场(%d场)，跳过虚拟投注", len(qualified))

    from config.settings import send_dingtalk
    title = f"+EV 投注推荐: {body.count('#####')} 条"
    ok = send_dingtalk(title, body)
    if ok:
        new_fps = {_make_fingerprint(o) for o in qualified}
        existing = _load_fingerprints()
        existing.update(new_fps)
        _save_fingerprints(existing)
        logger.info("BB vs Pinnacle +EV report pushed (%d opportunities, %d new)", body.count('#####'), len(new_fps))
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
