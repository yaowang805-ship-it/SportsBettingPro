"""BB 比分结算 — 用 BB 自己的赛果结算投注 (解决 ESPN 覆盖不到的联赛)。

BB API /v1/match/getList type=6 返回已结束比赛, nsg 字段含比分:
  pe=1000(全场) + tyg=5(比分) → sc=[主队, 客队]

用法: python3 -m src.monitor.bb_score_settle [--dry-run]
"""
import json
from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

# 各运动"全场比分"的 pe 码 (tyg=5 为比分): 足球=1000, 篮球=3001, 网球=5000(盘分), 棒球=7001, 美足=6001
# (棒球/美足 type=6 记录其实也有 nsg 字段 — 2026-08-16 探测确认, 之前注释有误)
FULLTIME_PE = {"football": 1000, "basketball": 3001, "tennis": 5000,
               "baseball": 7001, "american_football": 6001}


def fetch_bb_scores():
    """拉 BB 已结束比赛的比分 → {(home_cn, away_cn): [home_score, away_score]}"""
    from src.scrapers.bb_api_fetcher import api_post, SPORTS

    score_map = {}
    id_map = {}  # BB match id → [home_score, away_score] (精确匹配, 免队名错配)
    for sid, sport_key, sport_cn in SPORTS:
        # 同时拉中英文名, 兼容旧中文投注(CMN)和新英文投注(EN, 08-16切换英文队名)
        for lang in ("CMN", "EN"):
            params = {"sportId": sid, "type": 6, "current": 1, "pageSize": 100,
                      "isPC": True, "languageType": lang}
            try:
                resp = api_post("/v1/match/getList", params, platform="BB")
            except Exception:
                continue
            if not resp or not resp.get("success"):
                continue
            data = resp.get("data", {})
            pages = data.get("pageTotal", 1)
            for page in range(1, pages + 1):
                if page > 1:
                    params["current"] = page
                    try:
                        resp = api_post("/v1/match/getList", params, platform="BB")
                    except Exception:
                        break
                    if not resp or not resp.get("success"):
                        break
                    records = resp.get("data", {}).get("records", [])
                else:
                    records = data.get("records", [])
                for rec in records:
                    teams = rec.get("ts", [])
                    if len(teams) < 2:
                        continue
                    mid = rec.get("id", "")
                    home = teams[0].get("na", "")
                    away = teams[1].get("na", "")
                    if not home or not away:
                        continue
                    # 各运动"全场比分"的 pe 码 (tyg=5 为比分): 足球=1000, 篮球=3001
                    pe_full = FULLTIME_PE.get(sport_key)
                    if not pe_full:
                        continue
                    for sg in rec.get("nsg", []):
                        if sg.get("pe") == pe_full and sg.get("tyg") == 5:
                            sc = sg.get("sc", [])
                            if len(sc) >= 2:
                                try:
                                    hs_as = [int(sc[0]), int(sc[1])]
                                    score_map[(home, away)] = hs_as
                                    if mid:
                                        id_map[str(mid)] = hs_as
                                except (ValueError, TypeError):
                                    pass  # 非数字比分(如加时 "3+1"), 跳过该场
                            break
    return score_map, id_map


def settle_via_bb(dry_run: bool = False) -> dict:
    """用 BB 比分结算 pending 且比赛已结束的投注。

    V5.8: 首选 getMatchDetail 按 bb_match_id 逐场拉比分(可靠, 已结束比赛仍可查);
    type=6 批量仅作兜底 — 实测 type=6 只返回 bt∈[now-1h, now+10h] 窗口(多数 ms=4 未开赛),
    已结束>1h 的比赛会掉出窗口导致漏结算, 故不能再作为主源。
    """
    from src.monitor.result_fetcher import determine_result
    from src.monitor.bet_tracker import get_unsettled_bets, settle_bet
    from src.scrapers.bb_api_fetcher import fetch_bb_match_result

    bets = get_unsettled_bets(hours_after_match=1.0)
    if not bets:
        return {"settled": 0, "failed": 0, "matched": 0}

    # 兜底: type=6 批量(仅覆盖最近~1h 内开始、仍在线/刚结束的比赛)
    score_map, id_map = fetch_bb_scores()

    settled = 0
    matched = 0
    addressable = 0   # BB 确实给了比分的注数(静默失效告警的分母)
    for b in bets:
        home = (b.get("home_cn") or b.get("home", "") or "").strip()
        away = (b.get("away_cn") or b.get("away", "") or "").strip()
        _bid = str(b.get("bb_match_id") or "").strip()
        sc = None
        swapped = False
        ht_sc = None   # V5.10: 半场比分, 供 ht/ht_hc/ht_ou/ht_dc 盘口判定
        # 1. 首选: getMatchDetail 按 bb_match_id 逐场拉(可靠, 已结束比赛仍可查, 无窗口限制)
        if _bid:
            _detail = fetch_bb_match_result(_bid, language_type="EN")
            # 跨运动 id 冲突(2026-08-25 实测): BB 的 id 不唯一, 足球 bb_match_id 可能被
            # getMatchDetail 查成乒乓球。若返回的 sport 与投注 sport 不符, 丢弃(不能拿
            # 别的运动比分去结算)。
            if (_detail and _detail.get("sport") and b.get("sport")
                    and _detail["sport"] != b["sport"]):
                _detail = None
            # 静默失效判据: BB 确实返回了这场的数据(有比分)才算"本可结算"。
            # BB 的 getMatchDetail 对过老的 id 会返回空壳(ms=None/nsg=0), 那种
            # 属于数据过期、不是链路故障, 不能计入告警分母 —— 否则会对着一堆
            # 永远救不回来的老账天天叫, 告警一旦变噪声就等于没做。
            if _detail and _detail.get("home_score") is not None:
                addressable += 1
            if (_detail and _detail.get("completed")
                    and _detail.get("home_score") is not None
                    and _detail.get("away_score") is not None):
                sc = [_detail["home_score"], _detail["away_score"]]
                if _detail.get("ht_home_score") is not None:
                    ht_sc = [_detail["ht_home_score"], _detail["ht_away_score"]]
        # 2. 兜底: type=6 id_map 精确匹配(免队名错配)
        if sc is None:
            sc = id_map.get(_bid) if _bid else None
        # 3. 回退队名匹配: 中文名 + BB英文名 + Pin英文名, 逐个尝试(兼容新旧投注命名)
        if sc is None:
            _candidates = [
                (home, away),
                ((b.get("home") or "").strip(), (b.get("away") or "").strip()),
                ((b.get("home_pin") or "").strip(), (b.get("away_pin") or "").strip()),
            ]
            for _ch, _ca in _candidates:
                if not _ch or not _ca:
                    continue
                sc = score_map.get((_ch, _ca))
                if sc is not None:
                    swapped = False
                    break
                sc = score_map.get((_ca, _ch))
                if sc is not None:
                    swapped = True
                    break
        if not sc:
            continue
        matched += 1
        hs, as_ = sc
        if swapped:
            hs, as_ = as_, hs
        match_result = {"home_score": hs, "away_score": as_}
        if ht_sc:
            _hh, _ha = (ht_sc[1], ht_sc[0]) if swapped else (ht_sc[0], ht_sc[1])
            match_result["ht_home_score"], match_result["ht_away_score"] = _hh, _ha
        try:
            result, hs2, as2, mult = determine_result(b, match_result)
        except Exception as e:
            logger.warning("判定失败 %s: %s", b.get("push_id", ""), e)
            continue
        if dry_run:
            logger.info("[dry] %s vs %s %d:%d → %s (%s)", home, away, hs2, as2, result, b.get("designation", ""))
        else:
            settle_bet(b["push_id"], result, hs2, as2, source="bb")
        settled += 1

    # 静默失效监控: 有待结算注却一笔都匹配不上 → 连续多次即告警。
    # 这条链路曾因 completed 判定漏掉 ms=0 而完全失效, 从 8/15 死到 8/19,
    # 期间每 30 分钟正常运行、正常退出、日志写着 matched:0, 无人发现。
    if not dry_run:
        try:
            from src.monitor.silent_failure_watch import record_run
            record_run("bb_score_settle", produced=matched, expected=addressable,
                       detail=(f"BB 已返回比分的 {addressable} 笔注, 一笔都没能结算。"
                               f"排查方向: getMatchDetail 的 completed 判定 / "
                               f"determine_result 盘口分支 / 队名匹配。"))
        except Exception:
            pass

    return {"settled": settled, "failed": 0, "matched": matched}


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    r = settle_via_bb(dry_run=dry)
    print(json.dumps(r, ensure_ascii=False, indent=2))
