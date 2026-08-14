"""权重矩阵自动校准器 — 基于 Pin 收盘数据 + 我们结算数据的双重验证。

触发条件:
  1. 定时: 每月1号自动运行, 下载最新 Football-Data 赛季数据
  2. 偏差: 我们实际ROI vs Pin预测ROI 偏差 >5% → 触发校准
  3. 手动: python3 -m src.backtest.weight_calibrator --force

输出:
  - 更新后的权重建议 (config/weight_matrix_v5.py 中的数组)
  - 变动报告 (data/reports/weight_changes_YYYY-MM-DD.json)

用法:
  python3 -m src.backtest.weight_calibrator           # 检查是否需要更新
  python3 -m src.backtest.weight_calibrator --force   # 强制执行
  python3 -m src.backtest.weight_calibrator --report  # 仅生成差异报告
"""
import csv, json, os, sys, glob, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

# ── 配置 ──
CALIBRATION_INTERVAL_DAYS = 30       # 校准间隔(天)
SIGNIFICANT_ROI_SHIFT = 2.0          # ROI变动超过2%绝对值为"显著"
FOOTBALL_DATA_DIR = DATA_DIR / "history" / "football_data"
CALIBRATION_LOG = DATA_DIR / "reports" / "weight_calibration_log.json"
LAST_CALIBRATION_FILE = DATA_DIR / "reports" / "last_calibration.txt"

LG_NAMES = {
    'E0': '英超', 'E1': '英冠', 'E2': '英甲', 'E3': '英乙',
    'D1': '德甲', 'D2': '德乙', 'I1': '意甲', 'I2': '意乙',
    'SP1': '西甲', 'SP2': '西乙', 'F1': '法甲', 'F2': '法乙',
    'N1': '荷甲', 'P1': '葡超', 'T1': '土超', 'B1': '比甲',
    'SC0': '苏超', 'G1': '希超',
}

ODDS_BUCKETS = [
    ('<2.0',    0,    2.0),
    ('2.0-3.0', 2.0,  3.0),
    ('3.0-4.0', 3.0,  4.0),
    ('4.0-5.0', 4.0,  5.0),
    ('5.0-7.0', 5.0,  7.0),
    ('7.0-10.0',7.0, 10.01),
    ('>10.0',  10.01, 999),
]


def load_pin_data():
    """加载所有已下载的 Football-Data CSV 文件。"""
    all_matches = []
    for fpath in glob.glob(str(FOOTBALL_DATA_DIR / "*.csv")):
        fname = os.path.basename(fpath)
        if '_2627' in fname:  # 空赛季
            continue
        lg_code = fname.split('_')[0]
        lg_name = LG_NAMES.get(lg_code, lg_code)
        with open(fpath, encoding='utf-8', errors='replace') as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    ph = float(r.get('PSH', 0) or 0)
                    if ph < 1.01:
                        continue
                    all_matches.append({
                        'league': lg_name,
                        'hg': int(float(r.get('FTHG', 0) or 0)),
                        'ag': int(float(r.get('FTAG', 0) or 0)),
                        'result': r.get('FTR', '').strip(),
                        'ph': ph,
                        'pd': float(r.get('PSD', 0) or 0),
                        'pa': float(r.get('PSA', 0) or 0),
                    })
                except (ValueError, KeyError):
                    pass
    return all_matches


def calculate_roi_matrix(matches):
    """计算 league × odds_range 的 Pin 收盘 ROI 矩阵。"""
    matrix = defaultdict(lambda: defaultdict(lambda: {'bets': 0, 'stake': 0, 'ret': 0}))

    for m in matches:
        lg = m['league']
        for odds, outcome in [(m['ph'], 'H'), (m['pd'], 'D'), (m['pa'], 'A')]:
            if odds <= 1.0:
                continue
            for bname, lo, hi in ODDS_BUCKETS:
                if lo <= odds < hi:
                    matrix[lg][bname]['bets'] += 1
                    matrix[lg][bname]['stake'] += 1
                    if m['result'] == outcome:
                        matrix[lg][bname]['ret'] += odds
                    break

    # 计算 ROI
    roi_matrix = {}
    for lg, buckets in matrix.items():
        roi_matrix[lg] = {}
        for bname, data in buckets.items():
            if data['bets'] >= 15:
                roi = (data['ret'] - data['stake']) / data['stake'] * 100
                roi_matrix[lg][bname] = {
                    'roi': round(roi, 2),
                    'bets': data['bets'],
                }

    return roi_matrix


def roi_to_weight(pin_roi: float) -> int:
    """将 Pin 收盘 ROI 转换为建议权重 (0-6%)。

    PinROI + BB溢价(~7%) > 0 → 正期望.
    """
    effective_roi = pin_roi + 7.0  # BB溢价
    if effective_roi > 5:
        return 6
    elif effective_roi > 2:
        return 4
    elif effective_roi > 0:
        return 2
    elif effective_roi > -3:
        return 1
    else:
        return 0


def generate_weight_recommendations(roi_matrix):
    """根据 ROI 矩阵生成权重建议。"""
    recommendations = {}
    for lg, buckets in roi_matrix.items():
        weights = []
        for bname, _, _ in ODDS_BUCKETS:
            data = buckets.get(bname)
            if data and data['bets'] >= 15:
                w = roi_to_weight(data['roi'])
            else:
                w = 1  # 默认保守
            weights.append(w)
        recommendations[lg] = weights
    return recommendations


def compare_with_current(recommendations):
    """对比建议权重与当前权重矩阵, 返回变动列表。"""
    from config.weight_matrix_v5 import FB_1X2_WEIGHTS

    changes = []
    for lg, new_weights in recommendations.items():
        # 查找匹配的当前权重
        current = None
        for keyword, weights in sorted(FB_1X2_WEIGHTS.items(), key=lambda x: -len(x[0])):
            if keyword == "_DEFAULT":
                continue
            if len(keyword) <= 2 and not lg.startswith(keyword):
                continue
            if keyword in lg:
                current = weights
                break
        if current is None:
            current = FB_1X2_WEIGHTS.get("_DEFAULT", [3,3,1,1,1,0,0])

        # 逐区间对比
        bucket_names = [b[0] for b in ODDS_BUCKETS]
        for i in range(min(len(new_weights), len(current))):
            if abs(new_weights[i] - current[i]) >= 2:  # 至少2%变化才报告
                changes.append({
                    'league': lg,
                    'bucket': bucket_names[i],
                    'old_weight': current[i],
                    'new_weight': new_weights[i],
                    'delta': new_weights[i] - current[i],
                })

    return changes


def should_calibrate(force: bool = False) -> bool:
    """判断是否需要校准。"""
    if force:
        return True

    if not LAST_CALIBRATION_FILE.exists():
        return True

    try:
        last_ts = float(LAST_CALIBRATION_FILE.read_text().strip())
        days_since = (time.time() - last_ts) / 86400
        return days_since >= CALIBRATION_INTERVAL_DAYS
    except (ValueError, OSError):
        return True


def check_settlement_divergence() -> list:
    """检查我们实际 ROI 与 Pin 预测 ROI 的偏差。"""
    pf_file = DATA_DIR / "storage" / "virtual_portfolio.json"
    if not pf_file.exists():
        return []

    pf = json.loads(pf_file.read_text())
    history = pf.get("history", [])
    settled = [h for h in history if h.get("status") in ("won", "lost")]
    if len(settled) < 50:
        return []

    # 按联赛+赔率区间 计算实际 ROI
    actual_roi = defaultdict(lambda: {'stake': 0, 'profit': 0})
    for h in settled:
        lg = h.get("league", "")
        odds = h.get("odds", 0)
        for bname, lo, hi in ODDS_BUCKETS:
            if lo <= odds < hi:
                key = f"{lg}|{bname}"
                actual_roi[key]['stake'] += h.get("stake", 0)
                actual_roi[key]['profit'] += h.get("profit", 0)
                break

    # 与 Pin 预测对比
    matches = load_pin_data()
    pin_roi = calculate_roi_matrix(matches)

    divergences = []
    for key, data in actual_roi.items():
        if data['stake'] < 100:  # 至少 ¥100 投注
            continue
        actual = data['profit'] / data['stake'] * 100
        lg, bname = key.split('|')
        pin_data = pin_roi.get(lg, {}).get(bname)
        if pin_data:
            predicted = pin_data['roi'] + 7.0  # +BB溢价
            if abs(actual - predicted) > 5.0:
                divergences.append({
                    'league': lg,
                    'bucket': bname,
                    'actual_roi': round(actual, 1),
                    'predicted_roi': round(predicted, 1),
                    'delta': round(actual - predicted, 1),
                })

    return divergences


def run_calibration(force: bool = False, dry_run: bool = True):
    """执行校准流程。"""
    if not should_calibrate(force):
        logger.info("未到校准时间 (上次: %s)",
                     datetime.fromtimestamp(float(LAST_CALIBRATION_FILE.read_text().strip())).strftime("%Y-%m-%d"))
        return None

    logger.info("开始权重矩阵校准...")

    # 1. 加载 Pin 数据
    matches = load_pin_data()
    logger.info("加载 %d 场比赛", len(matches))

    # 2. 计算 ROI 矩阵
    roi = calculate_roi_matrix(matches)

    # 3. 生成权重建议
    recommendations = generate_weight_recommendations(roi)

    # 4. 对比当前权重
    changes = compare_with_current(recommendations)
    logger.info("发现 %d 处显著变化", len(changes))

    # 5. 检查结算偏差
    divergences = check_settlement_divergence()
    if divergences:
        logger.warning("发现 %d 处实际vs预测偏差 >5%", len(divergences))
        for d in divergences[:5]:
            logger.warning("  %s %s: 实际%+.1f%% vs 预测%+.1f%% (差%+.1f%%)",
                           d['league'], d['bucket'], d['actual_roi'], d['predicted_roi'], d['delta'])

    # 6. 保存结果
    report = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'total_matches': len(matches),
        'changes': changes,
        'divergences': divergences,
        'total_significant_changes': len(changes),
    }

    CALIBRATION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_LOG, 'w') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 7. 如果非 dry_run, 自动更新权重
    if not dry_run and changes:
        _apply_weight_changes(changes)
        logger.info("已自动应用 %d 处权重变动", len(changes))

    # 8. 更新时间戳
    LAST_CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_CALIBRATION_FILE.write_text(str(time.time()))

    return report


def _apply_weight_changes(changes: list):
    """自动应用权重变动到 config/weight_matrix_v5.py。"""
    # 读取当前文件
    wm_file = ROOT / "config" / "weight_matrix_v5.py"
    content = wm_file.read_text()

    # 对每个变动, 在文件中找到对应行并修改
    for c in changes:
        lg = c['league']
        old_w = c['old_weight']
        new_w = c['new_weight']
        bucket_idx = [b[0] for b in ODDS_BUCKETS].index(c['bucket'])

        # 找到匹配行 (如 '    "德甲": [4, 6, 4, 4, 2, 2, 2],')
        import re
        pattern = rf'(\s+\"{lg}\":\s+\[)([^\]]+)(\])'
        match = re.search(pattern, content)
        if match:
            weights_str = match.group(2)
            weights = [int(w.strip()) for w in weights_str.split(',')]
            if bucket_idx < len(weights):
                weights[bucket_idx] = new_w
            new_weights_str = ', '.join(str(w) for w in weights)
            content = content[:match.start(2)] + new_weights_str + content[match.end(2):]

    # 备份后写入
    backup = wm_file.with_suffix('.py.bak.' + str(int(time.time())))
    backup.write_text(wm_file.read_text())
    wm_file.write_text(content)


def main():
    force = "--force" in sys.argv
    report_only = "--report" in sys.argv

    if report_only:
        matches = load_pin_data()
        roi = calculate_roi_matrix(matches)
        recs = generate_weight_recommendations(roi)
        changes = compare_with_current(recs)

        print("权重校准报告 ({})".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
        print("数据: {} 场比赛".format(len(matches)))
        print()
        if changes:
            print("显著变动 (>=2% 权重差异):")
            for c in changes:
                direction = "↑" if c['delta'] > 0 else "↓"
                print("  {} {}: {}% → {}% {}".format(
                    c['league'], c['bucket'], c['old_weight'], c['new_weight'], direction))
        else:
            print("无显著变动。当前权重与 Pin 数据一致。")

        divergences = check_settlement_divergence()
        if divergences:
            print()
            print("实际vs预测偏差 (>5%):")
            for d in divergences[:10]:
                print("  {} {}: 实际{:+.1f}% vs 预测{:+.1f}%".format(
                    d['league'], d['bucket'], d['actual_roi'], d['predicted_roi']))
    else:
        dry_run = "--apply" not in sys.argv
        report = run_calibration(force=force, dry_run=dry_run)
        if report:
            print("校准完成. {} 处变化, {} 处偏差.".format(
                len(report['changes']), len(report['divergences'])))
            if dry_run:
                print("Dry run — 未应用. 加 --apply 生效.")


if __name__ == "__main__":
    from config.logging_config import setup_logging
    setup_logging()
    main()
