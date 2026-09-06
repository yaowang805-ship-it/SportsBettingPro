"""BB 秒级比价监控 (2026-09-06)。

G04 赔率推送触发即时 EV 计算: 相对 Pinnacle 公平价缓存, BB 赔率一变立刻算出新 EV,
不再等 5min 轮询。可靠路径走 CDP 挂浏览器现有 WS(--tap), 见 bb_ws_push.py。

数据流:
  1. 加载 `data/storage/bb_vs_pinnacle_comparison.json` → 建 {bb_match_id: 公平价}
     (该文件由主扫描 bb_vs_pinnacle 周期刷新, 含 bb_match_id + 每盘口 fair_price)
  2. CDP tap 收 G04(matchId + 盘口 + items[].value=最新BB赔率)
  3. 命中缓存 → ev = (bb_odds - fair_price)/fair_price*100 → ≥阈值打日志/推送

用法:
    .venv312/bin/python -m src.scrapers.second_level_monitor --threshold 3 --listen 0
"""
import asyncio
import json
import sys
import time
import random
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.bb_ws_push import tap_browser_g04

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

COMPARISON_FILE = ROOT / "data" / "storage" / "bb_vs_pinnacle_comparison.json"

# EV-Kelly 半凯利仓位(秒级自动下单注额)。有效资金 = ¥5000(日目标, 2026-09-06 用户选 C),
# 让弱 edge 落在 ¥300 上限以下, 强 edge 顶格, 真正按 edge 分档。
BANKROLL = 5000        # 有效资金(日目标口径, 不是 2 万全仓)
KELLY_FRACTION = 0.5   # 半凯利
MAX_STAKE = 300        # 单盘口上限(与 bb_auto_bet MAX_MARKET_STAKE 一致)
MIN_STAKE = 30         # stake<30 拦截铁律

# 滚球验证模式(2026-09-06 用户要求): 滚球秒级自动下单预算 ¥1000, 投满后停真下单,
# 转虚拟投注进观察库积累数据(待验证滚球秒级 edge 是否成立)。
LIVE_BUDGET = 1000
LIVE_BUDGET_FILE = ROOT / "data" / "storage" / "live_bet_budget.json"
LIVE_PAPER_FILE = ROOT / "data" / "storage" / "live_paper_bets.json"
LIVE_SETTLED_FILE = ROOT / "data" / "storage" / "live_settled_notified.json"  # 已推送过结算的 order_id

# G04 market(盘口名) → 缓存子盘口 key
_MARKET_KEYWORDS = [
    ("double_chance", ("双重", "双胜彩")),
    ("over_under", ("大/小", "大小", "进球数", "总进球")),
    ("handicap", ("让球", "让分", "handicap")),
    ("opportunities", ("独赢", "1x2", "胜平负", "输赢")),
]

# 方向 → 各盘口 designation(与 comparison 输出一致)
_DIR_DESIGNATION = {
    "handicap": {"主": "让球主胜", "客": "让球客胜"},
    "over_under": {"大": "大球", "小": "小球"},
    "opportunities": {"主": "主胜", "客": "客胜", "和": "和局", "平": "和局"},
    "double_chance": {"主": "主/和", "客": "客/和", "和": "主/客"},
}


def load_fair_cache(path=None):
    """读 comparison 输出 → {bb_match_id: {meta, markets}}。

    markets = {sub_market: {designation: fair_price}}
    """
    p = path or COMPARISON_FILE
    cache = {}
    try:
        data = json.loads(Path(p).read_text())
    except Exception:
        return cache
    for m in data.get("details", []):
        mid = m.get("bb_match_id")
        if not mid:
            continue
        mid = int(mid)
        markets = {}
        for key in ("opportunities", "handicap", "over_under", "double_chance"):
            sub = {}
            for o in m.get(key) or []:
                desig = o.get("designation", "")
                if desig and o.get("fair_price"):
                    sub[desig] = float(o["fair_price"])
            if sub:
                markets[key] = sub
        cache[mid] = {
            "home_cn": m.get("home_bb_cn") or m.get("home_bb", ""),
            "away_cn": m.get("away_bb_cn") or m.get("away_bb", ""),
            "sport": m.get("sport", ""),
            "league_cn": m.get("league_cn", ""),
            "markets": markets,
        }
    return cache


def _sub_market_of(market_name):
    """G04 的 market(盘口名) → 子盘口 key。

    排除特殊/半场盘口(角球/罚牌/上半场/第1盘/正确比分等), 这些不是主流 FT 盘口,
    且 comparison 输出里它们的 fair_price 在别的字段(非 opportunities/handicap/ou)。
    """
    mn = (market_name or "")
    for skip in ("角球", "罚牌", "上半场", "半场", "第1盘", "第1节", "第1局",
                 "正确比分", "波胆", "先进球", "最后进球", "任一进球"):
        if skip in mn:
            return None
    for key, kws in _MARKET_KEYWORDS:
        if any(kw.lower() in mn.lower() for kw in kws):
            return key
    return None


def _direction_of(item_name):
    """G04 items[].name("主 +0/0.5") → 方向(主/客/和/大/小)。"""
    n = (item_name or "")
    if "大" in n:
        return "大"
    if "小" in n:
        return "小"
    if "客" in n:
        return "客"
    if "主" in n:
        return "主"
    if "和" in n or "平" in n:
        return "和"
    return None


def _extract_line(item_name):
    """G04 items[].name 提取盘口线(如 "主 -0.5"→-0.5, "大 2.5"→2.5)。quarter-ball 返回 None。"""
    import re
    m = re.search(r'([+-]?\d+(?:\.\d+)?)', item_name or "")
    if not m:
        return None
    s = m.group(1)
    # quarter-ball(含 / 的)跳过: "主 +0/0.5" 提取到 0 会错配
    if "/" in (item_name or ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _match_2way_line(line, d2way):
    """匹配 2-way 盘口线(spread/total)。主/客方向线符号相反, 试 line 和 -line。"""
    if d2way is None:
        return None
    if line in d2way:
        return d2way[line]
    if line is not None and -line in d2way:
        return d2way[-line]
    return None


def _is_settleable(desig, line):
    """盘口能否被 _settle_paper_bets 判输赢。

    1x2(主/和/客) 不需要 line; hc/ou(让球/大小) 必须带 line(缺 line 进库也无法结算)。
    其它盘口(如 dc 的"主/和")当前结算逻辑不支持, 一律判为不可结算。
    """
    if desig in ("主胜", "客胜", "和局"):
        return True
    if desig in ("让球主胜", "让球客胜", "大球", "小球"):
        return line is not None
    return False


class SecondLevelMonitor:
    def __init__(self, threshold=3.0, on_signal=None, auto_bet=False, stake=None):
        self.threshold = threshold
        self.on_signal = on_signal
        self.auto_bet = auto_bet
        self.stake = stake  # None=EV-Kelly, >0=固定注额
        self.cache = {}
        self._cache_mtime = 0.0
        self.live_cache = {}  # bb_match_id -> {pin_matchup_id, home, away, moneyline}
        self._live_cache_ts = 0.0
        self._live_spent = 0.0  # 当日滚球已投注额(读自 LIVE_BUDGET_FILE)
        self._bet_notify_until = 0.0  # 钉钉下单通知节流(30min 内最多一条)
        self._token_remind_until = 0.0  # token 失效钉钉提醒节流(30min)
        self._token_ok_until = 0.0     # token 有效缓存到期时间戳(10min 缓存, 省每单 1s 探测)
        self._attempted = {}           # 滚球指纹去重: match_id -> {market_id -> 尝试时间戳}
        self._last_bet_time = 0.0      # 上次下单时间(非阻塞限频用)
        self._bet_delay = 0.0          # 下一单需等待的随机间隔(10-15s, 每次下单后重抽)

    def refresh_cache(self):
        """comparison 文件变了就重载公平价缓存。返回是否刷新。"""
        try:
            mt = COMPARISON_FILE.stat().st_mtime
        except Exception:
            return False
        if mt == self._cache_mtime:
            return False
        self.cache = load_fair_cache()
        self._cache_mtime = mt
        return True

    def refresh_live_cache(self):
        """刷新滚球公平价缓存(BB live ↔ Pin live 匹配)。30s 节流。"""
        if time.time() - self._live_cache_ts < 30:
            return False
        self._live_cache_ts = time.time()
        try:
            from src.scrapers.pinnacle_live import match_live_bb_pin
            self.live_cache = match_live_bb_pin()
            return True
        except Exception as e:
            print(f"[slm] 滚球缓存刷新失败: {str(e)[:80]}", flush=True)
            return False

    def _load_live_spent(self):
        """读当日滚球已投注额。日期不匹配自动重置。"""
        today = time.strftime("%Y-%m-%d")
        try:
            if LIVE_BUDGET_FILE.exists():
                d = json.loads(LIVE_BUDGET_FILE.read_text())
                if d.get("date") == today:
                    self._live_spent = float(d.get("spent", 0))
                    return
        except Exception:
            pass
        self._live_spent = 0.0

    def _save_live_spent(self):
        today = time.strftime("%Y-%m-%d")
        try:
            LIVE_BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
            LIVE_BUDGET_FILE.write_text(json.dumps({"date": today, "spent": self._live_spent}))
        except Exception:
            pass

    def _append_live_paper_bet(self, sig):
        """滚球虚拟投注进观察库(live_paper_bets.json), 待结算积累数据。

        结算护栏: 只收 _settle_paper_bets 能判输赢的盘口, 缺 line 的 hc/ou 或未支持
        盘口一律不进库 —— 进库却结算不了会污染 ROI 统计(历史 88 条 line=None 已剔)。
        """
        desig = sig.get("desig", "")
        if not _is_settleable(desig, sig.get("line")):
            print(f"[slm] 跳过无法结算的虚拟投注({desig!r} line={sig.get('line')!r}), 不进观察库", flush=True)
            return
        try:
            data = []
            if LIVE_PAPER_FILE.exists():
                data = json.loads(LIVE_PAPER_FILE.read_text())
            data.append({
                "ts": time.time(), "match_id": sig["match_id"],
                "market_id": sig.get("market_id"), "option_type": sig.get("option_type"),
                "home": sig["match"].get("home", ""), "away": sig["match"].get("away", ""),
                "designation": sig["desig"], "sub": sig.get("sub"), "line": sig.get("line"),
                "bb_odds": sig["bb_odds"], "fair": sig["fair"], "ev": sig["ev"],
                "stake": sig["_stake"], "settled": False, "result": None, "profit": None,
            })
            LIVE_PAPER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        except Exception as e:
            print(f"[slm] 虚拟投注写入失败: {str(e)[:80]}", flush=True)

    def _settle_paper_bets(self):
        """结算虚拟投注(观察库): 查赛果(type=6)判输赢, 写回 result/profit, 打印 ROI。"""
        from src.betting.bb_auto_bet import read_token, read_domain, _session
        if not LIVE_PAPER_FILE.exists():
            return
        try:
            bets = json.loads(LIVE_PAPER_FILE.read_text())
        except Exception:
            return
        tok = read_token(); dom = read_domain()
        if not tok:
            return
        # 拉赛果(type=6)拿最终比分
        s = _session()
        score_map = {}
        for sport in (1, 3, 5, 7, 6):
            try:
                r = s.post(f"{dom}/v1/match/getList",
                           json={"sportId": sport, "type": 6, "current": 1, "pageSize": 50,
                                 "isPC": True, "languageType": "EN"},
                           headers={"Content-Type": "application/json", "user-token": tok,
                                    "User-Agent": _UA}, timeout=15, verify=False)
                d = r.json()
                for m in (d.get("data") or {}).get("records") or []:
                    for g in m.get("nsg") or []:
                        if g.get("pe") == 1000 and g.get("tyg") == 5:
                            score_map[int(m.get("id"))] = g.get("sc")
                            break
            except Exception:
                pass
        changed = False
        for b in bets:
            if b.get("settled"):
                continue
            mid = b.get("match_id")
            sc = score_map.get(int(mid)) if mid else None
            if sc is None:
                continue  # 还没赛果
            desig = b.get("designation", ""); line = b.get("line")
            stake = float(b.get("stake", 0)); odds = float(b.get("bb_odds", 0))
            home, away = sc[0], sc[1]
            # 判输赢(含走盘: 总分==盘口线 或 让球后打平 = 退款 profit 0)
            if desig in ("主胜",):
                diff = home - away
            elif desig in ("客胜",):
                diff = away - home
            elif desig in ("和局",):
                diff = 0.0 if home == away else (1.0 if home > away else -1.0)
            elif line is None:
                continue  # hc/ou 缺 line, 跳(旧记录)
            elif desig in ("让球主胜",):
                diff = (home + line) - away
            elif desig in ("让球客胜",):
                diff = (away + line) - home
            elif desig in ("大球",):
                diff = (home + away) - line
            elif desig in ("小球",):
                diff = line - (home + away)
            else:
                continue
            if abs(diff) < 0.0001:
                result = "push"; profit = 0.0
            else:
                result = "won" if diff > 0 else "lost"
                profit = stake * (odds - 1) if result == "won" else -stake
            b["settled"] = True; b["result"] = result; b["profit"] = round(profit, 1)
            changed = True
        if changed:
            LIVE_PAPER_FILE.write_text(json.dumps(bets, ensure_ascii=False, indent=1))
            settled = [b for b in bets if b.get("settled")]
            pnl = sum(b.get("profit", 0) for b in settled)
            stk = sum(b.get("stake", 0) for b in settled)
            won_n = sum(1 for b in settled if b.get("result") == "won")
            roi = pnl / stk * 100 if stk else 0
            print(f"[slm] 虚拟投注结算: {len(settled)}/{len(bets)} 条, 盈亏 {pnl:+.1f}, ROI {roi:+.1f}% (胜{won_n})", flush=True)

    def _handle_live_g04(self, match_id, data):
        """滚球 G04: 用 Pin live 公平价(moneyline/spread/total)算 EV。1x2/让球/大小球。"""
        lv = self.live_cache.get(match_id)
        if not lv:
            return
        # 流动性门槛: Pin maxRiskStake 低 = 薄盘 = 尺子不准, 假 edge 概率高
        if float(lv.get("max_stake", 0) or 0) < 200:
            return
        sub = _sub_market_of(data.get("market", ""))
        if sub not in ("opportunities", "handicap", "over_under"):
            return
        try:
            from src.scrapers.devig import shin_fair_odds
        except Exception:
            return
        for it in data.get("items", []):
            d = _direction_of(it.get("name", ""))
            if not d:
                continue
            bb = float(it.get("value") or 0)
            if bb <= 0:
                continue
            # 按盘口类型取原始赔率 + 方向索引 + designation
            line_val = None
            if sub == "opportunities":
                ml = lv.get("moneyline") or []
                if len(ml) != 3 or not any(ml):
                    continue
                idx = {"主": 0, "和": 1, "客": 2}
                desig = {"主": "主胜", "和": "和局", "客": "客胜"}
                raw = ml
            elif sub == "handicap":
                line_val = _extract_line(it.get("name", ""))
                raw = _match_2way_line(line_val, lv.get("spread"))
                if not raw:
                    continue
                idx = {"主": 0, "客": 1}
                desig = {"主": "让球主胜", "客": "让球客胜"}
            elif sub == "over_under":
                line_val = _extract_line(it.get("name", ""))
                raw = _match_2way_line(line_val, lv.get("total"))
                if not raw:
                    continue
                idx = {"大": 0, "小": 1}
                desig = {"大": "大球", "小": "小球"}
            else:
                continue
            i = idx.get(d)
            if i is None:
                continue
            try:
                fair = shin_fair_odds(raw)
            except Exception:
                continue
            if not fair or len(fair) <= i or fair[i] <= 0:
                continue
            fair_p = fair[i]
            ev = (bb - fair_p) / fair_p * 100.0
            tag = f"{lv['home']} vs {lv['away']} [滚球] {desig[d]}"
            if ev < self.threshold:
                continue
            sig = {"match_id": match_id, "sub": sub, "desig": desig[d],
                   "bb_odds": bb, "fair": fair_p, "ev": ev, "line": line_val,
                   "match": {"home": lv["home"], "away": lv["away"], "sport": "", "league_cn": "滚球"},
                   "pin_matchup_id": lv.get("pin_matchup_id"),
                   "league_id": lv.get("league_id"),
                   "max_stake": lv.get("max_stake", 0)}
            oid = str(it.get("oid") or data.get("id") or "")
            parts = oid.split("-")
            if len(parts) >= 2 and parts[0].isdigit() and parts[-1].isdigit():
                sig["market_id"] = int(parts[0])
                sig["option_type"] = int(parts[-1])
            else:
                sig["market_id"] = None
                sig["option_type"] = None
            print(f"⚡滚球+EV {ev:+.2f}% | {tag} | BB {bb:.2f} vs 公平 {fair_p:.2f}", flush=True)
            if self.on_signal:
                try:
                    self.on_signal(sig)
                except Exception as e:
                    print(f"[slm] on_signal 异常: {e}")
            if self.auto_bet:
                self._try_live_auto_bet(sig)

    def _try_live_auto_bet(self, sig):
        """滚球秒级下单: 预算 ¥1000 封顶, 超额转虚拟投注进观察库。"""
        self._load_live_spent()
        stake = self._stake_for(sig)
        if stake < MIN_STAKE:
            return
        tag = f"{sig['match']['home']} vs {sig['match']['away']} {sig['desig']}"
        # 延迟修正: 下注前重拉 Pin 滚球价, 重验 EV(缓存 30s 可能过期, 防止临时高价假机会)
        fresh_ev = self._reverify_live_ev(sig)
        if fresh_ev is not None and fresh_ev < self.threshold:
            print(f"  ⏸️ Pin 滚球价已漂移(重验 EV {fresh_ev:+.2f}% < {self.threshold}%), 放弃 {tag}", flush=True)
            return
        sig["_stake"] = stake
        # 预算封顶 → 虚拟投注(不进真下单)
        if self._live_spent + stake > LIVE_BUDGET:
            self._append_live_paper_bet(sig)
            print(f"  📝 滚球预算已满(已投¥{self._live_spent:.0f}/{LIVE_BUDGET}), 转虚拟投注 {tag}", flush=True)
            return
        market_id = sig.get("market_id")
        if market_id is None:
            return
        from src.betting.bb_auto_bet import _load_stake_record, place_single_bet
        rec = _load_stake_record()
        if rec.get(str(sig["match_id"]), {}).get(str(market_id), 0.0) > 0:
            print(f"  ⏭️ 已下过注, 跳过 {tag}", flush=True)
            return
        # 指纹去重: 同一 (match, market) 5min 内尝试过(无论成败)则跳过, 避免每 2s 轮询重复下单
        _d = self._attempted.get(str(sig["match_id"]), {})
        if time.time() - _d.get(str(market_id), 0) < 300:
            return
        if not self._token_ok():
            return
        # 非阻塞限频: 距上次下单 < 随机间隔(10-15s)则跳过, 下一轮 2s 后重新评估(用新鲜赔率)
        if time.time() - self._last_bet_time < self._bet_delay:
            return
        print(f"  🎯 滚球下单 {tag} @{sig['bb_odds']:.2f} 注额¥{stake}", flush=True)
        code, order_id, msg = place_single_bet(
            market_id, sig["bb_odds"], sig["option_type"], stake=stake,
            match_id=sig["match_id"], check_limit=True, verify_price=True)
        # 记录尝试(成败都记), 5min 内不再重复尝试同一盘口
        self._attempted.setdefault(str(sig["match_id"]), {})[str(market_id)] = time.time()
        # 更新限频时间戳 + 抽下一单随机间隔(10-15s, 防风控"投注过于频繁")
        self._last_bet_time = time.time()
        self._bet_delay = random.uniform(10, 15)
        if code == 14010:
            self._invalidate_token_cache()
        if code == 0:
            self._live_spent += stake
            self._save_live_spent()
            print(f"  ✅ 滚球下单成功 {tag} | 注额¥{stake} | 累计¥{self._live_spent:.0f}/{LIVE_BUDGET}", flush=True)
            # 每笔成功下单都推钉钉(不限频) + 显示账户总余额
            from src.betting.bb_auto_bet import fetch_balance as _fetch_balance
            _bal = _fetch_balance() or "未知"
            self._notify_bet(
                f"🟦【滚球】下单成功 {tag}",
                f"注额¥{stake} @{sig['bb_odds']:.2f} | EV{sig['ev']:+.2f}% | 订单{order_id}\n"
                f"账户余额 ¥{_bal} | 今日滚球累计 ¥{self._live_spent:.0f}/{LIVE_BUDGET}")
        else:
            # 下单失败(如 token 过期 14010) → 也记虚拟投注, 保证验证数据积累不中断
            self._append_live_paper_bet(sig)
            print(f"  ❌ 滚球下单失败({code} {msg}), 已记虚拟投注 {tag}", flush=True)

    def on_g04(self, data):
        """G04 回调: matchId + 盘口 + items → 命中公平价 → 算 EV → 打信号/自动下单。"""
        match_id = int(data.get("matchId") or 0)
        match = self.cache.get(match_id)
        if not match:
            # 早盘缓存没有 → 试滚球缓存(Pin live 公平价)
            if match_id in self.live_cache:
                self._handle_live_g04(match_id, data)
            return  # 缓存里没有(未匹配/非主流盘口/缓存过期)
        sub = _sub_market_of(data.get("market", ""))
        if not sub:
            return
        desigs = _DIR_DESIGNATION.get(sub, {})
        markets = match.get("markets", {}).get(sub, {})
        if not markets:
            return
        for it in data.get("items", []):
            direction = _direction_of(it.get("name", ""))
            if not direction:
                continue
            desig = desigs.get(direction)
            if not desig or desig not in markets:
                continue
            # 从 oid("{marketId}-{optionType}") 提取下单所需的 marketId + optionType
            oid = str(it.get("oid") or data.get("id") or "")
            parts = oid.split("-")
            if len(parts) < 2 or not parts[0].isdigit() or not parts[-1].isdigit():
                continue
            market_id = int(parts[0])
            option_type = int(parts[-1])
            fair = markets[desig]
            bb = float(it.get("value") or 0)
            if bb <= 0 or fair <= 0:
                continue
            ev = (bb - fair) / fair * 100.0
            tag = f"{match['home_cn']} vs {match['away_cn']} [{match['sport']}/{match['league_cn']}] {desig}"
            if ev >= self.threshold:
                sig = {"match_id": match_id, "market_id": market_id, "option_type": option_type,
                       "sub": sub, "desig": desig, "bb_odds": bb, "fair": fair, "ev": ev,
                       "match": match}
                print(f"⚡秒级+EV {ev:+.2f}% | {tag} | BB {bb:.2f} vs 公平 {fair:.2f}", flush=True)
                if self.on_signal:
                    try:
                        self.on_signal(sig)
                    except Exception as e:
                        print(f"[slm] on_signal 异常: {e}")
                if self.auto_bet:
                    self._try_auto_bet(sig)
            elif ev >= self.threshold - 2.0:
                print(f"  接近 {ev:+.2f}% | {tag} | BB {bb:.2f} vs {fair:.2f}", flush=True)

    def _reverify_live_ev(self, sig):
        """下注前重拉 Pin 滚球价, 重算该方向 EV(1x2/让球/大小球)。返回新 EV(或 None=盘口已关)。"""
        from src.scrapers.pinnacle_live import reverify_live_markets
        from src.scrapers.devig import shin_fair_odds
        pin_mid = sig.get("pin_matchup_id"); lid = sig.get("league_id")
        if not pin_mid or not lid:
            return None
        fresh = reverify_live_markets(pin_mid, lid)
        if not fresh:
            return None
        sub = sig.get("sub")
        if sub == "opportunities":
            ml = fresh.get("moneyline")
            if not ml or len(ml) != 3:
                return None
            raw = ml
            idx = {"主胜": 0, "和局": 1, "客胜": 2}
        elif sub == "handicap":
            raw = _match_2way_line(sig.get("line"), fresh.get("spread"))
            idx = {"让球主胜": 0, "让球客胜": 1}
        elif sub == "over_under":
            raw = _match_2way_line(sig.get("line"), fresh.get("total"))
            idx = {"大球": 0, "小球": 1}
        else:
            return None
        if not raw:
            return None
        try:
            fair = shin_fair_odds(raw)
        except Exception:
            return None
        i = idx.get(sig["desig"])
        if i is None or not fair or len(fair) <= i or fair[i] <= 0:
            return None
        return (sig["bb_odds"] - fair[i]) / fair[i] * 100.0

    def _stake_for(self, sig):
        """EV-Kelly 半凯利: stake = BANKROLL × 0.5 × (ev/100) / (odds-1), 封顶 ¥300。

        --stake 显式给固定注额(>0)时用固定值; 否则按 EV-Kelly(秒级默认)。
        """
        if self.stake and self.stake > 0:
            return self.stake
        edge = sig["ev"] / 100.0
        odds = sig["bb_odds"]
        if odds <= 1 or edge <= 0:
            return 0
        stake = BANKROLL * KELLY_FRACTION * edge / (odds - 1)
        stake = int(min(max(stake, MIN_STAKE), MAX_STAKE))
        # 四舍五入到 10: 避免 ¥91/¥82 有零有整被风控识别为机器下单
        return int(round(stake / 10.0) * 10)

    def _try_auto_bet(self, sig):
        """秒级信号 → 自动下单。复用 place_single_bet(注额上限 + 下注前验价 + 记录)。"""
        from src.betting.bb_auto_bet import _load_stake_record, place_single_bet
        match_id = sig["match_id"]; market_id = sig["market_id"]
        m = sig["match"]
        tag = f"{m['home_cn']} vs {m['away_cn']} {sig['desig']}"
        stake = self._stake_for(sig)
        if stake < MIN_STAKE:
            return  # EV-Kelly 算出来 < 30, 拦截(铁律)
        # 去重: 该盘口已下过注(主扫描或本监控)则跳过, 防重复下注
        rec = _load_stake_record()
        if rec.get(str(match_id), {}).get(str(market_id), 0.0) > 0:
            print(f"  ⏭️ 已下过注, 跳过 {tag}", flush=True)
            return
        if not self._token_ok():
            return
        print(f"  🎯 秒级下单 {tag} @{sig['bb_odds']:.2f} 注额¥{stake} (EV-Kelly)", flush=True)
        code, order_id, msg = place_single_bet(
            market_id, sig["bb_odds"], sig["option_type"], stake=stake,
            match_id=match_id, check_limit=True, verify_price=True)
        if code == 14010:
            self._invalidate_token_cache()
        if code == 0:
            print(f"  ✅ 下单成功 {tag} | 订单{order_id}", flush=True)
            self._notify_dingtalk(
                f"🟦 秒级自动下单 {tag}",
                f"注额¥{stake} @{sig['bb_odds']:.2f} | EV{sig['ev']:+.2f}% | 订单{order_id}")
        else:
            print(f"  ❌ 下单失败 {tag} | code={code} {msg}", flush=True)

    def _notify_dingtalk(self, title, body):
        """下单成功钉钉通知(30min 节流, 避免刷屏)。"""
        if time.time() < self._bet_notify_until:
            return
        self._bet_notify_until = time.time() + 30 * 60
        try:
            from config.settings import send_dingtalk
            send_dingtalk(title, body)
        except Exception as e:
            print(f"[slm] 钉钉通知异常: {e}")

    def _notify_bet(self, title, body):
        """每笔成功下单都推钉钉(不限频 —— 下单限频已把间隔拉到 10-15s, 不会刷屏)。"""
        try:
            from config.settings import send_dingtalk
            send_dingtalk(title, body)
        except Exception as e:
            print(f"[slm] 钉钉通知异常: {e}")

    def _check_settled(self):
        """查 BB 已结算订单, 对新的(未通知的)推钉钉(含盈亏 + 账户余额)。"""
        from src.betting.bb_auto_bet import read_token, read_domain, _session, fetch_balance
        tok = read_token(); dom = read_domain()
        if not tok:
            return
        notified = set()
        if LIVE_SETTLED_FILE.exists():
            try:
                notified = set(json.loads(LIVE_SETTLED_FILE.read_text()))
            except Exception:
                pass
        try:
            r = _session().post(f"{dom}/v1/order/new/bet/list",
                                json={"languageType": "CMN", "isSettled": True, "current": 1, "size": 20},
                                headers={"Content-Type": "application/json", "Authorization": tok,
                                         "User-Agent": _UA}, timeout=15, verify=False)
            d = r.json()
            if d.get("code") != 0:
                return
        except Exception:
            return
        records = (d.get("data") or {}).get("records") or []
        new_notified = set(notified)
        from config.settings import send_dingtalk
        for o in records:
            oid = o.get("id")
            if not oid or oid in notified:
                continue
            ops = o.get("ops") or []
            op = ops[0] if ops else {}
            mn = op.get("mn", "?"); on = op.get("on", "?")
            stake = o.get("sat", 0); pnl_raw = o.get("uwl", "0")
            try:
                pnl = float(pnl_raw)
            except (TypeError, ValueError):
                pnl = 0.0
            won = pnl > 0
            bal = fetch_balance() or "未知"
            title = f"{'✅ 赢了' if won else '❌ 输了'} {mn}"
            body = f"{on}\n注额 ¥{stake} | 盈亏 {('+' if won else '')}{pnl}\n账户余额 ¥{bal}"
            try:
                send_dingtalk(title, body)
                new_notified.add(oid)
                print(f"[slm] 结算推送: {title} | {pnl}", flush=True)
            except Exception as e:
                print(f"[slm] 结算推送异常: {e}")
        if new_notified != notified:
            try:
                LIVE_SETTLED_FILE.write_text(json.dumps(list(new_notified)))
            except Exception:
                pass

    def _token_ok(self):
        """下单前探 token 有效性(10min 缓存)。失效自动续期(读浏览器), 续不到发钉钉提醒。"""
        if time.time() < self._token_ok_until:
            return True  # 缓存有效, 跳过探测
        try:
            from src.betting.bb_auto_bet import read_token, read_domain, _session, auto_renew_token
            tok = read_token(); dom = read_domain()
            if not tok:
                return False
            r = _session().post(f"{dom}/v1/order/new/bet/list",
                                json={"languageType": "CMN", "isSettled": False, "current": 1, "size": 1},
                                headers={"Content-Type": "application/json", "Authorization": tok,
                                         "User-Agent": _UA}, timeout=10, verify=False)
            if r.json().get("code") == 0:
                self._token_ok_until = time.time() + 600  # 10min 缓存
                return True
            # token 失效 → 自动续期(读浏览器 fresh st-auth)
            ok, msg = auto_renew_token()
            if ok:
                self._token_ok_until = time.time() + 600
                print(f"[slm] token 已自动续期: {msg}", flush=True)
                return True
            self._token_remind()
            print(f"[slm] token 续期失败: {msg}", flush=True)
            return False
        except Exception:
            return False

    def _invalidate_token_cache(self):
        """下单返回 14010 时失效 token 缓存, 下次重新探测。"""
        self._token_ok_until = 0.0

    def _token_remind(self):
        """token 失效钉钉提醒(30min 节流)。"""
        if time.time() < self._token_remind_until:
            return
        self._token_remind_until = time.time() + 30 * 60
        try:
            from config.settings import send_dingtalk
            send_dingtalk("⚠️ BB token 失效",
                          "秒级监控下单前探测 token 无效, 滚球/早盘自动下单已暂停, 请自助续期(.bb_token)")
        except Exception as e:
            print(f"[slm] token 提醒异常: {e}")

    def _opp_to_sig(self, opp):
        """fetch_live_opportunities 的 opp dict → 下单 sig dict(对齐 _try_live_auto_bet)。"""
        sub_map = {"1x2": "opportunities", "hc": "handicap", "ou": "over_under", "dc": "double_chance"}
        desig_map = {
            "1x2": {"主": "主胜", "和": "和局", "客": "客胜"},
            "hc": {"主": "让球主胜", "客": "让球客胜"},
            "ou": {"大": "大球", "小": "小球"},
            "dc": {"主": "主/和", "客": "客/和", "和": "主/客"},
        }
        return {
            "match_id": opp["bb_match_id"], "market_id": opp["market_id"],
            "option_type": opp["option_type"], "sub": sub_map.get(opp["sub"], opp["sub"]),
            "desig": desig_map.get(opp["sub"], {}).get(opp["direction"], opp["direction"]),
            "bb_odds": opp["bb_odds"], "fair": opp["fair"], "ev": opp["ev"], "line": opp["line"],
            "match": {"home": opp["home"], "away": opp["away"], "sport": "", "league_cn": "滚球"},
            "pin_matchup_id": opp["pin_matchup_id"], "league_id": opp["league_id"],
            "max_stake": opp["max_stake"],
        }

    def _poll_live(self):
        """轮询 getList type=1 滚球赔率 + 匹配 Pin live → 打信号/自动下单。返回机会数。"""
        from src.scrapers.pinnacle_live import fetch_live_opportunities
        opps = fetch_live_opportunities(self.threshold)
        for opp in opps:
            sig = self._opp_to_sig(opp)
            print(f"⚡滚球+EV {sig['ev']:+.2f}% | {sig['match']['home']} vs {sig['match']['away']} "
                  f"{sig['desig']} | BB {sig['bb_odds']:.2f} vs 公平 {sig['fair']:.2f}", flush=True)
            if self.on_signal:
                try:
                    self.on_signal(sig)
                except Exception as e:
                    print(f"[slm] on_signal 异常: {e}")
            if self.auto_bet:
                self._try_live_auto_bet(sig)
        return len(opps)

    async def run(self, seconds=0, refresh_every=2):
        """轮询 getList type=1 滚球赔率(HTTP, 不依赖浏览器), 每 refresh_every 秒一次。"""
        self._load_live_spent()
        print(f"[slm] 滚球秒级监控(HTTP轮询, 每 {refresh_every}s), 阈值 {self.threshold}%, "
              f"预算已投 ¥{self._live_spent:.0f}/{LIVE_BUDGET}")
        deadline = time.time() + seconds if seconds else None
        poll_count = 0
        while deadline is None or time.time() < deadline:
            try:
                n = self._poll_live()
                if n:
                    print(f"[slm] 本轮发现 {n} 个滚球机会")
                poll_count += 1
                if poll_count % 15 == 0:  # 每 ~30s 查一次已结算订单 → 推钉钉
                    self._check_settled()
                    self._settle_paper_bets()
            except Exception as e:
                print(f"[slm] 轮询异常: {type(e).__name__} {str(e)[:80]}", flush=True)
            await asyncio.sleep(refresh_every)


def main():
    ap = argparse.ArgumentParser(description="BB 秒级比价监控")
    ap.add_argument("--threshold", type=float, default=3.0, help="EV 信号阈值%(默认3)")
    ap.add_argument("--listen", type=int, default=0, help="监听秒数(0=常驻)")
    ap.add_argument("--refresh", type=int, default=2, help="滚球轮询间隔秒(默认2)")
    ap.add_argument("--auto-bet", action="store_true", help="秒级+EV 自动下单(默认关)")
    ap.add_argument("--stake", type=float, default=0.0, help="固定注额(默认0=EV-Kelly半凯利自动定仓)")
    args = ap.parse_args()
    if args.auto_bet:
        if args.stake > 0:
            print(f"⚠️ 自动下单已开启, 固定单注 ¥{args.stake:.0f}")
        else:
            print("⚠️ 自动下单已开启, 注额=EV-Kelly半凯利(封顶¥300)")
    mon = SecondLevelMonitor(threshold=args.threshold, auto_bet=args.auto_bet,
                             stake=(args.stake if args.stake > 0 else None))
    try:
        asyncio.run(mon.run(seconds=args.listen, refresh_every=args.refresh))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
