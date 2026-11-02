#!/usr/bin/env python3
"""每日去水偏差监控: 隐含概率(1/fair) vs 实际胜率, 验证 Power 去水是否修好 Shin bias。

背景(2026-11-02): Shin 去水 favorite-longshot bias 高估赢率 6.5pp(冷门段 11pp), 是
「CLV 正 / 纸面正 / 实盘负」的根因。已换 Power 去水 + platt 校准, 本脚本每天算一次偏差:
  - 偏差接近 0 → Power 去水修好了。
  - 偏差仍 >3pp → 去水还没修好, 需继续排查。
只统计 Power 去水生效后(ts >= POWER_SWITCH_TS)的注, 旧 Shin 数据不算。
推钉钉, 供每天一眼看趋势。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STORAGE = ROOT / "data" / "storage"

# Power 去水生效时间(2026-11-02 20:00 GMT+8 起, 之后的注才是 Power fair)
POWER_SWITCH_TS = datetime.fromisoformat("2026-11-02T20:00:00+08:00").timestamp()


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bias(bets):
    """返回 (n, 隐含概率%, 实际胜率%, 偏差pp)。"""
    imp_sum = 0.0
    won = 0
    n = 0
    for b in bets:
        fair = f(b.get("fair"))
        if not fair or fair <= 1:
            continue
        imp_sum += 1.0 / fair
        n += 1
        if b.get("result") == "won":
            won += 1
    if n == 0:
        return 0, 0.0, 0.0, 0.0
    return n, imp_sum / n * 100, won / n * 100, (imp_sum / n - won / n) * 100


def _bias_by_bucket(bets, lo, hi):
    sub = []
    for b in bets:
        fair = f(b.get("fair"))
        if not fair or fair <= 1:
            continue
        p = 1.0 / fair
        if lo <= p < hi:
            sub.append(b)
    return _bias(sub)


def main():
    real = json.load(open(STORAGE / "real_bets.json"))
    bets = list(real.get("bets", real).values())
    # 只统计 Power 去水生效后的注(旧 Shin fair 不算)
    new = [b for b in bets if (b.get("ts") or 0) >= POWER_SWITCH_TS
           and b.get("result") in ("won", "lost")]
    early = [b for b in new if b.get("trigger_source") in ("pin_drop", "ws_scan")]
    live = [b for b in new if b.get("trigger_source") not in ("pin_drop", "ws_scan")]

    lines = ["📏 去水偏差日检 (Power 去水后)"]
    lines.append(f"(只统计 11-02 20:00 后的新注, 已结算 {len(new)} 笔)")
    if not new:
        lines.append("\n暂无 Power 新样本, 等实盘积累(2% 入库门槛, 攒得较快)")

    warn = []
    for name, sub in [("早盘", early), ("滚球", live)]:
        n, imp, wr, bias = _bias(sub)
        if n == 0:
            lines.append(f"{name}: 暂无样本")
            continue
        flag = "✅" if abs(bias) <= 3 else "⚠️"
        lines.append(f"{name}: n={n} 隐含={imp:.1f}% 胜率={wr:.1f}% 偏差={bias:+.1f}pp {flag}")
        if abs(bias) > 3:
            warn.append(name)

    # 冷门段(隐含概率 < 25%, 赔率 > 4)是 Shin bias 最严重的地方
    for name, sub in [("早盘", early), ("滚球", live)]:
        n, imp, wr, bias = _bias_by_bucket(sub, 0.0, 0.25)
        if n >= 10:
            flag = "✅" if abs(bias) <= 5 else "⚠️"
            lines.append(f"  └ {name}冷门段(<25%): n={n} 偏差={bias:+.1f}pp {flag}")

    if new:
        n, imp, wr, bias = _bias(new)
        lines.append(f"整体: n={n} 偏差={bias:+.1f}pp")
    if warn:
        lines.append(f"\n⚠️ {'/'.join(warn)} 偏差仍 >3pp, Power 去水可能未完全修好")
    elif new:
        lines.append("\n✅ 偏差已收敛到 ≤3pp, Power 去水生效")

    body = "\n".join(lines)
    print(body)
    try:
        from config.settings import send_dingtalk
        send_dingtalk("📏 去水偏差日检", body)
    except Exception as e:
        print(f"[devig_bias_check] 推送失败: {e}")


if __name__ == "__main__":
    main()
