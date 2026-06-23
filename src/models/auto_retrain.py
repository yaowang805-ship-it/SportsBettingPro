#!/usr/bin/env python3
"""职业级月度模型重训练 — 支持退化信号触发 + 定期重训。

重训触发条件（任一达标即触发）：
  1. 距上次训练 >= frequency_days（默认30天）
  2. 模型衰减检测报告 is_decaying=True
  3. 从模型精度历史检测到显著下滑趋势
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from config.settings import MODEL_DIR

MODEL_DIR_PATH = MODEL_DIR if isinstance(MODEL_DIR, Path) else Path(MODEL_DIR)
META_FILE = MODEL_DIR_PATH / 'model_metadata.json'
DECAY_REPORT_FILE = ROOT / "data" / "storage" / "model_decay_report.json"


def load_metadata():
    if META_FILE.exists():
        with open(META_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'last_train': None, 'train_frequency_days': 30, 'models': {}}


def save_metadata(data):
    with open(META_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _check_decay_signal() -> bool:
    """读取 model_decay_report.json，检查是否有退化信号。"""
    if not DECAY_REPORT_FILE.exists():
        return False
    try:
        report = json.loads(DECAY_REPORT_FILE.read_text())
        if report.get("is_decaying"):
            logger.warning("  ⚠️ 检测到模型退化信号: %s", report.get("decay_signal", "?"))
            try:
                from src.monitor.alert_log import log_alert
                log_alert("model", "模型退化信号", report.get("decay_signal", "?"), "WARNING")
            except Exception:
                pass
            return True
        return False
    except Exception:
        return False


def _check_accuracy_drop() -> bool:
    """从 model_accuracy_history.csv 检查是否有显著下滑。"""
    csv_path = ROOT / "data" / "storage" / "model_accuracy_history.csv"
    if not csv_path.exists():
        return False
    try:
        df = pd.read_csv(csv_path)
        if len(df) < 10:
            return False
        recent = df.tail(10)["baseline"].values
        early = df.tail(20).head(10)["baseline"].values
        if len(recent) >= 5 and len(early) >= 5:
            recent_mean = recent.mean()
            early_mean = early.mean()
            if early_mean - recent_mean >= 0.05:  # 5pp 下滑
                logger.warning("  ⚠️ 准确率下滑 %.1fpp (近期 %.1f%% → 前期 %.1f%%)",
                              (early_mean - recent_mean) * 100, recent_mean * 100, early_mean * 100)
                try:
                    from src.monitor.alert_log import log_alert
                    log_alert("model", "准确率下滑", f"全局准确率 {(early_mean-recent_mean)*100:.1f}pp", "WARNING")
                except Exception:
                    pass
                return True
        return False
    except Exception:
        return False


def should_retrain(frequency_days: int = 30) -> bool:
    """检查是否需要重新训练模型（全局+按运动项目）。"""
    meta = load_metadata()
    # 条件1: 从未训练过
    if meta['last_train'] is None:
        return True
    # 条件2: 时间到期（全局阈值）
    last_train = datetime.fromisoformat(meta['last_train'])
    if (datetime.now() - last_train).days >= frequency_days:
        logger.info("  时间触发: 距上次训练 %d 天 >= %d 天",
                   (datetime.now() - last_train).days, frequency_days)
        return True
    # 条件3: 退化信号
    if _check_decay_signal():
        return True
    # 条件4: 准确率下滑
    if _check_accuracy_drop():
        return True
    return False


def _sport_should_retrain(sport: str, meta: dict, frequency_days: int = 30) -> bool:
    """检查单个运动项目是否需要重训（基于上次训练时间 + 数据更新时间）。"""
    sport_key = f'last_train_{sport}'
    last = meta.get(sport_key)
    if last is None:
        logger.info("  %s 从未训练过，需要重训", sport.upper())
        return True

    last_dt = datetime.fromisoformat(last)
    days_since = (datetime.now() - last_dt).days
    if days_since >= frequency_days:
        logger.info("  %s 距上次训练 %d 天 >= %d 天，需要重训",
                    sport.upper(), days_since, frequency_days)
        return True

    # 检查数据是否有更新
    csv_path = ROOT / "data" / "processed" / f"{sport}_features.csv"
    if csv_path.exists():
        mtime = datetime.fromtimestamp(csv_path.stat().st_mtime)
        if mtime > last_dt:
            logger.info("  %s 数据已更新 (%s)，需要重训",
                        sport.upper(), mtime.strftime("%Y-%m-%d"))
            return True

    logger.info("  %s 无需重训（%d 天前训练，数据无更新）", sport.upper(), days_since)
    return False


def mark_training_complete(sport: str = None):
    """标记模型训练完成。sport=None 标记全部，否则只标记指定项目。"""
    meta = load_metadata()
    now = datetime.now().isoformat()
    if sport:
        meta[f'last_train_{sport}'] = now
    else:
        meta['last_train'] = now
    save_metadata(meta)


def auto_retrain():
    """在需要时自动重新训练模型，按运动项目分别检查。"""
    meta = load_metadata()
    success = False

    for sport in ['bb', 'fb']:
        if not _sport_should_retrain(sport, meta):
            continue
        try:
            from src.models.ensemble_trainer import train_sport_ensemble
            train_sport_ensemble(sport, quick=True)
            meta[f'last_train_{sport}'] = datetime.now().isoformat()
            print(f"  ✅ {sport.upper()} 重训完成")
            success = True
        except Exception as e:
            print(f"  ⚠️ {sport.upper()} 重训失败: {e}")

    # Dixon-Coles 比分模型（经典点估计优先，快速稳定）
    dc_key = 'last_train_dc'
    dc_last = meta.get(dc_key)
    dc_needed = False
    if dc_last is None:
        dc_needed = True
    else:
        dc_dt = datetime.fromisoformat(dc_last)
        if (datetime.now() - dc_dt).days >= 30:
            dc_needed = True

    if dc_needed:
        try:
            from src.models.dixon_coles import train_dixon_coles
            dc_model = train_dixon_coles()
            if dc_model.fitted:
                print(f"  ✅ 经典 DC 训练完成 ({dc_model.n_teams} 支球队)")
                meta[dc_key] = datetime.now().isoformat()
                success = True
        except Exception:
            try:
                from src.models.bayesian_dixon_coles import train_bayesian_dc
                dc_model = train_bayesian_dc(draws=400, tune=400)  # 缩减采样加速
                if dc_model.fitted:
                    print(f"  ✅ 贝叶斯 DC 训练完成 (回退, {dc_model.n_teams} 支球队)")
                    meta[dc_key] = datetime.now().isoformat()
                    success = True
            except Exception as e:
                print(f"  ⚠️ DC 训练跳过: {e}")

    # 泊松模型重训
    poisson_key = 'last_train_poisson'
    poisson_last = meta.get(poisson_key)
    poisson_needed = False
    if poisson_last is None:
        poisson_needed = True
    else:
        poisson_dt = datetime.fromisoformat(poisson_last)
        if (datetime.now() - poisson_dt).days >= 30:
            poisson_needed = True

    if poisson_needed:
        try:
            from src.models.poisson_model import train_poisson_model
            poisson_model = train_poisson_model()
            if poisson_model.fitted:
                print(f"  ✅ 泊松模型训练完成 ({poisson_model.n_teams} 支球队)")
                meta[poisson_key] = datetime.now().isoformat()
                success = True
        except Exception as e:
            print(f"  ⚠️ 泊松重训失败: {e}")

    # 统一保存
    meta['last_train'] = datetime.now().isoformat()
    save_metadata(meta)

    if not success:
        print("✅ 所有模型已是最新，无需重训")
        return False
    else:
        print("✅ 重训练完成")
        return True


def get_model_health() -> dict:
    """获取模型整体健康度。"""
    meta = load_metadata()
    result = {
        'last_trained': meta.get('last_train'),
        'days_since_train': None,
        'needs_retrain': should_retrain(),
        'decay_signal': _check_decay_signal(),
    }
    if meta.get('last_train'):
        result['days_since_train'] = (datetime.now() - datetime.fromisoformat(meta['last_train'])).days
    return result


def main():
    health = get_model_health()
    print(f"📋 模型状态:\n  最后训练: {health['last_trained']}\n  距上次训练天数: {health['days_since_train']}\n  需要重新训练: {health['needs_retrain']}\n  退化信号: {health['decay_signal']}")
    if auto_retrain():
        print("✅ 已执行自动重新训练")


if __name__ == '__main__':
    from config.logging_config import setup_logging, get_logger
    setup_logging()
    logger = get_logger(__name__)
    main()
