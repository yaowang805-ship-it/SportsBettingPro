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


_SPORT_CN = {"football": "足球", "basketball": "篮球", "tennis": "网球",
             "baseball": "棒球", "american_football": "美足", "ice_hockey": "冰球"}


def _direction(desig):
    """designation('让分主胜(+3.5)'/'大球'/'主胜') → 方向(主/客/和/大/小)。"""
    d = desig or ''
    if '大' in d:
        return '大'
    if '小' in d:
        return '小'
    if '和' in d or '平' in d:
        return '和'
    if '主' in d:
        return '主'
    if '客' in d:
        return '客'
    return '其他'


def _forward_clv_by_market(min_n=20):
    """实时 CLV 按 (运动, 盘口, 方向) 分组统计(过程指标)。

    正 CLV = 真优势(赢率 vs 隐含 判据应该也正); 负 CLV = 假溢价。
    与释放判据的「赢率 vs 隐含」交叉验证, 两者一致才采信。
    返回 [(sport_cn, sub, dr, n, mean, positive_pct)]。
    """
    from collections import defaultdict
    f = DATA_DIR / "clv_results.csv"
    if not f.exists():
        return []
    agg = defaultdict(list)
    seen = set()
    for r in csv.reader(open(f)):
        if len(r) <= 19:
            continue
        k = (r[2], r[9])  # (match_key, designation) 去重(同一场同盘口多次采集取一次)
        if k in seen:
            continue
        seen.add(k)
        try:
            clv = float(r[19])
        except (ValueError, TypeError):
            continue
        agg[(r[3], r[10], _direction(r[9]))].append(clv)
    out = []
    for (sport, sub, dr), clvs in sorted(agg.items(), key=lambda t: -len(t[1])):
        n = len(clvs)
        if n < min_n:  # 样本太少是噪声, 不展示
            continue
        mean = statistics.mean(clvs)
        pos = sum(1 for c in clvs if c > 0) / n * 100
        out.append((_SPORT_CN.get(sport, sport), sub, dr, n, mean, pos))
    return out


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

    # 盘口×方向 CLV 明细(过程指标, 2026-09-13): 正=真优势 / 负=假溢价,
    # 与释放判据「赢率 vs 隐含」交叉验证(两者一致才采信)。
    by_market = _forward_clv_by_market()
    if by_market:
        lines.append("**盘口×方向 CLV 明细**（正=真优势 / 负=假溢价，与赢率vs隐含交叉验证）")
        lines.append("| 运动·盘口·方向 | n | 均值CLV | 正率 |")
        lines.append("|---|---|---|---|")
        for sport_cn, sub, dr, n, mean, pos in by_market:
            lines.append(f"| {sport_cn}·{sub}·{dr} | {n} | {mean:+.1f}% | {pos:.0f}% |")
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

    # 门槛精细化进度: 离"每个运动×联赛×盘口都数据驱动"还有多远(scripts/clv_cell_coverage.py 产出)
    cov_file = DATA_DIR / "clv_cell_coverage.json"
    if cov_file.exists():
        try:
            cov = json.loads(cov_file.read_text())
            lines.append("")
            lines.append(f"门槛下沉进度(样本 {cov['n_samples']} 条 / 其中真实投注 {cov['n_push']} 条, "
                         f"{cov['rate_per_day']:.0f} 条/天):")
            for lvl, d in cov.get("levels", {}).items():
                lines.append(f"- {lvl}: {d['confirmed']}确认 / {d['directional']}方向性 "
                             f"/ {d['insufficient']}样本不足 (共{d['cells']}格)")
            ready = cov.get("push_ready_markets") or []
            lines.append(f"- 真实投注库已达 n≥30 的盘口: {'、'.join(ready) if ready else '无'}")
        except (json.JSONDecodeError, ValueError, KeyError, OSError) as e:
            logger.debug("格子覆盖读取失败: %s", e)

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
