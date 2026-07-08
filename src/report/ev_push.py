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
MLB_RESULTS_FILE = DATA_DIR / "mlb_line_shopping_results.json"
TENNIS_RESULTS_FILE = DATA_DIR / "tennis_line_shopping_results.json"
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
    "basketball_wnba": "WNBA",
    "basketball_nba": "NBA",
    "basketball_euroleague": "EuroLeague",
    "tennis_atp_wimbledon": "ATP温网",
    "tennis_wta_wimbledon": "WTA温网",
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
        display = label
    elif mkt == "over_under":
        if outcome.startswith("over"):
            display = f"大{outcome.split('_')[1]}"
        elif outcome.startswith("under"):
            display = f"小{outcome.split('_')[1]}"
        else:
            display = outcome
    elif mkt == "btts":
        display = "双方进球" if outcome == "yes" else "不进球"
    elif mkt == "corners_1x2":
        display = _OUTCOME_CN.get(outcome, outcome)
    elif mkt in ("1x2", "double_chance"):
        display = _OUTCOME_CN.get(outcome, outcome)
    else:
        display = outcome

    # 非足球玩法加市场前缀（如"独赢 主胜"、"让分盘 客胜+5.5"）
    if o.get("sport") in ("basketball", "baseball", "tennis"):
        mkt_label = o.get("market_label", "")
        pt_str = o.get("point_str", "") or ""
        if mkt_label:
            return f"{mkt_label} {display}{pt_str}"

    return display


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
    total_kelly = sum(o.get("kelly_pct") or 0 for o in opps)
    if total_kelly <= 0:
        for o in opps:
            o["_stake"] = 0
        return opps
    for o in opps:
        k = o.get("kelly_pct") or 0
        stake = round((k / total_kelly) * bankroll)
        if stake < 5:
            stake = 0
        o["_stake"] = stake
    return opps


def build_ev_report(seen_set: set = None) -> tuple:
    """构建足球 +EV 推送报告，返回 (body, new_fingerprints)。

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

    # === 篮球 +EV 机会 ===
    if BB_RESULTS_FILE.exists():
        bb_data = json.loads(BB_RESULTS_FILE.read_text())
        for o in bb_data.get("opportunities", []):
            if o.get("edge_pct", 0) >= 3:
                ct = o.get("commence_time", "")
                if ct:
                    try:
                        dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                        if dt < now:
                            continue
                    except Exception:
                        pass
                # 篮球没有 kelly_pct，按标准 Kelly 公式补齐
                if o.get("kelly_pct") is None:
                    retail = o.get("retail_odds", 0)
                    if retail > 1:
                        kelly = (o["edge_pct"] / 100) / (retail - 1) * 0.25
                        o["kelly_pct"] = round(kelly * 100, 2)
                    else:
                        o["kelly_pct"] = 0
                qualified.append(o)

    # === 棒球 +EV 机会 ===
    for rf, label in [(MLB_RESULTS_FILE, "棒球"), (TENNIS_RESULTS_FILE, "网球")]:
        if rf.exists():
            try:
                sdata = json.loads(rf.read_text())
                for o in sdata.get("opportunities", []):
                    if o.get("edge_pct", 0) >= 3:
                        ct = o.get("commence_time", "")
                        if ct:
                            try:
                                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                                if dt < now:
                                    continue
                            except Exception:
                                pass
                        if o.get("kelly_pct") is None:
                            retail = o.get("retail_odds", 0)
                            if retail > 1:
                                kelly = (o["edge_pct"] / 100) / (retail - 1) * 0.25
                                o["kelly_pct"] = round(kelly * 100, 2)
                            else:
                                o["kelly_pct"] = 0
                        qualified.append(o)
            except Exception:
                pass

    qualified.sort(key=lambda x: x["edge_pct"], reverse=True)
    qualified = _calc_stakes(qualified[:MAX_BETS])
    qualified = [o for o in qualified if o["_stake"] > 0]

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

    if not qualified:
        if seen_set is not None:
            return "no new +EV opportunities", set()
        return "no +EV opportunities", set()

    # 同一场比赛的 double_chance 只保留 edge 最高的那条
    dc_groups = {}
    deduped = []
    for o in qualified:
        if o.get("market") == "double_chance":
            key = (o.get("home_team", ""), o.get("away_team", ""), o.get("commence_time", ""))
            if key in dc_groups:
                if o["edge_pct"] > dc_groups[key]["edge_pct"]:
                    deduped.remove(dc_groups[key])
                    dc_groups[key] = o
                    deduped.append(o)
            else:
                dc_groups[key] = o
                deduped.append(o)
        else:
            deduped.append(o)
    qualified = deduped

    # 按比赛分组
    from collections import defaultdict
    by_match = defaultdict(list)
    for o in qualified:
        key = (o.get("home_team", ""), o.get("away_team", ""), o.get("commence_time", ""))
        by_match[key].append(o)

    now_str = now.strftime("%m/%d %H:%M")
    total_allocated = sum(o["_stake"] for o in qualified)
    lines = []

    for idx, ((home, away, ct), bets) in enumerate(
        sorted(by_match.items(), key=lambda x: max(b.get("edge_pct", 0) for b in x[1]), reverse=True), 1
    ):
        sport = bets[0].get("sport", "football")
        h_cn = cn_team(home, sport)
        a_cn = cn_team(away, sport)
        tc = _fmt_time(ct)
        league = bets[0].get("league", "")
        match_total = sum(b["_stake"] for b in bets)

        lines.append(f"##### #{idx} {h_cn} 对 {a_cn}（{_cn_league(league)}）{tc}")
        for b in bets:
            oc = _opp_label(b)
            # 兼容足球(model_prob/odds)和篮球(fair_price/retail_odds)的字段名
            if b.get("fair_price") is not None:
                fair = round(b["fair_price"], 2)
            elif b.get("model_prob", 0) > 0:
                fair = round(1.0 / b["model_prob"], 2)
            else:
                fair = "?"
            retail = b.get("retail_odds") or b.get("odds", "-")
            ev_pct = b["edge_pct"]
            stake = b["_stake"]
            lines.append(
                f"> [{oc}] 公平价: {fair} | 零售: {retail} | 溢价: +{ev_pct}% | 投注: ¥{stake:,}"
            )
        if len(bets) > 1:
            lines.append(f"> **本场合计: ¥{match_total:,}**")
        lines.append("")

    title = f"+EV 投注推荐: {len(qualified)} 条"
    body = (
        f"**{title}**\n\n"
        f"扫描 {now_str} | ≥3% 溢价 | 总额 ¥{total_allocated:,}\n\n"
        + "\n".join(lines).strip()
    )
    body += "\n\n---\n💡 BB赔率 > **公平价** = +EV | 零售=市场最佳价(非推荐)"

    return body, new_fps


def _check_body_chinese(body: str) -> list:
    """检查推送内容是否有英文名残留，返回问题列表。"""
    import re
    issues = []
    for line in body.split("\n"):
        if line.startswith("#####"):
            m = re.search(r"##### #\d+ (.+?)（", line)
            if m:
                name_part = m.group(1)
                english_words = re.findall(r"\b[A-Z][a-z]{2,}\b", name_part)
                if english_words:
                    issues.append(f"英文名残留: {' '.join(english_words[:3])} ← {line.strip()[:60]}")
    return issues


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

    # 中文自检：发现英文队名/球员名残留时报警
    en_issues = _check_body_chinese(body)
    if en_issues:
        for issue in en_issues:
            logger.warning("⚠️ %s", issue)
        logger.warning("推送内容有英文名残留，请检查 team_names.py 是否缺少映射")
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
