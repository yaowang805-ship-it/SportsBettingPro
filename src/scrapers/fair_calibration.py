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
# 2026-10-18 时间切分样本外验证:
#   prematch 前半拟合→后半测试 ev_cal>0 ROI +9.0%(194笔) → 通过, 开
#   live     前半拟合→后半测试 ev_cal>0 ROI -10.7%(419笔) → 失败(1天数据偏差不稳定), 关
CALIBRATE_FAIR = {"prematch": True, "live": False}

_TABLE_FILES = {
    "live": "fair_calibration.json",
    "prematch": "fair_calibration_prematch.json",
}

_cal_tables = {}   # market -> [(p_implied, p_cal), ...]
_cal_loaded = {}   # market -> bool


def _load_table(market="live"):
    if _cal_loaded.get(market):
        return _cal_tables.get(market)
    _cal_loaded[market] = True
    try:
        p = ROOT / "data" / "storage" / _TABLE_FILES.get(market, _TABLE_FILES["live"])
        if p.exists():
            d = json.load(open(p, encoding="utf-8"))
            tbl = d.get("table")
            if tbl and len(tbl) >= 2:
                _cal_tables[market] = [(float(t["p_implied"]), float(t["p_cal"])) for t in tbl]
    except Exception:
        _cal_tables[market] = None
    return _cal_tables.get(market)


def calibrate_fair(fair, market="live"):
    """校准公平价: fair(十进制赔率) → fair_cal(校准到真实胜率)。

    无表/开关关/非法输入 → 返回原 fair(no-op)。
    market ∈ {'live', 'prematch'}。
    """
    if not CALIBRATE_FAIR.get(market, False):
        return fair
    if fair is None or fair <= 1.0:
        return fair
    tbl = _load_table(market)
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
