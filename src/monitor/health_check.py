#!/usr/bin/env python3
"""SportsBettingPro 系统整体健康检查与告警。"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from config.settings import DATA_DIR, DINGTALK_WEBHOOK, DEFAULT_BUDGET
from config.logging_config import get_logger
logger = get_logger(__name__)

HEALTH_FILE = DATA_DIR / 'system_health.json'

def check_model_health():
    """检查模型训练状态。"""
    try:
        from src.models.auto_retrain import get_model_health
        return get_model_health()
    except Exception as e:
        return {'error': str(e)}

def check_risk_health():
    """检查风险管理系统状态。"""
    try:
        from src.risk.manager import RiskManager
        rm = RiskManager()
        return rm.get_health_check()
    except Exception as e:
        return {'error': str(e)}

def check_performance_health():
    """检查性能监控状态。"""
    try:
        perf_file = DATA_DIR / "performance_history.csv"
        if not perf_file.exists():
            return {'total_bets': 0, 'win_rate': 0, 'roi': 0}

        import pandas as pd
        df = pd.read_csv(perf_file)
        if df.empty:
            return {'total_bets': 0, 'win_rate': 0, 'roi': 0}

        resolved = df[df['result'].isin(['won', 'lost'])].copy()
        pending = df[df['result'] == 'pending'].copy()
        total_bets = len(resolved)
        winning_bets = len(resolved[resolved['result'] == 'won'])
        win_rate = winning_bets / total_bets if total_bets > 0 else 0

        final_balance = resolved['cumulative_balance'].iloc[-1] if not resolved.empty and 'cumulative_balance' in resolved.columns else DEFAULT_BUDGET
        roi = (final_balance - DEFAULT_BUDGET) / DEFAULT_BUDGET if DEFAULT_BUDGET > 0 else 0

        max_drawdown = 0.0
        if 'cumulative_balance' in resolved.columns and not resolved.empty:
            peak = resolved['cumulative_balance'].cummax()
            drawdowns = (resolved['cumulative_balance'] - peak) / peak.replace(0, 1)
            max_drawdown = drawdowns.min() if not drawdowns.empty else 0.0
        avg_stake = float(resolved['stake'].mean()) if 'stake' in resolved.columns and not resolved.empty else 0.0
        return {
            'total_bets': total_bets,
            'pending_bets': len(pending),
            'win_rate': win_rate,
            'roi': roi,
            'current_balance': final_balance,
            'avg_stake': avg_stake,
            'max_drawdown': max_drawdown,
        }
    except Exception as e:
        return {'error': str(e)}

def check_api_connectivity():
    """检查API连接状态。"""
    try:
        from config.settings import (
            ODDS_API_KEY,
            ODDS_API_IO_KEY,
            FOOTBALL_ODDS_API_KEY,
            FOOTBALL_API_KEY,
            BASKETBALL_API_KEY,
        )
        import requests

        results = {
            'odds_api': False,
            'football_api': bool(FOOTBALL_API_KEY),
            'basketball_api': bool(BASKETBALL_API_KEY),
        }

        def try_the_odds(url, params):
            try:
                resp = requests.get(url, params=params, timeout=10)
                return resp.status_code == 200
            except Exception as exc:
                results.setdefault('odds_api_errors', []).append(str(exc))
                return False

        if BASKETBALL_API_KEY:
            results['odds_api_basketball_key'] = try_the_odds(
                'https://api.the-odds-api.com/v4/sports',
                {'apiKey': BASKETBALL_API_KEY},
            )
            if results['odds_api_basketball_key']:
                results['odds_api'] = True

        if not results['odds_api'] and FOOTBALL_ODDS_API_KEY:
            results['odds_api_football_odds_key'] = try_the_odds(
                'https://api.the-odds-api.com/v4/sports',
                {'apiKey': FOOTBALL_ODDS_API_KEY},
            )
            if results['odds_api_football_odds_key']:
                results['odds_api'] = True

        if not results['odds_api'] and ODDS_API_KEY:
            results['odds_api_generic_key'] = try_the_odds(
                'https://api.the-odds-api.com/v4/sports',
                {'apiKey': ODDS_API_KEY},
            )
            if results['odds_api_generic_key']:
                results['odds_api'] = True

        if not results['odds_api'] and ODDS_API_IO_KEY:
            results['odds_api_io_key'] = try_the_odds(
                'https://api.odds-api.io/v4/sports',
                {'apiKey': ODDS_API_IO_KEY},
            )
            if results['odds_api_io_key']:
                results['odds_api'] = True

        if not results['odds_api'] and 'odds_api_errors' not in results:
            results['odds_api_errors'] = ['未配置有效Odds API key']

        return results
    except Exception as e:
        return {'error': str(e)}

def generate_health_report():
    """生成完整的健康报告。"""
    now = datetime.now()

    report = {
        'timestamp': now.isoformat(),
        'model_health': check_model_health(),
        'risk_health': check_risk_health(),
        'performance_health': check_performance_health(),
        'api_connectivity': check_api_connectivity(),
    }

    # 保存报告
    with open(HEALTH_FILE, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report

def analyze_health_report(report):
    """分析健康报告，生成告警。"""
    alerts = []

    # 模型健康检查
    model = report.get('model_health', {})
    if model.get('error'):
        alerts.append(f"❌ 模型系统错误: {model['error']}")
    elif model.get('needs_retrain'):
        alerts.append("⚠️ 模型需要重新训练")

    # 风险健康检查
    risk = report.get('risk_health', {})
    if risk.get('error'):
        alerts.append(f"❌ 风险管理系统错误: {risk['error']}")
    elif not risk.get('under_daily_limit', True):
        alerts.append("🚨 触发日亏损限额")
    elif not risk.get('under_monthly_limit', True):
        alerts.append("🚨 触发月亏损限额")

    # 性能健康检查
    perf = report.get('performance_health', {})
    if perf.get('error'):
        alerts.append(f"❌ 性能监控错误: {perf['error']}")
    elif perf.get('total_bets', 0) >= 20:
        if perf.get('win_rate', 0) < 0.48:
            alerts.append(f"⚠️ 胜率偏低: {perf['win_rate']:.1%}")
        if perf.get('roi', 0) < -0.05:
            alerts.append(f"⚠️ 累计亏损: {perf['roi']:.1%}")
        if perf.get('max_drawdown', 0) < -0.15:
            alerts.append(f"⚠️ 最大回撤过大: {perf['max_drawdown']:.1%}")
    elif perf.get('total_bets', 0) == 0 and perf.get('pending_bets', 0) > 0:
        alerts.append(f"⚠️ 当前暂无已结算投注，仅 {perf['pending_bets']} 笔待结算记录")

    # API连接检查
    api = report.get('api_connectivity', {})
    if not api.get('odds_api'):
        alerts.append("❌ Odds API 连接失败")

    return alerts

def send_health_alert(alerts):
    """发送健康告警到钉钉。"""
    if not alerts or not DINGTALK_WEBHOOK:
        return

    message = "🏥 SportsBettingPro 系统健康检查\n\n" + "\n".join(alerts)

    try:
        import requests
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "系统健康告警", "text": message}
        }
        resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("📤 健康告警已发送")
        else:
            logger.error("⚠️ 健康告警发送失败: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("⚠️ 告警发送失败: %s", e)

def main():
    logger.info("🔍 SportsBettingPro 系统健康检查")
    logger.info("=" * 50)

    report = generate_health_report()

    # 打印报告
    logger.info("📊 模型健康:")
    model = report['model_health']
    if 'error' in model:
        logger.error("  ❌ 错误: %s", model['error'])
    else:
        logger.info("  最后训练: %s", model.get('last_trained', '未知'))
        logger.info("  距今天数: %s", model.get('days_since_train', '未知'))
        logger.info("  需要重训: %s", '是' if model.get('needs_retrain') else '否')

    logger.info("\n💰 风险管理:")
    risk = report['risk_health']
    if 'error' in risk:
        logger.error("  ❌ 错误: %s", risk['error'])
    else:
        logger.info("  当前资金: %.0f", risk.get('balance', 0))
        logger.info("  ROI: %+.1f%%", risk.get('roi', 0) * 100)
        logger.info("  回撤: %.1f%%", risk.get('drawdown', 0) * 100)

    logger.info("\n📈 性能监控:")
    perf = report['performance_health']
    if 'error' in perf:
        logger.error("  ❌ 错误: %s", perf['error'])
    else:
        logger.info("  总投注: %s", perf.get('total_bets', 0))
        logger.info("  胜率: %.1f%%", perf.get('win_rate', 0) * 100)
        logger.info("  ROI: %+.1f%%", perf.get('roi', 0) * 100)

    logger.info("\n🌐 API连接:")
    api = report.get('api_connectivity', {})
    for name, status in api.items():
        if status == True:
            logger.info("  ✅ %s", name)
        elif status == False:
            logger.error("  ❌ %s", name)
        elif status == 'no_key':
            logger.warning("  ⚠️ %s: 未配置密钥", name)
        elif 'error' in str(status):
            logger.error("  ❌ %s: %s", name, status)

    # 分析并发送告警
    alerts = analyze_health_report(report)
    if alerts:
        logger.warning("\n🚨 发现 %d 个问题:", len(alerts))
        for alert in alerts:
            logger.info("  %s", alert)
        send_health_alert(alerts)
    else:
        logger.info("\n✅ 系统运行正常")

    logger.info("\n💾 详细报告已保存至: %s", HEALTH_FILE)

if __name__ == '__main__':
    main()
