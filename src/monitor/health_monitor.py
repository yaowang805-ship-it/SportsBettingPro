#!/usr/bin/env python3
"""模型健康度实时监控：滚动Brier / 胜率 / 对数损失 + 自动告警"""
import pandas as pd
import json
import sys
from pathlib import Path
from sklearn.metrics import brier_score_loss
import requests

sys.path.insert(0, str(Path.cwd()))

from config.settings import DINGTALK_WEBHOOK
from config.logging_config import get_logger
logger = get_logger(__name__)

DATA_DIR = Path("data/storage")
PERF_FILE = DATA_DIR / "performance_history.csv"
ALERT_THRESHOLD = {
    'brier_30d': 0.25,      # 30天滚动Brier超过0.25告警
    'winrate_15d': 0.45,    # 15日胜率低于45%告警
    'consecutive_lose': 7,  # 连续亏损7天告警
}

def check_model_health():
    """检查模型健康度，异常时发送钉钉告警"""
    if not PERF_FILE.exists():
        return {'brier_30d': None, 'winrate_15d': None, 'consecutive_lose': 0, 'alerts': [], 'status': '无历史数据'}

    df = pd.read_csv(PERF_FILE, parse_dates=['date'])
    if len(df) < 30:
        return {'brier_30d': None, 'winrate_15d': None, 'consecutive_lose': 0, 'alerts': [], 'status': '数据不足30天'}

    # 只分析有结果的记录
    settled = df[df['result'] != 'pending'].copy()
    if len(settled) < 10:
        return {'brier_30d': None, 'winrate_15d': None, 'consecutive_lose': 0, 'alerts': [], 'status': '有效记录不足'}

    settled['date'] = pd.to_datetime(settled['date'])
    settled = settled.sort_values('date')

    # 计算真实标签（won=1, lost=0）
    settled['actual'] = (settled['result'] == 'won').astype(int)

    # 30天滚动Brier
    last_30 = settled.tail(30)
    brier_30 = brier_score_loss(last_30['actual'], last_30['prob'])

    # 15日胜率
    last_15 = settled.tail(15)
    winrate_15 = last_15['actual'].mean()

    # 连续亏损天数
    settled['is_loss'] = (settled['profit'] <= 0).astype(int)
    consecutive_lose = 0
    for val in reversed(settled['is_loss'].values):
        if val == 1:
            consecutive_lose += 1
        else:
            break

    alerts = []
    if brier_30 > ALERT_THRESHOLD['brier_30d']:
        alerts.append(f"30日Brier={brier_30:.3f} > {ALERT_THRESHOLD['brier_30d']}")
    if winrate_15 < ALERT_THRESHOLD['winrate_15d']:
        alerts.append(f"15日胜率={winrate_15:.1%} < {ALERT_THRESHOLD['winrate_15d']:.0%}")
    if consecutive_lose >= ALERT_THRESHOLD['consecutive_lose']:
        alerts.append(f"连续亏损{consecutive_lose}天")

    # 发送钉钉告警
    if alerts and DINGTALK_WEBHOOK:
        try:
            from config.settings import send_dingtalk
            body = "🚨 投注推荐系统健康告警\n\n" + "\n".join([f"• {a}" for a in alerts]) + \
                   f"\n\nBrier(30d)={brier_30:.3f}\nWinRate(15d)={winrate_15:.1%}\n连续亏损={consecutive_lose}天"
            send_dingtalk("系统健康告警", body)
        except Exception:
            logger.error("⚠️ 钉钉告警发送失败")

    return {
        'brier_30d': round(brier_30, 4),
        'winrate_15d': round(winrate_15, 4),
        'consecutive_lose': consecutive_lose,
        'alerts': alerts,
        'status': 'ok'
    }

if __name__ == "__main__":
    result = check_model_health()
    logger.info("健康检查结果: %s", json.dumps(result, indent=2, ensure_ascii=False))
