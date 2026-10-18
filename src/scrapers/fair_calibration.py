"""公平价校准 — 把「下注时 Pin 公平价」映射到「真实胜率」。

问题(2026-10-18 实测): Pin 公平价(Shin 去水后)系统性高估热门, 隐含胜率比实际
高 ~8-10pp(滚球) / ~7pp(早盘), 导致 ev=(bb-fair)/fair 虚高, 投的是假溢价。

方案: 用历史已结算注拟合 isotonic 校准曲线(scripts/calibrate_live_fair.py 生成
data/storage/fair_calibration.json), 把 p_implied=1/fair 映射到 p_cal, 再回代
fair_cal=1/p_cal。回测: 校准后 ev_cal>0 只剩 ~15% 注, 实盘 ROI 从 -2.5% 翻 +7%。

用法:
    from src.scrapers.fair_calibration import calibrate_fair, calibrated_ev
    fair_cal = calibrate_fair(fair)              # 校准公平价(无表时 no-op 返回原价)
    ev_cal, ev_raw, fair_cal = calibrated_ev(bb, fair)  # 校准后 EV + 原始 EV + 校准公平价

开关: CALIBRATE_FAIR=False 时全部 no-op(可回退)。
"""
import bisect
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 总开关(可回退): 关掉则校准完全不生效, 回到 ev=(bb-fair)/fair 原始口径
CALIBRATE_FAIR = True

# 校准表缓存(懒加载一次)
_cal_table = None
_cal_loaded = False


def _load_table():
    global _cal_table, _cal_loaded
    if _cal_loaded:
        return _cal_table
    _cal_loaded = True
    try:
        p = ROOT / "data" / "storage" / "fair_calibration.json"
        if p.exists():
            d = json.load(open(p, encoding="utf-8"))
            tbl = d.get("table")
            if tbl and len(tbl) >= 2:
                _cal_table = [(float(t["p_implied"]), float(t["p_cal"])) for t in tbl]
    except Exception:
        _cal_table = None
    return _cal_table


def calibrate_fair(fair):
    """校准公平价: fair(十进制赔率) → fair_cal(校准到真实胜率)。

    无表/开关关/非法输入 → 返回原 fair(no-op)。
    """
    if not CALIBRATE_FAIR:
        return fair
    if fair is None or fair <= 1.0:
        return fair
    tbl = _load_table()
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
        p_cal = max(p_cal, 0.01)  # 防 fair_cal 爆炸(0.7-0.8 档校准后 p 可很低)
        return 1.0 / p_cal
    except Exception:
        return fair


def calibrated_ev(bb, fair):
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
        fair_cal = calibrate_fair(fair)
        if fair_cal is None or fair_cal <= 0:
            return None, ev_raw, None
        ev_cal = (bb - fair_cal) / fair_cal * 100.0
        return ev_cal, ev_raw, fair_cal
    except Exception:
        return None, None, None
