"""BB 比分结算 — 用 BB 自己的赛果结算投注 (解决 ESPN 覆盖不到的联赛)。

BB API /v1/match/getList type=6 返回已结束比赛, nsg 字段含比分:
  pe=1000(全场) + tyg=5(比分) → sc=[主队, 客队]

用法: python3 -m src.monitor.bb_score_settle [--dry-run]
"""
import json
from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

# 各运动"全场比分"的 pe 码 (tyg=5 为比分): 足球=1000, 篮球=3001, 棒球=7001, 美足=6001
# (棒球/美足 type=6 记录其实也有 nsg 字段 — 2026-08-16 探测确认, 之前注释有误)
FULLTIME_PE = {"football": 1000, "basketball": 3001,
               "baseball": 7001, "american_football": 6001}


def fetch_bb_scores():
    """拉 BB 已结束比赛的比分 → {(home_cn, away_cn): [home_score, away_score]}"""
    from src.scrapers.bb_api_fetcher import api_post, SPORTS

    score_map = {}
    for sid, sport_key, sport_cn in SPORTS:
        params = {"sportId": sid, "type": 6, "current": 1, "pageSize": 100,
                  "isPC": True, "languageType": "CMN"}
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
                                score_map[(home, away)] = [int(sc[0]), int(sc[1])]
                            except (ValueError, TypeError):
                                pass  # 非数字比分(如加时 "3+1"), 跳过该场
                        break
    return score_map


def settle_via_bb(dry_run: bool = False) -> dict:
    """用 BB 比分结算 pending 且比赛已结束的投注。"""
    from src.monitor.result_fetcher import determine_result
    from src.monitor.bet_tracker import get_unsettled_bets, settle_bet

    score_map = fetch_bb_scores()
    if not score_map:
        logger.info("BB 未拉到已结束比分")
        return {"settled": 0, "failed": 0, "matched": 0}

    bets = get_unsettled_bets(hours_after_match=1.0)
    if not bets:
        return {"settled": 0, "failed": 0, "matched": 0}

    settled = 0
    matched = 0
    for b in bets:
        # V5.5: 优先用中文名(home_cn)匹配, 兼容旧中文投注和新英文投注(带home_cn)
        home = (b.get("home_cn") or b.get("home", "") or "").strip()
        away = (b.get("away_cn") or b.get("away", "") or "").strip()
        sc = score_map.get((home, away))
        swapped = False
        if sc is None:
            sc = score_map.get((away, home))
            swapped = True
        if not sc:
            continue
        matched += 1
        hs, as_ = sc
        if swapped:
            hs, as_ = as_, hs
        match_result = {"home_score": hs, "away_score": as_}
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

    return {"settled": settled, "failed": 0, "matched": matched}


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    r = settle_via_bb(dry_run=dry)
    print(json.dumps(r, ensure_ascii=False, indent=2))
