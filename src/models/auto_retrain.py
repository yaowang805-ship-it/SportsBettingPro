#!/usr/bin/env python3
"""职业级月度模型重训练与自动化管理。"""
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


def load_metadata():
    if META_FILE.exists():
        with open(META_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'last_train': None, 'train_frequency_days': 30, 'models': {}}


def save_metadata(data):
    with open(META_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def should_retrain(frequency_days: int = 30) -> bool:
    """检查是否需要重新训练模型。"""
    meta = load_metadata()
    if meta['last_train'] is None:
        return True
    last_train = datetime.fromisoformat(meta['last_train'])
    return (datetime.now() - last_train).days >= frequency_days


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
    
    print("🔄 启动月度模型重新训练...")
    try:
        from src.models.ensemble_trainer import train_sport_ensemble
        train_sport_ensemble('bb')
        train_sport_ensemble('fb')

        # Dixon-Coles 比分模型
        try:
            from src.models.dixon_coles import train_dixon_coles
            dc_model = train_dixon_coles()
            if dc_model.fitted:
                print(f"  ✅ Dixon-Coles 训练完成 ({dc_model.n_teams} 支球队)")
        except Exception as e:
            print(f"  ⚠️ Dixon-Coles 训练跳过: {e}")

        mark_training_complete()
        print("✅ 月度重训练完成")
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
    }
    if meta.get('last_train'):
        result['days_since_train'] = (datetime.now() - datetime.fromisoformat(meta['last_train'])).days
    return result


def main():
    health = get_model_health()
    print(f"📋 模型状态:\n  最后训练: {health['last_trained']}\n  距上次训练天数: {health['days_since_train']}\n  需要重新训练: {health['needs_retrain']}")
    if auto_retrain():
        print("✅ 已执行自动重新训练")


if __name__ == '__main__':
    main()
