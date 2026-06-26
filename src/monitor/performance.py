#!/usr/bin/env python3
"""绩效监控 — 自动结果匹配 + 资金曲线跟踪 + 告警。

工作流:
  1. 从 virtual_portfolio.json 同步已结算投注
  2. 从历史比赛 CSV 匹配 pending 投注
  3. 生成绩效报告 → 更新 health_check
"""
import json
import sys
from pathlib import Path
from datetime import datetime

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import DATA_DIR, DINGTALK_WEBHOOK, DEFAULT_BUDGET
from config.logging_config import get_logger, setup_logging
logger = get_logger(__name__)

PERF_FILE = DATA_DIR / "performance_history.csv"
PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"
HEALTH_FILE = DATA_DIR / "system_health.json"

from src.monitor.result_matcher import auto_settle, _read_perf, _recalc


def update_performance():
    """主入口：自动结算 → 更新资金曲线 → 写健康检查。"""
    # 1. 自动结算 pending 记录
    auto_settle()

    # 2. 读取 perf 并计算指标
    perf = _read_perf()
    settled = perf[perf['result'].isin(['won', 'lost'])].copy()
    pending = perf[perf['result'] == 'pending'].copy()

    total_bets = len(settled)
    winning_bets = len(settled[settled['result'] == 'won'])
    win_rate = winning_bets / total_bets if total_bets > 0 else 0

    if not settled.empty:
        settled['profit'] = settled['profit'].astype(float)
        total_profit = settled['profit'].sum()
        cumulative = float(DEFAULT_BUDGET) + total_profit
        roi = total_profit / float(DEFAULT_BUDGET)

        if 'cumulative_balance' not in settled.columns:
            settled = _recalc(settled)
        peak = settled['cumulative_balance'].cummax()
        drawdowns = (peak - settled['cumulative_balance']) / peak.replace(0, np.nan)
        max_drawdown = drawdowns.max() if not drawdowns.empty else 0.0
        avg_stake = float(settled['stake'].mean()) if 'stake' in settled.columns else 0.0

        # ── 夏普比率（年化） ──
        returns = settled['profit'].values / float(DEFAULT_BUDGET)
        sharpe = 0.0
        if len(returns) >= 5 and returns.std() > 0:
            sharpe = (returns.mean() / returns.std()) * np.sqrt(252)

        # ── Sortino 比率（年化，仅考虑下行波动） ──
        sortino = 0.0
        if len(returns) >= 5:
            down_returns = returns[returns < 0]
            if len(down_returns) > 0 and down_returns.std() > 0:
                sortino = (returns.mean() / down_returns.std()) * np.sqrt(252)

        # ── 校准度（预测概率 vs 实际频率） ──
        calibration_error = 0.0
        if 'prob' in settled.columns:
            settle_probs = settled['prob'].astype(float).values
            settle_results = (settled['result'] == 'won').astype(int).values
            if len(settle_probs) >= 20:
                bins = np.linspace(0, 1, 11)
                bin_errors = []
                for i in range(len(bins) - 1):
                    mask = (settle_probs >= bins[i]) & (settle_probs < bins[i + 1])
                    if mask.sum() >= 5:
                        avg_pred = settle_probs[mask].mean()
                        actual = settle_results[mask].mean()
                        bin_errors.append(abs(avg_pred - actual))
                calibration_error = np.mean(bin_errors) if bin_errors else 0.0
    else:
        total_profit = 0.0
        cumulative = float(DEFAULT_BUDGET)
        roi = 0.0
        max_drawdown = 0.0
        avg_stake = 0.0
        sharpe = 0.0
        sortino = 0.0
        calibration_error = 0.0

    # 3. 打印绩效摘要
    logger.info("=" * 50)
    logger.info("📊 绩效报告 — %s", datetime.now().strftime('%Y-%m-%d %H:%M'))
    logger.info("=" * 50)
    logger.info("   已结算: %d 笔", total_bets)
    logger.info("   待结算: %d 笔", len(pending))
    logger.info("   胜: %d / 负: %d", winning_bets, total_bets - winning_bets)
    logger.info("   胜率: %.1f%%", win_rate * 100)
    logger.info("   利润: ¥%+.0f", total_profit)
    logger.info("   ROI: %+.1f%%", roi * 100)
    logger.info("   资金: ¥%.0f / ¥%.0f", cumulative, float(DEFAULT_BUDGET))
    logger.info("   回撤: %.1f%%", max_drawdown * 100)
    logger.info("   均注: ¥%.0f", avg_stake)
    if sharpe != 0:
        logger.info("   夏普(年化): %.2f", sharpe)
    if sortino != 0:
        logger.info("   Sortino(年化): %.2f", sortino)
    if calibration_error > 0:
        logger.info("   校准误差: %.1f%%", calibration_error * 100)
    logger.info("=" * 50)

    # 4. 更新 health_check 文件
    health = {
        'timestamp': datetime.now().isoformat(),
        'performance_health': {
            'total_bets': total_bets,
            'pending_bets': len(pending),
            'winning_bets': winning_bets,
            'losing_bets': total_bets - winning_bets,
            'win_rate': round(win_rate, 4),
            'roi': round(roi, 4),
            'total_profit': round(total_profit, 2),
            'current_balance': round(cumulative, 2),
            'avg_stake': round(avg_stake, 2),
            'max_drawdown': round(max_drawdown, 4),
            'sharpe_ratio': round(sharpe, 4) if sharpe != 0 else None,
            'sortino_ratio': round(sortino, 4) if sortino != 0 else None,
            'calibration_error': round(calibration_error, 4) if calibration_error > 0 else None,
        },
    }

    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_FILE, 'w', encoding='utf-8') as f:
        json.dump(health, f, ensure_ascii=False, indent=2)

    # 双写: performance_snapshots 表
    try:
        from src.storage.database import db
        db.record_performance(
            balance=round(cumulative, 2),
            equity=round(cumulative, 2),
            drawdown_pct=round(max_drawdown, 4) if max_drawdown else 0.0,
            roi=round(roi, 4) if roi else 0.0,
            win_rate=round(win_rate, 4),
            total_bets=total_bets,
            settled_bets=total_bets,
            notes="auto sync from update_performance()",
        )
    except Exception:
        pass

    # 5. 告警检查
    from src.monitor.alert_log import log_alert
    alerts = []
    if total_bets >= 20 and win_rate < 0.45:
        msg = f"胜率过低: {win_rate:.1%}"
        alerts.append(f"⚠️ {msg}")
        log_alert("performance", msg, f"近 {total_bets} 场胜率 {win_rate:.1%}", "WARNING")
    if roi < -0.10:
        msg = f"累计亏损: {roi:.1%}"
        alerts.append(f"🚨 {msg}")
        log_alert("risk", msg, f"累计 ROI {roi:.1%}", "ERROR")
    if max_drawdown > 0.15:
        msg = f"回撤过大: {max_drawdown:.1%}"
        alerts.append(f"⚠️ {msg}")
        log_alert("risk", msg, f"最大回撤 {max_drawdown:.1%}", "WARNING")
    if total_bets == 0 and len(pending) > 0:
        alerts.append(f"⏳ {len(pending)} 笔待结算")

    if alerts and DINGTALK_WEBHOOK:
        try:
            from config.settings import send_dingtalk
            body = "📊 系统绩效报告\n" + "\n".join(alerts)
            send_dingtalk("绩效报告", body)
        except Exception as e:
            logger.warning("告警发送失败: %s", e)

    return health


if __name__ == "__main__":
    setup_logging(log_to_file=False)
    update_performance()
