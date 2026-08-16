"""Dixon-Coles 模型 — 正确比分概率预测。

模型参数在 models/dixon_coles_model.json (mu/home_adv/rho/attack/defence, log-space)。
正确比分概率 = Poisson(i, λh) * Poisson(j, λa) * τ(i,j) (低比分调整)。
"""
import json, math
from pathlib import Path

_MODEL = None
_MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "models" / "dixon_coles_model.json"


def _load_model():
    global _MODEL
    if _MODEL is None:
        try:
            _MODEL = json.loads(_MODEL_PATH.read_text())
        except Exception:
            _MODEL = {}
    return _MODEL


def _poisson(k, lam):
    if lam <= 0:
        return 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def correct_score_probs(home_team, away_team, max_score=6):
    """Dixon-Coles 计算正确比分概率 {f'{i}-{j}': prob}, 队名找不到返回 {}."""
    m = _load_model()
    if not m:
        return {}
    mu = m.get('mu', 0.0)
    home_adv = m.get('home_adv', 0.0)
    rho = m.get('rho', 0.0)
    attack = m.get('attack', {})
    defence = m.get('defence', {})

    # 队名匹配: 精确优先, 否则子串/归一化
    def _find(t):
        if t in attack:
            return t
        tl = t.lower().strip()
        for k in attack:
            if k.lower() == tl or tl in k.lower() or k.lower() in tl:
                return k
        return None

    hk = _find(home_team)
    ak = _find(away_team)
    if not hk or not ak:
        return {}

    lh = math.exp(mu + home_adv + attack[hk] + defence.get(ak, 0.0))
    la = math.exp(mu + attack[ak] + defence.get(hk, 0.0))

    probs = {}
    for i in range(max_score + 1):
        for j in range(max_score + 1):
            p = _poisson(i, lh) * _poisson(j, la)
            # Dixon-Coles 低比分相关性调整
            if i == 0 and j == 0:
                p *= 1.0 - lh * la * rho
            elif i == 0 and j == 1:
                p *= 1.0 + lh * rho
            elif i == 1 and j == 0:
                p *= 1.0 + la * rho
            elif i == 1 and j == 1:
                p *= 1.0 - rho
            probs[f"{i}-{j}"] = max(0.0, p)
    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}
    return probs
