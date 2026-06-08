#!/usr/bin/env python3
"""职业级月度模型重训练 — 支持退化信号触发 + 定期重训。

重训触发条件（任一达标即触发）：
  1. 距上次训练 >= frequency_days（默认30天）
  2. 模型衰减检测报告 is_decaying=True
  3. 从模型精度历史检测到显著下滑趋势
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import joblib
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
        recent = df.tail(10)["accuracy"].values
        early = df.tail(20).head(10)["accuracy"].values
        if len(recent) >= 5 and len(early) >= 5:
            recent_mean = recent.mean()
            early_mean = early.mean()
            if early_mean - recent_mean >= 0.05:  # 5pp 下滑
                logger.warning("  ⚠️ 准确率下滑 %.1fpp (近期 %.1f%% → 前期 %.1f%%)",
                              (early_mean - recent_mean) * 100, recent_mean * 100, early_mean * 100)
                return True
        return False
    except Exception:
        return False


def should_retrain(frequency_days: int = 30) -> bool:
    """检查是否需要重新训练模型（时间+退化双触发）。"""
    meta = load_metadata()
    # 条件1: 从未训练过
    if meta['last_train'] is None:
        return True
    # 条件2: 时间到期
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


def mark_training_complete():
    """标记模型训练完成。"""
    meta = load_metadata()
    meta['last_train'] = datetime.now().isoformat()
    save_metadata(meta)


def auto_retrain():
    """在需要时自动重新训练所有模型。"""
    if not should_retrain():
        print("模型已是最新，无需重新训练")
        return False

    print("🔄 启动模型重新训练（时间/退化信号触发）...")
    try:
        from src.models.ensemble_trainer import train_sport_ensemble
        train_sport_ensemble('bb')
        train_sport_ensemble('fb')
        train_sport_ensemble('nfl')

        # Dixon-Coles 比分模型（贝叶斯优先）
        try:
            from src.models.bayesian_dixon_coles import train_bayesian_dc
            dc_model = train_bayesian_dc()
            if dc_model.fitted:
                print(f"  ✅ 贝叶斯 DC 训练完成 ({dc_model.n_teams} 支球队)")
        except Exception:
            try:
                from src.models.dixon_coles import train_dixon_coles
                dc_model = train_dixon_coles()
                if dc_model.fitted:
                    print(f"  ✅ 传统 DC 训练完成 ({dc_model.n_teams} 支球队)")
            except Exception as e:
                print(f"  ⚠️ DC 训练跳过: {e}")

        mark_training_complete()
        print("✅ 重训练完成")
        return True
    except Exception as e:
        print(f"❌ 重新训练失败: {e}")
        return False


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
