"""公平价校准 — 把「下注时 Pin 公平价」映射到「真实胜率」。

问题(2026-10-18 实测): Pin 公平价(Shin 去水后)系统性高估热门, 隐含胜率比实际
高 ~8-10pp(滚球) / ~7pp(早盘), 导致 ev=(bb-fair)/fair 虚高, 投的是假溢价。

方案: 用历史已结算注拟合 isotonic 校准曲线, 把 p_implied=1/fair 映射到 p_cal,
再回代 fair_cal=1/p_cal。滚球/早盘分开拟合(偏差幅度不同):
  - scripts/calibrate_live_fair.py     → fair_calibration.json (滚球, 回测 ROI -2.5%→+7%)
  - scripts/calibrate_prematch_fair.py → fair_calibration_prematch.json (早盘, 回测 +0.8%→+14%)

用法:
    from src.scrapers.fair_calibration import calibrate_fair, calibrated_ev
    fair_cal = calibrate_fair(fair, market="prematch")      # 校准公平价(无表时 no-op)
    ev_cal, ev_raw, fair_cal = calibrated_ev(bb, fair, market="prematch")

开关: CALIBRATE_FAIR=False 时全部 no-op(可回退)。
"""
import bisect
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 总开关(分市场, 可回退): 关掉则该端校准不生效, 回到 ev=(bb-fair)/fair 原始口径。
# 2026-11-02 改: 早盘校准从 isotonic(逐段拟合=过拟合根因)换成 platt(2参数平滑单调), 不再过拟合,
# 早盘重新打开。配合 Power 去水(修 Shin favorite-longshot bias 的源头)双保险。
# 2026-11-10 关早盘+滚球校准: 校准表(platt 概率收缩)抬冷门压中间段/热门, 制造冷门假溢价。
# 早盘=脏数据拟合+双校准错位; 滚球=n=101样本少深冷门抬升2.5倍过度。都关掉回纯 Power 去水口径,
# 等干净数据(11-08后)每盘口 n≥50 再重拟合。
CALIBRATE_FAIR = {"prematch": False, "live": False}

_TABLE_FILES = {
    "live": "fair_calibration.json",
    "prematch": "fair_calibration_prematch.json",
}

_cal_tables = {}   # (market, sub_market) -> [(p_implied, p_cal), ...]
_cal_loaded = {}   # (market, sub_market) -> mtime


def _load_table(market="live", sub_market=None):
    p = ROOT / "data" / "storage" / _TABLE_FILES.get(market, _TABLE_FILES["live"])
    key = (market, sub_market or "all")
    try:
        mtime = p.stat().st_mtime if p.exists() else 0.0
    except OSError:
        mtime = 0.0
    # 2026-10-21 改 mtime 缓存: 校准表每日自动更新后, 无需重启进程即可重读新表(旧 bool 缓存永不刷新)
    if _cal_loaded.get(key) == mtime:
        return _cal_tables.get(key)
    _cal_loaded[key] = mtime
    try:
        if p.exists():
            d = json.load(open(p, encoding="utf-8"))
            tbl = None
            # 2026-11-09 分表: 优先按盘口(sub_market)取, 回退 all, 再回退旧单表格式(向后兼容)
            _tables = d.get("tables")
            if isinstance(_tables, dict):
                _t = _tables.get(sub_market) or _tables.get("all") or {}
                tbl = _t.get("table")
            if not tbl:
                tbl = d.get("table")  # 旧单表格式
            if tbl and len(tbl) >= 2:
                _cal_tables[key] = [(float(t["p_implied"]), float(t["p_cal"])) for t in tbl]
    except Exception:
        _cal_tables[key] = None
    return _cal_tables.get(key)


def calibrate_fair(fair, market="live", sub_market=None):
    """校准公平价: fair(十进制赔率) → fair_cal(校准到真实胜率)。

    无表/开关关/非法输入 → 返回原 fair(no-op)。
    market ∈ {'live', 'prematch'}。
    sub_market(2026-11-09): 按盘口(1x2/hc/ou)分表, 修正全局单曲线掩盖盘口 bias 差异(hc-81% vs 1x2-30%)。
    """
    if not CALIBRATE_FAIR.get(market, False):
        return fair
    if fair is None or fair <= 1.0:
        return fair
    tbl = _load_table(market, sub_market)
    if not tbl:
        return fair
    try:
        p_imp = 1.0 / fair
        xs = [t[0] for t in tbl]
        ys = [t[1] for t in tbl]
        if p_imp <= xs[0]:
            p_cal = ys[0]
        elif p_imp >= xs[-1]:
            p_cal = ys[-1]
        else:
            i = bisect.bisect_right(xs, p_imp) - 1
            x0, x1 = xs[i], xs[i + 1]
            y0, y1 = ys[i], ys[i + 1]
            p_cal = y0 + (y1 - y0) * (p_imp - x0) / (x1 - x0)
        p_cal = max(p_cal, 0.01)  # 防 fair_cal 爆炸(热门档校准后 p 可很低)
        return 1.0 / p_cal
    except Exception:
        return fair


def kelly_shrink(fair, market="live", lam=10.0):
    """不确定性调整 Kelly 收缩系数(2026-11-01 职业团队做法)。

    f_adj = f* × exp(-λ × KL(p_imp || p_cal))。KL 散度度量「隐含概率 vs 校准概率」的偏差,
    偏差越大(edge 估计越不可靠)注额收缩越多——防止「高估 edge + 全 Kelly」走向破产。
    校准表无/开关关 → 返回 1.0(不收缩)。λ=风险厌恶系数(默认 10, 中间段偏差~11pp → 收缩~16%)。
    """
    if not CALIBRATE_FAIR.get(market, False):
        return 1.0
    if fair is None or fair <= 1.0:
        return 1.0
    tbl = _load_table(market)
    if not tbl:
        return 1.0
    try:
        import math
        p_imp = 1.0 / fair
        xs = [t[0] for t in tbl]
        ys = [t[1] for t in tbl]
        if p_imp <= xs[0]:
            p_cal = ys[0]
        elif p_imp >= xs[-1]:
            p_cal = ys[-1]
        else:
            i = bisect.bisect_right(xs, p_imp) - 1
            x0, x1 = xs[i], xs[i + 1]
            y0, y1 = ys[i], ys[i + 1]
            p_cal = y0 + (y1 - y0) * (p_imp - x0) / (x1 - x0)
        p_imp = max(p_imp, 0.01)
        p_cal = max(p_cal, 0.01)
        kl = p_imp * math.log(p_imp / p_cal) + (1.0 - p_imp) * math.log((1.0 - p_imp) / (1.0 - p_cal))
        kl = max(kl, 0.0)
        return math.exp(-lam * kl)
    except Exception:
        return 1.0


def calibrated_ev(bb, fair, market="live"):
    """返回 (ev_cal, ev_raw, fair_cal)。

    ev_cal  用校准公平价, 纠正热门高估(用于闸门/定仓)
    ev_raw  原始 ev=(bb-fair)/fair(落盘对照)
    fair_cal 校准后公平价
    非法输入返回 (None, None, None)。
    """
    if not bb or not fair or bb <= 0 or fair <= 0:
        return None, None, None
    try:
        ev_raw = (bb - fair) / fair * 100.0
        fair_cal = calibrate_fair(fair, market=market)
        if fair_cal is None or fair_cal <= 0:
            return None, ev_raw, None
        ev_cal = (bb - fair_cal) / fair_cal * 100.0
        return ev_cal, ev_raw, fair_cal
    except Exception:
        return None, None, None
