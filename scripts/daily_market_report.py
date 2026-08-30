#!/usr/bin/env python3
"""数据盘口日报 — 每天 07:20 推钉钉: 各盘口门槛 / CLV / 真实ROI + 门槛变动对比。

数据源:
  ev_threshold_matrix.json — 数据驱动门槛(每晚 compute_ev_thresholds 重算) + CLV(_details)
  tracked_bets.json        — 已定胜负投注, 按 sub_market 算真实 ROI(滞后指标)
  状态文件 .market_report_prev.json — 上次门槛快照, 对比"是否有变动"

用法: .venv312/bin/python scripts/daily_market_report.py
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 让 config.settings 可被 import
DATA = ROOT / "data" / "storage"

THRESHOLD_FILE = DATA / "ev_threshold_matrix.json"
BETS_FILE = DATA / "tracked_bets.json"
STATE_FILE = DATA / ".market_report_prev.json"


def _f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def load_thresholds():
    if not THRESHOLD_FILE.exists():
        return None
    try:
        return json.loads(THRESHOLD_FILE.read_text())
    except Exception:
        return None


def load_roi():
    """按 sub_market 统计已定胜负投注的真实 ROI(profit/stake)。"""
    if not BETS_FILE.exists():
        return {}
    try:
        d = json.loads(BETS_FILE.read_text())
    except Exception:
        return {}
    bets = d if isinstance(d, list) else (d.get("bets") or list(d.values()))
    agg = defaultdict(lambda: {"stake": 0.0, "profit": 0.0, "n": 0})
    for b in bets:
        if not isinstance(b, dict):
            continue
        if str(b.get("result")) not in ("won", "lost"):
            continue
        sm = b.get("sub_market", "?")
        stake = _f(b.get("stake"), 0) or 0
        profit = _f(b.get("profit"), None)
        if profit is None:
            # 兜底: 按 bb_odds 重算
            o = _f(b.get("bb_odds"), 0) or 0
            profit = stake * (o - 1) if str(b.get("result")) == "won" else -stake
        agg[sm]["stake"] += stake
        agg[sm]["profit"] += profit
        agg[sm]["n"] += 1
    out = {}
    for sm, v in agg.items():
        if v["stake"] > 0:
            out[sm] = {"n": v["n"], "roi": v["profit"] / v["stake"] * 100, "profit": v["profit"]}
    return out


def load_prev_state():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def save_state(markets, generated_at):
    try:
        STATE_FILE.write_text(json.dumps(
            {"generated_at": generated_at, "markets": markets}, ensure_ascii=False))
    except Exception:
        pass


def build_report():
    mtx = load_thresholds()
    roi = load_roi()
    prev = load_prev_state()

    if not mtx:
        return "❌ 无 ev_threshold_matrix.json，跳过盘口日报"

    markets = mtx.get("markets", {})
    details = mtx.get("_details", {})
    generated = mtx.get("generated_at", "")
    settled_n = sum(v["n"] for v in roi.values())

    lines = ["📊 数据盘口日报（表现好）", ""]
    lines.append(f"门槛矩阵 {generated[:16].replace('T', ' ')} | 已定胜负 {settled_n} 笔 | 口径: CLV正+正率>55%")
    lines.append("")
    all_markets = set(markets.keys()) | set(roi.keys())
    rows = []
    for sm in all_markets:
        thr = markets.get(sm)
        det = details.get(sm, {})
        r = roi.get(sm)
        n = det.get("n") or 0
        med = det.get("median")
        pos = det.get("pos_rate")
        # 只保留 CLV 为正 且 正率>55% 的盘口(其余不展示, 精简日报)
        if med is None or med <= 0:
            continue
        if pos is None or pos <= 55:
            continue
        med_s = f"{med:+.1f}%"
        pos_s = f"{pos:.0f}%"
        thr_s = f"{thr:.0f}%" if thr is not None else "—"
        if r:
            roi_s = f"{r['roi']:+.1f}%({r['n']}注)"
            concl = "✅真edge" if r['roi'] > 0 else "⚠️CLV正ROI负"
        else:
            roi_s = None
            concl = "⏳待结算"
        rows.append((sm, thr_s, n, med_s, pos_s, roi_s, concl))
    rows.sort(key=lambda x: -(x[2] if isinstance(x[2], int) else 0))

    if not rows:
        lines.append("（暂无 CLV 正 + 正率>55% 的盘口）")
    for sm, thr_s, n, med_s, pos_s, roi_s, concl in rows:
        roi_txt = f"ROI {roi_s}" if roi_s else "ROI 无结算"
        lines.append(f"• {sm}: 门槛{thr_s} | CLV {med_s}(正率{pos_s}) n={n} | {roi_txt} {concl}")

    # 门槛变动对比
    lines.append("")
    if prev and prev.get("markets"):
        prev_markets = prev["markets"]
        changed = []
        for sm in sorted(set(markets) | set(prev_markets)):
            old = prev_markets.get(sm)
            new = markets.get(sm)
            if old != new:
                if old is None:
                    changed.append(f"  ➕ {sm}: 新增 门槛 {new:.0f}%")
                elif new is None:
                    changed.append(f"  ➖ {sm}: 移除 (原 {old:.0f}%)")
                else:
                    arrow = "⬆️" if new > old else ("⬇️" if new < old else "➖")
                    changed.append(f"  {sm}: {old:.0f}% → {new:.0f}% {arrow}")
        if changed:
            lines.append("📈 门槛变动（对比上次）:")
            lines.extend(changed)
        else:
            lines.append("📈 门槛变动: 无变化")
    else:
        lines.append("📈 门槛变动: 首次运行，无对比基准")

    save_state(markets, generated)
    return "\n".join(lines)


def main():
    body = build_report()
    try:
        from config.settings import send_dingtalk
        ok = send_dingtalk("数据盘口日报", body, timeout=10)
        print("✅ 已推送" if ok else "⚠️ 推送失败(配额或钉钉失败)")
    except Exception as e:
        print(f"❌ 推送异常: {e}")
        print(body)


if __name__ == "__main__":
    main()
