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
from datetime import datetime
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
MAX_STAKE = 400        # 单盘口上限(与 bb_auto_bet MAX_MARKET_STAKE 一致, 2026-09-06 用户要求 400)
MIN_STAKE = 30         # stake<30 拦截铁律

# 滚球实盘(2026-09-07 起只投小球under, 2026-09-09 放大预算: 小球累计40笔ROI+14.2%稳定正)。滚动预算(结算后释放额度)。
LIVE_BUDGET = 3000  # 2026-09-10 用户要求: 2000 → 3000(滚球大小球+15%是唯一真edge, 集中投入)
LIVE_BUDGET_FILE = ROOT / "data" / "storage" / "live_bet_budget.json"
LIVE_PAPER_FILE = ROOT / "data" / "storage" / "live_paper_bets.json"
LIVE_SETTLED_FILE = ROOT / "data" / "storage" / "live_settled_notified.json"  # 已推送过结算的 order_id

# 滚球实盘只投小球(under), EV-Kelly 最优定仓, 单注上限 ¥400(预算1/5)兼顾分散。
# 其它盘口(大球/1x2/让球)仍只进观察库, 不下真单。
LIVE_REAL_BET_ENABLED = True
LIVE_UNDER_MAX_STAKE = 600  # 单注上限(2026-09-12 500→600: 小球73笔赢率70%vs隐含57%+14pp持续稳定, 再放量20%)
BB_SPORT_CN = {1: "足球", 3: "篮球", 5: "网球", 7: "棒球", 6: "美式足球"}

# 滚球 → 观察库统一口径(2026-09-12 观察库释放改造): sport 数字→英文; sub→sub_market。
# 用于查 observe_release_caps(运动×"滚球"×盘口), 与 compute_market_release.py 同口径。
BB_SPORT_EN = {1: "football", 3: "basketball", 5: "tennis", 7: "baseball", 6: "american_football"}
BB_SUB_TO_SM = {"over_under": "ou", "handicap": "hc", "opportunities": "1x2"}
MARKET_RELEASE_FILE = ROOT / "data" / "storage" / "market_release.json"
OBS_STATE_FILE = ROOT / "data" / "storage" / "observe_release_state.json"
DAILY_STAKE_LIMIT = 1000  # 新释放盘口当日累计投注额上限(2026-09-12 用户要求), 次日实盘ROI>4%解除
PENDING_SETTLE_FILE = ROOT / "data" / "storage" / "pending_settle.json"  # 结算明细缓存(每小时汇总推一次)
SETTLE_PUSH_INTERVAL = 3600  # 结算明细每小时汇总推一次(2026-09-12 用户要求, 不一场推一场)

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


_obs_caps_cache = None
_obs_caps_mtime = 0.0


def _load_obs_caps():
    """读 observe_release_caps(运动×联赛×盘口 → 150/300), 带 mtime 缓存。

    观察库释放盘口的投注额 cap(2026-09-12 用户要求): 新释放 150, 实盘满7天ROI>4% 300。
    文件由 compute_market_release.py 生成, 滚球格子 league 归 "滚球"。
    """
    global _obs_caps_cache, _obs_caps_mtime
    if not MARKET_RELEASE_FILE.exists():
        return {}
    try:
        m = MARKET_RELEASE_FILE.stat().st_mtime
        if _obs_caps_cache is None or m != _obs_caps_mtime:
            _obs_caps_cache = (json.loads(MARKET_RELEASE_FILE.read_text()) or {}).get(
                "observe_release_caps", {}) or {}
            _obs_caps_mtime = m
        return _obs_caps_cache
    except Exception:
        return {}


_release_state_cache = None
_release_state_mtime = 0.0


def _load_release_state():
    """读 observe_release_state.json(释放盘口状态: daily_stake/daily_stake_date/limit_removed), 带 mtime 缓存。"""
    global _release_state_cache, _release_state_mtime
    if not OBS_STATE_FILE.exists():
        return {}
    try:
        m = OBS_STATE_FILE.stat().st_mtime
        if _release_state_cache is None or m != _release_state_mtime:
            _release_state_cache = json.loads(OBS_STATE_FILE.read_text()) or {}
            _release_state_mtime = m
        return _release_state_cache
    except Exception:
        return {}


def _update_daily_stake(key, stake):
    """下单成功后更新释放盘口的当日累计投注额(2026-09-12)。"""
    try:
        state = _load_release_state()
        rs = state.get(key)
        if not rs:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if rs.get("daily_stake_date", "") != today:
            rs["daily_stake"] = 0
            rs["daily_stake_date"] = today
        rs["daily_stake"] = rs.get("daily_stake", 0) + stake
        state[key] = rs
        tmp = OBS_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        tmp.replace(OBS_STATE_FILE)
    except Exception:
        pass


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
    if desig in ("让球主胜", "让球客胜"):
        # 让球0(line=0)BB会 void(st=2 退款, 实测 26 笔里 1 笔=3.8%), getMatchDetail 拿不到
        # void 状态 → 无法可靠判输赢, 不进库(与 quarter-ball 同策略: 结算不了的样本不采)。
        return line is not None and abs(line) > 0.0001
    if desig in ("大球", "小球"):
        return line is not None
    return False


def _bet_score(b):
    """下注瞬间比分 [主,客] → (hb, ab)。无(旧记录/新盘口未带)则回退 (0,0)=全场比分。"""
    v = b.get("bsc")
    if isinstance(v, list) and len(v) >= 2:
        try:
            return int(v[0]), int(v[1])
        except (ValueError, TypeError):
            pass
    return 0, 0


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
        self._live_spent = 0.0  # 当日滚球累计已投注额(读自 LIVE_BUDGET_FILE)
        self._live_outstanding = 0.0  # 当日滚球未结算投注额(滚动预算: 结算后释放额度)
        self._live_bets = {}   # 未结算订单 order_id -> 注额(结算时扣减 outstanding)
        self._bet_notify_until = 0.0  # 钉钉下单通知节流(30min 内最多一条)
        self._token_remind_until = 0.0  # token 失效钉钉提醒节流(30min)
        self._token_ok_until = 0.0     # token 有效缓存到期时间戳(10min 缓存, 省每单 1s 探测)
        self._attempted = {}           # 滚球指纹去重: match_id -> {market_id -> 尝试时间戳}
        self._last_bet_time = 0.0      # 上次下单时间(非阻塞限频用)
        self._bet_delay = 0.0          # 下一单需等待的随机间隔(10-15s, 每次下单后重抽)
        self._last_settle_push = 0.0   # 上次结算汇总推送时间(每小时推一次, 2026-09-12)
        self._reversion_track = {}     # Reversion check: (match_id, market_id, option_type) -> {bb_odds, ts}

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
        """读当日滚球已投注额/未结算额。日期不匹配自动重置。"""
        today = time.strftime("%Y-%m-%d")
        try:
            if LIVE_BUDGET_FILE.exists():
                d = json.loads(LIVE_BUDGET_FILE.read_text())
                if d.get("date") == today:
                    self._live_spent = float(d.get("spent", 0))
                    self._live_outstanding = float(d.get("outstanding", 0))
                    self._live_bets = d.get("bets", {})
                    return
        except Exception:
            pass
        self._live_spent = 0.0
        self._live_outstanding = 0.0
        self._live_bets = {}

    def _save_live_spent(self):
        today = time.strftime("%Y-%m-%d")
        try:
            LIVE_BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
            LIVE_BUDGET_FILE.write_text(json.dumps({
                "date": today, "spent": self._live_spent,
                "outstanding": self._live_outstanding, "bets": self._live_bets,
            }, ensure_ascii=False))
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
            # 指纹去重: 同一 (match_id, market_id, option_type, sub) 只记一次, 避免每 2s 轮询重复入库
            key = (sig["match_id"], sig.get("market_id"), sig.get("option_type"), sig.get("sub"))
            for b in data:
                if (b.get("match_id"), b.get("market_id"), b.get("option_type"), b.get("sub")) == key:
                    return
            data.append({
                "ts": time.time(), "match_id": sig["match_id"],
                "market_id": sig.get("market_id"), "option_type": sig.get("option_type"),
                "sport": sig.get("sport", ""),
                "home": sig["match"].get("home", ""), "away": sig["match"].get("away", ""),
                "designation": sig["desig"], "sub": sig.get("sub"), "line": sig.get("line"),
                "bsc": sig.get("bsc"),  # [主,客] 下注瞬间比分(让球按当前比分结算用)
                "bb_odds": sig["bb_odds"], "fair": sig["fair"], "ev": sig["ev"],
                "stake": sig.get("_stake", 0), "settled": False, "result": None, "profit": None,
            })
            LIVE_PAPER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        except Exception as e:
            print(f"[slm] 虚拟投注写入失败: {str(e)[:80]}", flush=True)

    def _settle_paper_bets(self):
        """结算虚拟投注(观察库): 按 match_id 定向查 getMatchDetail 判输赢, 写回 result/profit。"""
        from src.betting.bb_auto_bet import read_token
        if not LIVE_PAPER_FILE.exists():
            return
        try:
            bets = json.loads(LIVE_PAPER_FILE.read_text())
        except Exception:
            return
        tok = read_token()
        if not tok:
            return
        print(f"[slm] 观察库结算开始: {len(bets)} 条, tok={'有' if tok else '无'}", flush=True)
        # 按 match_id 定向查比分(2026-09-12 修结算bug): getList type=6 的 pageSize 被 BB 限制为
        # 50 条(分页拉全量完赛不生效), 覆盖不了 32h 累积的观察库样本 → 永远结算不到。
        # 改用 getMatchDetail 逐场定向查(无窗口限制, 已结束比赛仍可查), 与 bb_score_settle 同源。
        from src.scrapers.bb_api_fetcher import fetch_bb_match_result
        changed = False
        now = time.time()
        attempted = 0
        MAX_PER_BATCH = 50  # 每批最多查 50 场(1178条逐场 getMatchDetail 太慢, 分批避免阻塞主循环 30s 轮询)
        for b in bets:
            if b.get("settled"):
                continue
            mid = b.get("match_id")
            ts = b.get("ts", 0)
            # 只结算「已捕捉超过 2h」(足球应已完赛) 的样本, 避免还没完赛的无效请求
            if not mid or not ts or now - ts < 2 * 3600:
                continue
            if attempted >= MAX_PER_BATCH:
                break
            attempted += 1
            detail = fetch_bb_match_result(mid, language_type="EN")
            if detail is None:
                # match_id 找不到比赛(用户纠正: BB 对所有比赛都能拿赛果, 拿不到就是 match_id 找错了)。
                # 连续 3 次返回 None 视为「找错」→ 标记删除, 避免永远结算不了污染统计。
                b["_not_found"] = b.get("_not_found", 0) + 1
                if b["_not_found"] >= 3:
                    b["_unresolvable"] = True
                    changed = True
                continue
            if not detail.get("completed"):
                continue
            if detail.get("home_score") is None or detail.get("away_score") is None:
                continue
            sc = [detail["home_score"], detail["away_score"]]
            desig = b.get("designation", ""); line = b.get("line")
            stake = float(b.get("stake", 0)); odds = float(b.get("bb_odds", 0))
            home, away = sc[0], sc[1]
            # 判输赢。1x2(主/和/客)是三向盘、没有走盘: 打平对主胜/客胜是输、对和局是赢。
            # 走盘(push 退款)只存在 hc/ou: 让球后打平 / 总分恰等于盘口线。
            if desig == "主胜":
                result = "won" if home > away else "lost"
            elif desig == "客胜":
                result = "won" if away > home else "lost"
            elif desig == "和局":
                result = "won" if home == away else "lost"
            elif desig in ("让球主胜", "让球客胜", "大球", "小球"):
                if line is None:
                    continue  # hc/ou 缺 line, 跳(旧记录)
                if desig == "让球主胜":
                    # 让球按「当前比分让球」(2026-09-13 修根因): BB 滚球让球线相对下注瞬间比分,
                    # 之前拿全场终局比分结算, 把「输/退款」误判「赢」→ 让球主胜赢率虚高到 57%(实盘40%)。
                    hb, ab = _bet_score(b)
                    diff = ((home - hb) + line) - (away - ab)
                elif desig == "让球客胜":
                    hb, ab = _bet_score(b)
                    diff = ((away - ab) + line) - (home - hb)
                elif desig == "大球":
                    diff = (home + away) - line
                else:  # 小球
                    diff = line - (home + away)
                if abs(diff) < 0.0001:
                    result = "push"
                else:
                    result = "won" if diff > 0 else "lost"
            else:
                continue
            profit = 0.0 if result == "push" else (stake * (odds - 1) if result == "won" else -stake)
            b["settled"] = True; b["result"] = result; b["profit"] = round(profit, 1)
            changed = True
        # 删除「match_id 找不到比赛」的样本(连续 3 次 getMatchDetail 返回 None)
        _before = len(bets)
        bets = [b for b in bets if not b.get("_unresolvable")]
        if len(bets) < _before:
            print(f"[slm] 删除 match_id 无效样本: {_before - len(bets)} 条", flush=True)
            changed = True
        if attempted > 0:
            _settled_n = sum(1 for b in bets if b.get("settled"))
            print(f"[slm] 观察库结算扫描: 尝试 {attempted} 场, 已结算 {_settled_n}/{len(bets)} 条", flush=True)
        if changed:
            LIVE_PAPER_FILE.write_text(json.dumps(bets, ensure_ascii=False, indent=1))
            settled = [b for b in bets if b.get("settled")]
            pnl = sum(b.get("profit", 0) for b in settled)
            stk = sum(b.get("stake", 0) for b in settled)
            won_n = sum(1 for b in settled if b.get("result") == "won")
            roi = pnl / stk * 100 if stk else 0
            print(f"[slm] 虚拟投注结算: {len(settled)}/{len(bets)} 条, 盈亏 {pnl:+.1f}, ROI {roi:+.1f}% (胜{won_n})", flush=True)
            # 按运动×盘口分账(2026-09-06): 只打印有样本的格子, 供判断哪个运动哪个盘口盈利
            from collections import defaultdict
            grid = defaultdict(lambda: [0.0, 0.0, 0])
            for b in settled:
                _sp = b.get("sport")
                sp = BB_SPORT_CN.get(_sp) if isinstance(_sp, int) else (f"sp{_sp}" if _sp else "未标运动")
                k = (sp, b.get("sub", "?"))
                grid[k][0] += b.get("profit", 0) or 0
                grid[k][1] += b.get("stake", 0) or 0
                grid[k][2] += 1
            for (sp, sub), (pnl2, stk2, n2) in sorted(grid.items(), key=lambda x: -x[1][2]):
                if stk2 > 0:
                    print(f"    {sp}/{sub}: n={n2} 盈亏{pnl2:+.0f} ROI{pnl2/stk2*100:+.1f}%", flush=True)

    def _check_reversion(self):
        """Reversion check(2026-09-07): 下注 30s 后复验 BB 价, 回落≥10% 标记为尖峰假 EV。

        尖峰假 EV = BB 临时高价(下单后几秒就回落), 不是真 edge。标记后结算统计时剔除。
        """
        if not self._reversion_track:
            return
        now = time.time()
        ready = {k: v for k, v in self._reversion_track.items() if now - v["ts"] >= 30}
        if not ready:
            return
        try:
            from src.scrapers.pinnacle_live import fetch_bb_live_matches
            bb = fetch_bb_live_matches()
        except Exception:
            return
        for (mid, mkt, opt), v in ready.items():
            del self._reversion_track[(mid, mkt, opt)]
            try:
                m = bb.get(int(mid))
                if not m:
                    continue
                for mk in m.get("markets", []):
                    if str(mk.get("market_id")) == mkt and str(mk.get("option_type")) == opt:
                        cur = mk.get("odds", 0)
                        if cur > 0 and v["bb_odds"] > 0:
                            drop_pct = (v["bb_odds"] - cur) / v["bb_odds"] * 100
                            if drop_pct >= 10:
                                self._mark_spike(int(mid), mkt, opt, drop_pct)
                        break
            except Exception:
                pass

    def _mark_spike(self, match_id, market_id, option_type, drop_pct):
        """把 live_paper_bets 对应记录标记 spike(尖峰假 EV), 供统计剔除。"""
        try:
            if not LIVE_PAPER_FILE.exists():
                return
            bets = json.loads(LIVE_PAPER_FILE.read_text())
            changed = False
            for b in bets:
                if (b.get("match_id") == match_id
                        and str(b.get("market_id")) == str(market_id)
                        and str(b.get("option_type")) == str(option_type)):
                    if not b.get("spike"):
                        b["spike"] = round(drop_pct, 1)
                        changed = True
                    break
            if changed:
                LIVE_PAPER_FILE.write_text(json.dumps(bets, ensure_ascii=False, indent=1))
                print(f"[slm] Reversion check: 尖峰假 EV 标记 {drop_pct:.0f}% 回落 (match={match_id})", flush=True)
        except Exception:
            pass

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
                if len(ml) == 3:
                    idx = {"主": 0, "和": 1, "客": 2}
                    desig = {"主": "主胜", "和": "和局", "客": "客胜"}
                    raw = ml
                elif len(ml) == 2:
                    # 2-way(网球/篮球等无和局): 主/客
                    idx = {"主": 0, "客": 1}
                    desig = {"主": "主胜", "客": "客胜"}
                    raw = ml
                else:
                    continue
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
                   "match": {"home": lv.get("home_cn", "") or lv["home"], "away": lv.get("away_cn", "") or lv["away"], "sport": lv.get("sport", ""), "league_cn": lv.get("league_cn", "") or lv.get("league_name", "滚球"), "mc": lv.get("mc", 0)},
                   "pin_matchup_id": lv.get("pin_matchup_id"),
                   "league_id": lv.get("league_id"),
                   "max_stake": lv.get("max_stake", 0),
                   "bsc": lv.get("sc"),  # [主,客] 下注瞬间比分(让球按当前比分结算用, 2026-09-13)
            }
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
        """滚球机会处理: 观察库入库前验价(漂移假EV不入库), 实盘下单由 LIVE_REAL_BET_ENABLED 控制。"""
        # 2026-09-09 用户要求: 滚球价秒级漂移, 扫描瞬间EV是假的 → 入库前重新验价,
        # 漂移/盘口关闭的不入库(假样本没分析意义)。验价结果同时供实盘小球下单复用。
        fresh_ev = self._reverify_live_ev(sig)
        if fresh_ev is None or fresh_ev < self.threshold:
            return
        sig["ev"] = fresh_ev  # 入库/下单都用验价后的真实 EV
        stake = self._stake_for(sig)
        sig["_stake"] = stake
        # 2026-09-12 纠正: 观察库必须采集所有运动及盘口的有效+EV信号(不只实盘方向)。
        # 之前误改"只记实盘方向"会堵死新盘口释放通道(非实盘方向永远没数据凑不到n>100)。
        # 观察库释放靠「赢率>隐含」判据区分真假溢价(大球赢率<隐含不释放, 小球赢率>隐含释放),
        # 不需要在采样层就过滤。有效=验价通过(fresh_ev≥threshold)+可结算(白名单), 已由上游保证。
        self._append_live_paper_bet(sig)
        if not LIVE_REAL_BET_ENABLED:
            return
        # 2026-09-12 优化: 不再硬编码"只投小球/网球/篮球", 改为「观察库释放优先, 已验证方向其次」。
        # 观察库释放的盘口(让球主胜/客胜等赢率>隐含+n>100)也实盘投(cap 150/300 试探)。
        _sport = sig["match"].get("sport")
        _sub = sig.get("sub")
        _desig = sig.get("desig")
        # 方向归一化(大/小/平/客/主), 与 compute_market_release._direction 同口径
        if "大" in _desig:
            _dr = "大"
        elif "小" in _desig:
            _dr = "小"
        elif ("和" in _desig or "平" in _desig) and "客" not in _desig and "主" not in _desig:
            _dr = "平"
        elif "客" in _desig:
            _dr = "客"
        elif "主" in _desig:
            _dr = "主"
        else:
            _dr = "其他"
        _sp_en = BB_SPORT_EN.get(_sport)
        _sm = BB_SUB_TO_SM.get(_sub)
        _cap = None
        # 1. 观察库释放盘口 → 投(cap 150/300)
        if _sp_en and _sm:
            _cap = _load_obs_caps().get(f"{_sp_en}|{_sm}|{_dr}|live")
        # 2. 已验证方向(足球小球/网球独赢/篮球大小让分) → 投(cap 600/100)
        if _cap is None:
            _bettable = (
                (_sport == 1 and _sub == "over_under" and _desig == "小球")
                or (_sport == 5 and _sub == "opportunities")      # 网球独赢(主/客)
                or (_sport == 3 and _sub == "over_under")         # 篮球大小分
                or (_sport == 3 and _sub == "handicap")           # 篮球让分
            )
            if _bettable:
                _cap = LIVE_UNDER_MAX_STAKE if (_sport == 1 and _desig == "小球") else 100
        if _cap is None:
            return
        stake = min(stake, _cap)
        # 当日累计投注额上限(2026-09-12 用户要求): 观察库释放且未解除限制的盘口, 当日累计≤1000
        if _sp_en and _sm:
            _rs = _load_release_state().get(f"{_sp_en}|{_sm}|{_dr}|live", {})
            if not _rs.get("limit_removed", False):
                _today = datetime.now().strftime("%Y-%m-%d")
                _daily_stake = _rs.get("daily_stake", 0) if _rs.get("daily_stake_date", "") == _today else 0
                if _daily_stake + stake > DAILY_STAKE_LIMIT:
                    print(f"  📝 当日累计超限({_daily_stake:.0f}+{stake:.0f}>{DAILY_STAKE_LIMIT}), 跳过 "
                          f"{sig['match']['home']} vs {sig['match']['away']} {sig['desig']}", flush=True)
                    return
        sig["_stake"] = stake
        if stake < MIN_STAKE:
            return
        self._load_live_spent()
        tag = f"{sig['match']['home']} vs {sig['match']['away']} {sig['desig']}"
        # 预算封顶(滚动预算): 按"未结算额"封顶, 结算后释放额度 → 结算的钱可继续投滚球
        if self._live_outstanding + stake > LIVE_BUDGET:
            print(f"  📝 滚球预算已满(未结算¥{self._live_outstanding:.0f}/{LIVE_BUDGET}), 已记观察库 {tag}", flush=True)
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
        # 全局冷却(2026-09-08): 跨进程共享时间戳, 避免早盘+滚球"同一时间"下单像机器投注
        from src.betting.bb_auto_bet import global_bet_cooldown
        if global_bet_cooldown(15, 45) > 0:
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
        # 记录全局下单时间戳(早盘+滚球共享冷却起点)
        from src.betting.bb_auto_bet import record_global_bet
        record_global_bet()
        if code == 14010:
            self._invalidate_token_cache()
        if code == 0:
            self._live_spent += stake
            self._live_outstanding += stake
            if order_id:
                # 记录投注时的 fair/ev/bb_odds 等(2026-09-12: 结算汇总时按 order_id 关联补全明细要素)
                self._live_bets[str(order_id)] = {
                    "stake": stake,
                    "fair": sig.get("fair", 0), "ev": sig.get("ev", 0),
                    "bb_odds": sig.get("bb_odds", 0), "desig": sig.get("desig", ""),
                    "sub": sig.get("sub", ""), "home": sig["match"].get("home", ""),
                    "away": sig["match"].get("away", ""), "sport": sig["match"].get("sport", ""),
                }
            self._save_live_spent()
            # 更新释放盘口的当日累计投注额(2026-09-12 用户要求: 当日累计≤1000)
            if _sp_en and _sm:
                _update_daily_stake(f"{_sp_en}|{_sm}|{_dr}|live", stake)
            print(f"  ✅ 滚球下单成功 {tag} | 注额¥{stake} | 累计¥{self._live_spent:.0f}/{LIVE_BUDGET}", flush=True)
            # 每笔成功下单都推钉钉(不限频) + 显示账户总余额
            from src.betting.bb_auto_bet import fetch_balance as _fetch_balance
            _bal = _fetch_balance() or "未知"
            _sport_cn = BB_SPORT_CN.get(sig["match"].get("sport"), "") or ""
            _league = sig["match"].get("league_cn", "滚球") or "滚球"
            _mc = int(sig["match"].get("mc", 0) or 0)
            _clock = f"进行中 {_mc // 60} 分钟" if _mc > 0 else "进行中"
            _bj = datetime.now().strftime("%H:%M")
            _sub_cn = {"over_under": "大小球", "handicap": "让球", "opportunities": "独赢"}.get(sig.get("sub"), "")
            _desig = f"{_sub_cn}-{sig.get('desig', '')}" if _sub_cn else sig.get("desig", "")
            self._notify_bet(
                "🟦 滚球已投注",
                f"{_sport_cn} | {_league} | 滚球{_clock} | 投注 {_bj}\n"
                f"{sig['match']['home']} vs {sig['match']['away']} | {_desig}\n"
                f"BB {sig['bb_odds']:.2f} vs 公平价 {sig['fair']:.2f} | 溢价 {sig['ev']:+.2f}% | 置信度:滚球\n"
                f"注额 ¥{stake} | 账户余额 ¥{_bal} | 今日累计 ¥{self._live_spent:.0f}/{LIVE_BUDGET}")
            # Reversion check(2026-09-07): 记下注时 BB 价, 30s 后复验是否尖峰回落(假 EV)
            self._reversion_track[(str(sig["match_id"]), str(market_id), str(sig.get("option_type")))] = {
                "bb_odds": sig["bb_odds"], "ts": time.time(),
            }
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
            ok = bool(send_dingtalk(title, body))
            if not ok:
                print(f"[slm] 秒级下单推送失败: {title}", flush=True)
        except Exception as e:
            print(f"[slm] 钉钉通知异常: {e}")

    def _notify_bet(self, title, body):
        """每笔成功下单都推钉钉(不限频 —— 下单限频已把间隔拉到 10-15s, 不会刷屏)。"""
        try:
            from config.settings import send_dingtalk
            ok = bool(send_dingtalk(title, body))
            if not ok:
                print(f"[slm] 滚球投注推送失败: {title}", flush=True)
        except Exception as e:
            print(f"[slm] 钉钉通知异常: {e}")

    def _push_settle_summary(self, pending):
        """每小时汇总推结算明细(2026-09-12 用户要求, 不一场推一场)。"""
        try:
            from config.settings import send_dingtalk
        except Exception:
            return
        total_pnl = sum((p.get("uwl", 0) or 0) for p in pending)
        lines = []
        for p in pending:
            mn = p.get("mn", "?"); mgn = p.get("mgn", ""); on = p.get("on", "?")
            od = p.get("od", 0); sat = p.get("sat", 0); pnl = p.get("uwl", 0); won = p.get("won", False)
            sid = p.get("sid", 0)
            sp_cn = BB_SPORT_CN.get(sid, "") or ""
            _desig = f"{mgn}-{on}" if mgn else on
            sign = "+" if won else ""
            fair = p.get("fair", 0); ev = p.get("ev", 0)
            _fair_str = f" | 公平价 {fair:.2f}" if fair else ""
            _ev_str = f" | 溢价 {ev:+.1f}%" if ev else ""
            lines.append(f"{sp_cn} {mn} | {_desig} @{od}{_fair_str}{_ev_str} | 注额¥{sat} | {sign}{pnl:.0f}")
        body = f"📊 滚球结算汇总({len(pending)}笔, 总盈亏{total_pnl:+.0f})\n\n" + "\n".join(lines)
        try:
            ok = bool(send_dingtalk("📊 滚球结算汇总(赢了/输了)", body))
            if ok:
                PENDING_SETTLE_FILE.write_text(json.dumps([], ensure_ascii=False))
                print(f"[slm] 结算汇总推送成功: {len(pending)}笔, 总盈亏{total_pnl:+.0f}", flush=True)
        except Exception as e:
            print(f"[slm] 结算汇总推送异常: {e}", flush=True)

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
        # 滚动预算: 已结算的滚球订单从"未结算额"里释放, 结算的钱可继续投滚球
        settled_info = {}  # oid -> 投注时 info(供结算明细关联 fair/ev)
        if self._live_bets:
            _released = False
            for o in records:
                oid = str(o.get("id", ""))
                if oid and oid in self._live_bets:
                    _v = self._live_bets.pop(oid, 0)
                    settled_info[oid] = _v  # 保留投注时 info
                    _stake = _v.get("stake", 0) if isinstance(_v, dict) else _v
                    self._live_outstanding = max(0.0, self._live_outstanding - float(_stake))
                    _released = True
            if _released:
                self._save_live_spent()
                print(f"[slm] 结算释放额度: 未结算降至 ¥{self._live_outstanding:.0f}/{LIVE_BUDGET}", flush=True)
        new_notified = set(notified)
        # 收集结算明细到缓存(不立即推, 每小时汇总推一次, 2026-09-12 用户要求)
        pending = []
        if PENDING_SETTLE_FILE.exists():
            try:
                pending = json.loads(PENDING_SETTLE_FILE.read_text())
            except Exception:
                pending = []
        for o in records:
            oid = o.get("id")
            if not oid or oid in notified:
                continue
            ops = o.get("ops") or []
            op = ops[0] if ops else {}
            mn = op.get("mn", "?"); on = op.get("on", "?")
            mgn = op.get("mgn", "")  # 盘口名(大/小/让球/独赢)
            stake = o.get("sat", 0); pnl_raw = o.get("uwl", "0")
            try:
                pnl = float(pnl_raw)
            except (TypeError, ValueError):
                pnl = 0.0
            won = pnl > 0
            od = op.get("od", 0)  # 赔率
            sid = op.get("sid", 0)  # 运动 id
            # 关联投注时的 fair/ev(从 settled_info, 按 order_id)
            _bi = settled_info.get(str(oid), {})
            _fair = _bi.get("fair", 0) if isinstance(_bi, dict) else 0
            _ev = _bi.get("ev", 0) if isinstance(_bi, dict) else 0
            pending.append({
                "mn": mn, "mgn": mgn, "on": on, "od": od,
                "sat": stake, "uwl": pnl, "sid": sid, "won": won,
                "fair": _fair, "ev": _ev,
            })
            new_notified.add(oid)
            print(f"[slm] 结算收集: {'✅赢' if won else '❌输'} {mn} {mgn}-{on} | {pnl:+.0f}", flush=True)
        if pending:
            try:
                PENDING_SETTLE_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=1))
            except Exception:
                pass
        if new_notified != notified:
            try:
                LIVE_SETTLED_FILE.write_text(json.dumps(list(new_notified)))
            except Exception:
                pass
        # 每小时汇总推一次结算明细
        if pending and time.time() - self._last_settle_push >= SETTLE_PUSH_INTERVAL:
            self._push_settle_summary(pending)
            self._last_settle_push = time.time()

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
            "sport": opp.get("sport", ""),
            "match": {"home": opp["home"], "away": opp["away"], "sport": opp.get("sport", ""),
                      "league_cn": opp.get("league_cn", "滚球") or "滚球"},
            "pin_matchup_id": opp["pin_matchup_id"], "league_id": opp["league_id"],
            "max_stake": opp["max_stake"],
            "bsc": opp.get("sc"),  # [主,客] 下注瞬间比分(让球按当前比分结算用, 2026-09-13)
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
              f"累计已投 ¥{self._live_spent:.0f} | 未结算 ¥{self._live_outstanding:.0f}/{LIVE_BUDGET}")
        deadline = time.time() + seconds if seconds else None
        poll_count = 0
        # 启动时立即结算一次观察库(2026-09-12): poll_count%15 触发被 pin_live 超时拖慢(每轮
        # 20-30s, 到15需5-7min), 观察库 1180 条久久结算不到。启动先结一批, 之后每 60s 独立补结。
        try:
            self._settle_paper_bets()
        except Exception as e:
            print(f"[slm] 启动结算异常: {type(e).__name__} {str(e)[:80]}", flush=True)
        last_settle = time.time()
        while deadline is None or time.time() < deadline:
            try:
                n = self._poll_live()
                if n:
                    print(f"[slm] 本轮发现 {n} 个滚球机会")
                poll_count += 1
                # 观察库结算: 每 60s 结一批(独立时间戳, 不依赖 poll_count — pin_live 超时拖慢轮询,
                # 依赖 poll_count%15 会把 1130 条拖到 2-3h 才结完)
                if time.time() - last_settle >= 60:
                    last_settle = time.time()
                    self._settle_paper_bets()
                if poll_count % 15 == 0:  # 每 ~30s 查一次已结算订单 → 推钉钉
                    self._check_settled()
                    self._check_reversion()  # 下注后 30s 复验 BB 价, 尖峰假 EV 标记
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
        if not LIVE_REAL_BET_ENABLED:
            print(f"⚠️ 滚球实盘下单已暂停(LIVE_REAL_BET_ENABLED=False), 所有+EV只进观察库")
        elif args.stake > 0:
            print(f"⚠️ 自动下单已开启, 固定单注 ¥{args.stake:.0f}")
        else:
            print(f"⚠️ 自动下单已开启, 注额=EV-Kelly半凯利(封顶¥{MAX_STAKE:.0f})")
    mon = SecondLevelMonitor(threshold=args.threshold, auto_bet=args.auto_bet,
                             stake=(args.stake if args.stake > 0 else None))
    try:
        asyncio.run(mon.run(seconds=args.listen, refresh_every=args.refresh))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
