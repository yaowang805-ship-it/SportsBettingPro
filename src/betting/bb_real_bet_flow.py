"""BB 全自动下单流程(2026-09-05)。

从 +EV 机会列表 → 匹配 getList 比赛 → 找 marketId → 真实下单 → 钉钉通知。

机会对象字段(来自 bb_ev_push / comparison):
    home_cn / away_cn: 中文队名
    sub_market: 盘口(1x2/hc/ou/dc/ht...)
    designation: 方向(主胜/客胜/和局/大球/让球主胜...)
    _stake: 注额
    bb_odds: 推送时的赔率

用法:
    from src.betting.bb_real_bet_flow import auto_bet_flow
    results = auto_bet_flow(opportunities)
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.betting.bb_auto_bet import (
    read_token, read_domain, place_single_bet, find_market_from_match,
    MAX_MATCH_STAKE, MAX_MARKET_STAKE,
)
from config.settings import DATA_DIR

# 运动英文名 → 中文名(推送展示用)
SPORT_CN = {"football": "足球", "basketball": "篮球", "tennis": "网球",
            "baseball": "棒球", "american_football": "美式足球", "ice_hockey": "冰球"}

# 2026-10-17 用户要求取消早盘日限额(原 25笔/600元), 改为无限(风控改由单注上限+硬性止损承担)
EARLY_DAILY_MAX_BETS = float('inf')
EARLY_DAILY_MAX_STAKE = float('inf')
DAILY_EXPOSURE_FILE = DATA_DIR / "daily_exposure.json"


def _load_daily_exposure():
    """读当日已投笔数/金额(跨调用持久化, 按北京日期重置)。"""
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    try:
        if DAILY_EXPOSURE_FILE.exists():
            d = json.loads(DAILY_EXPOSURE_FILE.read_text())
            if d.get("date") == today:
                return d.get("bets", 0), d.get("stake", 0.0)
    except (json.JSONDecodeError, OSError):
        pass
    return 0, 0.0


def _add_daily_exposure(bets_delta, stake_delta):
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    bets, stake = _load_daily_exposure()
    bets += bets_delta; stake += stake_delta
    try:
        DAILY_EXPOSURE_FILE.write_text(json.dumps({"date": today, "bets": bets, "stake": round(stake, 2)}))
    except OSError:
        pass


def _win_label(epoch):
    """时间窗标签(推送用): 滚球(已开赛/无epoch)/临场<6h/近场6-24h/远场24-72h。"""
    if not epoch:
        return "滚球"
    try:
        lead = float(epoch) - time.time()
    except (TypeError, ValueError):
        return "滚球"
    if lead < 0:
        return "滚球"
    if lead < 6 * 3600:
        return "临场"
    if lead < 24 * 3600:
        return "近场"
    return "远场"


_get_all_matches_cache = {"ts": 0.0, "records": [], "token": ""}


def _get_all_matches(token, domain, cache_ttl=180):
    """getList 拉全量比赛(英文名), 返回 list[record]。用 EN 与对比引擎 home_bb 英文直配。

    2026-10-18 加 3min 缓存: 之前每次 auto_bet_flow 都重拉 5 运动分页(~26s), ws_scan 每 30s
    扫一轮 → 重复拉取浪费。早盘比赛列表几分钟内不变, 缓存省 26s。token 变化失效; 拉取失败用旧缓存兜底。
    """
    global _get_all_matches_cache
    now = time.time()
    if (_get_all_matches_cache["records"] and _get_all_matches_cache["token"] == token
            and now - _get_all_matches_cache["ts"] < cache_ttl):
        return _get_all_matches_cache["records"]
    import requests
    import urllib3
    urllib3.disable_warnings()
    s = requests.Session(); s.trust_env = False
    s.proxies = {"http": "", "https": ""}
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36")
    all_recs = []
    for sport_id in (1, 3, 5, 7, 6):  # 足球/篮球/网球/棒球/美足
        page = 1
        while page <= 40:  # 分页拉全量(每页50场, 足球1876场=38页)
            try:
                r = s.post(f"{domain}/v1/match/getList",
                           json={"sportId": sport_id, "type": 2, "current": page, "pageSize": 50,
                                 "isPC": True, "languageType": "EN"},
                           headers={"Content-Type": "application/json", "user-token": token,
                                    "User-Agent": UA}, timeout=15, verify=False)
                d = r.json()
                if d.get("code") != 0:
                    break
                data = d.get("data") or {}
                recs = data.get("records") or []
                for _rec in recs:
                    _rec["_sport_id"] = sport_id  # 2026-10-15 打标运动id, 供 find_market_from_match 按运动找盘口码
                all_recs.extend(recs)
                page_total = data.get("pageTotal") or 1
                if page >= page_total or not recs:
                    break
                page += 1
            except Exception:
                break
    if all_recs:
        _get_all_matches_cache = {"ts": now, "records": all_recs, "token": token}
    else:
        # 拉取失败/空 → 旧缓存兜底(避免缓存过期+拉取失败=无数据可匹配)
        if _get_all_matches_cache["records"]:
            return _get_all_matches_cache["records"]
    return all_recs


def _find_match(records, home_cn, away_cn):
    """按中文队名匹配 getList 比赛。返回 record 或 None。"""
    def norm(n):
        return (n or "").strip().replace(" ", "").lower()
    h = norm(home_cn); a = norm(away_cn)
    for m in records:
        ts = m.get("ts") or []
        if len(ts) < 2:
            continue
        mh = norm(ts[0].get("na", "")); ma = norm(ts[1].get("na", ""))
        if (h and a) and (h == mh and a == ma):
            return m
        # 队名可能只给主队名或客队名, 宽松匹配
        if h and (h in mh or mh in h) and a and (a in ma or ma in a):
            return m
    return None


def _sub_market_to_key(sub_market):
    """comparison 的 sub_market → find_market_from_match 的盘口 key。"""
    m = {
        "1x2": "1x2", "ml": "1x2",
        "hc": "hc", "handicap": "hc",
        "ou": "ou", "over_under": "ou",
        "dc": "dc", "double_chance": "dc",
    }
    return m.get(sub_market, sub_market)


def _designation_to_dir(sub_market, designation):
    """designation → find_market_from_match 的方向 key。"""
    d = (designation or "")
    if sub_market == "dc":
        if "和局/客" in d or "客/和" in d:
            return "和局/客"
        if "主/和" in d or "和局/主" in d:
            return "主/和"
        if "主/客" in d:
            return "主/客"
    if "大" in d:
        return "大"
    if "小" in d:
        return "小"
    if "和" in d or "平" in d:
        return "和"
    if "客" in d:
        return "客"
    if "主" in d:
        return "主"
    return None


# 2026-09-12 用户要求: 早盘恢复实盘, 按释放清单控制(观察库样本>100且赢率>隐含才释放)。
# bb_ev_push 推送前已过 _is_market_released 过滤, 这里只下单、不再硬编码盘口白名单。
EARLY_BET_ENABLED = True  # 2026-10-17 恢复: num_1已修(daemon确认num_0), 早盘上限改200


def _early_outstanding():
    """早盘未结算投注额(读 real_bets.json 里早盘注单 且 result=None 的注额)。

    2026-10-18 修: 之前读 tracked_bets.json(status=pending) 冻结在 810。改读 real_bets。
    注意 trigger_source 区分: 早盘=pin_drop/ws_scan, 滚球=pin/poll/betfair_ws(滚球也有 trigger_source,
    不能只用「trigger_source 非空」判断早盘, 否则把滚球 pin 也当早盘)。
    """
    try:
        d = json.loads((DATA_DIR / "real_bets.json").read_text())
        bets = d.get("bets", {})
        return sum(float(b.get("stake", 0) or 0) for b in bets.values()
                   if b.get("trigger_source") in ("pin_drop", "ws_scan") and b.get("result") is None)
    except Exception:
        return 0.0


def auto_bet_flow(opportunities, token=None, domain=None, trigger_ts=None, trigger_source="pin_drop"):
    """全自动下单。返回 {成功: [...], 失败: [...]}。

    trigger_ts(2026-10-11): Pin drop 触发时刻(供端到端「总耗时」=触发→下单完成, 对齐滚球口径)。
    None(pipeline 扫描路径无触发时刻)则兜底用下单 HTTP 累加。
    trigger_source(2026-10-18 双轨制): pin_drop(Pin骤降触发) / ws_scan(WS全量扫描触发),
    落盘到 real_bets 供区分两条流程优劣。
    """
    if not EARLY_BET_ENABLED:
        return {"success": [], "failed": [], "error": "早盘投注已暂停"}
    token = token or read_token()
    domain = domain or read_domain()
    if not token:
        return {"success": [], "failed": [], "error": "无 token"}

    # 2026-10-18 余额熔断(用户要求): 总余额<1000 早盘停, 滚球不受影响(滚球走 second_level_monitor 自己的预算)。
    try:
        from src.betting.bb_auto_bet import fetch_balance
        _bal = fetch_balance()
        if _bal is not None and _bal < 1000:
            return {"success": [], "failed": [], "error": f"余额 {_bal:.0f}<1000, 早盘暂停"}
    except Exception:
        pass

    records = _get_all_matches(token, domain)
    success, failed = [], []
    sent_dingtalk = []
    total_latency = 0.0  # 投注总耗时(实际下单 HTTP 时间累加, 2026-10-11 用户要求推送到钉钉)
    _daily_bets, _daily_stake = _load_daily_exposure()  # 2026-10-14 早盘探针日限额

    for opp in opportunities:
        # 匹配用英文(home_bb), 展示用中文(home_cn) —— 英文直配 getList, 中文只给钉钉看
        match_home = opp.get("home_bb") or opp.get("home_cn") or opp.get("home", "")
        match_away = opp.get("away_bb") or opp.get("away_cn") or opp.get("away", "")
        disp_home = opp.get("home_cn") or opp.get("home_bb") or opp.get("home", "")
        disp_away = opp.get("away_cn") or opp.get("away_bb") or opp.get("away", "")
        sub = opp.get("sub_market") or opp.get("_sub_market") or "1x2"
        desig = opp.get("designation", "")
        stake = float(opp.get("_stake") or opp.get("stake") or 0)
        # 铁律(2026-08-27 stake-min-and-24h-push-rules): stake<30 一律不投。
        # 之前 `or 10` fallback 把 stake=0 的机会投成 10 元碎单, 违反铁律。
        if stake < 30:
            failed.append({"home": disp_home, "away": disp_away,
                           "reason": f"stake<30 不投(¥{stake:.0f})"})
            continue

        # 2026-09-06 用户要求: 早盘实盘只投"未开赛", 已开赛(lead<0)的场跳过(与暂停滚球秒级对齐)
        # 2026-10-12 用户要求: 去掉 2h-24h 窗口限制——早盘赔率紧无 EV 机会, 临场<45min 才松,
        # 但临场<2h 被旧规则挡掉导致「早盘没机会下注」。改为「未开赛(lead>=0)即可投」。
        _ep = opp.get("_pin_epoch")
        if _ep:
            try:
                _lead = float(_ep) - time.time()
                if _lead < 0:
                    failed.append({"home": disp_home, "away": disp_away, "reason": "已开赛, 早盘跳过"})
                    continue
                # 2026-10-18 用户要求: 早盘只投 72h 以内的比赛(超远早盘不投)
                if _lead > 72 * 3600:
                    failed.append({"home": disp_home, "away": disp_away,
                                   "reason": f"距开赛 {_lead/3600:.0f}h>72h, 跳过"})
                    continue
            except (TypeError, ValueError):
                pass

        # 匹配比赛(英文名)
        match = _find_match(records, match_home, match_away)
        if not match:
            failed.append({"home": disp_home, "away": disp_away, "reason": "getList 未匹配到比赛"})
            continue
        match_id = match.get("id")

        # 找 marketId
        mk = find_market_from_match(match, _sub_market_to_key(sub), _designation_to_dir(sub, desig))
        if not mk:
            failed.append({"home": disp_home, "away": disp_away, "reason": f"未找到盘口 {sub}/{desig}"})
            continue
        market_id, odds, option_type = mk

        # 同场同盘口互斥锁(2026-09-19): 同一 match 同一盘口只下一注(防对立方向/多线重复下注)
        from src.betting.bb_auto_bet import check_sub_market_bet, record_sub_market_bet
        if check_sub_market_bet(match_id, sub, "prematch"):
            failed.append({"home": disp_home, "away": disp_away,
                           "reason": f"同场同盘口已投({sub}), 跳过对立/重复"})
            continue
        # 2026-10-18 修并发重复下单: 先占坑 record(下单前), 防多个 ws_scan 并发线程同场同盘口重复下单。
        # 下单失败也保留占坑(该 opp 本轮已尝试, 不再重试, 防重复), 下次扫描靠 check 拦。
        record_sub_market_bet(match_id, sub, "prematch")

        # 全局冷却(2026-09-08): 跨进程共享时间戳, 与滚球流程错开, 避免"同一时间"下单
        from src.betting.bb_auto_bet import global_bet_cooldown, record_global_bet
        _wait = global_bet_cooldown(20, 90)
        if _wait > 0:
            time.sleep(_wait)

        # 余额熔断已取消(2026-10-11 用户要求): 不再用余额<1000 拦投注, 符合释放门槛就投

        # 让球0 seen 计数(2026-10-11 早盘, 不滤只计数供"早盘整数线效率"对照)
        if sub in ("hc", "handicap") and opp.get("line") is not None:
            try:
                if abs(float(opp.get("line"))) < 1e-6:
                    from src.scrapers.stats import count as _sc
                    _sc("line_zero_seen_pre")
            except (TypeError, ValueError):
                pass

        # 下单(含注额上限检查)
        # 2026-10-14 早盘探针日限额: 笔数 25 或金额 600 达上限即停(笔数为主, 金额兜底)
        if _daily_bets >= EARLY_DAILY_MAX_BETS or _daily_stake >= EARLY_DAILY_MAX_STAKE:
            print(f"  ⏭️ 早盘日限额已达(笔{_daily_bets}/{EARLY_DAILY_MAX_BETS} 额{_daily_stake:.0f}/{EARLY_DAILY_MAX_STAKE:.0f}), 停止")
            break
        # 2026-10-17 解冻注额公式: 恢复 Kelly 档(弱65/中125/强190), 早盘上限 200(用户充值5000定)。
        # (之前 cap 30 flat 是早盘探针, 把 Kelly 档全压成 30 → 无大额、不按 Kelly)
        # 2026-10-18 双轨制: 单笔上限按触发源区分 — drop 骤降(有 edge 信号)80 / ws_scan 全量扫描 50。
        _max_stake = 80.0 if trigger_source == "pin_drop" else 50.0
        stake = min(stake, _max_stake)
        # 2026-10-17: 下注时EV门槛与采集门槛对齐。之前硬编码 min_ev_pct=4.0 高于采集门槛
        # (get_min_ev 直接盘3%/dc 3%/特殊盘4%, 另有方向级覆盖), 把 2~4% 档机会扫进来又在下单时拒掉。
        _min_ev = opp.get("_min_ev")
        if _min_ev is None:
            from config.weight_matrix_v5 import get_min_ev
            _min_ev = get_min_ev(opp.get("sport", ""), opp.get("league_cn") or opp.get("league", ""),
                                 _sub_market_to_key(sub), odds)
        _t0 = time.time()
        code, order_id, msg = place_single_bet(
            market_id, odds, option_type, stake, token=token, domain=domain, match_id=match_id,
            fair_price=opp.get("fair_price") or opp.get("fair"), min_ev_pct=_min_ev)
        _latency = time.time() - _t0
        # 执行质量记录(2026-10-08): 按方向记拒单/延迟, 供早盘定仓的执行质量系数打折
        from src.scrapers.execution_quality import record as _eq_record
        _eq_record(f"{opp.get('sport')}|{sub}|{desig}", code, _latency)

        # 记录全局下单时间戳
        record_global_bet()

        # 防风控(2026-09-05): 每注之间随机间隔, 模拟真人看盘思考, 避免秒下多注被风控识别。
        # 2026-10-18 双轨制: ws_scan 全量扫描每 30s 来 13-16 个 opp, 20-90s 会让循环永不结束
        # (下单慢 + 钉钉攒批被吞), 缩短到 10-20s; drop 低频触发保留原 20-90s。
        import random as _random
        _sleep = _random.uniform(10, 20) if trigger_source == "ws_scan" else _random.uniform(20, 90)
        time.sleep(_sleep)

        if code == 0:
            opp["_bet_succeeded"] = True  # 2026-10-17 供 push_report 只对成功注写指纹(失败注下次可重试)
            rec = {
                "home": disp_home, "away": disp_away, "sub_market": sub, "designation": desig,
                "odds": odds, "stake": stake, "order_id": order_id,
                "ts": time.time(), "trigger_source": trigger_source,
            }
            success.append(rec)
            # 2026-10-18 早盘实盘也写 real_bets(与滚球同库), 供结算 + 未结统计。
            # 之前只写 bet_history → 早盘未结读 tracked_bets.json(旧追踪)冻结在 810, 结算系统也不认早盘注单。
            try:
                _rb_file = DATA_DIR / "real_bets.json"
                _rd = json.loads(_rb_file.read_text()) if _rb_file.exists() else {}
                _rd.setdefault("bets", {})[str(order_id)] = {
                    "ts": time.time(), "stake": stake,
                    "fair": opp.get("fair_price") or opp.get("fair"),
                    "ev": opp.get("ev_pct"), "bb_odds": odds,
                    "desig": desig, "sub": sub, "home": disp_home, "away": disp_away,
                    "sport": opp.get("sport"), "verify": True,
                    "result": None, "profit": None, "settled_ts": None,
                    "trigger_source": trigger_source,
                }
                _rb_file.write_text(json.dumps(_rd, ensure_ascii=False))
            except Exception:
                pass
            _daily_bets += 1; _daily_stake += stake
            _add_daily_exposure(1, stake)  # 2026-10-14 早盘探针日暴露累加
            total_latency += _latency
            _bj = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M")
            # 开赛时间 + 运动类型(2026-09-07 用户要求)
            _sport_cn = SPORT_CN.get(opp.get("sport", ""), opp.get("sport", ""))
            _league = opp.get("league_cn") or opp.get("league", "") or ""
            _fair = opp.get("fair_price") or opp.get("_fair_price") or 0
            _ev = opp.get("ev_pct") or 0
            _kickoff = ""
            if _ep:
                try:
                    _kickoff = datetime.fromtimestamp(float(_ep), timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
                except (TypeError, ValueError, OSError):
                    pass
            # 2026-10-11: 公平价已切 Pin 主锚(_oa_fair→consensus_fair_price skip_betfair=True), 旧 Betfair/pin_odds 分支废弃
            _fair_str = f" | Pin公平价 {float(_fair):.2f}" if _fair else ""
            _ev_str = f" | 溢价 {float(_ev):+.1f}%" if _ev else ""
            _kickoff_str = f" | 开赛 {_kickoff}" if _kickoff else ""
            _src_label = "WS扫描" if trigger_source == "ws_scan" else "Drop"
            # 余额 + 早盘未结 + 总耗时(2026-10-18 补回: 每单推不能丢这三个字段)
            _extra = []
            try:
                from src.betting.bb_auto_bet import fetch_balance
                _bal = fetch_balance()
                if _bal:
                    _extra.append(f"余额 ¥{_bal}")
            except Exception:
                pass
            try:
                _early_out = _early_outstanding()
                if _early_out > 0:
                    _extra.append(f"早盘未结 ¥{_early_out:.0f}")
            except Exception:
                pass
            if trigger_ts:
                _extra.append(f"总耗时 {time.time() - trigger_ts:.1f}s")
            _extra_str = ("\n  " + " | ".join(_extra)) if _extra else ""
            _single = (f"✅【{_win_label(opp.get('_pin_epoch'))}】{disp_home} vs {disp_away} | {desig}\n"
                       f"  {_sport_cn} | {_league} | BB {odds:.2f}{_fair_str}{_ev_str} | 注额 ¥{stake:.0f} | {_src_label} | 投注 {_bj}{_kickoff_str}{_extra_str}")
            sent_dingtalk.append(_single)
            # 2026-10-18 改: 每单立即推钉钉(不再攒批等循环结束, 否则 sleep 20-90s/单 循环不结束推送被吞)
            try:
                from config.settings import send_dingtalk
                send_dingtalk(f"🟦 早盘投注 {desig}", _single)
            except Exception:
                pass
            _append_bet_history(rec)
        else:
            failed.append({"home": disp_home, "away": disp_away, "reason": f"code={code} {msg}"})
            # token 过期(14010) → 标记, 循环结束后发一次钉钉提醒(带30分钟节流)
            if code == 14010:
                _token_expired_alert()

    # 2026-10-18 攒批推送已停用(改每单立即推, 见上面循环内 send_dingtalk)。
    # 原攒批逻辑: 循环结束后才发, 但 sleep 20-90s/单 让循环长时间不结束 → 推送被吞。
    # sent_dingtalk 列表保留(success 记录用), 但不再攒批发。

    return {"success": success, "failed": failed}


_token_alert_file = DATA_DIR / ".token_expired_alert.ts"


def _token_expired_alert():
    """token 过期(14010)时发钉钉提醒, 30分钟节流(避免每次扫描都刷屏)。"""
    try:
        # 30分钟节流
        if _token_alert_file.exists():
            try:
                last = float(_token_alert_file.read_text().strip())
                if time.time() - last < 30 * 60:
                    return
            except Exception:
                pass
        _token_alert_file.write_text(str(time.time()))
        from config.settings import send_dingtalk
        send_dingtalk("⚠️ BB token 已过期", "下单失败(账号登出), 请重新登录 BB 后告诉我, 我更新 token")
    except Exception:
        pass


def _append_bet_history(rec):
    """把已投注明细追加到 bet_history.json(供每日 9 点日报)。"""
    try:
        hist_file = DATA_DIR / "bet_history.json"
        data = {}
        if hist_file.exists():
            try:
                data = json.loads(hist_file.read_text())
            except Exception:
                data = {}
        bets = data.get("bets", [])
        bets.append(rec)
        # 只保留最近 7 天
        cutoff = time.time() - 7 * 86400
        bets = [b for b in bets if b.get("ts", 0) > cutoff]
        data["bets"] = bets
        hist_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass


if __name__ == "__main__":
    # 测试: 从 comparison 读 +EV 机会, 自动下单
    import sys
    comp = DATA_DIR / "bb_vs_pinnacle_comparison.json"
    if not comp.exists():
        print("无 comparison 文件"); sys.exit(1)
    data = json.loads(comp.read_text())
    opps = []
    for m in data.get("details", []):
        home = m.get("home_bb_cn") or m.get("home_bb", "")
        away = m.get("away_bb_cn") or m.get("away_bb", "")
        for opp in m.get("opportunities", [])[:2]:  # 只取前2个机会测试
            opps.append({**opp, "home_cn": home, "away_cn": away,
                         "sub_market": "1x2", "_stake": 10})
    print(f"测试 {len(opps)} 个机会")
    res = auto_bet_flow(opps)
    print("成功:", len(res["success"]), "失败:", len(res["failed"]))
    for f in res["failed"][:5]:
        print("  失败:", f)
