"""模型加载工具 — 优先加载集成模型，失败时回退到单模型。"""
import joblib
from pathlib import Path
from config.settings import MODEL_DIR

MODEL_DIR_PATH = Path(MODEL_DIR) if isinstance(MODEL_DIR, str) else MODEL_DIR


def load_model(prefix: str, target: str):
    """加载模型，优先返回集成模型。

    Args:
        prefix: 模型前缀，如 'model_bb', 'model_fb'
        target: 预测目标，如 'win', 'spread_result', 'total_result'

    返回: (model, model_type)
        model: 加载的模型对象
        model_type: 'ensemble' 或 'single'
    """
    # 先尝试集成模型
    ensemble_path = MODEL_DIR_PATH / f"{prefix}_{target}_ensemble.pkl"
    if ensemble_path.exists():
        try:
            model = joblib.load(ensemble_path)
            print(f"✅ 加载集成模型: {ensemble_path.name}")
            return model, 'ensemble'
        except Exception as e:
            print(f"⚠️ 集成模型加载失败 ({e}), 回退到单模型")

    # 回退到单模型
    single_path = MODEL_DIR_PATH / f"{prefix}_{target}.pkl"
    if single_path.exists():
        model = joblib.load(single_path)
        print(f"✅ 加载模型: {single_path.name}")
        return model, 'single'

    # 尝试不带 target 的模型名（兼容旧命名）
    alt_path = MODEL_DIR_PATH / f"{prefix}.pkl"
    if alt_path.exists():
        model = joblib.load(alt_path)
        print(f"✅ 加载模型: {alt_path.name}")
        return model, 'single'

    raise FileNotFoundError(f"未找到模型: {prefix}_{target}")
