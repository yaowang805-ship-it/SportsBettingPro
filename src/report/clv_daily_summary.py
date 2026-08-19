"""每日 CLV 汇总 — 汇总实时采集 + 归档库回溯两条线的 CLV, 推钉钉。

用于验证套利模式: CLV>0 说明 BB 开盘价长期高于 Pinnacle 收盘公平价(真优势),
CLV≈0/负 说明 +EV 是幻影(市场收敛)。实时采集口径与归档回溯口径统一为去抽水公平价。

用法:
    .venv312/bin/python -m src.report.clv_daily_summary [--no-push]
"""
import json, csv, statistics, argparse
from config.settings import DATA_DIR, send_dingtalk
from config.logging_config import get_logger

logger = get_logger(__name__)


def _load_forward_clv():
    """实时采集的 CLV (clv_results.csv, 列19=true_clv_pct)。"""
    f = DATA_DIR / "clv_results.csv"
    if not f.exists():
        return []
    seen = set()
    clvs = []
    for r in csv.reader(open(f)):
        if len(r) <= 19:
            continue
        k = (r[2], r[9])
        if k in seen:
            continue
        seen.add(k)
        try:
            clvs.append(float(r[19]))
        except (ValueError, TypeError):
            continue
    return clvs


def _load_archive_clv():
    """归档库回溯的 CLV (clv_archive_results.json)。"""
    f = DATA_DIR / "clv_archive_results.json"
    if not f.exists():
        return []
    try:
        d = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return [s['clv'] for s in d.get('samples', []) if 'clv' in s]


def _stats(clvs):
    if not clvs:
        return None
    return {
        'n': len(clvs),
        'mean': round(statistics.mean(clvs), 2),
        'median': round(statistics.median(clvs), 2),
        'positive_pct': round(sum(1 for c in clvs if c > 0) / len(clvs) * 100, 1),
    }


def main(push: bool = True):
    fwd = _load_forward_clv()
    arc = _load_archive_clv()
    s_fwd = _stats(fwd)
    s_arc = _stats(arc)
    all_clv = fwd + arc
    s_all = _stats(all_clv)

    lines = ["**投注推荐 · CLV 验证日报**", ""]
    lines.append("📊 CLV = (BB开盘价 - Pinnacle去抽水公平收盘价) / 公平收盘价，>0 才是真优势")
    lines.append("")
    lines.append("| 口径 | 样本 | 均值 | 中位 | 正率 |")
    lines.append("|---|---|---|---|---|")
    for name, s in [("实时采集(赛前1-20min)", s_fwd), ("归档库回溯", s_arc), ("合计", s_all)]:
        if s:
            lines.append(f"| {name} | {s['n']} | {s['mean']:+.1f}% | {s['median']:+.1f}% | {s['positive_pct']:.0f}% |")
        else:
            lines.append(f"| {name} | 0 | — | — | — |")
    lines.append("")

    # V5.10: 先报采集覆盖率再报结论 —— 样本残缺时结论没有意义。
    # 这里以前只报"采到多少条", 没有分母, 丢失率一度 57% 却全程无感。
    got = started = 0
    loss = 0.0
    try:
        import sys
        sys.path.insert(0, str(DATA_DIR.parent.parent))
        from scripts.clv_stats import compute_coverage, LOSS_ALERT_THRESHOLD
        got, started, loss = compute_coverage()
    except Exception as e:
        logger.debug("覆盖率计算失败: %s", e)
        LOSS_ALERT_THRESHOLD = 25.0
    if started:
        lines.append(f"采集覆盖: 已开赛 {started} 条 → 采到 {got} 条，丢失 {started - got} 条 ({loss:.0f}%)")
        if loss > LOSS_ALERT_THRESHOLD:
            lines.append(f"⚠️ **丢失率 {loss:.0f}% 超阈值 {LOSS_ALERT_THRESHOLD:.0f}%** — "
                         "样本不完整，下面的结论不可信。先查 Pinnacle 连通性与采集窗口。")
        lines.append("")

    if s_all and loss <= LOSS_ALERT_THRESHOLD:
        verdict = ("✅ 正 CLV，套利模式可能有效" if s_all['median'] > 1
                   else ("⚠️ CLV≈0，无优势(市场有效)" if abs(s_all['median']) <= 1
                         else "❌ 负 CLV，+EV 是幻影"))
        lines.append(f"结论: {verdict}")
    elif s_all:
        lines.append("结论: 暂缓判定 — 采集丢失率过高，样本有偏(丢的多是 Pin 连不上那批，非随机)")
    else:
        lines.append("结论: 暂无样本，等待新推送结算")

    body = "\n".join(lines)
    logger.info("CLV 日报: 实时%d + 归档%d = %d", len(fwd), len(arc), len(all_clv))
    if push:
        ok = send_dingtalk("CLV 验证日报", body, timeout=10)
        if not ok:
            logger.warning("CLV 日报推送失败")
    else:
        print(body)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-push', action='store_true')
    args = ap.parse_args()
    main(push=not args.no_push)
