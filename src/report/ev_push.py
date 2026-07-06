"""从缓存结果推送 +EV 投注建议到钉钉。

格式:
  **+EV 投注推荐: {n} 条**
  扫描时间 | ≥3% 溢价
  ##### #{i} {home} vs {away}（联赛）时间
  > [{outcome}] 公平价 | 零售 | 溢价 | 投注额
  ---
  💡 BB赔率 > 公平价 = +EV | 零售=市场最佳价(非推荐)
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging, get_logger
from config.settings import DATA_DIR, DINGTALK_WEBHOOK, TRUSTED_LEAGUES
from src.core.team_names import cn_team

logger = get_logger(__name__)
RESULTS_FILE = DATA_DIR / "line_shopping_results.json"
BB_RESULTS_FILE = DATA_DIR / "bb_line_shopping_results.json"
SEEN_FILE = DATA_DIR / "pushed_fingerprints.json"

BANKROLL = 10000
MAX_BETS = 12


def _make_fingerprint(o: dict) -> str:
    """生成机会的唯一指纹，用于去重。"""
    sport = o.get("sport", "football")
    market = o.get("market", "?")
    home = o.get("home_team", "?")
    away = o.get("away_team", "?")
    outcome = o.get("outcome", "?")
    pt = o.get("point") or o.get("line") or ""
    return f"{sport}|{market}|{home}|{away}|{outcome}|{pt}"


def _load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def _save_seen(seen: set):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False))


# 联赛中文名映射
_LEAGUE_CN = {
    "World Cup 2026": "2026年世界杯",
    "world cup 2026": "2026年世界杯",
    "Premier League": "英超",
    "English Premier League": "英超",
    "La Liga": "西甲",
    "Spain La Liga": "西甲",
    "Bundesliga": "德甲",
    "German Bundesliga": "德甲",
    "Serie A": "意甲",
    "Italy Serie A": "意甲",
    "Ligue 1": "法甲",
    "France Ligue 1": "法甲",
    "England Championship": "英冠",
    "Spain Segunda Division": "西乙",
    "German 2. Bundesliga": "德乙",
    "Eredivisie": "荷甲",
    "Netherlands Eredivisie": "荷甲",
    "Primeira Liga": "葡超",
    "Portugal Primeira Liga": "葡超",
    "Champions League": "欧冠",
    "UEFA Champions League": "欧冠",
    "Europa League": "欧联",
    "UEFA Europa League": "欧联",
    "Brazil Campeonato": "巴甲",
    "Copa Libertadores": "解放者杯",
    "K League 1": "韩职",
    "J1 League": "日职",
    "Allsvenskan": "瑞典超",
    "Veikkausliiga": "芬超",
    "Eliteserien": "挪威超",
    "MLS": "美职联",
    "Major League Soccer": "美职联",
    "Brasileirão Serie A": "巴甲",
    "Brazil Serie A": "巴甲",
}


def _cn_league(en: str) -> str:
    if not en:
        return ""
    return _LEAGUE_CN.get(en, en)


# 格式验证标记 — 不可删除，用于运行时检查推送格式是否被意外修改
_FORMAT_MARKERS = {
    "header_prefix": "**+EV 投注推荐:",
    "entry_prefix": "##### #",
    "fair_price": "公平价:",
    "retail": "零售:",
    "edge": "溢价:",
    "stake": "投注:",
    "footer": "BB赔率",
}


def _validate_format(body: str) -> bool:
    """验证推送内容包含所有必要格式标记，防止意外格式变更。"""
    checks = [
        body.startswith(_FORMAT_MARKERS["header_prefix"]),
        _FORMAT_MARKERS["entry_prefix"] in body,
        _FORMAT_MARKERS["fair_price"] in body,
        _FORMAT_MARKERS["retail"] in body,
        _FORMAT_MARKERS["edge"] in body,
        _FORMAT_MARKERS["stake"] in body,
        _FORMAT_MARKERS["footer"] in body,
    ]
    return all(checks)


_OUTCOME_CN = {"home": "主胜", "draw": "平局", "away": "客胜"}


def _opp_label(o: dict) -> str:
    """市场+结果中文标签。"""
    mkt = o.get("market", "1x2")
    outcome = o.get("outcome", "")
    label = o.get("outcome_label", "")
    if label:
        return label
    if mkt == "over_under":
        if outcome.startswith("over"):
            return f"大{outcome.split('_')[1]}"
        if outcome.startswith("under"):
            return f"小{outcome.split('_')[1]}"
    if mkt == "btts":
        return "双方进球" if outcome == "yes" else "不进球"
    if mkt == "corners_1x2":
        return _OUTCOME_CN.get(outcome, outcome)
    if mkt in ("1x2", "double_chance"):
        return _OUTCOME_CN.get(outcome, outcome)
    return outcome


def _fmt_time(ct: str) -> str:
    try:
        dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return ct[:10]


def _ct_timestamp(ct: str) -> float:
    try:
        return datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def _calc_stakes(opps: list, bankroll: float = BANKROLL) -> list:
    """按 Kelly 比例分配投注额。"""
    total_kelly = sum(o.get("kelly_pct", 0) for o in opps)
    if total_kelly <= 0:
        for o in opps:
            o["_stake"] = 0
        return opps
    for o in opps:
        k = o.get("kelly_pct", 0)
        stake = round((k / total_kelly) * bankroll)
        if stake < 5:
            stake = 0
        o["_stake"] = stake
    return opps


def build_ev_report(seen_set: set = None) -> tuple:
    """构建合并推送报告（足球+篮球），返回 (body, new_fingerprints)。

    seen_set: 已推送过的指纹集合，用于去重。None=不过滤。
    """
    if not RESULTS_FILE.exists():
        return "line_shopping_results.json not found", set()

    data = json.loads(RESULTS_FILE.read_text())
    opps = data.get("opportunities", [])

    now = datetime.now(timezone.utc)
    qualified = [
        o for o in opps
        if o.get("edge_pct", 0) >= 3
        and o.get("market", "") not in ("draw_no_bet",)
        and o.get("commence_time")
        and o.get("league", "") in TRUSTED_LEAGUES
    ]

    qualified.sort(key=lambda x: x["edge_pct"], reverse=True)
    qualified = _calc_stakes(qualified[:MAX_BETS])
    qualified = [o for o in qualified if o["_stake"] > 0]

    # 篮球
    now_ts = now.timestamp()
    bb_all = []
    if BB_RESULTS_FILE.exists():
        bb_data = json.loads(BB_RESULTS_FILE.read_text())
        bb_all = [
            o for o in bb_data.get("opportunities", [])
            if o.get("edge_pct", 0) >= 3
            and o.get("commence_time")
            and _ct_timestamp(o["commence_time"]) > now_ts
        ]
        bb_all.sort(key=lambda x: x["edge_pct"], reverse=True)
        bb_all = bb_all[:8]

    # 去重：按指纹过滤已推送过的
    new_fps = set()
    if seen_set is not None:
        fb_new = []
        for o in qualified:
            fp = _make_fingerprint(o)
            if fp not in seen_set:
                fb_new.append(o)
                new_fps.add(fp)
        qualified = fb_new

        bb_new = []
        for o in bb_all:
            fp = _make_fingerprint(o)
            if fp not in seen_set:
                bb_new.append(o)
                new_fps.add(fp)
        bb_all = bb_new

    total_items = len(qualified) + len(bb_all)
    if total_items == 0:
        if seen_set is not None:
            return "no new +EV opportunities", set()
        return "no +EV opportunities", set()

    # 按比赛分组（足球）
    from collections import defaultdict
    by_match = defaultdict(list)
    for o in qualified:
        key = (o.get("home_team", ""), o.get("away_team", ""), o.get("commence_time", ""))
        by_match[key].append(o)

    now_str = now.strftime("%m/%d %H:%M")
    total_allocated = sum(o["_stake"] for o in qualified)
    fb_count = len(qualified)
    lines = []

    # 足球段落
    if qualified:
        for idx, ((home, away, ct), bets) in enumerate(
            sorted(by_match.items(), key=lambda x: max(b.get("edge_pct", 0) for b in x[1]), reverse=True), 1
        ):
            h_cn = cn_team(home, "football")
            a_cn = cn_team(away, "football")
            tc = _fmt_time(ct)
            league = bets[0].get("league", "")
            match_total = sum(b["_stake"] for b in bets)

            lines.append(f"##### #{idx} {h_cn} 对 {a_cn}（{_cn_league(league)}）{tc}")
            for b in bets:
                oc = _opp_label(b)
                fair = round(1.0 / b.get("model_prob", 0.5), 2) if b.get("model_prob", 0) > 0 else "?"
                retail = b.get("odds", "-")
                ev_pct = b["edge_pct"]
                stake = b["_stake"]
                lines.append(
                    f"> [{oc}] 公平价: {fair} | 零售: {retail} | 溢价: +{ev_pct}% | 投注: ¥{stake:,}"
                )
            if len(bets) > 1:
                lines.append(f"> **本场合计: ¥{match_total:,}**")
            lines.append("")

    # 篮球段落
    if bb_all:
        lines.append(f"\n**🏀 WNBA +EV 机会: {len(bb_all)} 条**\n")
        for i, o in enumerate(bb_all, 1):
            h_cn = cn_team(o["home_team"], "basketball")
            a_cn = cn_team(o["away_team"], "basketball")
            mkt = o.get("market_label", o.get("market", ""))
            oc = o.get("outcome_label", o.get("outcome", ""))
            pt = o.get("point")
            pt_str = f"@{pt:+.1f}" if pt is not None else ""
            fair = o.get("fair_price", "?")
            retail = o.get("retail_odds", "-")
            ev = o["edge_pct"]
            tc = _fmt_time(o.get("commence_time", ""))

            lines.append(f"##### #{fb_count + i} {h_cn} 对 {a_cn}（WNBA）{tc}")
            lines.append(f"> [{mkt} {oc}{pt_str}] 公平价: {fair} | 零售: {retail} | 溢价: +{ev}% | 投注: -")
        lines.append("")

    title = f"+EV 投注推荐: {total_items} 条"
    body = (
        f"**{title}**\n\n"
        f"扫描 {now_str} | ≥3% 溢价{' | 总额 ¥' + f'{total_allocated:,}' if total_allocated else ''}\n\n"
        + "\n".join(lines).strip()
    )
    body += "\n\n---\n💡 BB赔率 > **公平价** = +EV | 零售=市场最佳价(非推荐)"

    return body, new_fps


def push_ev_report():
    setup_logging()
    if not DINGTALK_WEBHOOK:
        logger.info("no webhook configured")
        return

    seen = _load_seen()
    body, new_fps = build_ev_report(seen)
    if body.startswith("no") or body.startswith("line"):
        logger.info("no push needed: %s", body)
        return

    if not _validate_format(body):
        logger.error("推送格式验证失败！阻止发送。body=%s...", body[:100])
        return

    title = f"+EV 投注推荐: {body.count('#####')} 条"
    from config.settings import send_dingtalk
    ok = send_dingtalk(title, body)
    if ok:
        seen.update(new_fps)
        _save_seen(seen)
        logger.info("+EV report pushed, %d new fingerprints saved", len(new_fps))
    else:
        logger.warning("+EV push failed")


if __name__ == "__main__":
    push_ev_report()
