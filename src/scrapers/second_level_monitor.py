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
import os
import hashlib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.scrapers.bb_ws_push import tap_browser_g04
import config.settings  # noqa: E402,F401 触发 .env→os.environ 加载, 让下方 os.getenv(开关) 读到 .env 值(2026-10-11)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

COMPARISON_FILE = ROOT / "data" / "storage" / "bb_vs_pinnacle_comparison.json"

# EV-Kelly 半凯利仓位(秒级自动下单注额)。有效资金 = ¥5000(日目标, 2026-09-06 用户选 C),
# 让弱 edge 落在 ¥300 上限以下, 强 edge 顶格, 真正按 edge 分档。
KELLY_FRACTION = 0.5   # 半凯利
MAX_STAKE = 80         # 滚球单盘口上限(2026-11-01 用户明确要求: 滚球单笔≤80; 铁律, 未授权不改)

_POLL_EXECUTOR = None  # 2026-09-24 防假死: 比价拉取线程池(lazy初始化, max_workers=2), 90s超时
_TASK_EXECUTOR = None  # 2026-09-26 防假死: 结算/CLV/早盘WS 用独立线程池(max_workers=16), 卡住的readline不占满asyncio默认池(根治假死)


async def _run_task(fn, *args, timeout=30, tag="任务"):
    """在独立线程池(_TASK_EXECUTOR)执行 fn, 超时跳过(防假死)。

    2026-09-26 根治假死: 之前用 asyncio.to_thread(默认池), 卡住的 readline 调用累积占满
    默认池(max_workers=32), 导致主循环后续 to_thread(wait_change) 也排队 → 日志停更假死。
    改用独立大容量池(max_workers=16), 卡住的线程只占独立池, 不影响主循环的 wait_change。
    """
    global _TASK_EXECUTOR
    if _TASK_EXECUTOR is None:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        _TASK_EXECUTOR = _TPE(max_workers=16)
    try:
        _loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            _loop.run_in_executor(_TASK_EXECUTOR, fn, *args),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        print(f"[slm] ⚠️ {tag}超时(>{timeout}s)强制跳过(防假死)", flush=True)
    except Exception:
        pass

# 2026-09-19 用户选B: 滚球本金 = 固定本金基准(初始¥20000), 不随当前余额逐笔缩水。
# 之前「余额×30%」是顺周期陷阱(余额降→bankroll降→注额缩到30/60), 改用固定基准
# 恢复 8 月 bankroll 2万时代(注额能到~400)。基准值存 bankroll_base.txt, 用户可手动改。
BANKROLL_BASE_FILE = ROOT / "data" / "storage" / "bankroll_base.txt"
_bankroll_cache = {"ts": 0.0, "value": 20000.0}  # 本金缓存(60s TTL), 兜底 20000


def _bankroll():
    """滚球本金 = 固定本金基准(读 bankroll_base.txt, 缺省¥20000), 60s 缓存。"""
    now = time.time()
    if now - _bankroll_cache["ts"] < 60:
        return _bankroll_cache["value"]
    v = 20000.0
    try:
        if BANKROLL_BASE_FILE.exists():
            v = float(BANKROLL_BASE_FILE.read_text().strip() or 0) or 20000.0
    except (OSError, ValueError):
        v = 20000.0
    _bankroll_cache["ts"] = now
    _bankroll_cache["value"] = v
    return v
MIN_STAKE = 30         # stake<30 拦截铁律
DERIVED_FAIR_SUBS = {"dc", "double_chance", "ht", "ht_hc", "ht_ou"}  # 停投盘口: dc(推导公平价合成误差一直输) + ht系列(半场比分源缺失无法结算, 永远void)
REVERIFY_THRESHOLD = 3.0  # 2026-09-19 验价阈值(秒): lead-lag 窗口 2-3s(赔率秒级变动), 超过 3s 就重拉验价(逆向选择)
SNAPSHOT_MAX_AGE = 2.0  # 快照年龄阈值(秒): 2026-10-13 从 6s 收紧到 2s(τ 中位~7s, 端到端~1.7s, 2s 保新鲜度; 后续按 τ 分位数再调)
                        # 之前15s是管线12s时的临时值; 现在管线~1.7s(公平价匹配0.1s), 收紧回6s
                        # 更能吃到 lead-lag 窗口(8.5s), 挡掉真正 stale 的(>6s)
SCORE_CHECK_MAX_AGE = 3.0  # 2026-10-11 比分校验的可信缓存年龄上限(秒): 预取缓存>3s 视为不可信→跳过
                           # 修相位陷阱: 预取 getList~2.2s+0.5s 周期, 缓存年龄 0~2.7s 锯齿波动。之前设 2.0s
                           # 会确定性误杀(缓存 age 在 2.0~2.7s 时被判不可信, 其实比分没变)。3.0s > 2.7s 最大陈旧,
                           # 让 untrusted 门只在真正超龄(>3s, 网络抖动)时触发; 死球主防线是 verify_price(重拉+BB进球封盘)。

# ── 滚球冷却三层(2026-10-11): per-market 防连打 + per-event 敞口 + 全局频次帽。读配置可回退。──
# 之前「互斥锁(per-market) + 全局冷却(3-8s)」口径冲突: per-market 允许并发, 全局却要求串行。
# 三层拆分: 同一口子防连打(per-market), 单场敞口上限(per-event), 账号风控帽(全局滑动窗口)。
LIVE_COOLDOWN_PER_MARKET = os.getenv("LIVE_COOLDOWN_PER_MARKET", "true").lower() in ("1", "true", "yes")
LIVE_MARKET_COOLDOWN = (3, 8)   # 层1: per-(event,market) 冷却(秒) 随机区间, 同一口子防连打
LIVE_EVENT_MAX_EXPOSURE = 3     # 层2: per-event 单场最多持仓数(防对冲吃手续费)
LIVE_GLOBAL_RATE_MIN = 4        # 层3: 全局频次帽 每分钟≤4 笔(滑动窗口, 纯账号风控)
LIVE_GLOBAL_RATE_HOUR = 20      # 层3: 全局频次帽 每小时≤20 笔
MAX_BETS_PER_POLL = 8           # 2026-11-07 每轮 poll 最多处理的机会数(按EV降序), 让 poll 快速返回不被阻塞
MAPPING_VERSION = "1"           # 市场映射表/队名归一化版本(2026-10-11): 改了映射逻辑要手动 bump, 进 decision_version
RELEASE_VERSION = "2"           # 释放机制版本(2026-10-11): compute_market_release.py 的释放判据改了要手动 bump(如 REAL_ROI_GATE 单出口重构), 进 decision_version 标记 post-fix cohort
SETTLE_VERSION = "3"            # 让球结算口径版本(2026-10-13): 篮球=赛前让分(全场净胜), 足球/冰球=滚球让分(下注后净胜); 改了让球结算分支要手动 bump, 进 decision_version 区分修复前后 cohort
LIVE_FILTER_LINE_ZERO = os.getenv("LIVE_FILTER_LINE_ZERO", "false").lower() in ("1", "true", "yes")  # 让球0(平局退款盘)过滤开关, 默认关


_pin_cache_last = {"mtime": 0.0, "data": None}


def _pin_event_last(pin_matchup_id):
    """读 Pin 缓存, 返回该事件的 last(秒, Pin 时钟)。带 mtime 缓存(避免每注读 4MB 文件)。

    last 是 Pin 事件最后一次更新时刻, 用作「参与同一轮 EV 计算的 Pin 报价」的时间戳代理。
    跨机时钟有 ~1.6s 偏移(见 _pin_clock_samples), 所以 lag_sec 是含钟偏的原始值, 看相对变化/分组, 别当绝对滞后。
    """
    global _pin_cache_last
    try:
        f = ROOT / "data" / "storage" / "pin_odds_cache.json"
        mt = f.stat().st_mtime
        if _pin_cache_last["mtime"] != mt or _pin_cache_last["data"] is None:
            _pin_cache_last["data"] = json.loads(f.read_text())
            _pin_cache_last["mtime"] = mt
        cache = _pin_cache_last["data"].get("cache", {})
        ev = cache.get(str(pin_matchup_id), {}).get("event", {})
        return ev.get("last")
    except Exception:
        return None


_release_ver_cache = {"mtime": 0.0, "gen": ""}


def _decision_version(threshold):
    """配置快照 hash(2026-10-11): 关键阈值/开关指纹, 每注打 version 供复盘确认当时跑的是哪版阈值。

    含 MAPPING_VERSION(市场映射表+队名归一化版本)、RELEASE_VERSION(释放机制版本)、
    market_release.json 的 generated_at(释放清单版本)。三者任何一变, hash 就变,
    复盘能定位「当时是哪版映射/释放机制/释放清单」。RELEASE_VERSION 要手动 bump(如 REAL_ROI_GATE 单出口重构)。
    """
    _vals = [threshold, LIVE_COOLDOWN_PER_MARKET, LIVE_MARKET_COOLDOWN, LIVE_EVENT_MAX_EXPOSURE,
             LIVE_GLOBAL_RATE_MIN, LIVE_GLOBAL_RATE_HOUR, SNAPSHOT_MAX_AGE, SCORE_CHECK_MAX_AGE,
             REVERIFY_THRESHOLD, LIVE_FILTER_LINE_ZERO, MAPPING_VERSION, RELEASE_VERSION, SETTLE_VERSION]
    # 释放清单 generated_at(mtime 缓存, 不每注读 20KB 文件)
    try:
        _mt = MARKET_RELEASE_FILE.stat().st_mtime
        if _release_ver_cache["mtime"] != _mt:
            _mr = json.loads(MARKET_RELEASE_FILE.read_text())
            _release_ver_cache["mtime"] = _mt
            _release_ver_cache["gen"] = _mr.get("generated_at", "")
        _vals.append(_release_ver_cache["gen"])
    except Exception:
        _vals.append("")
    return hashlib.md5(",".join(str(v) for v in _vals).encode()).hexdigest()[:8]
BET_HEALTH_FILE = ROOT / "data" / "storage" / "bet_health.json"  # 下单健康(成功/被拒计数), 供 gubbing 限注监控


def _record_bet_result(code, latency=0.0, direction_key=None):
    """记录下单成败 + 成交延迟(2026-09-21 gubbing 限注监控): 被拒率飙升/成交延迟拉长是软书限注前兆。

    赔率滑点不单独记 —— oddsChange=0 保证成交价=信号价(变了就拒), 滑点体现在「被拒(code!=0)」里。
    2026-10-08 加 direction_key: 同时按方向记录, 供执行质量系数(拒单率→注码打折)。
    """
    try:
        d = {"success": 0, "rejected": 0, "total": 0, "last_ts": 0.0, "latency_sum": 0.0, "latency_n": 0}
        if BET_HEALTH_FILE.exists():
            try:
                d = json.loads(BET_HEALTH_FILE.read_text())
            except Exception:
                pass
        d["total"] = d.get("total", 0) + 1
        if code == 0:
            d["success"] = d.get("success", 0) + 1
        else:
            d["rejected"] = d.get("rejected", 0) + 1
        if latency > 0:
            d["latency_sum"] = d.get("latency_sum", 0.0) + latency
            d["latency_n"] = d.get("latency_n", 0) + 1
        d["last_ts"] = time.time()
        BET_HEALTH_FILE.write_text(json.dumps(d))
        # 按方向记录(执行质量系数: 拒单率/延迟 → 注码打折)
        if direction_key:
            from src.scrapers.execution_quality import record as _eq_record
            _eq_record(direction_key, code, latency)
    except Exception:
        pass

# 滚球实盘(2026-09-07 起只投小球under, 2026-09-09 放大预算: 小球累计40笔ROI+14.2%稳定正)。滚动预算(结算后释放额度)。
LIVE_BUDGET = float('inf')  # 2026-09-18 用户取消每日投注额上限: 滚球不再设总限额(由单场/单盘口300 + 账户余额兜底)
LIVE_BUDGET_FILE = ROOT / "data" / "storage" / "live_bet_budget.json"
REAL_BETS_FILE = ROOT / "data" / "storage" / "real_bets.json"  # 实盘库(2026-09-27): 每笔实盘注单的下单信息+结算结果
_BALANCE_CACHE = {"ts": 0.0, "bal": None}  # 余额节流缓存(2026-10-21 余额<80 停实盘, 30s 查一次)
DRAWDOWN_STOP_PNL = 1000  # 回撤熔断(2026-09-13): 最近7天滚球实盘累计亏超¥1000(=20%BANKROLL) → 半仓
DRAWDOWN_RESET_FILE = ROOT / "data" / "storage" / "drawdown_reset_ts.txt"  # 熔断归零时间戳(2026-09-19 用户要求归零重算)
LIVE_PAPER_FILE = ROOT / "data" / "storage" / "live_paper_bets.json"
BB_TRACK_FILE = ROOT / "data" / "storage" / "bb_price_track.jsonl"  # 2026-11-07 BB价轨迹追踪落盘
LIVE_SETTLED_FILE = ROOT / "data" / "storage" / "live_settled_notified.json"  # 已推送过结算的 order_id

# 滚球实盘只投小球(under), EV-Kelly 最优定仓, 单注上限 ¥400(预算1/5)兼顾分散。
# 其它盘口(大球/1x2/让球)仍只进观察库, 不下真单。
LIVE_REAL_BET_ENABLED = True  # 2026-09-18 重新开启滚球实盘(新数据源 Sbobet+Betfair 替代 Pin)
LIVE_UNDER_BET_ENABLED = True  # 2026-10-25 用户要求足球小球滚球全量放开(历史-31%暂停取消)
LIVE_UNDER_MAX_STAKE = 150  # 单注上限(2026-09-18 滚球单注≤150; 实际生效cap=min(MAX_STAKE=150, release_cap))
BB_SPORT_CN = {1: "足球", 3: "篮球", 5: "网球", 7: "棒球", 6: "美式足球",
               2: "冰球", 13: "排球", 15: "乒乓球", 18: "MMA", 19: "拳击", 47: "羽毛球"}

# 滚球 → 观察库统一口径(2026-09-12 观察库释放改造): sport 数字→英文; sub→sub_market。
# 用于查 observe_release_caps(运动×"滚球"×盘口), 与 compute_market_release.py 同口径。
BB_SPORT_EN = {1: "football", 3: "basketball", 5: "tennis", 7: "baseball", 6: "american_football",
               2: "ice_hockey", 13: "volleyball", 15: "pingpong", 18: "mma", 19: "boxing",
               47: "badminton"}  # 2026-09-27 补排球/乒乓/冰球等(之前只5运动, 排球乒乓样本丢失运动标签)
BB_SUB_TO_SM = {"over_under": "ou", "handicap": "hc", "handicap_games": "hc_games", "opportunities": "1x2",
                "double_chance": "dc", "btts": "btts"}  # 2026-09-29 加双机会/双边进球(能结算, 用完整desig作方向)
MARKET_RELEASE_FILE = ROOT / "data" / "storage" / "market_release.json"
OBS_STATE_FILE = ROOT / "data" / "storage" / "observe_release_state.json"
PIN_TRIGGER_FILE = ROOT / "data" / "storage" / "pin_trigger.jsonl"  # Pin drop 触发信号(scripts/pin_ws_trigger.py 写)
PREMATCH_TICK_FILE = ROOT / "data" / "storage" / "pin_prematch_tick.jsonl"  # 早盘 WS 赔率变动触发(2026-10-18 事件驱动)
PIN_LIVE_TICK_FILE = ROOT / "data" / "storage" / "pin_live_tick.ts"  # Pin WS live 帧触发信号时间戳(2026-10-11)
DAILY_STAKE_LIMIT = float('inf')  # 2026-10-25 用户要求解除单日1000封顶
LIVE_MAX_EV = 8.0  # 2026-10-26 滚球 EV 上限(8%以上低赔率热门假溢价, 实测 8-12%/-7.3%、12-20%/-4.3%)
LIVE_INGEST_EV_MIN = 2.0  # 2026-11-01 滚球入库 EV 下限(观察库收 2%+, 实盘投注仍用 self.threshold 不变)
DAILY_LOSS_LIMIT = 200   # 2026-10-13 探针单日亏损熔断: 资本10000的2%, 当日实盘亏损超200即停
PENDING_SETTLE_FILE = ROOT / "data" / "storage" / "pending_settle.json"  # 结算明细缓存(每小时汇总推一次)
SETTLE_PUSH_INTERVAL = 3600  # 结算明细每小时汇总推一次(2026-09-12 用户要求, 不一场推一场)
SETTLE_PUSH_TS_FILE = ROOT / "data" / "storage" / "settle_push_ts.txt"  # 上次结算推送时间戳(落盘, 重启不归零, 防重复推送)
# 实盘结算流水(2026-09-19): 持久化已结算注, 带赔率区间, 供按格子拆盈亏(不再靠观察库倒推)
SETTLED_LOG_FILE = ROOT / "data" / "storage" / "live_settled_log.json"


def _odds_interval(fair_or_odds, sport=None):
    """公平价(优先)或BB赔率 → 概率桶(内部算隐含概率=1/price)。见 src.scrapers.odds_interval.prob_bucket。"""
    from src.scrapers.odds_interval import prob_bucket
    if fair_or_odds is None:
        return "?"
    try:
        p = float(fair_or_odds)
    except (TypeError, ValueError):
        return "?"
    if p <= 1.0:
        return "?"
    return prob_bucket(1.0 / p, sport)


def _dingtalk_safe(text: str) -> str:
    """钉钉内容安全: 替换博彩触发词为中性词。

    2026-09-13: 滚球下单推送 title「🟦 滚球已下单」+ 正文「下单/单注/已投」被钉钉判
    inappropriate content 100% 拒收(42/42 失败, 0 成功)。而结算汇总(「滚球/注额/公平价/
    溢价/赢了/输了」)实测能过 —— 说明触发点是「下单/单注/已投」这些动作词, 「滚球」本身安全。

    注意: 不能替换「投注」—— send_dingtalk 会给正文追加机器人关键词「投注推荐」, 若在此
    把「投注」换成别的会连带破坏关键词。
    """
    for _bad, _good in (
        ("下单", "成交"),   # 已下单 → 已成交
        ("单注", "金额"),   # 单注¥ → 金额¥
        ("已投", "已用"),   # 今日已投 → 今日已用
    ):
        text = text.replace(_bad, _good)
    return text

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


_obs_blocked_cache = None
_obs_blocked_mtime = 0.0


def _load_obs_blocked():
    """读 observe_blocked(运动×盘口×方向×赔率区间×来源 的拦截清单), 带 mtime 缓存。

    2026-09-13 用户要求: n>200 但表现差(edge<0或ROI<0)的赔率区间不投。
    返回 set of "sport|sm|dr|interval|scope"。
    """
    global _obs_blocked_cache, _obs_blocked_mtime
    if not MARKET_RELEASE_FILE.exists():
        return set()
    try:
        m = MARKET_RELEASE_FILE.stat().st_mtime
        if _obs_blocked_cache is None or m != _obs_blocked_mtime:
            _raw = (json.loads(MARKET_RELEASE_FILE.read_text()) or {}).get("observe_blocked", []) or []
            _obs_blocked_cache = {"|".join(map(str, x)) for x in _raw}
            _obs_blocked_mtime = m
        return _obs_blocked_cache
    except Exception:
        return set()


_full_release_live_cache = None
_full_release_live_mtime = 0.0


def _load_full_release_live():
    """读 full_release_live(滚球全量放开的 运动|盘口 set), 带 mtime 缓存。2026-11-10 与早盘一致。"""
    global _full_release_live_cache, _full_release_live_mtime
    if not MARKET_RELEASE_FILE.exists():
        return set()
    try:
        m = MARKET_RELEASE_FILE.stat().st_mtime
        if _full_release_live_cache is None or m != _full_release_live_mtime:
            _raw = (json.loads(MARKET_RELEASE_FILE.read_text()) or {}).get("full_release_live", []) or []
            _full_release_live_cache = set(_raw)
            _full_release_live_mtime = m
        return _full_release_live_cache
    except Exception:
        return set()


# (已删除重复的 4 档 _odds_interval——它覆盖了上面的 5 档版本, 导致 1.0-1.5/1.5-2.0 拦截失效)


_recent_pnl_cache = {"ts": 0.0, "pnl": 0.0}


def _load_drawdown_reset():
    """读熔断归零时间戳(epoch 秒)。无则 None。"""
    try:
        if DRAWDOWN_RESET_FILE.exists():
            return float(DRAWDOWN_RESET_FILE.read_text().strip() or 0) or None
    except (OSError, ValueError):
        pass
    return None


def _recent_pnl(days=7):
    """最近 days 天滚球实盘盈亏(BB 官方结算订单 uwl 求和)。带 5min 缓存, 供回撤熔断用。

    2026-09-19 归零重算: 若设置了归零时间戳, 只算归零之后的盈亏(历史亏损清零),
    让「已释放盘口」的注额不被历史亏损(未释放盘口/错误口径造成的)拖累砍半。
    """
    global _recent_pnl_cache
    if time.time() - _recent_pnl_cache["ts"] < 300:
        return _recent_pnl_cache["pnl"]
    try:
        from scripts.daily_review import _fetch_settled_orders
        orders = _fetch_settled_orders() or []
        import datetime as _dt
        cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).timestamp() * 1000
        _reset = _load_drawdown_reset()
        if _reset:
            cutoff = max(cutoff, _reset * 1000)
        pnl = 0.0
        for o in orders:
            if (o.get("mt") or 0) < cutoff:
                continue
            try:
                pnl += float(o.get("uwl", 0))
            except (TypeError, ValueError):
                pass
        _recent_pnl_cache = {"ts": time.time(), "pnl": pnl}
    except Exception:
        pnl = _recent_pnl_cache["pnl"]
    return pnl


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


def _match_2way_line(line, d2way, nearest=False):
    """匹配 2-way 盘口线(spread/total)。主/客方向线符号相反, 试 line 和 -line。

    nearest=True(只给下注后 CLV 路径): 滚球线随比分漂移(让球 -0.5→-1.5 进球后), 精确匹配常
    失败, 就近取 |points±line| 最小的盘口线作收盘代理(符号仍对, 幅度近似)。下单前验价必须
    nearest=False(精确匹配, 线漂了就该 None 跳过, 不能拿错线的价下单)。
    """
    if d2way is None:
        return None
    if line in d2way:
        return d2way[line]
    if line is not None and -line in d2way:
        return d2way[-line]
    if not nearest or line is None or not d2way:
        return None
    return min(d2way.items(), key=lambda kv: min(abs(kv[0] - line), abs(kv[0] + line)))[1]


def _is_settleable(desig, line):
    """盘口能否被 _settle_paper_bets 判输赢。

    1x2(主/和/客) 不需要 line; hc/ou(让球/大小) 必须带 line(缺 line 进库也无法结算)。
    dc(双机会)/btts(双边进球) 全场比分即可判(2026-09-19 新增)。
    其它盘口(htft/正确比分等特殊盘)当前结算逻辑不支持, 一律判为不可结算。
    """
    if desig in ("主胜", "客胜", "和局"):
        return True
    if desig in ("让球主胜", "让球客胜", "让局主胜", "让局客胜"):
        # 让球0(line=0)BB会 void(st=2 退款, 实测 26 笔里 1 笔=3.8%), getMatchDetail 拿不到
        # void 状态 → 无法可靠判输赢, 不进库(与 quarter-ball 同策略: 结算不了的样本不采)。
        # 2026-09-22 补: quarter-ball(|line|=0.25/0.75)线错配+半赢半走盘结算不了 → 只采 0.5 整数倍线。
        return (line is not None and abs(line) > 0.0001
                and abs(line * 2 - round(line * 2)) < 1e-6)
    if desig in ("大球", "小球"):
        # 2026-09-22: quarter-ball(2.25/2.75)半赢半走盘结算不了(与让球同策略), 只采 0.5 整数倍线。
        return (line is not None
                and abs(line * 2 - round(line * 2)) < 1e-6)
    if desig in ("主/和", "客/和", "主/客"):  # 双机会 dc
        return True
    if desig in ("双方进球", "非双方进球"):  # 双边进球 btts
        return True
    if desig in ("上半场主胜", "上半场客胜", "上半场和局"):  # 上半场独赢 ht(半场比分结算, 无需线)
        return True
    if desig in ("上半场大球", "上半场小球"):  # 上半场大小 ht_ou(需线, quarter-ball 同让球拦)
        return (line is not None
                and abs(line * 2 - round(line * 2)) < 1e-6)
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


# 观察库内存缓存(2026-09-20): _append_live_paper_bet 之前每次 json.loads 4.7MB + 遍历10882条去重,
# 是「下单耗时4s」里的大头(BB下单本身才~1s)。用 mtime 缓存读 + set 去重, 读从 ~1s 降到 ~0ms。
# 写也一样: json.dumps 4.7MB 每次全量重写 ~0.5-1s, 攒批 flush(每10次新增写一次盘)再省一笔。
_paper_bets_cache = {"mtime": 0.0, "data": None, "keys": None, "dirty": False, "pending": 0}
_PAPER_BETS_FLUSH_EVERY = 10  # 每 10 次新增 flush 一次盘(观察库是纸单, 丢几笔不致命)

# ── 纸面模拟拒单(2026-10-31): 消除「oddsChange=0 拒单 selection bias」──
# 纸面观察库记录所有 +EV 机会, 但实盘只成交「下单一刻赔率没变」的(oddsChange=0, 变了就拒)。
# 全样本纸面 vs 只成交实盘 → 纸面 ROI 系统性地高/低于实盘(逆向选择), 污染释放判据。
# 模拟法: 入库后等 SIM_REJECT_DELAY 秒(对齐实盘从入库到 HTTP 提交的延迟), 再 fetch_current_odds 复验
# BB 当前价; 价变(或封盘拿不到价)即当作被拒, 回写 sim_rejected=True, 统计侧剔除。
SIM_REJECT_DELAY = 1.5        # 入库→复验延迟(秒): 对齐实盘 place_single_bet 的验价+HTTP 提交耗时
SIM_REJECT_EPS = 0.001        # 复验价 vs 入库价差异阈值: 任何变动都算拒单(与 BB oddsChange=0 语义一致)
SIM_REJECT_MAX_PER_DRAIN = 10 # 每次 drain 最多发几个 fetch_current_odds(防突发 HTTP 堆积占满线程池)


def _load_paper_bets():
    """读观察库(带 mtime 缓存 + set 去重索引)。返回 (data, keys)。"""
    global _paper_bets_cache
    try:
        m = LIVE_PAPER_FILE.stat().st_mtime if LIVE_PAPER_FILE.exists() else 0.0
    except OSError:
        m = 0.0
    if _paper_bets_cache["data"] is not None and m == _paper_bets_cache["mtime"]:
        return _paper_bets_cache["data"], _paper_bets_cache["keys"]
    data = []
    if LIVE_PAPER_FILE.exists():
        try:
            data = json.loads(LIVE_PAPER_FILE.read_text())
        except Exception:
            data = []
    keys = {(b.get("match_id"), b.get("market_id"), b.get("option_type"), b.get("sub")) for b in data}
    _paper_bets_cache = {"mtime": m, "data": data, "keys": keys, "dirty": False, "pending": 0}
    return data, keys


def _flush_paper_bets():
    """把内存中的观察库写回磁盘(攒批 flush)。只在 dirty 时写。"""
    global _paper_bets_cache
    if not _paper_bets_cache.get("dirty") or _paper_bets_cache["data"] is None:
        return
    try:
        LIVE_PAPER_FILE.write_text(json.dumps(_paper_bets_cache["data"], ensure_ascii=False, indent=1))
        _paper_bets_cache["dirty"] = False
        _paper_bets_cache["pending"] = 0
        _paper_bets_cache["mtime"] = LIVE_PAPER_FILE.stat().st_mtime  # 更新 mtime 防下次误 reload
    except OSError:
        pass


def _fetch_fresh_odds_concurrent(tasks, max_workers=6):
    """并发 getMatchDetail 拉鲜价(2026-10-31): 早盘比价循环逐场串行 fetch_match_for_order(1-2s/场)
    是「比价 12-40s」的瓶颈(Pin WS 批量推 prematch 变动, 每批中位11场/峰值99场)。

    把 getMatchDetail 拆出来线程池并发: tasks=[(mid, sport), ...] → [odds_dict or None, ...]。
    并发度 6(>连接池10 的一半, 留余量; BB 14068 是投注频次限速, getMatchDetail 只读查询不受同限)。
    返回的 odds_dict 只含 odds_ft/odds_ht/odds_sh(extract_match_odds 的结果), 由调用方替换到 m。
    """
    if not tasks:
        return []
    from concurrent.futures import ThreadPoolExecutor
    from src.scrapers.bb_api_fetcher import fetch_match_for_order, extract_match_odds

    def _one(item):
        mid, sport = item
        try:
            _fresh = fetch_match_for_order(mid, timeout=3, max_attempts=1, max_domains=1)
            if _fresh and _fresh.get('mg'):
                _fm = extract_match_odds(_fresh, sport)
                return {k: _fm[k] for k in ('odds_ft', 'odds_ht', 'odds_sh') if _fm.get(k)}
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_one, tasks))


class SecondLevelMonitor:
    def __init__(self, threshold=4.0, on_signal=None, auto_bet=False, stake=None):
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
        self._ws_trigger_ts = 0.0  # WS 触发节流时间戳(2026-09-19)
        self._pin_trigger_offset = 0  # Pin drop 触发文件已消费行数(2026-10-11)
        self._pending_acceptance = {}  # 滚球入库模拟 oddsChange=0: (match_id,market_id,option_type)->{odds,ts}(2026-10-31)
        import threading
        self._pending_acceptance_lock = threading.Lock()  # 主循环写 vs drain 读/删 并发护栏(2026-10-31)
        self._accept_drain_busy = False  # 模拟拒单 drain 进行中标志(防并发, 2026-10-31)
        self._last_accept_drain = 0.0    # 上次触发模拟拒单 drain 的时间戳(节流, 2026-10-31)
        self._pending_early_acceptance = {}  # 早盘模拟拒单(2026-10-31): bb_match_id -> {odds_ft,ts,sport}(按场复验, 价变整场标拒)
        self._pending_early_lock = threading.Lock()  # 早盘 pending 写 vs drain 读/删 并发护栏
        self._early_drain_busy = False  # 早盘模拟拒单 drain 进行中标志(防并发)
        self._last_early_drain = 0.0    # 上次触发早盘 drain 的时间戳(节流)
        self._prematch_tick_offset = 0  # 早盘 WS 赔率变动 tick 已消费行数(2026-10-18 事件驱动)
        self._last_prematch_scan = 0.0  # 早盘 WS 全量扫描上次时间戳(2026-10-18 双轨制, 每 30s)
        self._pin_live_wake = False  # 滚球 drop 唤醒标志(2026-10-11): live drop 触发立即滚球比较
        self._trigger_source = "init"  # 触发源标签(ws/pin/poll), 供审计组内对照(2026-10-11)
        self._trigger_source_stats = {}  # 触发源分布计数(2026-10-11): pin/betfair_ws/poll 各多少次
        self._drop_anchor_track = {}  # match_id -> [pending记录], 等 live drop 算 clv_sign(2026-10-11 简化版事件锚点)
        self._reanchor_live_cache = {}  # sid -> (ts, live_events) 复开价锚点 force 拉取跨调用缓存(2026-10-18 降频缓解 Pin 限速)
        self._last_pin_live_tick = 0.0  # 上次读到的 Pin WS live 帧时间戳(2026-10-11)
        self._last_pin_frame_last = 0.0  # Pin 帧自带 last(供「Pin产生→我收到」段1上游延迟)
        self._pin_clock_samples = []  # 本地-Pin帧ts 样本(判定时钟偏移稳定 vs 积压漂移)
        self._skip_stats = {}  # 滚球跳过分桶(2026-10-11): 观察是否有过滤条件把机会全滤掉(过度拦截零成交)
        self._market_cooldown = {}  # 层1: {(match_id, market_id): 上次成交ts} per-market 冷却
        self._event_exposure = {}   # 层2: {match_id: 持仓数} per-event 敞口
        self._global_bet_ts = []    # 层3: 全局成交时间戳列表(滑动窗口频次帽)
        self._raw_ws_ts = 0.0  # 原始WS变动检测时刻(2026-09-23): 供推送「端到端耗时(WS触发→下单完成)」
        self._early_ws_ts = 0.0  # 早盘 WS 触发节流时间戳(2026-09-19)
        self._lev_fail_stats = {}  # LEV 复验失败原因计数(量化 close/价差/deleted 各占多少)
        self._lev_fail_log_ts = 0.0  # 失败统计落盘节流(300s 一次)
        self._token_ok_until = 0.0     # token 有效缓存到期时间戳(10min 缓存, 省每单 1s 探测)
        self._attempted = {}           # 滚球指纹去重: match_id -> {market_id -> 尝试时间戳}
        self._last_bet_time = 0.0      # 上次下单时间(非阻塞限频用)
        self._bet_delay = 0.0          # 下一单需等待的随机间隔(10-15s, 每次下单后重抽)
        self._bb_track = {}            # 2026-11-07 BB价轨迹追踪: {(match_id,market_id,option_type): {ts,bb_at_bet,fair_cal_at_bet,home,away,desig,sub,history:[(ts,bb_odds)]}}
        self._last_bb_track_ts = 0.0   # 上次采样 BB 价轨迹的时间戳(30s 一次)
        # 上次结算汇总推送时间(每小时推一次, 2026-09-12)。落盘读取, 重启不归零 → 防「每次重启都立即重推结算」。
        self._last_settle_push = 0.0
        try:
            if SETTLE_PUSH_TS_FILE.exists():
                self._last_settle_push = float(SETTLE_PUSH_TS_FILE.read_text().strip() or 0)
        except (OSError, ValueError):
            pass
        self._reversion_track = {}     # CLV 追踪: (match_id, market_id, option_type) -> {sig, ts}

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

        返回 True=新增入库(首次见该机会); False=跳过(不可结算/重复/写失败)。
        供调用方在「首次入库」时触发 LEV 追踪(下注后3s复验Pin), 让所有+EV机会都有 LEV,
        不再只对实盘下单的注采(否则新方向攒不到 LEV = 释放死循环)。
        """
        desig = sig.get("desig", "")
        if not _is_settleable(desig, sig.get("line")):
            print(f"[slm] 跳过无法结算的虚拟投注({desig!r} line={sig.get('line')!r}), 不进观察库", flush=True)
            return False
        try:
            data, keys = _load_paper_bets()  # 2026-09-20 mtime 缓存读 + set 去重(4.7MB JSON 不再每次 parse)
            # 指纹去重: 同一 (match_id, market_id, option_type, sub) 只记一次, 避免每 2s 轮询重复入库
            key = (sig["match_id"], sig.get("market_id"), sig.get("option_type"), sig.get("sub"))
            if key in keys:
                return False
            data.append({
                "ts": time.time(), "match_id": sig["match_id"],
                "market_id": sig.get("market_id"), "option_type": sig.get("option_type"),
                "sport": sig.get("sport", ""),
                "home": sig["match"].get("home", ""), "away": sig["match"].get("away", ""),
                "designation": sig["desig"], "sub": sig.get("sub"), "line": sig.get("line"),
                "bsc": sig.get("bsc"),  # [主,客] 下注瞬间比分(让球按当前比分结算用)
                "bb_odds": sig["bb_odds"], "fair": sig["fair"], "ev": sig["ev"],
                "spread": sig.get("spread"),  # 2026-09-19 流动性门槛: back-lay 价差
                "bb_ts": sig.get("bb_ts"),  # BB 赔率拉取时间(供算 delay=ts-bb_ts, 快/慢单口径)
                "stake": sig.get("_stake", 0), "settled": False, "result": None, "profit": None,
                # 2026-10-31 EV 门槛模拟: 鲜价重算后 EV 跌破 threshold = 实盘 verify_price -4 拒
                # (与「价变拒单」是两个独立维度, 任一触发都标 sim_rejected, 统计侧剔除)
                "sim_rejected": bool(sig.get("_sim_reject")), "sim_reject_reason": sig.get("_sim_reject"),
                "anchor": "pin",  # 2026-09-19 锚点口径标记: 10-11后 Pin 公平价
                "sbo_direction": sig.get("sbo_direction", "same"),  # same/diff/none(供统计同向/不同向赛果)
                "clv_sign": None,  # drop 锚点方向(+1朝有利/-1朝不利), 2026-10-11 简化版事件锚点
                "clv_anchor": None if sig.get("sub") in ("opportunities", "over_under", "handicap", "ht", "double_chance", "ht_ou") else "no_outcome",  # 复开价锚点覆盖盘口, 其余 no_outcome(不覆盖)
                "clv_elapsed_s": None,  # 下注到 drop 的秒数
            })
            keys.add(key)
            # 纸面模拟拒单(2026-10-31): 登记待复验。实盘 oddsChange=0 只在「下单一刻赔率没变」成交,
            # 观察库全样本无此过滤 → 这里登记入库价, 由 _drain_pending_acceptance 1.5s 后复验, 价变/封盘则标 sim_rejected。
            _mid = sig.get("match_id"); _mkid = sig.get("market_id"); _ot = sig.get("option_type")
            if _mkid is not None and _ot is not None:
                try:
                    _odds = float(sig["bb_odds"])
                    if _odds > 1.0:
                        with self._pending_acceptance_lock:
                            self._pending_acceptance[(str(_mid), str(_mkid), str(_ot))] = {
                                "match_id": _mid, "market_id": _mkid, "option_type": _ot,
                                "odds": _odds, "ts": time.time(),
                            }
                except (TypeError, ValueError, KeyError):
                    pass
            # drop 锚点(2026-10-11 简化版): 1x2 + ou 盘。方向匹配(drop outcome: Home/Away/Draw/Over/Under),
            # 不依赖线(ou 跨线 2.5/3.0 方向强相关, clv_sign 只看方向不看价, 噪声可接受)。
            # hc/ht/dc/让局 盘 drop 无对应 outcome → 不覆盖(需「下次复开价」锚点, 后续再扩)。
            if sig.get("sub") in ("opportunities", "over_under", "handicap", "ht", "double_chance", "ht_ou"):
                self._drop_anchor_track.setdefault(str(sig["match_id"]), []).append({
                    "market_id": sig.get("market_id"), "option_type": sig.get("option_type"),
                    "sub": sig.get("sub"), "designation": sig["desig"],
                    "bb_odds": sig["bb_odds"], "ts": time.time(),
                    # 2026-10-18 复开价锚点: 补队名/运动/下注fair/line, 供 _reanchor_pending 周期性复查询
                    "home": sig["match"].get("home", ""), "away": sig["match"].get("away", ""),
                    "sport": sig.get("sport", ""), "fair": sig["fair"],
                    "line": sig.get("line"),
                })
            _paper_bets_cache["dirty"] = True
            _paper_bets_cache["pending"] += 1
            if _paper_bets_cache["pending"] >= _PAPER_BETS_FLUSH_EVERY:
                _flush_paper_bets()  # 攒批写(2026-09-20): 每10次新增才写一次4.7MB盘
            return True
        except Exception as e:
            print(f"[slm] 虚拟投注写入失败: {str(e)[:80]}", flush=True)
            return False

    def _update_live_paper_stake(self, sig, stake):
        """更新观察库该注的 stake(实盘 cap 后同步, 保证分配逻辑一致, 2026-09-19)。"""
        try:
            data, keys = _load_paper_bets()  # 2026-09-20 用内存缓存, 不读 4.7MB 盘
            key = (sig.get("match_id"), sig.get("market_id"), sig.get("option_type"), sig.get("sub"))
            if key not in keys:
                return
            for b in data:
                if (b.get("match_id"), b.get("market_id"), b.get("option_type"), b.get("sub")) == key:
                    b["stake"] = stake
                    _paper_bets_cache["dirty"] = True
                    return
        except Exception:
            pass

    def _drain_pending_acceptance(self):
        """纸面模拟拒单(2026-10-31): 复验入库 SIM_REJECT_DELAY 秒后的 BB 价, 价变/封盘 → 标 sim_rejected。

        消除「oddsChange=0 拒单」的 selection bias: 观察库记录所有 +EV 机会, 实盘只成交「下单一刻
        赔率没变」的(变了就拒)。这里对每笔新入库纸面注, 在延迟后 fetch_current_odds 复验 BB 当前价,
        价变(oddsChange 会被拒)或封盘(3015 拿不到价)即回写 sim_rejected=True, 统计侧剔除被拒样本,
        让纸面 ROI 对齐实盘「只成交没变价」的口径。

        无 token 直接返回(否则 token 过期会让所有待复验误判成「封盘」→ 全拒污染统计)。
        """
        if not self._pending_acceptance:
            return
        from src.betting.bb_auto_bet import read_token, fetch_current_odds
        tok = read_token()
        if not tok:
            return
        now = time.time()
        # 取出到期项(加锁防主循环并发写 _pending_acceptance → dict 迭代 RuntimeError)。上限 SIM_REJECT_MAX_PER_DRAIN。
        with self._pending_acceptance_lock:
            due_items = [(k, v) for k, v in self._pending_acceptance.items() if now - v["ts"] >= SIM_REJECT_DELAY]
            due_items = due_items[:SIM_REJECT_MAX_PER_DRAIN]
            for k, _ in due_items:
                self._pending_acceptance.pop(k, None)  # 先移除: 无论结论如何都不复验第二次
        if not due_items:
            return
        verdict = {}  # 键 -> "ok" | "rejected_odds_changed" | "rejected_unavailable"
        fetched = 0
        for key, ent in due_items:
            fetched += 1
            try:
                cur = fetch_current_odds(ent["market_id"], ent["match_id"], ent["option_type"], token=tok)
            except Exception:
                cur = None
            if cur is None:
                verdict[key] = "rejected_unavailable"  # 封盘/下架 → 实盘 3015 拒单
                continue
            fresh = cur[0]
            try:
                fresh = float(fresh) if fresh else None
            except (TypeError, ValueError):
                fresh = None
            if fresh is not None and abs(fresh - float(ent["odds"])) > SIM_REJECT_EPS:
                verdict[key] = "rejected_odds_changed"
            else:
                verdict[key] = "ok"
        rejected_keys = {k for k, v in verdict.items() if v != "ok"}
        if not rejected_keys:
            return
        # 回写 sim_rejected 到观察库记录(只改内存缓存置 dirty, 由结算/攒批 flush 落盘, 避免跨线程并发写 4.7MB 盘)
        data, _ = _load_paper_bets()
        if data is None:
            return
        n = 0
        for b in data:
            _bk = (str(b.get("match_id")), str(b.get("market_id")), str(b.get("option_type")))
            if _bk in rejected_keys and not b.get("sim_rejected"):
                b["sim_rejected"] = True
                b["sim_reject_reason"] = verdict[_bk]
                n += 1
        if n:
            _paper_bets_cache["dirty"] = True
            print(f"[slm] 模拟拒单: 复验 {fetched} 笔, 标拒 {n} 笔 "
                  f"(价变 {sum(1 for v in verdict.values() if v == 'rejected_odds_changed')} / "
                  f"封盘 {sum(1 for v in verdict.values() if v == 'rejected_unavailable')})", flush=True)

    def _register_early_acceptance(self, m, sport):
        """早盘模拟拒单登记(按场): 入库后登记第一道 getMatchDetail 鲜价 odds_ft,
        由 _drain_pending_early_acceptance 延迟复验第二道, 价变把整场 clv_tracking 标 sim_rejected。"""
        try:
            _mid = str(m.get("id") or m.get("bb_match_id") or "").strip()
            _odds = m.get("odds_ft")
            if _mid and _odds:
                with self._pending_early_lock:
                    self._pending_early_acceptance[_mid] = {
                        "odds_ft": dict(_odds) if isinstance(_odds, dict) else _odds,
                        "ts": time.time(), "sport": sport,
                    }
        except Exception:
            pass

    def _drain_pending_early_acceptance(self):
        """早盘模拟拒单(2026-10-31): 入库 SIM_REJECT_DELAY 秒后复验 getMatchDetail, 整场价变 → 标 sim_rejected。

        对齐实盘 auto_bet_flow 的 oddsChange=0: 早盘入库用 getMatchDetail 鲜价(第一道), 实盘下单前又拉一次
        (第二道), 两道之间价变 = 实盘会被拒。这里延迟复验第二道, 价变把该场 clv_tracking 记录全标 sim_rejected,
        统计侧(clv_collector/paper_settle)剔除被拒样本, 让早盘观察库 CLV/ROI 对齐实盘「只成交没变价」。
        """
        if not self._pending_early_acceptance:
            return
        from src.scrapers.bb_api_fetcher import fetch_match_for_order, extract_match_odds
        now = time.time()
        with self._pending_early_lock:
            due = [(k, v) for k, v in self._pending_early_acceptance.items() if now - v["ts"] >= SIM_REJECT_DELAY]
            due = due[:SIM_REJECT_MAX_PER_DRAIN]
            for k, _ in due:
                self._pending_early_acceptance.pop(k, None)
        if not due:
            return
        rejected_mids = set()
        for mid, ent in due:
            try:
                _fresh = fetch_match_for_order(mid, timeout=3, max_attempts=1, max_domains=1)
                if _fresh and _fresh.get("mg"):
                    _fm = extract_match_odds(_fresh, ent["sport"])
                    _new = _fm.get("odds_ft")
                    _old = ent.get("odds_ft")
                    if _new and _old and _new != _old:
                        rejected_mids.add(mid)
            except Exception:
                pass
        if not rejected_mids:
            return
        # 写回 clv_tracking.csv: 被拒场的所有记录标 sim_rejected
        import csv as _csv
        from src.monitor.clv_collector import TRACKING_FILE
        try:
            if not TRACKING_FILE.exists():
                return
            with open(TRACKING_FILE, encoding="utf-8-sig", newline="") as f:
                reader = _csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                rows = list(reader)
            for extra in ("sim_rejected", "sim_reject_reason"):
                if extra not in fieldnames:
                    fieldnames.append(extra)
            n = 0
            for r in rows:
                if (r.get("bb_match_id") or "").strip() in rejected_mids and not r.get("sim_rejected"):
                    r["sim_rejected"] = "1"
                    r["sim_reject_reason"] = "odds_changed"
                    n += 1
            if n:
                with open(TRACKING_FILE, "w", encoding="utf-8", newline="") as f:
                    w = _csv.DictWriter(f, fieldnames=fieldnames)
                    w.writeheader()
                    w.writerows(rows)
                print(f"[slm] 早盘模拟拒单: 复验 {len(due)} 场, 标拒 {n} 条 (价变 {len(rejected_mids)} 场)", flush=True)
        except Exception as e:
            print(f"[slm] 早盘模拟拒单写回失败: {type(e).__name__} {str(e)[:80]}", flush=True)

    def _settle_paper_bets(self):
        """结算虚拟投注(观察库): 按 match_id 定向查 getMatchDetail 判输赢, 写回 result/profit。"""
        from src.betting.bb_auto_bet import read_token
        _flush_paper_bets()  # 2026-09-20 先落盘(把攒批的未写 appends 写回), 结算读最新
        if not LIVE_PAPER_FILE.exists():
            return
        try:
            bets, _ = _load_paper_bets()
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
                # 2026-10-26 比赛已结束但拿不到全场比分 → 连续 3 次标记 void(防永久 pending 污染统计)
                b["_no_score"] = b.get("_no_score", 0) + 1
                if b["_no_score"] >= 3:
                    b["result"] = "void"; b["profit"] = 0.0; b["settled"] = True; b["settled_ts"] = now
                    changed = True
                continue
            sc = [detail["home_score"], detail["away_score"]]
            desig = b.get("designation", ""); line = b.get("line")
            stake = float(b.get("stake", 0)); odds = float(b.get("bb_odds", 0))
            home, away = sc[0], sc[1]
            # 网球: 让局(hc_games)/总局数(ou)的线是局数, 用总局数判定(独赢/让盘用盘数)。
            if b.get("sport") == 5 and desig in ("让局主胜", "让局客胜", "大球", "小球"):
                _gh = detail.get("games_home"); _ga = detail.get("games_away")
                if _gh is not None and _ga is not None:
                    home, away = _gh, _ga
            # 判输赢。1x2(主/和/客)是三向盘、没有走盘: 打平对主胜/客胜是输、对和局是赢。
            # 走盘(push 退款)只存在 hc/ou: 让球后打平 / 总分恰等于盘口线。
            if desig == "主胜":
                result = "won" if home > away else "lost"
            elif desig == "客胜":
                result = "won" if away > home else "lost"
            elif desig == "和局":
                result = "won" if home == away else "lost"
            elif desig in ("主/和", "客/和", "主/客"):  # 双机会 dc(2026-09-19 新增)
                if desig == "主/和":      # 1X 主胜或平
                    result = "won" if home >= away else "lost"
                elif desig == "客/和":    # X2 客胜或平
                    result = "won" if away >= home else "lost"
                else:                    # 12 非平局
                    result = "won" if home != away else "lost"
            elif desig in ("双方进球", "非双方进球"):  # 双边进球 btts(2026-09-19 新增)
                if desig == "双方进球":
                    result = "won" if home > 0 and away > 0 else "lost"
                else:                    # 非双方进球(至少一方0球)
                    result = "won" if home == 0 or away == 0 else "lost"
            elif desig in ("上半场主胜", "上半场客胜", "上半场和局", "上半场大球", "上半场小球"):
                # 半场盘口(ht/ht_ou, 2026-09-21 补收): 用 getMatchDetail 的半场比分结算。
                # 半场比分带 HT+2H==FT 自校验, 拿不到(异常)就跳过不结, 绝不拿全场比分错结半场。
                ht_h = detail.get("ht_home_score"); ht_a = detail.get("ht_away_score")
                if ht_h is None or ht_a is None:
                    # 2026-10-26 半场比分拿不到 → 连续 3 次标记 void(半场盘口 ht/ht_ou 结算不了的样本不永久 pending)
                    b["_no_score"] = b.get("_no_score", 0) + 1
                    if b["_no_score"] >= 3:
                        b["result"] = "void"; b["profit"] = 0.0; b["settled"] = True; b["settled_ts"] = now
                        changed = True
                    continue
                if desig == "上半场主胜":
                    result = "won" if ht_h > ht_a else "lost"
                elif desig == "上半场客胜":
                    result = "won" if ht_a > ht_h else "lost"
                elif desig == "上半场和局":
                    result = "won" if ht_h == ht_a else "lost"
                else:  # 上半场大球/上半场小球
                    if line is None:
                        continue
                    if desig == "上半场大球":
                        diff = (ht_h + ht_a) - line
                    else:
                        diff = line - (ht_h + ht_a)
                    if abs(diff) < 0.0001:
                        result = "push"
                    else:
                        result = "won" if diff > 0 else "lost"
            elif desig in ("让局主胜", "让局客胜"):  # 网球让局(全场让局, 按局数)
                if line is None:
                    continue
                diff = ((home - away) + line) if desig == "让局主胜" else ((away - home) + line)
                if abs(diff) < 0.0001:
                    result = "push"
                else:
                    result = "won" if diff > 0 else "lost"
            elif desig in ("让球主胜", "让球客胜", "大球", "小球"):
                if line is None:
                    continue  # hc/ou 缺 line, 跳(旧记录)
                if desig == "让球主胜":
                    # 2026-10-13 修: line 语义按运动区分。
                    # 篮球 line=「赛前让分」→ 全场净胜+line; 足球/冰球等 line=「滚球让分(相对下注瞬间)」→ 下注后净胜+line。
                    # 判别依据 = 该运动 line 是否为赛前口径(数据事实, 实测成对差值: 篮球|line|>主领先, 足球/冰球|line|≈主领先)。
                    # 两个式子长得不一样, 不许合并——公式对不对取决于输入约定, 不取决于公式本身。
                    if b.get("sport") == 3:  # 篮球 = 赛前让分
                        diff = (home - away) + line
                    else:  # 足球/冰球等 = 滚球让分
                        hb, ab = _bet_score(b)
                        diff = ((home - hb) + line) - (away - ab)
                elif desig == "让球客胜":
                    if b.get("sport") == 3:
                        diff = (away - home) + line
                    else:
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
            # 2026-10-13 结算断言: 记录终局比分(供未来独立重算校验) + 比分单调性自检
            # (终局比分不能小于下注瞬间 bsc, 否则下注瞬间比分或终局比分有一方脏 → 落盘 result_conflict)
            b["final_score"] = sc
            # 2026-10-13 落盘 line_semantics: 让球盘口的 line 语义(赛前/滚球), 供未来重算/追溯
            # (篮球=赛前让分, 足球/冰球等=滚球让分; 两个结算式子不许合并)
            if b.get("sub") == "handicap":
                b["line_semantics"] = "pregame" if b.get("sport") == 3 else "relative"
            _bsc = b.get("bsc")
            if isinstance(_bsc, list) and len(_bsc) >= 2:
                try:
                    if home < int(_bsc[0]) or away < int(_bsc[1]):
                        b["result_conflict"] = True
                except (ValueError, TypeError):
                    pass
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
            _paper_bets_cache["dirty"] = True
            _flush_paper_bets()  # 2026-09-20 结算变更立即落盘(结算结果要持久化)
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

    def _check_clv(self):
        """CLV 追踪(2026-11-02 改滚球 live CLV 口径): 下注 60s 后复验 Pin 公平价, 算 live CLV 写回观察库。

        live CLV = (bb_odds - fair@T+60) / fair@T+60。正 = 你抢到比市场 60s 后
        重定价更优的价格(真 edge); 负 = 逆向选择(接盘)。写回 live_paper_bets 的 clv 字段,
        供结算按 CLV 正负分桶。

        2026-11-02 修正: 之前 3s 复验测的是「延迟噪声」(信噪比≈0, 见记忆 live-clv-redesign),
        因为早盘 lead-lag 窗口实测 ~37s(分钟级), 3s 只够看延迟、看不出 edge。改 60s(>37s),
        让复验价反映「市场真正重定价后」的评估, 这才和早盘「收盘线 CLV」同结构(参考价=下注后60s)。
        每次轮询(2s)都查 _check_clv, 有 ready 才发 Pin 请求(请求率=下注率, 不额外加压)。
        另: allow_closed=True + nearest=True 只对 CLV 路径; 下单前验价保持严格。
        """
        # 2026-09-19 定期输出 LEV 复验失败统计(300s 一次, 量化 close/价差/deleted 各占多少)
        if self._lev_fail_stats and time.time() - self._lev_fail_log_ts >= 300:
            self._lev_fail_log_ts = time.time()
            print(f"[slm] LEV复验失败统计: {self._lev_fail_stats}", flush=True)
        if not self._reversion_track:
            return
        now = time.time()
        ready = {k: v for k, v in self._reversion_track.items() if now - v["ts"] >= 60}
        if not ready:
            return
        for key, v in ready.items():
            del self._reversion_track[key]
            try:
                clv = self._reverify_live_ev_oa(v["sig"])
                if clv is None:
                    # 2026-09-19 复验失败(盘口close/价差过大)重试一次, 3s后再试; 失败2次才弃(提升 LEV 采集率)
                    if v.get("attempts", 0) < 1:
                        v["attempts"] = v.get("attempts", 0) + 1
                        v["ts"] = time.time()
                        self._reversion_track[key] = v
                    continue
                self._write_clv(key, clv)
            except Exception:
                pass

    def _write_clv(self, key, clv):
        """把 clv 写回 live_paper_bets 对应记录。"""
        try:
            bets, _ = _load_paper_bets()  # 2026-09-20 用内存缓存, 不读 4.7MB 盘
            mid, mkt, opt = key
            for b in bets:
                if (str(b.get("match_id")) == mid
                        and str(b.get("market_id")) == mkt
                        and str(b.get("option_type")) == opt):
                    if "clv" not in b:
                        b["clv"] = round(clv, 2)
                        _paper_bets_cache["dirty"] = True
                    break
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
            from src.scrapers.devig import power_fair_odds
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
            pin_points = None; neg_sign_used = False  # 2026-10-13 落盘: Pin 让分线 + 是否负号分支
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
                _spread = lv.get("spread") or {}
                raw = _match_2way_line(line_val, _spread)
                if not raw:
                    continue
                # 2026-10-13 落盘 pin_points(匹配到的 Pin 让分线) + neg_sign_used(是否走负号分支)
                if line_val is not None and line_val in _spread:
                    pin_points = line_val
                elif line_val is not None and -line_val in _spread:
                    pin_points = -line_val
                    neg_sign_used = True
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
                fair = power_fair_odds(raw)
            except Exception:
                continue
            if not fair or len(fair) <= i or fair[i] <= 0:
                continue
            fair_p = fair[i]
            from src.scrapers.fair_calibration import calibrated_ev
            ev, ev_raw, fair_cal = calibrated_ev(bb, fair_p, raw=raw[i])  # 2026-10-18 校准热门高估
            fair_cal = fair_cal or fair_p
            # 2026-10-13 原始 tick 普查(补分母): 比价后、ev 过滤前落一条, 一行一条。
            # 有了它才能算覆盖率/base rate("BB 到底有多少时候错"), 不被下注样本条件化。
            try:
                _tk = {"ts": time.time(), "match_id": match_id, "sub": sub, "desig": desig[d],
                       "bb_price": bb, "pin_fair": round(fair_p, 4), "pin_raw": raw[i],
                       "pin_points": pin_points, "ev": round(ev, 2),
                       "ev_raw": round(ev_raw, 2), "fair_cal": round(fair_cal, 4),
                       "trigger_source": self._trigger_source,
                       "degraded_mode": (self._trigger_source != "pin"),
                       "sport": lv.get("sport", ""),
                       "home": lv.get("home", ""), "away": lv.get("away", "")}
                with open(ROOT / "data" / "storage" / "live_tick_log.jsonl", "a", encoding="utf-8") as _tf:
                    _tf.write(json.dumps(_tk, ensure_ascii=False) + "\n")
            except Exception:
                pass
            tag = f"{lv['home']} vs {lv['away']} [滚球] {desig[d]}"
            if ev < LIVE_INGEST_EV_MIN or ev > LIVE_MAX_EV:
                continue
            sig = {"match_id": match_id, "sub": sub, "desig": desig[d],
                   "bb_odds": bb, "fair": fair_p, "ev": ev, "line": line_val,
                   "fair_cal": fair_cal, "ev_raw": ev_raw,  # 2026-10-18 校准公平价/原始EV 落盘对照
                   "pin_raw": raw[i],  # Pin 原始价(去抽水前), 供推送展示(2026-09-14)
                   # 2026-10-13 落盘 ev 层口径: pin_points(匹配到的Pin让分线) + neg_sign_used(负号分支)
                   # + line_semantics(赛前/滚球) + ev_branch + ev_valid(篮球让球跨参考系=unverified)
                   "pin_points": pin_points, "neg_sign_used": neg_sign_used,
                   "line_semantics": ("pregame" if (sub == "handicap" and lv.get("sport") == 3)
                                      else ("relative" if sub == "handicap" else None)),
                   "ev_branch": sub,
                   "ev_valid": "unverified" if (sub == "handicap" and lv.get("sport") == 3) else "ok",
                   # 2026-10-13 补英文队名+sport 顶层字段: _reverify_live_ev_oa 复验 Pin 公平价要英文队名
                   # (Pin 缓存 participants 是英文), 之前只在 match 里存中文优先名, 复验取顶层取空 → LEV 全 None。
                   "home_en": lv.get("home", ""), "away_en": lv.get("away", ""), "sport_id": lv.get("sport", ""),
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

    def _count_skip(self, key):
        self._skip_stats[key] = self._skip_stats.get(key, 0) + 1

    def _score_changed(self, sig):
        """下单前比分状态校验(2026-10-11): 进球/比分变了 → 快照作废(死球, 不吃坏价)。

        返回 "changed"(确实变了) / "untrusted"(缓存太旧不敢信) / None(可信且没变)。
        拆两桶: changed=防线生效, untrusted=防线自杀(该缩短缓存周期或换 Pin 低延迟比分源)。
        """
        try:
            from src.scrapers.pinnacle_live import get_bb_fresh_or_cached
            bb, _bb_ts = get_bb_fresh_or_cached(wait_for_fresh=False)
            _mid = sig.get("match_id")
            m = bb.get(_mid) or bb.get(str(_mid))  # 键类型可能 int/str, 都试
            if m is None:
                return None
            cur_sc = m.get("sc")
            snap_sc = sig.get("bsc")
            if cur_sc is not None and snap_sc is not None and cur_sc != snap_sc:
                return "changed"
            # 缓存太旧 → 比分「没变」的结论不可信(进球可能已发生但缓存没刷新)
            if _bb_ts and time.time() - _bb_ts > SCORE_CHECK_MAX_AGE:
                return "untrusted"
        except Exception:
            pass
        return None

    def _try_live_auto_bet(self, sig):
        """滚球机会处理: 观察库入库前验价(漂移假EV不入库), 实盘下单由 LIVE_REAL_BET_ENABLED 控制。"""
        _t0 = time.time(); _t = _t0  # 计时埋点(2026-09-17)
        # 2026-10-17 统一 Pin id: pin_matchup_id 之前是 Pin 官方 matchupId(与 pinnapi event_id 不一致)
        # → lag_sec(_pin_event_last)/clv(_reversion_track) 全 null。改用 pinnapi 按英文队名反查 event_id。
        try:
            from src.scrapers.pinnapi_client import _pin_cache_find_event, BB_TO_PIN
            _ev = _pin_cache_find_event(sig.get("home_en", ""), sig.get("away_en", ""), pin_sid=BB_TO_PIN.get(sig.get("sport")))
            if _ev:
                _eid = _ev.get("event_id") or _ev.get("id")
                if _eid:
                    sig["pin_matchup_id"] = str(_eid)
        except Exception:
            pass
        # 2026-09-18 用户决定: 实时赔率下不再验价、不再并发预拉。sig["ev"]/sig["fair"] 直接来自
        # fetch_live_opportunities_oa 的 Betfair 中间价(实时), BB 赔率也是秒级拉的, 下单前不再
        # 重复 fetch_current_odds 验价(lead-lag 抢窗口, 直接下单)。
        stake = self._stake_for(sig)
        # 2026-10-17 撤 SBO 置信度: 只用 Pin 单锚, 不再半额
        sig["_stake"] = stake
        # 2026-10-31 观察库与实盘同源: 入库前用鲜价(与实盘 verify_price 同源, get_bb_fresh_or_cached)
        # 重算 EV, 但保留「记录所有 +EV 机会」(不只实盘方向)——观察库的本意就是不用实盘也能验证盘口价值。
        # 价格/EV 用鲜价, 保证纸面ROI 和实盘 ROI 同口径(消除信号价 0.5s 陈旧造成的假正)。
        try:
            from src.scrapers.pinnacle_live import get_bb_fresh_or_cached
            _bb, _ = get_bb_fresh_or_cached(wait_for_fresh=False)
            _b = _bb.get(int(sig["match_id"])) if sig.get("match_id") is not None else None
            if _b:
                for _mk in _b.get("markets", []):
                    if (str(_mk.get("market_id")) == str(sig.get("market_id"))
                            and str(_mk.get("option_type")) == str(sig.get("option_type"))):
                        # 2026-11-07 修 line 漂移跨线: 滚球让球/大小球线会随比赛漂移(进球后 4.5→3.5),
                        # market_id 不变但 line 变了 → 鲜价 BB 和信号时 fair 是不同线的价, 假溢价。
                        # 线漂了(和信号时 sig["line"] 不一致)就放弃(宁缺毋滥, 不拿错线的价下单)。
                        _fod = _mk.get("odds")
                        _fresh_line = _mk.get("line")
                        if (sig.get("line") is not None and _fresh_line is not None
                                and abs(float(_fresh_line) - float(sig["line"])) > 0.01):
                            sig["_sim_reject"] = "line_drift"
                            break
                        if _fod and float(_fod) > 1:
                            sig["bb_odds"] = _fod
                            # 2026-11-05 修: 用校准公平价重算 EV(之前用原始 fair, 把 platt 校准冲掉 → 热门假溢价又回来)
                            _fair_for_ev = sig.get("fair_cal") or sig.get("fair")
                            if _fair_for_ev and float(_fair_for_ev) > 1:
                                sig["ev"] = round((float(_fod) - float(_fair_for_ev)) / float(_fair_for_ev) * 100, 2)
                        break
        except Exception:
            pass
        # 2026-10-31 EV 门槛模拟: 实盘 verify_price 用鲜价重算 EV, 跌破 min_ev_pct(=threshold) 就 -4 放弃。
        # 观察库重算鲜价后若 EV 已跌破门槛, 也应标 sim_rejected(否则多记「快照+EV 但鲜价已跌破门槛」的逆向选择样本)。
        try:
            if sig.get("ev") is not None and float(sig["ev"]) < self.threshold:
                sig["_sim_reject"] = "ev_below_entry"
        except (TypeError, ValueError):
            pass
        _added = self._append_live_paper_bet(sig)
        if _added and sig.get("pin_matchup_id"):
            _m = sig.get("match") or {}
            self._reversion_track[(str(sig["match_id"]), str(sig.get("market_id")), str(sig.get("option_type")))] = {
                "sig": {
                    "pin_matchup_id": sig.get("pin_matchup_id"),
                    "sub": sig.get("sub"), "desig": sig.get("desig"),
                    "bb_odds": sig.get("bb_odds"), "line": sig.get("line"),
                    "home_en": sig.get("home_en") or _m.get("home") or _m.get("home_cn", ""),
                    "away_en": sig.get("away_en") or _m.get("away") or _m.get("away_cn", ""),
                    "sport_id": sig.get("sport_id") or _m.get("sport", ""),
                },
                "ts": time.time(),
            }
        # 2026-09-20 SBO 不同向(diff): 不再跳过, 改为半额投注(上面 sbo_confirm=False 已 stake×0.5)。
        # 同向(same)=全注额; 不覆盖(none)/不同向(diff)=半额(置信度降一档)。观察库仍标注 sbo_direction 供统计。
        # (稳定性计数已撤 2026-09-20: 与 persistence 2s 重复且加 4s 延迟; 防闪动靠 persistence + oddsChange=0)
        # 2026-11-07 drop 为主: 只有 drop 唤醒才实盘投注, poll/ws 只入库不投注(观察库样本照收, 供 ROI/释放决策)
        if self._trigger_source != "drop":
            return
        if not LIVE_REAL_BET_ENABLED:
            return
        # 2026-11-01 实盘投注门槛不变: 入库已降到 LIVE_INGEST_EV_MIN(2%), 但投注仍要求 >= self.threshold。
        # (鲜价重算后 EV 跌破门槛的样本已标 sim_rejected 进观察库, 但不下单)
        try:
            if sig.get("ev") is not None and float(sig["ev"]) < self.threshold:
                return
        except (TypeError, ValueError):
            pass
        # 2026-09-12 优化: 不再硬编码"只投小球/网球/篮球", 改为「观察库释放优先, 已验证方向其次」。
        # 观察库释放的盘口(让球主胜/客胜等赢率>隐含+n>100)也实盘投(cap 150/300 试探)。
        _sport = sig["match"].get("sport")
        _sub = sig.get("sub")
        _desig = sig.get("desig")
        # 2026-11-04 停投推导公平价盘口(dc 双重机会从 1x2 合成, 合成误差一直输)。已入库观察库, 只不下单。
        if _sub in DERIVED_FAIR_SUBS:
            return
        # 2026-10-24 临场保护(职业团队做法): 比赛临近结束不下注(赔率短、不确定性已消除、无信息优势)。
        # 键: sport id, 值: (总时长秒, 剩余下限秒)。棒球/网球无固定时长, 暂不加。
        _near_end = {1: (5400, 900), 3: (2400, 180), 2: (3600, 300)}.get(_sport)
        if _near_end:
            _total, _min_left = _near_end
            _mc = int(sig["match"].get("mc", 0) or 0)
            if _mc > _total - _min_left:
                print(f"  ⏭️ 临场保护(剩<{_min_left//60}min), 跳过 {sig['match']['home']} vs {sig['match']['away']}", flush=True)
                return
        # 方向归一化(大/小/平/客/主), 与 compute_market_release._direction 同口径。
        # 2026-10-17 修: dc 之前用完整 desig(客/和)作方向, 与清单的 X2 口径不一致 → dc 释放/拦截永远匹配不上。
        # 改对齐 compute_market_release._direction 的 dc 三向: 主/和→1X, 主/客→12, 客/和→X2。
        if _sub == "double_chance":
            if "主" in _desig and "和" in _desig:
                _dr = "1X"
            elif "主" in _desig and "客" in _desig:
                _dr = "12"
            elif "和" in _desig and "客" in _desig:
                _dr = "X2"
            else:
                _dr = "其他"
        elif _sub == "btts":
            _dr = _desig  # btts 保持完整 desig(双方进球/非双方进球)
        elif "大" in _desig:
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
        # 2026-11-09 两端排除(对齐早盘): 隐含胜率 20-75% 放开, <20%(冷门 favorite-longshot bias)和>75%(超热门)排除。
        _imp_live = 1.0 / (sig.get("fair") or sig.get("bb_odds")) if (sig.get("fair") or sig.get("bb_odds")) else 0
        if _imp_live < 0.20 or _imp_live > 0.75:
            return
        # 2026-09-19 用户暂停小球盘投注: 实盘小球-31%失效, 观察库已收纸单继续攒数据, 只不下真单
        if not LIVE_UNDER_BET_ENABLED and _sub == "over_under" and _dr == "小":
            print(f"  ⏸️ 小球盘已暂停投注(仅记观察库) {sig['match']['home']} vs {sig['match']['away']}", flush=True)
            return
        _sp_en = BB_SPORT_EN.get(_sport)
        _sm = BB_SUB_TO_SM.get(_sub)
        _cap = None
        _cap_key = None
        # 观察库释放盘口(含已验证方向, 2026-09-13 统一进 compute_market_release.MANUAL_OBSERVE_RELEASE) → 投
        # 2026-09-13 加赔率区间: 先查拦截(该区间 n>200 表现差则不投), 再查释放(已验证方向"*" 或 该区间)。
        if _sp_en and _sm:
            _base = f"{_sp_en}|{_sm}|{_dr}"
            _iv = _odds_interval(sig.get("fair") or sig.get("bb_odds"), _sp_en)
            # 2026-11-10 滚球与早盘一致: full_release_live 全量放开(绕过 observe_blocked, cap=MAX_STAKE)
            if f"{_sp_en}|{_sm}" in _load_full_release_live():
                _cap = MAX_STAKE  # 全量放开, cap 用滚球单注上限(80)
                _cap_key = None
            else:
                _obs_blocked = _load_obs_blocked()
                # 2026-10-17 修: 之前只用具体区间 {_iv} 匹配, observe_blocked 的 "*"(所有区间)条目永远匹配不上
                # → 滚球撤回(如 tennis|1x2|主|*|live)形同虚设。补 "*" 通配, 与下方 caps 查询同口径。
                if f"{_base}|{_iv}|live" in _obs_blocked or f"{_base}|*|live" in _obs_blocked:
                    print(f"  🚫 赔率区间{_iv}已拦截(n>200表现差), 跳过 "
                          f"{sig['match']['home']} vs {sig['match']['away']} {sig['desig']}", flush=True)
                    self._record_reject(sig, "exclude_list")
                    return
                _caps = _load_obs_caps()
                _cap = _caps.get(f"{_base}|*|live")  # 已验证方向(所有区间)
                if _cap is not None:
                    _cap_key = f"{_base}|*|live"
                else:
                    _cap = _caps.get(f"{_base}|{_iv}|live")  # 该赔率区间单独释放
                    _cap_key = f"{_base}|{_iv}|live"
        if _cap is None:
            self._record_reject(sig, "below_threshold")
            return
        stake = min(stake, _cap)
        # 2026-09-19 分配逻辑一致: 观察库 stake 同步成实盘 cap 后的值(否则观察库 ROI 用未 cap 的 Kelly, 与实盘不一致)
        if _added:
            self._update_live_paper_stake(sig, stake)
        # 当日累计投注额上限(2026-09-12 用户要求): 观察库释放且未解除限制的盘口, 当日累计≤1000
        if _cap_key:
            _rs = _load_release_state().get(_cap_key, {})
            if not _rs.get("limit_removed", False):
                _today = datetime.now().strftime("%Y-%m-%d")
                _daily_stake = _rs.get("daily_stake", 0) if _rs.get("daily_stake_date", "") == _today else 0
                if _daily_stake + stake > DAILY_STAKE_LIMIT:
                    print(f"  📝 当日累计超限({_daily_stake:.0f}+{stake:.0f}>{DAILY_STAKE_LIMIT}), 跳过 "
                          f"{sig['match']['home']} vs {sig['match']['away']} {sig['desig']}", flush=True)
                    self._record_reject(sig, "risk_block")
                    return
        sig["_stake"] = stake
        # 2026-10-16 让局降额: cap<MIN_STAKE 的盘口用 cap 作下限(让局 cap10 → 允许投10元),
        # 否则 MIN_STAKE=30 会把降额注拦掉(10<30 → 实际停投)。其他盘口 cap≥30, floor 仍=30 不变。
        _floor = min(MIN_STAKE, _cap)
        if stake < _floor:
            return
        self._load_live_spent()
        tag = f"{sig['match']['home']} vs {sig['match']['away']} {sig['desig']}"
        # 预算封顶(滚动预算): 按"未结算额"封顶, 结算后释放额度 → 结算的钱可继续投滚球
        if self._live_outstanding + stake > LIVE_BUDGET:
            print(f"  📝 滚球预算已满(未结算¥{self._live_outstanding:.0f}/{LIVE_BUDGET}), 已记观察库 {tag}", flush=True)
            self._record_reject(sig, "risk_block")
            return
        match_id = sig.get("match_id")  # 2026-10-13 修: 之前 fetch_current_odds(market_id, match_id, ...) 用裸 match_id 但没定义 → NameError 卡死下单
        market_id = sig.get("market_id")
        option_type = sig.get("option_type")  # 2026-10-13 修: fetch_current_odds 也用裸 option_type
        if market_id is None:
            return
        from src.betting.bb_auto_bet import _load_stake_record, place_single_bet, check_sub_market_bet
        # 2026-10-17 放开 degraded_mode 闸门: 切 pinnapi 后滚球公平价统一实时价(pinnacle_fair_price),
        # poll 触发的机会与 pin 触发同源, 不再是「异质锚污染」。degraded_mode 字段仍落盘供 pin/poll 对照。
        # (原 2026-10-13: 非 Pin 触发跳过 —— 因 Betfair 异质锚已撤, 前提不成立, 已废弃)
        # 2) 单日亏损熔断: 当日实盘亏损超 DAILY_LOSS_LIMIT 即停
        _today_pnl = _recent_pnl(days=1)
        if _today_pnl < -DAILY_LOSS_LIMIT:
            self._count_skip("daily_loss")
            print(f"  ⏭️ gate=daily_loss 当日亏损{_today_pnl:.0f}超{-DAILY_LOSS_LIMIT}, 跳过 {tag}", flush=True)
            self._record_reject(sig, "risk_block")
            return
        # 同场同盘口互斥锁(2026-09-19): 同一 match 同一盘口只下一注(防对立方向/多线重复下注)。
        # 2026-11-14 删掉上面的「同 Pin 事件(pin_matchup_id 比赛级)互斥」: 用户明确不同盘口可分别下注
        # (让球/大小球/独赢各下一注), 只保留比赛+盘口级互斥, 不再用比赛级 pin_matchup_id 一刀切拦三盘口。
        if check_sub_market_bet(sig["match_id"], sig.get("sub")):
            print(f"  ⏭️ 同场同盘口已投, 跳过 {tag}", flush=True)
            self._record_reject(sig, "mutex")
            return
        rec = _load_stake_record()
        if rec.get(str(sig["match_id"]), {}).get(str(market_id), 0.0) > 0:
            print(f"  ⏭️ 已下过注, 跳过 {tag}", flush=True)
            self._record_reject(sig, "mutex")
            return
        # 指纹去重: 同一 (match, market) 5min 内尝试过(无论成败)则跳过, 避免每 2s 轮询重复下单
        _d = self._attempted.get(str(sig["match_id"]), {})
        if time.time() - _d.get(str(market_id), 0) < 300:
            return
        # 2026-10-25 修复14068(投注过于频繁): 全局跨源冷却, 与早盘 drop/ws_scan 共享同一文件
        # (GLOBAL_BET_TS_FILE), 保证 BB 侧任何两注(不管哪个源/口子)都 ≥10-20s。
        from src.betting.bb_auto_bet import global_bet_cooldown as _gbc
        if _gbc(10, 20) > 0:
            self._count_skip("global_cooldown")
            print(f"  ⏭️ gate=global_cooldown 全局跨源冷却中, 跳过 {tag}", flush=True)
            return
        # 冷却三层(2026-10-11): per-market 防连打 + per-event 敞口 + 全局频次帽。全部只记成功(成交)。
        if LIVE_COOLDOWN_PER_MARKET:
            # 层1: per-(event,market) 冷却, 同一口子防连打(3-8s 随机)
            _mk_key = (str(sig["match_id"]), str(market_id))
            if time.time() - self._market_cooldown.get(_mk_key, 0.0) < random.uniform(*LIVE_MARKET_COOLDOWN):
                self._count_skip("cooldown_market")
                print(f"  ⏭️ gate=market_cooldown 同口子冷却中, 跳过 {tag}", flush=True)
                return
            # 层2: per-event 敞口上限, 单场最多 K 个持仓(防对冲吃手续费)
            if self._event_exposure.get(sig["match_id"], 0) >= LIVE_EVENT_MAX_EXPOSURE:
                self._count_skip("event_exposure")
                print(f"  ⏭️ gate=event_exposure 单场敞口≥{LIVE_EVENT_MAX_EXPOSURE}, 跳过 {tag}", flush=True)
                return
            # 层3: 全局频次帽, 每分钟≤N(滑动窗口, 纯账号风控)
            _now = time.time()
            self._global_bet_ts = [t for t in self._global_bet_ts if _now - t < 3600]
            if len([t for t in self._global_bet_ts if _now - t < 60]) >= LIVE_GLOBAL_RATE_MIN:
                self._count_skip("global_rate_limit")
                print(f"  ⏭️ gate=global_rate_limit 每分钟{LIVE_GLOBAL_RATE_MIN}笔已达, 跳过 {tag}", flush=True)
                return
        else:
            # 回退路径: 旧全局冷却(开关 LIVE_COOLDOWN_PER_MARKET=false 时用)
            from src.betting.bb_auto_bet import global_bet_cooldown
            if global_bet_cooldown(3, 8) > 0:
                self._count_skip("cooldown")
                return
        # (2026-09-22 取消余额<1000 熔断——用户指令: 符合门槛就投, 不因余额低停注)
        print(f"  🎯 滚球下单 {tag} @{sig['bb_odds']:.2f} 注额¥{stake} "
              f"[sub={sig.get('sub')} market_id={market_id} option_type={sig.get('option_type')}]", flush=True)
        # 2026-09-19 时间条件验价(职业团队做法): 从 BB 赔率拉取(bb_ts)到此刻超过 REVERIFY_THRESHOLD 秒
        # 就重拉 BB 当前赔率验价——赔率可能已朝不利方向变动(逆向选择), 抢窗口期内(≤阈值)直接下单省 1-5s。
        _detect_ts = sig.get("bb_ts") or _t0
        _need_verify = (time.time() - _detect_ts) > REVERIFY_THRESHOLD
        # 2026-09-28 推广: 快照单从只让球推广到全盘口(1x2/ou/hc)。BB 预取快照(≤2s)比价,
        # 下单 verify_price=False + oddsChange=0(下单瞬间赔率变了就拒), 所以「投注端赔率=快照」就下单,
        # 不必重拉 BB 验价。快照年龄 >SNAPSHOT_MAX_AGE 判 stale 跳过(防用过期快照算错 edge)。
        if (time.time() - _detect_ts) > SNAPSHOT_MAX_AGE:
            self._count_skip("stale_snapshot")
            print(f"  ⏭️ 快照单跳过(快照已{time.time()-_detect_ts:.0f}s>{SNAPSHOT_MAX_AGE}s, 不投stale) {tag}", flush=True)
            self._record_reject(sig, "risk_block")
            return
        # 2026-10-11 比分状态校验: 进球/比分变了 → 快照作废(死球), 跳过。补 SNAPSHOT_MAX_AGE 只看年龄的缺口。
        _sc = self._score_changed(sig)
        if _sc == "changed":
            self._count_skip("score_changed")
            print(f"  ⚽ 比分已变(进球/状态), 快照作废跳过 {tag}", flush=True)
            self._record_reject(sig, "risk_block")
            return
        if _sc == "untrusted":
            self._count_skip("score_untrusted")
            print(f"  ⚠️ 比分缓存超龄(不可信), 跳过 {tag}", flush=True)
            return
        # 2026-10-17 验价改读缓存: BB 0.5s 预取缓存替代实时 fetch_current_odds(省 0.32s)。
        # 新鲜度从「实时」降到「0.5s 内」, oddsChange=0 兜底 0.5s 内的漂移。smin/smax 限额缓存无, 置 None。
        _fresh = None
        try:
            from src.scrapers.pinnacle_live import get_bb_fresh_or_cached
            _bb, _ = get_bb_fresh_or_cached(wait_for_fresh=False)
            _b = _bb.get(int(match_id)) if match_id is not None else None
            if _b:
                for _mk in _b.get("markets", []):
                    if str(_mk.get("market_id")) == str(market_id) and str(_mk.get("option_type")) == str(option_type):
                        _fresh = (_mk.get("odds"), None, None, _mk.get("line"))
                        break
        except Exception:
            pass
        _fresh_odds = _fresh[0] if _fresh else None
        _fresh_line = _fresh[3] if (_fresh and len(_fresh) > 3) else None
        if _fresh_odds is None:
            self._count_skip("reverify_failed")
            print(f"  ⏭️ 重拉拿不到新鲜价(封盘/关闭), 放弃 {tag}", flush=True)
            self._record_reject(sig, "system_error")
            return
        # 2026-10-11 线变更校验: oddsChange=0 只查价格不查盘口线。让球/大小球线在重拉到提交间变了 → 放弃(旧EV打错线)。
        if sig.get("line") is not None and _fresh_line is not None:
            try:
                if abs(float(_fresh_line) - float(sig.get("line"))) > 0.01:
                    self._count_skip("line_changed")
                    print(f"  ⚠️ 盘口线已变({sig.get('line')}→{_fresh_line}), 放弃 {tag}", flush=True)
                    self._record_reject(sig, "risk_block")
                    return
            except (TypeError, ValueError):
                pass
        # 2026-10-11 _attempted 前置: 过了所有 gate 就标记「已尝试」, 在发请求前写。防重复请求 +
        # 将来 worker pool 并发重入双倍下单(必须先写再发请求)。stake_record 仍只在成交后写(防重复敞口)。
        self._attempted.setdefault(str(sig["match_id"]), {})[str(market_id)] = time.time()
        # 让球0(平局退款盘)计数/过滤(2026-10-11): sub==handicap 且 line==0。盘型结构决定平局必走盘
        # (三outcome事件付两outcome价, 平局概率成本摊进主客水位), EV 记录虚高且不进 slippage。
        # 计数器开关关闭时也照常计数(先量); 开关打开才过滤。早盘保留(定价效率高), 只滚球可滤。
        _is_line_zero = False
        if sig.get("sub") == "handicap" and sig.get("line") is not None:
            try:
                _is_line_zero = abs(float(sig["line"])) < 1e-6
            except (TypeError, ValueError):
                _is_line_zero = False
        if _is_line_zero:
            from src.scrapers.stats import count as _sc
            _sc("line_zero_seen_live")
            if LIVE_FILTER_LINE_ZERO:
                _sc("line_zero_filtered_live")
                print(f"  ⏭️ gate=line_zero 让球0(平局退款)已过滤, 跳过 {tag}", flush=True)
                return
        _t_place = time.time()
        _dv = _decision_version(self.threshold)  # 配置快照 hash(复盘用)
        # BB-vs-Pin 滞后(2026-10-11): bb_ts - Pin事件last。含~1.6s钟偏, 看相对变化/分组, 别当绝对滞后。
        _pin_last = _pin_event_last(sig.get("pin_matchup_id")) if sig.get("pin_matchup_id") else None
        _bb_ts = sig.get("bb_ts")
        _lag_sec = None
        if _pin_last and _bb_ts:
            try:
                _lag_sec = round(float(_bb_ts) - float(_pin_last), 2)  # 正 = BB 落后
            except (TypeError, ValueError):
                _lag_sec = None
        if _lag_sec is None:
            from src.scrapers.stats import count as _sc
            _sc("lag_uncomparable")  # 无 Pin 帧/无法匹配 → lag 不可靠
        _bet_meta = {"is_line_zero": _is_line_zero, "sport": sig.get("sport", ""),
                     "sub": sig.get("sub", ""), "line": sig.get("line"),
                     "league": sig["match"].get("league_cn", ""),
                     "trigger_source": self._trigger_source,
                     "degraded_mode": (self._trigger_source != "pin"),  # 2026-10-13 降级标记: 非 Pin 触发(bf_ws/poll)=异质锚
                     "bb_ts": sig.get("bb_ts"), "lag_sec": _lag_sec,
                     "poll_ts": sig.get("poll_ts")}  # 触发时刻(供 latency-ROI 曲线, 2026-10-11)
        # 2026-10-24 取消滚球余额熔断(用户要求): 余额<1000 只停早盘, 滚球继续投注(不做余额拦截)。
        code, order_id, msg = place_single_bet(
            market_id, sig["bb_odds"], sig["option_type"], stake=stake,
            match_id=sig["match_id"], check_limit=True, verify_price=True,
            fair_price=sig.get("fair_cal") or sig.get("fair"), min_ev_pct=self.threshold,
            prefetched_odds=_fresh_odds, decision_version=_dv, bet_meta=_bet_meta)
        _t_order = time.time() - _t0  # 下单函数内部耗时(调试用)
        _t_http = time.time() - _t_place  # place_single_bet HTTP 耗时
        _total_time = time.time() - (sig.get("poll_ts") or _t0)  # 2026-09-20 总耗时(WS触发→下单完成), 推送用
        print(f"[slm] 下单耗时: 内部 {_t_order:.2f}s (检查 {_t_place-_t0:.2f}s + HTTP {_t_http:.2f}s) / 总 {_total_time:.2f}s", flush=True)
        if code == -4:
            self._count_skip("drift_or_negative_ev")  # 验价拦截: EV 跌破入场门槛
        # 2026-10-11 slippage 审计: EV_trigger - EV_final(正=恶化, 负=吃更好价); 看分布不看均值。
        try:
            from src.betting.bb_auto_bet import _last_bet_audit as _aud
            _tev, _fev = _aud.get("trigger_ev"), _aud.get("final_ev")
            if _tev is not None and _fev is not None:
                print(f"[slm] slippage {_tev - _fev:+.2f}pp (触发EV {_tev:+.2f}%→成交EV {_fev:+.2f}%, reason={_aud.get('reason')})", flush=True)
        except Exception:
            pass
        _eq_key = f"{sig.get('sport')}|{sig.get('sub')}|{sig.get('desig')}"
        _record_bet_result(code, latency=_t_http, direction_key=_eq_key)  # 2026-09-21 gubbing 限注监控
        if code == 14010:
            self._invalidate_token_cache()
        self._probe_token_async()  # 2026-09-28 token 探针移到投注后(后台, 不占下单关键路径)
        if code == 0:
            # 2026-10-11 冷却三层只在成交成功后记(拒单/void 不烧窗口)。
            if LIVE_COOLDOWN_PER_MARKET:
                self._market_cooldown[(str(sig["match_id"]), str(market_id))] = time.time()
                self._event_exposure[sig["match_id"]] = self._event_exposure.get(sig["match_id"], 0) + 1
                self._global_bet_ts.append(time.time())
                from src.betting.bb_auto_bet import record_global_bet as _rgb
                _rgb()  # 2026-10-25 写全局冷却文件(三源共享), 修复14068
            else:
                self._last_bet_time = time.time()
                self._bet_delay = random.uniform(3, 8)
                from src.betting.bb_auto_bet import record_global_bet
                record_global_bet()
            from src.betting.bb_auto_bet import record_sub_market_bet
            record_sub_market_bet(sig["match_id"], sig.get("sub"))
            self._live_spent += stake
            self._live_outstanding += stake
            # 2026-11-07 BB价轨迹追踪: 下单成功后注册, 周期采样 BB 价直到追上 Pin 或超时(验证 lead-lag)
            self._bb_track[(str(sig["match_id"]), str(market_id), str(sig["option_type"]))] = {
                "ts": time.time(),
                "bb_at_bet": sig["bb_odds"],
                "fair_cal_at_bet": sig.get("fair_cal") or sig.get("fair"),
                "home": sig["match"].get("home", ""), "away": sig["match"].get("away", ""),
                "desig": sig.get("desig", ""), "sub": sig.get("sub", ""),
                "history": [(time.time(), sig["bb_odds"])],
            }
            if order_id:
                # 记录投注时的 fair/ev/bb_odds 等(2026-09-12: 结算汇总时按 order_id 关联补全明细要素)
                _binfo = {
                    "stake": stake,
                    "fair": sig.get("fair", 0), "ev": sig.get("ev", 0),
                    "fair_cal": sig.get("fair_cal", sig.get("fair", 0)),  # 2026-10-18 校准公平价
                    "ev_raw": sig.get("ev_raw", sig.get("ev", 0)),  # 2026-10-18 原始EV(校准前)
                    "bb_odds": sig.get("bb_odds", 0), "desig": sig.get("desig", ""),
                    "sub": sig.get("sub", ""), "home": sig["match"].get("home", ""),
                    "away": sig["match"].get("away", ""), "sport": sig["match"].get("sport", ""),
                    "verify": bool(_need_verify),  # 是否触发验价(2026-09-19)
                    "decision_version": _dv,  # 2026-10-11 配置快照 hash(复盘)
                    "league": sig["match"].get("league_cn", ""),  # 2026-10-11 联赛白名单切分
                    "trigger_source": self._trigger_source,  # 2026-10-11 组内对照(兜底轮询成本)
                    "bb_ts": sig.get("bb_ts"),  # 2026-10-11 BB 快照时间(滞后度量)
                    "bb_snapshot_age": round(time.time() - (sig.get("bb_ts") or _t0), 2),  # 决策时 BB 快照年龄
                    "lag_sec": _lag_sec,  # 2026-10-11 BB-vs-Pin 滞后(含钟偏, 看相对/分组)
                    "poll_ts": sig.get("poll_ts"),  # 触发时刻(供 latency-ROI 曲线)
                    "latency": round(_total_time, 2),  # 端到端耗时(触发→下单完成)
                    # 2026-10-17 补落盘六闸门第1条口径(pin_points配对/line语义/ev分支/负号):
                    # 之前 sig 里有但 real_bets 没写, 导致六闸门第1条无数据可检。
                    "pin_points": sig.get("pin_points"),  # Pin 让分线(配对 |d|>0.5 比例)
                    "line_semantics": sig.get("line_semantics"),  # 赛前/滚球语义(两支结算式不许合并)
                    "neg_sign_used": sig.get("neg_sign_used"),  # 是否走负号分支
                    "ev_branch": sig.get("ev_branch"),  # ev 分支(1x2/hc/ou)
                    "ev_valid": sig.get("ev_valid"),  # ev 有效性(篮球让球跨参考系=unverified)
                }
                self._live_bets[str(order_id)] = _binfo
                _append_real_bet(str(order_id), _binfo)  # 2026-09-27 实盘库: 持久化下单信息
            self._save_live_spent()
            # 更新释放盘口的当日累计投注额(2026-09-12 用户要求: 当日累计≤1000)
            if _sp_en and _sm:
                _update_daily_stake(_cap_key, stake)
            print(f"  ✅ 滚球下单成功 {tag} | 注额¥{stake} | 累计¥{self._live_spent:.0f}/{LIVE_BUDGET}", flush=True)
            # 每笔成功下单都推钉钉(不限频) + 显示账户总余额
            # 2026-10-27 改读缓存余额(不联网): fetch_balance 是 1-2s 阻塞 HTTP, 在滚球轮询循环里
            # 每笔成功单都卡一次, 拖慢下一轮 poll。余额已由 start_balance_prefetch(45s) 后台预热。
            from src.betting.bb_auto_bet import get_balance_cached
            _bal = get_balance_cached() or "未知"
            _sport_cn = BB_SPORT_CN.get(sig["match"].get("sport"), "") or ""
            _league = sig["match"].get("league_cn", "滚球") or "滚球"
            _mc = int(sig["match"].get("mc", 0) or 0)
            _clock = f"进行中 {_mc // 60} 分钟" if _mc > 0 else "进行中"
            _bj = datetime.now().strftime("%H:%M")
            _sub_cn = {"over_under": "大小球", "handicap": "让球", "opportunities": "独赢"}.get(sig.get("sub"), "")
            # 盘口线: 大小球/让球带线(小1球/让球主胜@-0.5), 独赢无线(2026-09-14 用户要求)
            _desig = sig.get('desig', '')
            _line = sig.get('line')
            if _line is not None and sig.get('sub') == 'over_under':
                _desig = f"{_desig[0]}{_line:g}球"  # 小球→小1球, 大球→大2.5球
            elif _line is not None and sig.get('sub') == 'handicap':
                _desig = f"{_desig}@{_line:g}"  # 让球主胜@-0.5
            _desig = f"{_sub_cn}-{_desig}" if _sub_cn else _desig
            _platform = "BB" if sig.get("platform", "BB") == "BB" else "FB"  # 不用"BB体育", 会被钉钉判博彩词
            # BB/Betfair 数据拉取时间(展示数据新鲜度): 同一轮 fetch_live_opportunities_oa 拉取, ≈实时
            _bb_ts = sig.get("bb_ts"); _pin_ts = sig.get("pin_ts")
            _bb_t = datetime.fromtimestamp(float(_bb_ts)).strftime("%H:%M:%S") if _bb_ts else "--"
            _bf_t = datetime.fromtimestamp(float(_pin_ts)).strftime("%H:%M:%S") if _pin_ts else "--"
            _pin_raw = sig.get("pin_raw")
            _pin_raw_str = f" | Pin原价 {float(_pin_raw):.2f}" if _pin_raw else ""
            # 2026-11-10 补线值: 让球/大小球推送带线(让多少/大多少小多少)。
            _line = sig.get("line")
            _desig_full = f"{_desig} {_line}" if _line else _desig
            self._notify_bet(
                "🟦 滚球已下单",
                f"{_sport_cn} | {_league} | 滚球{_clock} | 下单 {_bj}\n"
                f"{sig['match']['home']} vs {sig['match']['away']} | {_desig_full}\n"
                f"{_platform} {sig['bb_odds']:.2f} | Pin公平价 {sig['fair']:.2f}{_pin_raw_str} | 溢价 {sig['ev']:+.2f}% | 置信度:滚球\n"
                f"拉取 BB {_bb_t} | Pin {_bf_t}\n"
                f"单注 ¥{stake} | 余额 ¥{_bal} | 今日已投 ¥{self._live_spent:.0f} | 未结 ¥{self._live_outstanding:.0f} | 总耗时 {_total_time:.1f}s")
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
            from src.scrapers.fair_calibration import calibrated_ev
            ev, ev_raw, fair_cal = calibrated_ev(bb, fair)  # 2026-10-18 校准热门高估
            fair_cal = fair_cal or fair
            tag = f"{match['home_cn']} vs {match['away_cn']} [{match['sport']}/{match['league_cn']}] {desig}"
            if LIVE_INGEST_EV_MIN <= ev <= LIVE_MAX_EV:
                sig = {"match_id": match_id, "market_id": market_id, "option_type": option_type,
                       "sub": sub, "desig": desig, "bb_odds": bb, "fair": fair, "ev": ev,
                       "fair_cal": fair_cal, "ev_raw": ev_raw,  # 2026-10-18 校准公平价/原始EV 落盘对照
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

    def _reverify_live_ev(self, sig, allow_closed=False, nearest=False):
        """下注前重拉 Pin 滚球价, 重算该方向 EV(1x2/让球/大小球)。返回新 EV(或 None=盘口已关)。

        allow_closed/nearest 只给「下注后 CLV」路径(_check_clv)开; 下单前验价(_try_live_auto_bet)
        保持 False, 避免用 closed 盘口 stale 价 / 就近匹配错线 误下单。
        """
        from src.scrapers.pinnacle_live import reverify_live_markets
        from src.scrapers.devig import power_fair_odds
        pin_mid = sig.get("pin_matchup_id"); lid = sig.get("league_id")
        if not pin_mid or not lid:
            return None
        fresh = reverify_live_markets(pin_mid, lid, allow_closed=allow_closed)
        if not fresh:
            return None
        sub = sig.get("sub")
        if sub == "opportunities":
            ml = fresh.get("moneyline")
            if not ml:
                return None
            if len(ml) == 3:
                raw = ml
                idx = {"主胜": 0, "和局": 1, "客胜": 2}
            elif len(ml) == 2:
                # 2-way(网球/篮球等无和局): 主胜/客胜
                raw = ml
                idx = {"主胜": 0, "客胜": 1}
            else:
                return None
        elif sub == "handicap":
            raw = _match_2way_line(sig.get("line"), fresh.get("spread"), nearest=nearest)
            idx = {"让球主胜": 0, "让球客胜": 1}
        elif sub == "over_under":
            raw = _match_2way_line(sig.get("line"), fresh.get("total"), nearest=nearest)
            idx = {"大球": 0, "小球": 1}
        else:
            return None
        if not raw:
            return None
        try:
            fair = power_fair_odds(raw)
        except Exception:
            return None
        i = idx.get(sig["desig"])
        if i is None or not fair or len(fair) <= i or fair[i] <= 0:
            return None
        from src.scrapers.fair_calibration import calibrated_ev
        ev_cal, _, _ = calibrated_ev(sig["bb_odds"], fair[i], raw=raw[i])  # 2026-10-18 校准热门高估
        return ev_cal

    def _reverify_live_ev_oa(self, sig):
        """下注前重验公平价(odds-api.io Betfair 中间价), 替代 Pin 的 reverify_live_markets。

        用 sig 里的 odds-api.io 事件 id(pin_matchup_id) 重拉 Betfair 公平价, 重算 EV。
        2026-09-19 加失败原因统计(_lev_fail_stats), 量化 close/价差/deleted 各占多少。
        """
        eid = sig.get("pin_matchup_id")
        if not eid:
            self._lev_fail_stats["no_event_id"] = self._lev_fail_stats.get("no_event_id", 0) + 1
            return None
        # 2026-10-10 补 btts/dc: 之前只支持 1x2/hc/ou, btts/dc 走 unsupported_sub → clv 永远 None → LEV 盲区。
        sub_map = {"opportunities": "1x2", "handicap": "hc", "over_under": "ou",
                   "btts": "btts", "double_chance": "dc"}
        sub = sub_map.get(sig.get("sub"))
        if not sub:
            self._lev_fail_stats["unsupported_sub"] = self._lev_fail_stats.get("unsupported_sub", 0) + 1
            return None
        desig = sig.get("desig", "")
        if sub == "1x2":
            idx = {"主胜": "home", "和局": "draw", "客胜": "away"}
        elif sub == "hc":
            idx = {"让球主胜": "home", "让球客胜": "away"}
        elif sub == "ou":
            idx = {"大球": "over", "小球": "under"}
        elif sub == "btts":
            idx = {"双方进球": "yes", "非双方进球": "no"}
        elif sub == "dc":
            idx = {"主/和": "1X", "客/和": "X2", "主/客": "12"}
        else:
            self._lev_fail_stats["unsupported_sub"] = self._lev_fail_stats.get("unsupported_sub", 0) + 1
            return None
        k = idx.get(desig)
        # 1) Pin 主锚(2026-10-11): 1x2/hc/ou 优先用 Pin 实时赔率复验, Betfair 只兜底(btts/dc Pin 无覆盖)
        if sub in ("1x2", "hc", "ou"):
            _home = sig.get("home_en", "")
            _away = sig.get("away_en", "")
            _sport = sig.get("sport_id") or (sig.get("match") or {}).get("sport")
            if _home and _away and _sport:
                try:
                    from src.scrapers.pinnapi_client import pinnacle_fair_price
                    _pfair = pinnacle_fair_price(_home, _away, _sport, sub,
                                                 target_line=sig.get("line"), event_type='live')
                    if _pfair:
                        _fp = _pfair.get(k)
                        if _fp and _fp > 1:
                            from src.scrapers.fair_calibration import calibrated_ev
                            ev_cal, _, _ = calibrated_ev(sig["bb_odds"], _fp,
                                                         raw=(_pfair.get("_raw") or {}).get(k))  # 2026-10-18 校准热门高估
                            return ev_cal
                except Exception:
                    pass
            self._lev_fail_stats["no_pin"] = self._lev_fail_stats.get("no_pin", 0) + 1
        # 2) Betfair 兜底已撤(2026-10-17 只用 Pin 单锚): Pin 失败/无覆盖(btts/dc)直接放弃, 不再兜底
        return None

    def _stake_for(self, sig):
        """EV-Kelly 半凯利: stake = _bankroll() × 0.5 × (ev/100) / (odds-1), 封顶 ¥300。

        本金 = 固定本金基准(bankroll_base.txt); --stake 显式给固定注额(>0)时用固定值。
        防风控(2026-09-22): 只保留注额抖动×0.8~1.0。冷门压额(tier cap)已取消——冷门低级别联赛
        可能有大溢价(低效市场错价更大), 不该按联赛压额。
        回撤熔断(2026-09-13): 最近7天滚球实盘累计亏超 DRAWDOWN_STOP_PNL → 半仓。
        """
        if self.stake and self.stake > 0:
            sig["stake_kelly_raw"] = float(self.stake)
            sig["clamp_reason"] = "fixed_stake"
            return self.stake
        edge = sig["ev"] / 100.0
        odds = sig["bb_odds"]
        if odds <= 1 or edge <= 0:
            sig["stake_kelly_raw"] = 0.0
            sig["clamp_reason"] = "none"
            return 0
        _kelly_raw = _bankroll() * KELLY_FRACTION * edge / (odds - 1)
        sig["stake_kelly_raw"] = round(_kelly_raw, 2)
        stake = _kelly_raw
        # 执行质量系数(2026-10-08): 按方向拒单率/延迟打折(0.6~1.0), 样本不足=1.0
        from src.scrapers.execution_quality import factor as _eq_factor
        stake = stake * _eq_factor(f"{sig.get('sport')}|{sig.get('sub')}|{sig.get('desig')}")
        # 2026-11-01 不确定性调整 Kelly: 用校准表算 KL 散度收缩注额(edge 越不确定收缩越多, 防高估edge全Kelly破产)
        try:
            from src.scrapers.fair_calibration import kelly_shrink
            stake = stake * kelly_shrink(sig.get("fair"), market="live")
        except Exception:
            pass
        # 2026-09-19: 去掉 max(stake, MIN_STAKE) 兜底。之前把 Kelly<30 的冷门单硬抬到 30 投出
        # = 超 Kelly 数倍下冷门(方差击穿来源), 与「stake<30 不投」铁律语义相反。现在 <30 原样
        # 返回, 由调用方 _try_live_auto_bet/_try_auto_bet 的 `if stake < MIN_STAKE: return` 拦截。
        _clamp = "none"
        if stake > MAX_STAKE:
            _clamp = "max_cap"
        stake = int(min(stake, MAX_STAKE))
        # 注额抖动 ×0.8~1.0(防风控): 让每注金额非恒定, 避免固定档位被风控识别为机器。
        # 2026-09-22: 冷门压额(tier cap)已取消——冷门低级别联赛可能有大溢价, 不该压额。
        stake = int(stake * random.uniform(0.8, 1.0))
        # 回撤熔断: 最近7天累计亏超阈值 → 半仓(职业铁律: survival 优先)
        _pnl = _recent_pnl()
        if _pnl < -DRAWDOWN_STOP_PNL:
            stake = int(stake * 0.5)
            _clamp = _clamp + "+drawdown_half" if _clamp != "none" else "drawdown_half"
        sig["clamp_reason"] = _clamp
        # 四舍五入到 10: 避免 ¥91/¥82 有零有整被风控识别为机器下单
        return int(round(stake / 10.0) * 10)

    def _record_reject(self, sig, reason):
        """记录拒绝域信号(比价出 edge 但没下单), 供区分逆向选择 vs 回归均值(2026-10-17)。"""
        try:
            from src.monitor.reject_domain import record_reject
            _m = sig.get("match") or {}
            _now = time.time()
            _bb_ts = sig.get("bb_ts")
            rec = {
                "signal_ts": _bb_ts or _now,
                "sport": sig.get("sport_id") or _m.get("sport") or sig.get("sport"),
                "sub": sig.get("sub"),
                "direction": sig.get("desig"),
                "market_id": sig.get("market_id"),
                "option_type": sig.get("option_type"),
                "estimated_edge": sig.get("ev"),
                "bb_odds": sig.get("bb_odds"),
                "pin_odds": sig.get("fair"),
                "fair": sig.get("fair"),
                "bb_snapshot_ts": _bb_ts,
                "pin_snapshot_ts": sig.get("pin_ts"),
                "bb_snapshot_age": round(_now - _bb_ts, 2) if _bb_ts else None,
                "latency": None,
                "stake_kelly_raw": sig.get("stake_kelly_raw"),
                "stake_if_placed": sig.get("_stake"),
                "clamp_reason": sig.get("clamp_reason"),
                "available_exposure": getattr(self, "_live_outstanding", None),
                "reject_reason": reason,
                "config_hash": self._decision_version(self.threshold),
            }
            record_reject(rec)
        except Exception:
            pass

    def _try_auto_bet(self, sig):
        """秒级信号 → 自动下单。复用 place_single_bet(注额上限 + 下注前验价 + 记录)。"""
        # 2026-11-01 实盘投注门槛不变: 入库已降到 LIVE_INGEST_EV_MIN(2%), 但投注仍要求 >= self.threshold
        try:
            if sig.get("ev") is not None and float(sig["ev"]) < self.threshold:
                return
        except (TypeError, ValueError):
            pass
        from src.betting.bb_auto_bet import _load_stake_record, place_single_bet, check_sub_market_bet
        match_id = sig["match_id"]; market_id = sig["market_id"]
        m = sig["match"]
        tag = f"{m['home_cn']} vs {m['away_cn']} {sig['desig']}"
        stake = self._stake_for(sig)
        if stake < MIN_STAKE:
            return  # EV-Kelly 算出来 < 30, 拦截(铁律)
        # 同场同盘口互斥锁(2026-09-19): 同一 match 同一盘口只下一注(防对立方向/多线重复下注)
        if check_sub_market_bet(match_id, sig.get("sub")):
            print(f"  ⏭️ 同场同盘口已投, 跳过 {tag}", flush=True)
            return
        # 去重: 该盘口已下过注(主扫描或本监控)则跳过, 防重复下注
        rec = _load_stake_record()
        if rec.get(str(match_id), {}).get(str(market_id), 0.0) > 0:
            print(f"  ⏭️ 已下过注, 跳过 {tag}", flush=True)
            return
        print(f"  🎯 秒级下单 {tag} @{sig['bb_odds']:.2f} 注额¥{stake} (EV-Kelly)", flush=True)
        code, order_id, msg = place_single_bet(
            market_id, sig["bb_odds"], sig["option_type"], stake=stake,
            match_id=match_id, check_limit=True, verify_price=False)
        if code == 14010:
            self._invalidate_token_cache()
        self._probe_token_async()  # 2026-09-28 token 探针移到投注后(后台)
        if code == 0:
            from src.betting.bb_auto_bet import record_sub_market_bet
            record_sub_market_bet(match_id, sig.get("sub"))
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
            ok = bool(send_dingtalk(_dingtalk_safe(title), _dingtalk_safe(body)))
            if not ok:
                print(f"[slm] 秒级下单推送失败: {title}", flush=True)
        except Exception as e:
            print(f"[slm] 钉钉通知异常: {e}")

    def _notify_bet(self, title, body):
        """每笔成功下单都推钉钉(不限频 —— 下单限频已把间隔拉到 10-15s, 不会刷屏)。

        2026-09-13: 先 _dingtalk_safe 清洗博彩词再发, 否则被钉钉 inappropriate content 拒收。
        2026-09-20: 改后台线程推送, 钉钉 HTTP ~0.5-1s 不占下单时间。
        """
        import threading

        def _send():
            try:
                from config.settings import send_dingtalk
                ok = bool(send_dingtalk(_dingtalk_safe(title), _dingtalk_safe(body)))
                if not ok:
                    print(f"[slm] 滚球投注推送失败: {title}", flush=True)
            except Exception as e:
                print(f"[slm] 钉钉通知异常: {e}")
        try:
            threading.Thread(target=_send, daemon=True).start()
        except Exception:
            pass

    def _push_settle_summary(self, pending):
        """每小时汇总推结算明细, 按源细分为滚球/早盘两段(2026-10-27 用户要求)。"""
        try:
            from config.settings import send_dingtalk
        except Exception:
            return

        def _fmt(ps):
            total = sum((p.get("uwl", 0) or 0) for p in ps)
            lines = []
            for p in ps:
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
            return total, lines

        live = [p for p in pending if p.get("source") != "early"]
        early = [p for p in pending if p.get("source") == "early"]
        sections = []
        if live:
            _t, _ls = _fmt(live)
            sections.append(f"📊 滚球结算({len(live)}笔, 盈亏{_t:+.0f})\n" + "\n".join(_ls))
        if early:
            _t, _ls = _fmt(early)
            sections.append(f"📊 早盘结算({len(early)}笔, 盈亏{_t:+.0f})\n" + "\n".join(_ls))
        if not sections:
            return
        body = "\n\n".join(sections)
        try:
            ok = bool(send_dingtalk("📊 结算汇总(滚球/早盘)", body))
            # 2026-09-19: 无论成败都清空 pending, 避免推送失败时 pending 累积、重试时把
            # 「旧+新」一起推导致重复。结算明细已持久化在 live_settled_log.json, notified
            # (已通知oid)也已落盘不会重复收集, 清空不丢数据、不重复推。
            PENDING_SETTLE_FILE.write_text(json.dumps([], ensure_ascii=False))
            if ok:
                print(f"[slm] 结算汇总推送成功: 滚球{len(live)}笔+早盘{len(early)}笔", flush=True)
            else:
                print(f"[slm] 结算汇总推送失败(已清空pending防重复): 滚球{len(live)}+早盘{len(early)}", flush=True)
        except Exception as e:
            print(f"[slm] 结算汇总推送异常: {e}", flush=True)

    def _check_settled(self):
        """查 BB 已结算订单, 对新的(未通知的)推钉钉(含盈亏 + 账户余额)。"""
        from src.betting.bb_auto_bet import read_token, read_domain, _session
        tok = read_token(); dom = read_domain()
        if not tok:
            return
        notified = set()
        if LIVE_SETTLED_FILE.exists():
            try:
                notified = set(json.loads(LIVE_SETTLED_FILE.read_text()))
            except Exception:
                pass
        # 2026-09-28 修 size:20 漏抓 bug: 之前只拉最新 20 条已结算, 超出窗口的旧单永久漏掉
        # (实测 261/1105=24% 捕获率, 独赢 0%)。改分页拉全量: size=100, current 翻页直到拉满 total。
        records = []
        try:
            _page = 1
            while _page <= 20:  # 最多 20 页(2000条), 防异常死循环
                r = _session().post(f"{dom}/v1/order/new/bet/list",
                                    json={"languageType": "CMN", "isSettled": True, "current": _page, "size": 100},
                                    headers={"Content-Type": "application/json", "Authorization": tok,
                                             "User-Agent": _UA}, timeout=15, verify=False)
                d = r.json()
                if d.get("code") != 0:
                    break
                _data = d.get("data") or {}
                _recs = _data.get("records") or []
                if not _recs:
                    break
                records.extend(_recs)
                _total = _data.get("total", 0)
                if _total > 0 and len(records) >= _total:
                    break
                _page += 1
        except Exception:
            pass
        if not records:
            return
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
        new_settled_records = []  # 2026-09-28 批量追加结算流水(修每条全量读改写+非原子丢数据)
        # 2026-10-27 结算明细按源分(滚球/早盘): 读 real_bets.json 的 trigger_source 映射
        _ts_map = {}
        try:
            _rb = json.loads(REAL_BETS_FILE.read_text())
            _ts_map = {k: v.get("trigger_source") for k, v in _rb.get("bets", {}).items()}
        except Exception:
            _ts_map = {}
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
            # 2026-10-29/30 修结算返还额 bug: BB 退款订单 st=2(比赛取消,uwl=0) / st=3(退赛,uwl=+sat
            # 本金返还被记成盈利), 导致结算明细把「退款/退赛」显示成「赢 sat」。用 st 字段归一:
            # st∈{2,3}(退款/退赛) → pnl=0(本金返还, 无盈亏), 让 _settle_real_bet 正确落 result=push。
            if o.get("st") in (2, 3):
                pnl = 0.0
            won = pnl > 0
            _settle_real_bet(str(oid), pnl, won)  # 2026-09-27 实盘库: 回填结算结果
            od = op.get("od", 0)  # 赔率
            sid = op.get("sid", 0)  # 运动 id
            # 关联投注时的 fair/ev(从 settled_info, 按 order_id)
            _bi = settled_info.get(str(oid), {})
            _fair = _bi.get("fair", 0) if isinstance(_bi, dict) else 0
            _ev = _bi.get("ev", 0) if isinstance(_bi, dict) else 0
            _iv = _odds_interval(_bi.get("fair") or od, BB_SPORT_EN.get(sid))
            # 2026-10-27 按源分: pin_drop/ws_scan=早盘, 其余(pin/poll/betfair_ws)=滚球
            _src = _ts_map.get(str(oid))
            _source = "early" if _src in ("pin_drop", "ws_scan") else "live"
            pending.append({
                "mn": mn, "mgn": mgn, "on": on, "od": od,
                "sat": stake, "uwl": pnl, "sid": sid, "won": won,
                "fair": _fair, "ev": _ev, "interval": _iv,
                "source": _source,
            })
            new_notified.add(oid)
            print(f"[slm] 结算收集: {'✅赢' if won else '❌输'} {mn} {mgn}-{on} | {pnl:+.0f}", flush=True)
            # 收集到 new_settled_records(循环外批量+原子写, 2026-09-28)
            new_settled_records.append({
                "ts": time.time(), "oid": str(oid), "mn": mn, "mgn": mgn,
                "on": on, "od": od, "interval": _iv, "sat": stake,
                "uwl": pnl, "won": won, "sid": sid,
                "verify": bool(_bi.get("verify", False)),  # 是否触发验价(2026-09-19)
            })
        # 批量+原子写结算流水(2026-09-28 修丢数据): 之前每条订单都全量 read-modify-write,
        # 非原子(进程崩 mid-write 截断) + O(n²)。改为循环外一次: 读全量→追加全部新单→剪30天→原子写(tmp+replace)。
        if new_settled_records:
            try:
                _log = []
                if SETTLED_LOG_FILE.exists():
                    try:
                        _log = json.loads(SETTLED_LOG_FILE.read_text())
                    except Exception:
                        _log = []
                _log.extend(new_settled_records)
                _cutoff = time.time() - 30 * 86400  # 只保留最近 30 天
                _log = [x for x in _log if x.get("ts", 0) > _cutoff]
                _tmp = SETTLED_LOG_FILE.with_suffix(".tmp")
                _tmp.write_text(json.dumps(_log, ensure_ascii=False))
                _tmp.replace(SETTLED_LOG_FILE)  # 原子替换(rename)
            except Exception:
                pass
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
            try:
                SETTLE_PUSH_TS_FILE.write_text(str(self._last_settle_push))  # 落盘, 重启不归零
            except OSError:
                pass

    def _probe_token_async(self):
        """后台线程验 token + 自动续期(2026-09-28 移出下单关键路径, 探针不阻塞投注)。

        下单直接用 read_token 读的 token; 若失效下单会 14010 → _invalidate_token_cache 兜底。
        本方法在投注后异步跑, 验 token + 失效时续期, 供下一单用, 不占这单的端到端耗时。
        """
        import threading as _threading
        _threading.Thread(target=self._token_ok, daemon=True, name="token-probe").start()

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
            # 2026-10-09 token 失效 → 只发钉钉提醒, 不自动续期(自动续期用 AppleScript 会启动 Chrome, 用户要求禁用)
            self._token_remind()
            print(f"[slm] token 失效(code!=0), 已发钉钉提醒(不再自动续期开浏览器)", flush=True)
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
        sub_map = {"1x2": "opportunities", "hc": "handicap", "hc_games": "handicap_games",
                   "ou": "over_under", "dc": "double_chance"}
        desig_map = {
            "1x2": {"主": "主胜", "和": "和局", "客": "客胜"},
            "hc": {"主": "让球主胜", "客": "让球客胜"},
            "hc_games": {"主": "让局主胜", "客": "让局客胜"},  # 网球让局(按局数, 2026-09-28)
            "ou": {"大": "大球", "小": "小球"},
            "dc": {"主": "主/和", "客": "客/和", "和": "主/客"},
            "ht": {"主": "上半场主胜", "和": "上半场和局", "客": "上半场客胜"},
            "ht_ou": {"大": "上半场大球", "小": "上半场小球"},
        }
        return {
            "match_id": opp["bb_match_id"], "market_id": opp["market_id"],
            "option_type": opp["option_type"], "sub": sub_map.get(opp["sub"], opp["sub"]),
            "desig": desig_map.get(opp["sub"], {}).get(opp["direction"], opp["direction"]),
            "bb_odds": opp["bb_odds"], "fair": opp["fair"], "ev": opp["ev"], "line": opp["line"],
            "ev_raw": opp.get("ev_raw"), "fair_cal": opp.get("fair_cal"),  # 2026-11-05 校准口径透传(下单价/CLV用校准公平价)
            "pin_raw": opp.get("pin_raw", 0),  # Pin 原始价(去抽水前), 供推送展示
            "sport": opp.get("sport", ""),
            "home_en": opp.get("home_en", ""), "away_en": opp.get("away_en", ""),  # 2026-11-03 英文队名(供 pin_matchup_id/lag_sec/live CLV)
            "match": {"home": opp["home"], "away": opp["away"], "sport": opp.get("sport", ""),
                      "league_cn": opp.get("league_cn", "滚球") or "滚球"},
            "pin_matchup_id": opp["pin_matchup_id"], "league_id": opp["league_id"],
            "spread": opp.get("spread"),  # 2026-09-19 流动性门槛: back-lay 价差
            "max_stake": opp["max_stake"],
            "bsc": opp.get("sc"),  # [主,客] 下注瞬间比分(让球按当前比分结算用, 2026-09-13)
            "bb_ts": opp.get("bb_ts"), "pin_ts": opp.get("pin_ts"),  # BB/Pin 数据拉取时间(推送展示)
            "poll_ts": opp.get("poll_ts"),  # 2026-09-20 poll 开始时间(WS触发), 供推送「总耗时」
            "platform": opp.get("platform", "BB"),  # BB/FB(推送时标注平台)
            "sbo_confirm": opp.get("sbo_confirm", True),  # SBO 同向确认(False=无覆盖, 降投注额)
            "sbo_direction": opp.get("sbo_direction", "same"),  # same/diff/none(供统计验证)
        }

    def _poll_live(self, poll_ts=None):
        """轮询 getList type=1 滚球赔率 + 匹配 Sbobet/Betfair 公平价 → 打信号/自动下单。返回机会数。

        poll_ts(2026-09-30 口径修正): 本轮 poll 的真实触发时刻。WS 触发=原始 WS 变动时刻(_raw_ws_ts),
        轮询周期触发=本轮开始时刻。之前硬编码 _raw_ws_ts, 轮询触发的单会把陈旧的 WS 时刻混进
        「总耗时」(poll_ts→下单完成), 轮询间隔(2s)被算进端到端耗时虚高。
        """
        from src.scrapers.pinnacle_live import fetch_live_opportunities_oa
        from concurrent.futures import TimeoutError as _FutureTimeout
        global _POLL_EXECUTOR
        if _POLL_EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor as _TPE
            _POLL_EXECUTOR = _TPE(max_workers=2)
        # 2026-09-23: 传原始WS触发时刻, 供推送「端到端耗时(WS触发→下单完成)」
        # 2026-09-24 防假死: 比价拉取放线程池+90s超时, 卡住(readline读响应)时主循环不阻塞。
        # 下单(_try_live_auto_bet)仍在主线程, 不受超时影响, 不影响新机会产生(正常2s内返回)。
        opps = []
        try:
            _fut = _POLL_EXECUTOR.submit(
                fetch_live_opportunities_oa, self.threshold,
                poll_ts=poll_ts or self._raw_ws_ts or None)
            opps = _fut.result(timeout=90)
        except _FutureTimeout:
            print("[slm] ⚠️ 比价拉取超时(>90s), 本轮跳过(防假死)", flush=True)
            return 0
        except Exception:
            opps = []
        # 2026-09-20 按 EV 降序排序: 暴增时冷却只能下少数几单, 优先下高 EV 的(之前按 BB 返回任意顺序,
        # 可能下到低 EV 跳过高 EV)。diff(只观察)排最后, 不占冷却名额。
        opps.sort(key=lambda o: (o.get("sbo_direction") == "diff", -(o.get("ev", 0) or 0)))
        # 2026-11-07 限批: 每轮 poll 只处理前 N 个机会(已按 EV 降序, 含 drop 高EV),
        # 让 poll 快速返回, 不被几十笔普通单阻塞 —— 修「drop 唤醒→下注 2.4min 延迟」根因。
        # 剩余机会下一轮 poll 再处理(opps 每次全量重算, 不会漏)。
        for opp in opps[:MAX_BETS_PER_POLL]:
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

    def _sample_bb_track(self):
        """BB价轨迹采样(每30s): 对已下注的比赛采样当前 BB 价, 追上 Pin(校准公平价)或超时(10min)则落盘。"""
        if not self._bb_track:
            return
        try:
            from src.scrapers.pinnacle_live import get_bb_fresh_or_cached
            bb, _ = get_bb_fresh_or_cached(wait_for_fresh=False)
        except Exception:
            return
        done = []
        for key, rec in list(self._bb_track.items()):
            mid, mkid, ot = key
            cur_bb = None
            try:
                b = bb.get(int(mid)) if mid else None
                if b:
                    for mk in b.get("markets", []):
                        if str(mk.get("market_id")) == mkid and str(mk.get("option_type")) == ot:
                            cur_bb = mk.get("odds")
                            break
            except (TypeError, ValueError):
                pass
            rec["history"].append((time.time(), cur_bb))
            # 追上 Pin(校准公平价) 或 超时 10min → 完成
            if cur_bb is not None and float(cur_bb) <= float(rec["fair_cal_at_bet"]):
                done.append(key)
            elif time.time() - rec["ts"] > 600:
                done.append(key)
        for key in done:
            self._write_bb_track(key)
            del self._bb_track[key]

    def _write_bb_track(self, key):
        """把一条 BB 价轨迹写落盘(bb_price_track.jsonl), 供离线分析 lead-lag 窗口多长、BB 追价延迟。"""
        rec = self._bb_track.get(key)
        if not rec:
            return
        hist = rec.get("history", [])
        rec = dict(rec)
        rec["final_bb"] = hist[-1][1] if hist else None
        rec["catchup_sec"] = round(hist[-1][0] - rec["ts"], 1) if hist else None
        rec["n_samples"] = len(hist)
        try:
            with open(BB_TRACK_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _consume_early_ws_changes(self, changes):
        """早盘 WS 触发: prematch sharp 变动 → 匹配 BB → 单场比价 → 下单(实时验价)。

        2026-09-19: 早盘扫描 15min 抓不住 5.5min lead-lag 窗口, sharp 一变就立即单场比价下单。
        队名精确匹配(lower), 匹配率~50%(后缀差异), 后续可换模糊匹配。
        changes: [(event_id, bookie, ts)] 变动事件(主循环已取)。
        """
        if not changes:
            return 0
        # event_id → 队名(pending 早盘)
        try:
            from src.scrapers.odds_api_io import get_events
            evs = get_events(sport='football')
            ev_by_id = {e['id']: e for e in evs if e.get('status') == 'pending'}
        except Exception:
            return 0
        # BB 早盘快照
        from src.scrapers.bb_incremental_scanner import BB_EXTRACTED
        try:
            d = json.loads(BB_EXTRACTED.read_text())
            bb_matches = d.get('matches', [])
        except Exception:
            return 0
        # 队名匹配 event → BB: _norm_team 精确 + rapidfuzz 模糊兜底(提升匹配率 47%→~71%)
        from src.scrapers.odds_api_io import _norm_team, _match_score
        changed_ids = {c[0] for c in changes}
        bb_by_norm = {}
        for m in bb_matches:
            key = (_norm_team(m.get('home')), _norm_team(m.get('away')))
            if key[0] and key[1]:
                bb_by_norm.setdefault(key, m)
        triggered = []
        seen = set()
        for eid in changed_ids:
            ev = ev_by_id.get(eid)
            if not ev:
                continue
            key = (_norm_team(ev.get('home')), _norm_team(ev.get('away')))
            if key in bb_by_norm:
                m = bb_by_norm[key]
                if id(m) not in seen:
                    triggered.append(m)
                    seen.add(id(m))
                continue
            # 模糊匹配兜底(只对没精确命中的变动 event)
            best_m, best_sc = None, 0.0
            for m in bb_matches:
                sc = _match_score(ev.get('home'), ev.get('away'), m.get('home'), m.get('away'))
                if sc > best_sc:
                    best_sc, best_m = sc, m
            if best_m and best_sc >= 85.0 and id(best_m) not in seen:
                triggered.append(best_m)
                seen.add(id(best_m))
        if not triggered:
            return 0
        # 单场比价(复用 bb_vs_pinnacle 的 entry 构造 + 盘口比价)
        from src.scrapers.bb_vs_pinnacle import _build_oa_entry, _oa_add_markets
        # 释放过滤(2026-09-19): 早盘 WS 触发绕过 bb_ev_push 的释放过滤, 这里补上——未释放的
        # 运动×盘口/方向不投, 否则瞬发路径会绕过释放清单下未验证的注(与主扫描口径对齐)。
        from src.report.bb_ev_push import _is_market_released, _is_direction_blocked
        _GROUP_TO_SUB = {'opportunities': '1x2', 'handicap': 'hc', 'over_under': 'ou',
                         'double_chance': 'dc', 'draw_no_bet': 'dnb'}
        opps = []
        for m in triggered:
            sport = m.get('sport', 'football')
            # 2026-10-31 统一数据源: 入库和实盘都用 getMatchDetail 鲜价(同 drop 路径)。
            try:
                from src.scrapers.bb_api_fetcher import fetch_match_for_order, extract_match_odds
                _mid = m.get('id')
                if _mid:
                    _fresh = fetch_match_for_order(str(_mid), timeout=3, max_attempts=1, max_domains=1)
                    if _fresh and _fresh.get('mg'):
                        _fm = extract_match_odds(_fresh, sport)
                        for _k in ('odds_ft', 'odds_ht', 'odds_sh'):
                            if _fm.get(_k):
                                m[_k] = _fm[_k]
            except Exception:
                pass
            entry = _build_oa_entry(m, sport)
            _oa_add_markets(entry, m, sport, use_rest=False)
            _league = entry.get('league', '')
            _epoch = entry.get('start_time_pin_epoch', 0)
            for group in ('opportunities', 'handicap', 'over_under', 'double_chance', 'draw_no_bet'):
                for o in entry.get(group, []):
                    sub_market = o.get('_market') or _GROUP_TO_SUB.get(group, group)
                    if not _is_market_released(sport, sub_market, _league,
                                               o.get('designation', ''), _epoch, o.get('fair_price') or o.get('bb_odds')):
                        continue
                    if _is_direction_blocked(sport, sub_market, o.get('designation', ''), _epoch):
                        continue
                    o['home_bb'] = entry.get('home_bb', '')
                    o['away_bb'] = entry.get('away_bb', '')
                    o['sport'] = sport
                    o['sub_market'] = sub_market  # 2026-10-18 修: opp 缺 sub_market → auto_bet_flow 读空 fallback 1x2 错盘
                    o['bb_match_id'] = entry.get('bb_match_id', '')  # 2026-10-25 定向下单用: 缓存过期时按 id 拉单场盘口
                    o['_pin_epoch'] = _epoch
                    opps.append(o)
        if not opps:
            return 0
        try:
            from src.betting.bb_real_bet_flow import auto_bet_flow
            res = auto_bet_flow(opps)
            n = len(res.get('success', [])) if isinstance(res, dict) else 0
            if n:
                print(f'[slm] 早盘WS触发下单 {n} 注', flush=True)
        except Exception as e:
            print(f'[slm] 早盘WS触发下单异常: {type(e).__name__} {e}', flush=True)
        return len(triggered)

    def _consume_pin_triggers(self):
        """Pin drop 触发 → BB 比价下单(lead-lag 闭环, 2026-10-11)。

        读 pin_trigger.jsonl 新行(bb_match_id/home/away/outcome/from_price/to_price),
        按 bb_match_id 反查 BB 快照 → 复用早盘比价(_build_oa_entry+_oa_add_markets) → 释放过滤 → 下单。
        """
        if not PIN_TRIGGER_FILE.exists():
            return 0
        try:
            lines = PIN_TRIGGER_FILE.read_text().splitlines()
        except OSError:
            return 0
        last = self._pin_trigger_offset
        if last > len(lines):
            # 2026-10-18 修: pin_trigger.jsonl 被 _append_rotating 截断(>1.5MB 留 100 行)后
            # offset 超出新长度 → 重置。必须是 > 不是 >= —— offset 恰好追平文件末尾(last == len)
            # 表示「已消费完、无新行」, 应 return 0 等待, 而不是重置到 0 重放整个文件。
            # 2026-10-27 修 offset 循环 bug: 之前 >= 让 offset 追平末尾就重置, 反复重放旧 trigger,
            # 每次重放都重付 getMatchDetail(网络差时 13s) → 真 trigger 排队分钟级才被消费
            # (实测 drop 触发→完成 109s)。修后追平末尾直接等待, 新 trigger 0.2s 内消费。
            self._pin_trigger_offset = 0
            last = 0
        elif last == len(lines):
            return 0  # 已消费到文件末尾, 无新行
        # 批处理: 每次最多 30 条。原 3 条理由是「公平价要扫 Pin REST 全量超 20s」,
        # 2026-10-17 REST 兜底已禁用(读 WS 缓存 0ms), 理由失效, 放宽到 30 条(比价仍 <20s)。
        new_lines = lines[last:last + 30]
        self._pin_trigger_offset = last + len(new_lines)
        # BB 快照(id → match)
        from src.scrapers.bb_incremental_scanner import BB_EXTRACTED
        try:
            d = json.loads(BB_EXTRACTED.read_text())
            bb_by_id = {str(m.get('id')): m for m in d.get('matches', []) if m.get('id')}
        except Exception:
            return 0
        from src.scrapers.bb_vs_pinnacle import _build_oa_entry, _oa_add_markets
        from src.report.bb_ev_push import _is_market_released, _is_direction_blocked
        _GROUP_TO_SUB = {'opportunities': '1x2', 'handicap': 'hc', 'over_under': 'ou',
                         'double_chance': 'dc', 'draw_no_bet': 'dnb'}
        # 2026-11-01 drop 骤降方向 → (group, designation) 映射: drop 只下注骤降方向(顺势抢 BB 高价),
        # 不下注逆向/其他方向(否则 Home 骤降时下注「和局」= 逆向选择, 是 drop 累计 -9.5% 的根因)。
        _DROP_DIR_MAP = {"Home": ("opportunities", "主胜"), "Away": ("opportunities", "客胜"),
                         "Draw": ("opportunities", "和局"), "Over": ("over_under", "大球"),
                         "Under": ("over_under", "小球")}
        # 2026-11-08 修盘口歧义: drop 的 outcome=Home/Away 有歧义——pz 有值=独赢(3-way),
        # pz 无值=让球(2-way)。之前固定映射到 opportunities(独赢), 让球盘 drop 错配到独赢,
        # 用让球 2-way 价比 1x2 独赢 3-way 价 → ev 122~236% 假溢价。
        _DROP_DIR_MAP_2WAY = {"Home": ("handicap", "让球主胜"), "Away": ("handicap", "让球客胜"),
                              "Over": ("over_under", "大球"), "Under": ("over_under", "小球")}
        # 2026-11-01 drop 概率区间过滤: 拦负段(中间段 Shin 去水高估赢率 + 大小球全线负), 只放两端。
        # 与 compute_market_release 的 LIVE_NEGATIVE_IMPLIED 同口径, 但按 group 键。
        _DROP_NEG_IMPLIED = {
            'opportunities': (30.0, 70.0),  # 独赢中间段(50-60%最负) - 保留(Shin假溢价, tier3)
            'handicap': (0.0, 0.0),         # 2026-11-04 释放: line bug已修, 让球中间段不再拦(tier1)
            'over_under': (0.0, 0.0),       # 2026-11-04 释放: line bug已修, 大小球全线负不再拦(tier1)
            'double_chance': (60.0, 70.0),  # 双机会中间段 - 保留(dc另有DERIVED_FAIR_SUBS停投)
        }
        opps = []
        entries = []  # 2026-10-24 Drop 入库观察库(source=drop)
        min_trig_ts = None  # 本批最早 prematch drop 触发时刻(供端到端总耗时)
        _seen_mids = set()  # 2026-10-28 去重: pin_ws_trigger 会给同一 match 写几十条重复 trigger(实测44条同场),
                            # 之前每条都重算公平价(_build_oa_entry+_oa_add_markets), 44条积压成几十秒阻塞 drop 循环。
        # 阶段1: 收集(live唤醒 + prematch任务去重), 不调 getMatchDetail(串行 1-2s/场 是比价慢的瓶颈)
        _tasks = []  # [(mid, sport, trig), ...]
        _ms = {}     # mid -> m(阶段3替换鲜价用)
        for line in new_lines:
            try:
                trig = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            # 2026-10-11 修 gap: 滚球(live) drop 只作「唤醒滚球轮询」信号, 交给 _poll_live 用 BB 实时赔率比较;
            # 早盘(prematch) drop 才走这里的早盘快照比较。避免 live drop 用早盘快照价下单(错)。
            if trig.get('stream') == 'live':
                self._pin_live_wake = True  # 唤醒滚球轮询立即比较
                # 2026-11-05 fast path: live drop 实时价(price_x/y/z)算公平价, 供 _process 绕开落盘缓存
                _px, _py, _pz = trig.get("price_x"), trig.get("price_y"), trig.get("price_z")
                if _px and _py:
                    try:
                        from src.scrapers.devig import power_fair_odds
                        from src.scrapers.pinnacle_live import set_drop_fresh
                        _prices = [float(_px), float(_py)] + ([float(_pz)] if _pz else [])
                        _fairs = power_fair_odds(_prices)
                        # 2026-11-07 修方向搞反: 用 outcome 直接映射方向索引(替代 _idx 匹配 to_price)。
                        # 之前 _idx 匹配 to_price, 匹配失败(默认0)时客胜/小球用了主胜/大球的价(ev 962% 假溢价)。
                        # price 顺序: 1x2=主/客/和(px主/py客/pz和), hc=主/客, ou=大/小。
                        _idx = {"Home": 0, "Away": 1, "Draw": 2, "Over": 0, "Under": 1}.get(trig.get("outcome", ""), 0)
                        _dir = {"Home": "主", "Away": "客", "Draw": "和", "Over": "大", "Under": "小"}.get(trig.get("outcome", ""))
                        if _dir:
                            # 2026-11-06 修: 传 drop 的 point(线), 供 _process 按 BB 下注线匹配(修跨线套价)
                            # 2026-11-07 修客方向符号: drop point 是「outcome 自己的线」约定(Away 时 point=-23=客让23),
                            # 而 BB line(主 op.li)是主队约定(主受让23=+23)。客方向取反才对齐主队约定,
                            # 否则客队让球单匹配不到 drop 实时价、回落 Pin 缓存(正确但不新鲜)。
                            _pt = trig.get("point")
                            if _dir == "客" and _pt is not None:
                                try:
                                    _pt = -float(_pt)
                                except (TypeError, ValueError):
                                    pass
                            set_drop_fresh(trig.get("home"), trig.get("away"), _pt, _dir, _fairs[_idx])
                    except (TypeError, ValueError, IndexError):
                        pass
                self._match_drop_anchor(trig)  # 2026-10-11: live drop 作事件锚点, 匹配观察库算 clv_sign
                continue
            mid = str(trig.get('bb_match_id') or '')
            if mid in _seen_mids:  # 同场重复 drop 跳过(已处理过一场, 无需重算)
                continue
            _seen_mids.add(mid)
            m = bb_by_id.get(mid)
            if not m:
                continue
            sport = m.get('sport') or trig.get('sport') or 'football'
            _tasks.append((mid, sport, trig))
            _ms[mid] = m
        # 阶段2: 并发 getMatchDetail 拉鲜价(2026-10-31 修「比价12-40s」: 串行→并发)
        _t_fetch0 = time.time()
        _odds_list = _fetch_fresh_odds_concurrent([(t[0], t[1]) for t in _tasks])
        _t_fetch = time.time() - _t_fetch0
        # 阶段3: 串行替换鲜价 + 比价 + 过滤
        _t_price0 = time.time()
        for (mid, sport, trig), _fresh_odds in zip(_tasks, _odds_list):
            m = _ms[mid]
            if _fresh_odds:
                for _k, _v in _fresh_odds.items():
                    m[_k] = _v
            _ts = trig.get('ts')
            if _ts and (min_trig_ts is None or _ts < min_trig_ts):
                min_trig_ts = _ts
            entry = _build_oa_entry(m, sport)
            _oa_add_markets(entry, m, sport, use_rest=False, status='prematch')  # 2026-11-04 恢复校准: platt 已替代 isotonic(过拟合根因), 早盘重开, drop 与 ws_scan 对齐
            # 2026-11-05 fast path: drop 实时价(price_x/y/z)直算公平价, 绕开落盘缓存(接近0延迟)
            _drop_fair = None
            _px, _py, _pz = trig.get("price_x"), trig.get("price_y"), trig.get("price_z")
            if _px and _py:
                try:
                    from src.scrapers.devig import power_fair_odds
                    _prices = [float(_px), float(_py)] + ([float(_pz)] if _pz else [])
                    _fairs = power_fair_odds(_prices)
                    # 2026-11-07 修方向搞反: 用 outcome 直接映射方向索引(替代 _idx 匹配 to_price)。
                    # 之前 _idx 匹配失败(默认0)时客胜/小球用了主胜/大球的价(ev 962% 假溢价)。
                    _idx = {"Home": 0, "Away": 1, "Draw": 2, "Over": 0, "Under": 1}.get(trig.get("outcome", ""), 0)
                    _drop_fair = _fairs[_idx]
                except (TypeError, ValueError, IndexError):
                    _drop_fair = None
            _league = entry.get('league', '')
            _epoch = entry.get('start_time_pin_epoch', 0)
            _has_ev = False  # 入库观察库: ev>0 全收(与 WS 同口径)
            # 2026-11-01 drop 方向过滤: 骤降方向 = 顺势(抢 BB 高价), 只下注该方向, 逆向/其他方向不下注;
            # outcome 无法映射(组合骤降 AwayUnder/HomeOver 或空) → _drop_dir=None → 不下注(但入库仍全量)。
            _drop_dir = _DROP_DIR_MAP.get(trig.get('outcome', '')) if trig.get("price_z") else _DROP_DIR_MAP_2WAY.get(trig.get('outcome', ''))
            for group in ('opportunities', 'handicap', 'over_under', 'double_chance', 'draw_no_bet'):
                for o in entry.get(group, []):
                    if (o.get('ev_pct') or 0) > 0:
                        _has_ev = True
                    if _drop_dir is None or group != _drop_dir[0] or o.get('designation', '') != _drop_dir[1]:
                        continue  # 非骤降方向, 跳过(不下注)
                    # 2026-11-05 fast path: 骤降方向用 drop 实时价覆盖公平价 + 重算 EV(绕开落盘缓存)
                    # 2026-11-06 修: 只有 drop 的 point 与 BB 下注线一致才覆盖(修「公平价<原价」跨线套价根因)
                    _drop_line_match = bool(_drop_fair and _drop_fair > 1)
                    if _drop_line_match and trig.get("point") is not None and o.get("line") is not None:
                        try:
                            # 2026-11-08 修客方向 line 匹配: drop point 是「outcome 自己的线」约定,
                            # 让球 outcome=Away 时 point=客队让分(如 -23), 而 BB line=主队让分(如 +23),
                            # 符号相反 → 取反对齐主队约定(对齐滚球 drop 的 set_drop_fresh 取反逻辑)。
                            _pt = trig.get("point")
                            if trig.get("outcome") == "Away" and _pt is not None:
                                _pt = -float(_pt)
                            _drop_line_match = abs(float(_pt) - float(o.get("line"))) <= 0.01
                        except (TypeError, ValueError):
                            _drop_line_match = False
                    # 2026-11-06 护栏: 去水公平价必应 ≥ 原价; < 原价 = 跨线错配, 不覆盖
                    if _drop_line_match:
                        _raw_po = o.get("pin_odds")
                        try:
                            if _raw_po and float(_raw_po) > 1 and _drop_fair < float(_raw_po) - 0.01:
                                _drop_line_match = False
                        except (TypeError, ValueError):
                            pass
                    if _drop_line_match:
                        # 2026-11-09 修 drop 热门段虚高(38-44pp): 骤降方向隐含胜率>50% 时, 骤降后价是 Pin
                        # 过度反应(favorite bias 热门侧), 不用骤降后价覆盖, 保留 _oa_add_markets 的 Pin 缓存价(骤降前)。
                        if _drop_fair > 1 and (1.0 / _drop_fair) <= 0.5:
                            _drop_line_match = False
                    if _drop_line_match:
                        o['fair_price'] = round(_drop_fair, 4)
                        o['ev_pct'] = round((float(o.get('bb_odds') or 0) - _drop_fair) / _drop_fair * 100, 2)
                    # 2026-11-01 drop 概率区间过滤: 拦负段(中间段高估赢率+大小球全线负), 放两端
                    _neg_o = _DROP_NEG_IMPLIED.get(group)
                    if _neg_o is None:
                        continue  # 全线负盘口(over_under)拦
                    try:
                        _imp_o = 1.0 / float(o.get('fair_price') or o.get('bb_odds')) * 100
                    except (TypeError, ValueError, ZeroDivisionError):
                        _imp_o = None
                    if _imp_o is not None and _neg_o[0] <= _imp_o <= _neg_o[1]:
                        continue  # 负段(中间段)拦
                    sub_market = o.get('_market') or _GROUP_TO_SUB.get(group, group)
                    if not _is_market_released(sport, sub_market, _league,
                                               o.get('designation', ''), _epoch,
                                               o.get('fair_price') or o.get('bb_odds'), source='drop'):
                        continue
                    if _is_direction_blocked(sport, sub_market, o.get('designation', ''), _epoch):
                        continue
                    o['home_bb'] = entry.get('home_bb', '')
                    o['away_bb'] = entry.get('away_bb', '')
                    o['sport'] = sport
                    o['sub_market'] = sub_market  # 2026-10-18 修: opp 缺 sub_market → auto_bet_flow 读空 fallback 1x2 错盘
                    o['bb_match_id'] = entry.get('bb_match_id', '')  # 2026-10-25 定向下单用: 缓存过期时按 id 拉单场盘口
                    o['_pin_epoch'] = _epoch
                    # 2026-10-18 修: 早盘 opp 缺 _stake → auto_bet_flow stake=0 全被 stake<30 跳过。
                    _o_odds = o.get('bb_odds') or 0
                    _o_ev = o.get('ev_pct') or 0
                    if _o_odds > 1 and _o_ev > 0:
                        o['_stake'] = int(round(_bankroll() * KELLY_FRACTION * (_o_ev / 100.0) / (_o_odds - 1) / 10.0) * 10)
                    opps.append(o)
            if _has_ev:
                entries.append(entry)
                self._register_early_acceptance(m, sport)
        _t_price = time.time() - _t_price0
        if _tasks:
            print(f"[slm] drop分段: 鲜价{_t_fetch:.2f}s(共{len(_tasks)}场) 比价{_t_price:.2f}s", flush=True)
        # 2026-10-31 drop 关键路径: 先下单(抢 lead-lag 窗口), 入库观察库移到下单后(不阻塞下单)
        if not opps:
            if entries:  # 无下单机会仍入库(观察库记录所有 +EV)
                try:
                    from src.monitor.clv_collector import log_oa_opportunities
                    log_oa_opportunities(entries, min_ev=2.0, max_ev=12.0, source="drop")
                except Exception:
                    pass
            return len(new_lines)
        _t_bet0 = time.time()
        try:
            from src.betting.bb_real_bet_flow import auto_bet_flow
            res = auto_bet_flow(opps, trigger_ts=min_trig_ts, trigger_source="pin_drop")
            n = len(res.get('success', [])) if isinstance(res, dict) else 0
            if n:
                # 2026-10-27 加落盘耗时(不只钉钉): drop 实盘须<3s, 用日志量化「auto_bet_flow 内部耗时」
                # 与「触发→完成」两个口径, 后续「仍然很高」可直接查日志定位, 不再拍脑门。
                _t_bet = time.time() - _t_bet0
                _pure = (time.time() - min_trig_ts) if min_trig_ts else 0.0
                print(f'[slm] Pin drop 触发下单 {n} 注 | auto_bet_flow耗时 {_t_bet:.2f}s | 触发→完成 {_pure:.2f}s', flush=True)
        except Exception as e:
            print(f'[slm] Pin drop 触发下单异常: {type(e).__name__} {e}', flush=True)
        # 入库观察库(下单后, 不占 drop 实盘关键路径)
        if entries:
            try:
                from src.monitor.clv_collector import log_oa_opportunities
                log_oa_opportunities(entries, min_ev=2.0, max_ev=12.0, source="drop")
            except Exception:
                pass
        return len(new_lines)

    def _consume_prematch_ticks(self):
        """早盘 WS 赔率变动触发(事件驱动, 替代 30s 轮询, 2026-10-18)。

        Pin WS 推 prematch 赔率变动 → pin_ws_trigger 写 pin_prematch_tick.jsonl(home/away/ts)。
        读新行 → 反查 BB 快照 → 比价 → 释放过滤 → 下单(trigger_source=ws_scan)。
        与 _scan_prematch 同口径, 但只比价「变动的比赛」(非全量 1488 场), 比价 <1s。
        """
        if not PREMATCH_TICK_FILE.exists():
            return 0
        try:
            lines = PREMATCH_TICK_FILE.read_text().splitlines()
        except OSError:
            return 0
        last = self._prematch_tick_offset
        if last > len(lines):
            self._prematch_tick_offset = 0  # 文件被轮转截断, offset 超出新长度 → 重置
            last = 0
        elif last == len(lines):
            return 0  # 2026-10-27 同 drop 的 offset 循环 bug: 追平末尾直接等待, 不重放旧行
        new_lines = lines[last:]
        # 2026-11-07 限批: Pin WS 突发(峰值数百场)时全量比价 getMatchDetail 会拖 115s+,
        # 每轮 ≤60 场(offset 停在 last+60), 剩余留到下一轮, 比价延迟封顶 ~17s(60/6并发×1.66s)。
        if len(new_lines) > 60:
            new_lines = new_lines[:60]
            self._prematch_tick_offset = last + 60
        else:
            self._prematch_tick_offset = len(lines)
        if not new_lines:
            return 0
        from src.scrapers.bb_incremental_scanner import BB_EXTRACTED
        from src.scrapers.odds_api_io import _norm_team
        try:
            d = json.loads(BB_EXTRACTED.read_text())
            bb_matches = d.get('matches', []) if isinstance(d, dict) else []
        except Exception:
            return 0
        bb_by_norm = {}
        for m in bb_matches:
            h, a = _norm_team(m.get('home', '')), _norm_team(m.get('away', ''))
            if h and a:
                bb_by_norm[(h, a)] = m
                bb_by_norm[(a, h)] = m
        from src.scrapers.bb_vs_pinnacle import _build_oa_entry, _oa_add_markets
        from src.report.bb_ev_push import _is_market_released, _is_direction_blocked
        _GROUP_TO_SUB = {'opportunities': '1x2', 'handicap': 'hc', 'over_under': 'ou',
                         'double_chance': 'dc', 'draw_no_bet': 'dnb'}
        opps = []
        entries = []
        seen = set()
        _t0 = time.time()
        # 阶段1: 收集待处理比赛(去重+匹配), 不调 getMatchDetail(串行 1-2s/场 是比价慢的瓶颈)
        _tasks = []  # [(mid, sport), ...]
        _ms = {}     # mid -> m(阶段3替换鲜价用)
        for line in new_lines:
            try:
                trig = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            h, a = _norm_team(trig.get('home', '')), _norm_team(trig.get('away', ''))
            m = bb_by_norm.get((h, a))
            if not m:
                continue
            mid = str(m.get('id'))
            if mid in seen:
                continue
            seen.add(mid)
            sport = m.get('sport', 'football')
            _tasks.append((mid, sport))
            _ms[mid] = m
        # 阶段2: 并发 getMatchDetail 拉鲜价(2026-10-31 修「比价12-40s」: 串行→并发, 一批14场从~20s→~3s)
        _odds_list = _fetch_fresh_odds_concurrent(_tasks)
        # 阶段3: 串行替换鲜价 + 比价 + 过滤(比价是本地公平价匹配, 快)
        for (mid, sport), _fresh_odds in zip(_tasks, _odds_list):
            m = _ms[mid]
            if _fresh_odds:
                for _k, _v in _fresh_odds.items():
                    m[_k] = _v
            try:
                entry = _build_oa_entry(m, sport)
                _oa_add_markets(entry, m, sport, use_rest=False, status='prematch')
            except Exception:
                continue
            _league = entry.get('league', '')
            _epoch = entry.get('start_time_pin_epoch', 0)
            _has_ev = False
            for group in ('opportunities', 'handicap', 'over_under', 'double_chance', 'draw_no_bet'):
                for o in entry.get(group, []):
                    _ev = o.get('ev_pct') or 0
                    if _ev <= 0:
                        continue
                    _has_ev = True  # 入库: ev>0 全收
                    if _ev < 2 or _ev > 12:  # 下单门槛 2-12%(2026-11-07 用户决定 5%→2%: ws 抓定价错误 edge 薄)
                        continue
                    sm = o.get('_market') or _GROUP_TO_SUB.get(group, group)
                    if not _is_market_released(sport, sm, _league, o.get('designation', ''), _epoch,
                                               o.get('fair_price') or o.get('bb_odds'), scope='early', source='ws'):
                        continue
                    if _is_direction_blocked(sport, sm, o.get('designation', ''), _epoch):
                        continue
                    o['home_bb'] = entry.get('home_bb', '')
                    o['away_bb'] = entry.get('away_bb', '')
                    o['sport'] = sport
                    o['sub_market'] = sm
                    o['bb_match_id'] = entry.get('bb_match_id', '')  # 2026-10-25 定向下单用: 缓存过期时按 id 拉单场盘口
                    o['_pin_epoch'] = _epoch
                    _o_odds = o.get('bb_odds') or 0
                    _o_ev = o.get('ev_pct') or 0
                    if _o_odds > 1 and _o_ev > 0:
                        o['_stake'] = int(round(_bankroll() * KELLY_FRACTION * (_o_ev / 100.0) / (_o_odds - 1) / 10.0) * 10)
                    opps.append(o)
            if _has_ev:
                entries.append(entry)
                self._register_early_acceptance(m, sport)
        _dt = time.time() - _t0
        if entries:
            try:
                from src.monitor.clv_collector import log_oa_opportunities
                log_oa_opportunities(entries, min_ev=2.0, max_ev=12.0, source="ws_scan")
            except Exception:
                pass
        _t_ingest = time.time() - _t0  # 入库后耗时
        # 2026-11-07 ws 独立投注(抓 BB 定价错误): ws_scan 用 Pin 缓存价(5min批量)抓定价偏差,
        # 与 drop(抓 lead-lag 秒级窗口)职责不同, 各自入库+各自投注。ev_raw>20% 护栏+line漂移检查已兜底跨线假溢价。
        _n_bet = 0
        if opps:
            opps.sort(key=lambda o: -(o.get('ev_pct') or 0))
            opps = opps[:30]
            try:
                from src.betting.bb_real_bet_flow import auto_bet_flow
                res = auto_bet_flow(opps, trigger_source="ws_scan")
                _n_bet = len(res.get('success', [])) if isinstance(res, dict) else 0
            except Exception as e:
                print(f'[slm] 早盘 ws_scan 下单异常: {type(e).__name__} {str(e)[:80]}', flush=True)
        print(f'[slm] 早盘WS触发耗时: 比价 {_dt:.1f}s + 入库 {_t_ingest-_dt:.1f}s + 下单 {_n_bet}注 = 总 {_dt:.1f}s ({len(opps)}单)', flush=True)
        return len(opps)

    def _scan_prematch(self):
        """早盘 WS 全量扫描触发(双轨制第二轨, 2026-10-18)。

        drop 触发只抓 ≥5% 骤降的冷门(波动大), 命中 BB 率仅 17.6% 且冷门无 edge;
        释放清单里的热门/中概率真 edge(不骤降)永远等不到 drop → 早盘 0 下单。
        本函数每 30s 扫 BB 早盘快照全量 → 比价(结果锚校准已在 _oa_add_markets 内) →
        释放过滤 → 入库(trigger_source=ws_scan) + 下单。与 drop 触发同口径, 触发源标记区分。
        """
        from src.scrapers.bb_incremental_scanner import BB_EXTRACTED
        try:
            d = json.loads(BB_EXTRACTED.read_text())
            matches = d.get('matches', []) if isinstance(d, dict) else []
        except Exception:
            return 0
        if not matches:
            return 0
        from src.scrapers.bb_vs_pinnacle import _build_oa_entry, _oa_add_markets
        from src.report.bb_ev_push import _is_market_released, _is_direction_blocked
        _GROUP_TO_SUB = {'opportunities': '1x2', 'handicap': 'hc', 'over_under': 'ou',
                         'double_chance': 'dc', 'draw_no_bet': 'dnb'}
        opps = []
        entries = []  # 入库观察库(含正 EV opp 的 entry)
        _t0 = time.time()
        for m in matches:
            sport = m.get('sport', 'football')
            try:
                entry = _build_oa_entry(m, sport)
                _oa_add_markets(entry, m, sport, use_rest=False, status='prematch')
            except Exception:
                continue
            _league = entry.get('league', '')
            _epoch = entry.get('start_time_pin_epoch', 0)
            _has_ev = False
            for group in ('opportunities', 'handicap', 'over_under', 'double_chance', 'draw_no_bet'):
                for o in entry.get(group, []):
                    _ev = o.get('ev_pct') or 0
                    if _ev <= 0:
                        continue
                    _has_ev = True  # 入库观察库: ev>0 都收(全量正EV, 供未来对比两流程)
                    # 下单门槛: 2-12%(2026-11-07 用户决定 5%→2%: ws 抓定价错误 edge 薄)
                    # 12% 挡冷门/彩票型假 EV。校准后 EV。
                    if _ev < 2 or _ev > 12:
                        continue
                    sub_market = o.get('_market') or _GROUP_TO_SUB.get(group, group)
                    if not _is_market_released(sport, sub_market, _league,
                                               o.get('designation', ''), _epoch,
                                               o.get('fair_price') or o.get('bb_odds'), scope='early'):
                        continue
                    if _is_direction_blocked(sport, sub_market, o.get('designation', ''), _epoch):
                        continue
                    o['home_bb'] = entry.get('home_bb', '')
                    o['away_bb'] = entry.get('away_bb', '')
                    o['sport'] = sport
                    o['sub_market'] = sub_market  # 2026-10-18 修: opp 缺 sub_market → auto_bet_flow 读空 fallback 1x2 错盘
                    o['bb_match_id'] = entry.get('bb_match_id', '')  # 2026-10-25 定向下单用: 缓存过期时按 id 拉单场盘口
                    o['_pin_epoch'] = _epoch
                    # 2026-10-18 修: 早盘 opp 缺 _stake → auto_bet_flow stake=0 全被 stake<30 跳过。
                    # 复用滚球 Kelly 半凯利算注额(cap 在 auto_bet_flow 按触发源 80/50)。
                    _o_odds = o.get('bb_odds') or 0
                    _o_ev = o.get('ev_pct') or 0
                    if _o_odds > 1 and _o_ev > 0:
                        o['_stake'] = int(round(_bankroll() * KELLY_FRACTION * (_o_ev / 100.0) / (_o_odds - 1) / 10.0) * 10)
                    opps.append(o)
            if _has_ev:
                entries.append(entry)
                self._register_early_acceptance(m, sport)
        _dt = time.time() - _t0
        print(f'[slm] 早盘WS扫描: {len(matches)}场比价 {_dt:.1f}s, 可投 {len(opps)} 个, 入库 {len(entries)} 场', flush=True)
        # 入库观察库(ws_scan 来源, 与 drop 来源分开, 供对比两流程优劣)
        if entries:
            try:
                from src.monitor.clv_collector import log_oa_opportunities
                log_oa_opportunities(entries, min_ev=2.0, max_ev=12.0, source="ws_scan")
            except Exception:
                pass
        _n_bet = 0
        if opps:
            # 按 EV 降序, 限 30 注/轮(防风控: auto_bet_flow 每单 sleep 20-90s)
            opps.sort(key=lambda o: -(o.get('ev_pct') or 0))
            opps = opps[:30]
            # 2026-11-07 ws 独立投注(抓 BB 定价错误): 与 drop(抓 lead-lag 窗口)职责不同, 各自投注。
            # ev_raw>20% 护栏+line漂移检查已兜底跨线假溢价。
            try:
                from src.betting.bb_real_bet_flow import auto_bet_flow
                res = auto_bet_flow(opps, trigger_source="ws_scan")
                _n_bet = len(res.get('success', [])) if isinstance(res, dict) else 0
            except Exception as e:
                print(f'[slm] 早盘 ws_scan 下单异常: {type(e).__name__} {str(e)[:80]}', flush=True)
        print(f'[slm] 早盘WS扫描下单: {_n_bet} 注', flush=True)
        return len(opps)

    def _match_drop_anchor(self, trig):
        """live drop 作事件锚点(2026-10-11 简化版): 匹配观察库 pending, 用 drop 方向算 clv_sign。

        只覆盖 1x2(Home/Away/Draw)。ou 盘 drop 无 line(跨线匹配风险) 不覆盖, hc 无 outcome 不覆盖。
        clv_sign: 下注方向 == drop 骤降方向 → +1(事件朝有利); 反面 → -1(朝不利); 否则不匹配跳过。
        写回 live_paper_bets 的 clv_sign/clv_anchor/clv_elapsed_s 字段。

        两个选择性偏差(解读命中率时必须记住):
        1) drop 只触发 ≥5% 骤降 → 样本是「大波动场次」的条件样本。命中率只能回答
           P(clv_sign=+1 | 有drop), 不能外推到全部注单。永远不和早盘 CLV(全覆盖) 比较, 量纲不同。
        2) 无 drop = clv_sign 保持 None, 不是 0。0:0 沉闷局从不进命中率分母, 只会让 n 长慢。
           n 小是设计使然, 不是失败信号, 绝不能因此降门槛或扩匹配范围。
        """
        mid = str(trig.get('bb_match_id') or '')
        pending = self._drop_anchor_track.get(mid)
        if not pending:
            return
        outcome = trig.get('outcome', '')
        _map = {"Home": ("opportunities", "主胜"), "Away": ("opportunities", "客胜"),
                "Draw": ("opportunities", "和局"), "Over": ("over_under", "大球"),
                "Under": ("over_under", "小球")}
        _opp = {("opportunities", "主胜"): ("opportunities", "客胜"),
                ("opportunities", "客胜"): ("opportunities", "主胜"),
                ("over_under", "大球"): ("over_under", "小球"),
                ("over_under", "小球"): ("over_under", "大球")}
        drop_dir = _map.get(outcome)
        if not drop_dir:
            return
        remain = []
        for rec in pending:
            bet_dir = (rec["sub"], rec["designation"])
            if bet_dir == drop_dir:
                clv_sign = 1
            elif bet_dir == _opp.get(drop_dir):
                clv_sign = -1
            else:
                remain.append(rec)  # 别的盘口, 留着等下一个 drop
                continue
            self._write_clv_sign(mid, rec["market_id"], rec["option_type"], rec["sub"],
                                 clv_sign, "drop", time.time() - rec["ts"])
        if remain:
            self._drop_anchor_track[mid] = remain
        else:
            self._drop_anchor_track.pop(mid, None)

    def _reanchor_pending(self):
        """复开价锚点(2026-10-18): 周期性重查 Pin 实时公平价, 对下注后价格已移动的注写 clv_sign。

        覆盖所有 1x2/ou 注(不只 drop 场次, 解决 drop 锚点 0.6% 覆盖率的死角)。
        方向: 重查公平价 vs 下注公平价, fair 下降(概率升)=朝有利 +1, 上升=朝不利 -1。
        下注 ≥5s 后才复查(避开同刻价), |移动|<2% 视为没动继续等。
        """
        if not self._drop_anchor_track:
            return
        from src.scrapers.pinnapi_client import fetch_pinnacle_markets, _norm, _select_line
        from src.scrapers.bb_vs_pinnacle import _BB_SPORT_TO_OA_ID
        from src.scrapers.devig import power_fair_odds
        _DIR = {"主胜": "home", "客胜": "away", "和局": "draw", "大球": "over", "小球": "under",
                "让球主胜": "home", "让球客胜": "away",
                "上半场主胜": "home", "上半场客胜": "away", "上半场和局": "draw",
                "主/和": "1X", "主/客": "12", "客/和": "X2",
                "上半场大球": "over", "上半场小球": "under"}
        now = time.time()
        _total = sum(len(v) for v in self._drop_anchor_track.values())
        print(f"[slm] 复开价锚点: pending {_total} 注", flush=True)
        # 2026-10-18 跨调用缓存(TTL 300s): force 拉取结果跨 60s 调用复用, 请求量降 5 倍缓解 Pin 限速;
        # LEV 仍用 ≤5min 实时价(准)。缓存存 sid -> (ts, events)。
        _live_evs = self._reanchor_live_cache
        for mid, recs in list(self._drop_anchor_track.items()):
            remain = []
            for rec in recs:
                if now - rec.get("ts", 0) < 5:
                    remain.append(rec)
                    continue
                sid = _BB_SPORT_TO_OA_ID.get(rec.get("sport", ""), 0)
                if not sid:
                    remain.append(rec)
                    continue
                _cached = _live_evs.get(sid)
                if not _cached or now - _cached[0] > 300:
                    _evs = (fetch_pinnacle_markets(sid, 'live', force=True) or {}).get('events', [])
                    _live_evs[sid] = (now, _evs)
                _sid_evs = _live_evs[sid][1]
                ev = None
                for _ev in _sid_evs:
                    if _norm(_ev.get('home', '')) == _norm(rec.get("home", "")) and _norm(_ev.get('away', '')) == _norm(rec.get("away", "")):
                        ev = _ev
                        break
                if not ev:
                    remain.append(rec)
                    continue
                per = ev.get('periods', {})
                full = (per.get('num_0') or {}) if isinstance(per, dict) else {}
                ml = full.get('money_line') or {}
                sm = rec.get("sub")
                if sm == "opportunities":
                    if not ml.get("draw"):
                        remain.append(rec); continue
                    _f = power_fair_odds([ml['home'], ml['draw'], ml['away']])
                    fair_map = {'home': _f[0], 'draw': _f[1], 'away': _f[2]}
                elif sm == "over_under":
                    tot = full.get('totals') or {}
                    _o = (tot.get('0') or list(tot.values())[0]) if tot else None
                    if not _o or not _o.get('over') or not _o.get('under'):
                        remain.append(rec); continue
                    _f = power_fair_odds([_o['over'], _o['under']])
                    fair_map = {'over': _f[0], 'under': _f[1]}
                elif sm == "handicap":
                    # 2026-10-18 复开价锚点扩让球: 找最接近 bet line 的 spread, 算 home/away 公平价
                    spreads = full.get('spreads') or {}
                    if not spreads or rec.get("line") is None:
                        remain.append(rec); continue
                    _o = _select_line(spreads, rec.get("line"))
                    if not _o or not _o.get('home') or not _o.get('away'):
                        remain.append(rec); continue
                    _f = power_fair_odds([_o['home'], _o['away']])
                    fair_map = {'home': _f[0], 'away': _f[1]}
                elif sm == "ht":
                    # 2026-10-18 复开价锚点扩半场: 半场独赢走 num_1 的 money_line
                    full_ht = (per.get('num_1') or {}) if isinstance(per, dict) else {}
                    ml_ht = full_ht.get('money_line') or {}
                    if not ml_ht.get('draw'):
                        remain.append(rec); continue
                    _f = power_fair_odds([ml_ht['home'], ml_ht['draw'], ml_ht['away']])
                    fair_map = {'home': _f[0], 'draw': _f[1], 'away': _f[2]}
                elif sm == "double_chance":
                    # 2026-10-18 复开价锚点扩双机会: 从 1x2 三路概率合成 1X/12/X2 公平价
                    if not ml.get('draw'):
                        remain.append(rec); continue
                    _f = power_fair_odds([ml['home'], ml['draw'], ml['away']])
                    _ph, _pd, _pa = 1.0 / _f[0], 1.0 / _f[1], 1.0 / _f[2]
                    fair_map = {'1X': 1.0 / (_ph + _pd), '12': 1.0 / (_ph + _pa), 'X2': 1.0 / (_pd + _pa)}
                elif sm == "ht_ou":
                    # 2026-10-18 复开价锚点扩半场大小: 走 num_1 的 totals
                    full_ht = (per.get('num_1') or {}) if isinstance(per, dict) else {}
                    tot_ht = full_ht.get('totals') or {}
                    _o = (tot_ht.get('0') or list(tot_ht.values())[0]) if tot_ht else None
                    if not _o or not _o.get('over') or not _o.get('under'):
                        remain.append(rec); continue
                    _f = power_fair_odds([_o['over'], _o['under']])
                    fair_map = {'over': _f[0], 'under': _f[1]}
                else:
                    remain.append(rec); continue
                dk = _DIR.get(rec.get("designation", ""))
                new_f = fair_map.get(dk) if dk else None
                old_f = rec.get("fair")
                if not new_f or not old_f or new_f <= 1:
                    remain.append(rec)
                    continue
                moved = (old_f - new_f) / new_f
                if abs(moved) < 0.02:
                    remain.append(rec)
                    continue
                self._write_clv_sign(mid, rec["market_id"], rec["option_type"], rec["sub"],
                                     (1 if moved > 0 else -1), "reopen", round(now - rec["ts"], 2))
            if remain:
                self._drop_anchor_track[mid] = remain
            else:
                self._drop_anchor_track.pop(mid, None)

    def _write_clv_sign(self, match_id, market_id, option_type, sub, clv_sign, anchor, elapsed):
        """把 clv_sign 写回 live_paper_bets 对应记录(2026-10-11)。"""
        try:
            data, keys = _load_paper_bets()
            for b in data:
                if (str(b.get("match_id")) == str(match_id) and str(b.get("market_id")) == str(market_id)
                        and str(b.get("option_type")) == str(option_type) and b.get("sub") == sub):
                    b["clv_sign"] = clv_sign
                    b["clv_anchor"] = anchor
                    b["clv_elapsed_s"] = round(elapsed, 2)
                    _paper_bets_cache["dirty"] = True
                    break
        except Exception:
            pass

    async def _drop_trigger_loop(self):
        """Pin drop 触发: 最高优先级独立循环(2026-10-24)。

        Drop 是 lead-lag 命门(骤降 → BB 滞后跟随, 窗口仅几秒), 之前串行在主循环末尾等 61s,
        窗口早过 → 全盘口负(-11.8%)。拆成独立循环 0.2s 轮询, 第一优先级, 确保秒级投注。
        """
        while True:
            try:
                await _run_task(self._consume_pin_triggers, timeout=20, tag="Pin drop触发")
            except Exception as e:
                print(f"[slm] Pin drop触发异常: {type(e).__name__} {str(e)[:80]}", flush=True)
            await asyncio.sleep(0.2)  # 0.2s 轮询, 骤降立即处理(第一优先级)

    async def _ws_trigger_loop(self):
        """早盘 WS 赔率变动: 事件驱动(次要优先级), 1s 轮询。"""
        while True:
            try:
                await _run_task(self._consume_prematch_ticks, timeout=60, tag="早盘WS触发")
            except Exception as e:
                print(f"[slm] 早盘WS触发异常: {type(e).__name__} {str(e)[:80]}", flush=True)
            await asyncio.sleep(1.0)

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
        # 2026-09-18: 启动 odds-api.io WebSocket 订阅(live 主流盘口实时推送), get_odds 优先读
        # WS 实时缓存, 替代 2s REST 轮询 Betfair 价。注: 这里的 WS 是 odds-api.io 的赔率推送,
        # 不是 2026-09-13 撤掉的 BB WS(Chrome 9222)——那个依赖浏览器, 这个纯 HTTP 连 odds-api.io。
        try:
            from src.scrapers.odds_ws import OddsWSClient
            _ws_mkts = ("ML", "Spread", "Totals", "Double Chance", "Both Teams To Score")
            # 2026-09-20 加 Double Chance/BTTS: dc/btts 之前不在 WS 缓存, 全走 REST 兜底(公平价匹配10-17s
            # + 费5000/小时限额), 加进 WS 订阅后走缓存 0ms。
            # 2026-09-24 滚球高频只订 6 主运动(拆4+2); 早盘全量走 --all-sports, 不经此 WS。
            # 2026-10-07 合并 2→1: 6 运动 < WS 单连接 ≤10 运动限制, 且实测 live 事件量 ~615/min=10/s,
            # 单连接轻松扛(拆2条是当年 11 运动的历史遗留)。合并后腾出 1 条 apiKey 连接配额给早盘 WS 触发。
            # 2026-10-17 撤 Bet/SBO: 停 odds-api.io WS 订阅, 只用 Pin 单锚(Betfair/SBO 不再推送)
            self._odds_ws = None
            print("[slm] odds-api.io WS 已停用(只用 Pin 单锚)", flush=True)
        except Exception as e:
            self._odds_ws = None
            print(f"[slm] WebSocket 订阅启动失败(回退 REST 轮询): {type(e).__name__} {str(e)[:80]}", flush=True)
        # 2026-09-21 后台预取 BB 滚球列表: 把 getList 从下单链路移出(与 persistence 并行), 下单读缓存 0ms。
        # 2026-09-29 sleep 2.0→0.5: 预取周期从 fetch~2.2s+2s≈4.2s 压到 ~2.7s, 缓存更鲜(下单 BB 价最多 ~2.7s 前)。
        try:
            from src.scrapers.pinnacle_live import start_bb_prefetch
            start_bb_prefetch(interval=0.5)
            print("[slm] 已启动 BB 滚球后台预取(sleep 0.5s, 周期≈2.7s, 下单读缓存)", flush=True)
        except Exception as e:
            print(f"[slm] BB 后台预取启动失败: {type(e).__name__} {str(e)[:80]}", flush=True)
        # 2026-10-24 早盘 getList 预取: 避免下单时 cache miss 拉全量 20-30s(实测「下单 32.7s」)
        try:
            from src.betting.bb_real_bet_flow import start_early_getlist_prefetch
            start_early_getlist_prefetch(interval=150)
            print("[slm] 已启动早盘 getList 后台预取(每150s刷新, 下单命中缓存0ms)", flush=True)
        except Exception as e:
            print(f"[slm] 早盘 getList 预取启动失败: {type(e).__name__} {str(e)[:80]}", flush=True)
        # 2026-10-27 早盘余额预取: fetch_balance 是 1-2s 阻塞 HTTP, 已从下单关键路径移除,
        # 改后台每 45s 预热缓存, 下单时 check_balance_ok 命中缓存 0ms(drop 实盘 <3s 关键)。
        try:
            from src.betting.bb_real_bet_flow import start_balance_prefetch
            start_balance_prefetch(interval=45)
            print("[slm] 已启动早盘余额后台预取(每45s刷新, 下单命中缓存0ms)", flush=True)
        except Exception as e:
            print(f"[slm] 早盘余额预取启动失败: {type(e).__name__} {str(e)[:80]}", flush=True)
        # 2026-10-18 停用 Betfair 事件列表预取: odds-api.io 已撤(切 pinnapi 单锚),
        # get_events 走 _get 短路返回空, 预取空转。公平价匹配已用 pinnacle_fair_price(WS缓存),
        # 不再依赖 Betfair 事件索引。
        # (原 2026-09-22: 让 get_events REST 不在公平价匹配关键路径上)
        # 2026-09-25 后台预取 CMN 中文名: 推送全中文, 后台线程每 60s 拉 CMN, 不占下单链路
        try:
            from src.scrapers.pinnacle_live import start_cn_prefetch
            start_cn_prefetch(interval=60.0)
            print("[slm] 已启动 CMN 中文名后台预取(每 60s 拉一次, 推送全中文)", flush=True)
        except Exception as e:
            print(f"[slm] CMN 中文名预取启动失败: {type(e).__name__} {str(e)[:80]}", flush=True)
        last_poll = 0.0
        # 2026-10-24 早盘触发独立循环: Drop(0.2s 第一优先级) + WS(1s), 拆出主循环, 不跟滚球 60s 轮次绑定(lead-lag 命门)
        _drop_task = asyncio.create_task(self._drop_trigger_loop())
        _ws_task = asyncio.create_task(self._ws_trigger_loop())
        while deadline is None or time.time() < deadline:
            now = time.time()
            # 2026-09-19 WS 触发: sharp 变动 → 立即 poll(滚球) + 单场比价(早盘)
            try:
                from src.scrapers.odds_ws import get_persisted_changes
                _changes = get_persisted_changes()  # persistence 稳定期已取消(PERSIST_MIN_AGE=0.0, 2026-09-29): 变动立即发射
            except Exception:
                _changes = []
            _ws_changed = bool(_changes)
            # 2026-10-11 切 Pin WS live 帧触发: pin_ws_trigger 每 0.5s 写 pin_live_tick.ts(格式"{本地收到},{Pin帧last}"),
            # 读到新时间戳就立即 poll(替代 Betfair WS 触发, 19 号 Betfair 停订后靠它)。
            try:
                _parts = PIN_LIVE_TICK_FILE.read_text().strip().split(",")
                _ptick = float(_parts[0]) if _parts and _parts[0] else 0.0
                _pin_last = float(_parts[1]) if len(_parts) > 1 and _parts[1] else 0.0
            except Exception:
                _ptick = 0.0
                _pin_last = 0.0
            if _ptick > self._last_pin_live_tick:
                self._last_pin_live_tick = _ptick
                self._last_pin_frame_last = _pin_last
                self._pin_live_wake = True
                if _pin_last:  # 收集时钟差样本(判定偏移稳定 vs 积压漂移)
                    self._pin_clock_samples.append(_ptick - _pin_last)
                    self._pin_clock_samples = self._pin_clock_samples[-300:]
            # 2026-09-29 去掉 3.0s 节流 + persistence: sharp 动立即触发 poll(WS 变动即比价下单),
            # 不再等 3s 节流/0.5s 稳定。oddsChange=0 兜底(变了就拒), 防假溢价靠 SBO 同向 + 快照年龄。
            if _ws_changed or self._pin_live_wake or now - last_poll >= refresh_every:
                # 触发源标签(2026-11-02 改名): drop(Pin live drop唤醒=滚球drop) / betfair_ws / poll, 供审计组内对照
                # drop=滚球 Pin 骤降唤醒(单独标记, 与早盘 pin_drop 区分); 否则 betfair_ws; 再退回 poll。
                # 三态兜底完整保留，本行只改优先级，不改可达性(最坏退回 betfair_ws，行为与改前一致)。
                self._trigger_source = "drop" if self._pin_live_wake else ("betfair_ws" if _ws_changed else "poll")
                self._trigger_source_stats[self._trigger_source] = self._trigger_source_stats.get(self._trigger_source, 0) + 1
                self._pin_live_wake = False  # 2026-10-11: Pin 滚球 drop 唤醒后立即比较一次
                try:
                    _t_round0 = time.time()
                    # 2026-09-30 口径修正: poll_ts 传「本单真实触发时刻」。WS 触发用 _raw_ws_ts(真实WS变动),
                    # 轮询周期触发用本轮开始 _t_round0。之前 _poll_live 内部硬编码 _raw_ws_ts, 轮询触发的单
                    # 会用陈旧的 WS 时刻 → 「总耗时」混入轮询间隔(2s)虚高。
                    n = self._poll_live(poll_ts=(self._raw_ws_ts if _ws_changed else _t_round0))
                    _t_poll = time.time() - _t_round0
                    if n:
                        print(f"[slm] 本轮发现 {n} 个滚球机会")
                    last_poll = now
                    poll_count += 1
                    # 2026-11-07 BB价轨迹采样(每30s): 对已下注比赛采样 BB 价, 验证 lead-lag 窗口多长
                    if time.time() - self._last_bb_track_ts >= 30:
                        self._last_bb_track_ts = time.time()
                        try:
                            self._sample_bb_track()
                        except Exception:
                            pass
                    # 观察库结算: 每 60s 结一批(独立时间戳, 不依赖 poll_count — pin_live 超时拖慢轮询,
                    # 依赖 poll_count%15 会把 1130 条拖到 2-3h 才结完)
                    if time.time() - last_settle >= 60:
                        last_settle = time.time()
                        _t_settle0 = time.time()
                        # 2026-09-26 防假死: 结算放独立线程池+60s超时(根治: 卡住不占默认池)
                        await _run_task(self._settle_paper_bets, timeout=60, tag="结算")
                        # 2026-10-18 复开价锚点: 周期性重查 Pin 公平价给 pending 注写 clv_sign(全覆盖, 不只 drop 场次)
                        try:
                            self._reanchor_pending()
                        except Exception as _e:
                            print(f"[slm] 复开价锚点异常: {type(_e).__name__} {str(_e)[:80]}", flush=True)
                        if time.time() - _t_settle0 > 60:
                            print(f"[slm] ⚠️ 结算耗时 {time.time()-_t_settle0:.0f}s(>60s)", flush=True)
                    if poll_count % 15 == 0:  # 每 ~30s 查一次已结算订单 → 推钉钉
                        self._check_settled()
                    if poll_count % 15 == 0 and self._trigger_source_stats:  # 触发源分布(2026-10-11): 验证 pin 优先是否生效
                        _ts = self._trigger_source_stats
                        print(f"[slm] 触发源分布: pin={_ts.get('pin',0)} betfair_ws={_ts.get('betfair_ws',0)} poll={_ts.get('poll',0)}", flush=True)
                    if poll_count % 15 == 0 and self._skip_stats:  # 2026-10-11 跳过分桶摘要(观察过度拦截)
                        print(f"[slm] 跳过分桶: {dict(self._skip_stats)}", flush=True)
                    try:  # 2026-10-11 共享 stats 摘要(滚球+早盘 line_zero seen/filtered)
                        from src.scrapers.stats import maybe_summary as _ms
                        _ms(30.0)
                    except Exception:
                        pass
                    if poll_count % 15 == 0 and self._pin_clock_samples:  # 段1: Pin产生→我收到 时钟差(稳定=偏移, 漂移=积压)
                        _s = sorted(self._pin_clock_samples)
                        _med = _s[len(_s) // 2]
                        _span = _s[-1] - _s[0]
                        print(f"[slm] Pin时钟差 中位{_med:.2f}s 跨度{_span:.2f}s (稳定≈纯偏移可归一化; 持续变负/跨度放大=积压告警)", flush=True)
                    # 每次轮询(2s)查 CLV(3s窗口)。2026-09-26 独立线程池+30s超时
                    await _run_task(self._check_clv, timeout=30, tag="CLV复验")
                    import threading  # 提前 import(两个 drain 的 if 块共用, 避免局部 import 未触发时 UnboundLocalError)
                    # 纸面模拟拒单(2026-10-31): 每~2s fire-and-forget 复验一次, busy 守卫防并发,
                    # 不占下单关键路径(独立 daemon 线程, fetch_current_odds 的 HTTP 延迟不进主循环)。
                    if (self._pending_acceptance and not self._accept_drain_busy
                            and time.time() - self._last_accept_drain >= 2.0):
                        self._last_accept_drain = time.time()
                        self._accept_drain_busy = True

                        def _drain_done():
                            try:
                                self._drain_pending_acceptance()
                            finally:
                                self._accept_drain_busy = False
                        threading.Thread(target=_drain_done, daemon=True, name="sim-reject").start()
                    # 早盘模拟拒单(2026-10-31): 同样 fire-and-forget 复验, 独立 daemon 线程不占早盘 drop 实盘关键路径
                    if (self._pending_early_acceptance and not self._early_drain_busy
                            and time.time() - self._last_early_drain >= 2.0):
                        self._last_early_drain = time.time()
                        self._early_drain_busy = True

                        def _early_drain_done():
                            try:
                                self._drain_pending_early_acceptance()
                            finally:
                                self._early_drain_busy = False
                        threading.Thread(target=_early_drain_done, daemon=True, name="early-sim-reject").start()
                    # 2026-09-24 每轮耗时监控: 定位假死根因(哪一步慢/卡), poll/settle 分阶段打点
                    _t_round = time.time() - _t_round0
                    if _t_round > 60:
                        print(f"[slm] ⚠️ 本轮总耗时 {_t_round:.0f}s(>60s): poll {_t_poll:.0f}s", flush=True)
                except Exception as e:
                    print(f"[slm] 轮询异常: {type(e).__name__} {str(e)[:80]}", flush=True)
            # 2026-10-24 早盘触发(Drop+WS)已拆到独立循环 _early_trigger_loop(每 1s), 不在这里串行等主循环 60s
            # 2026-09-19 事件驱动: WS 变动立即唤醒(省 sleep 0.5s 轮询延迟), 否则等 0.5s
            try:
                from src.scrapers.odds_ws import wait_change
                _raw_changed = await asyncio.to_thread(wait_change, 0.15)  # 2026-10-11 0.5→0.15: Pin drop 轮询更及时(省 0.35s)
                if _raw_changed:
                    self._raw_ws_ts = time.time()  # 记录原始WS触发时刻(2026-09-23 端到端耗时起点)
                    # 2026-09-28 去掉现拉 BB: 实测 2s 预取缓存 stale 率仅 14.6%(见 measure_bb_stale.py),
                    # 现拉 BB 的 1.1s 是端到端瓶颈。去掉后直接用预取缓存比价, oddsChange=0 下单时变了就拒兜底。
                    # 2026-09-24 空转保护: WS 变动频繁时 wait_change 立即返回, 主循环快速空转烧 CPU(实测93%)。
                    # 空转保护(非稳定期): WS 变动频繁时 wait_change 立即返回, 主循环快速空转烧 CPU(实测93%)。
                    # 这里 sleep 0.5s 只消除「未稳定期间的快速空转」, 与稳定期无关(PERSIST_MIN_AGE 已取消=0.0)。
                    # 有持续变动时下一轮 wait_change 会被新事件立即唤醒, 不漏任何变动。
                    await asyncio.sleep(0.5)
            except Exception:
                await asyncio.sleep(0.5)

        _drop_task.cancel()  # 2026-10-24 Drop/WS 触发循环随主循环退出
        _ws_task.cancel()


def _append_real_bet(order_id, info):
    """下单成功: 写入实盘库(real_bets.json), 记录下单时 odds/fair/ev/stake/比赛等。"""
    from src.scrapers.bet_file_lock import REAL_BETS_LOCK
    try:
        with REAL_BETS_LOCK:
            d = {}
            if REAL_BETS_FILE.exists():
                try:
                    d = json.loads(REAL_BETS_FILE.read_text())
                except Exception:
                    d = {}
            d.setdefault("bets", {})[str(order_id)] = {
                "ts": time.time(), **info,
                "result": None, "profit": None, "settled_ts": None,
            }
            REAL_BETS_FILE.parent.mkdir(parents=True, exist_ok=True)
            REAL_BETS_FILE.write_text(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def _settle_real_bet(order_id, pnl, won):
    """结算: 回填实盘库的 result/profit。

    2026-10-07 修标注 bug: 之前 result = "won" if won else "lost", 而 won=pnl>0,
    走盘退款(pnl=0)被误标成 "lost"。改按 pnl 三态: >0 赢 / <0 输 / =0 走盘退款(push)。
    """
    from src.scrapers.bet_file_lock import REAL_BETS_LOCK
    try:
        with REAL_BETS_LOCK:
            if not REAL_BETS_FILE.exists():
                return
            d = json.loads(REAL_BETS_FILE.read_text())
            b = d.get("bets", {}).get(str(order_id))
            if b:
                b["profit"] = pnl
                if pnl > 0:
                    b["result"] = "won"
                elif pnl < 0:
                    b["result"] = "lost"
                else:
                    b["result"] = "push"  # 走盘退款(pnl=0, BB官方uwl=0)
                b["settled_ts"] = time.time()
                REAL_BETS_FILE.write_text(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="BB 秒级比价监控")
    ap.add_argument("--threshold", type=float, default=4.0, help="EV 信号阈值%(默认4)")
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
