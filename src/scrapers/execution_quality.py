"""执行质量系数(2026-10-08): 按方向(运动×盘口×方向)统计拒单率+下单延迟, 算 0.6~1.0 折扣系数。

Kelly 是理论最优, 但执行环境(拒单/滑点/延迟)会打折实际 edge。职业做法:
  最终注码 = Kelly × 分数系数 × 执行质量系数
- 拒单率 = oddsChange=0 被拒(成交价=信号价保证, 变了就拒), 拒单高 = 执行差 = 降权。
- 延迟 = 下单 HTTP 耗时, 延迟长 = 成交慢 = 降权。
样本 n<20 不打折(默认1.0, 小样本拒单率噪声大), 拒单率>10% 开始线性降权, 底 0.6。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EQ_FILE = ROOT / "data" / "storage" / "execution_quality.json"
MIN_SAMPLE = 20       # 样本门槛: 低于此不打折
REJECT_FLOOR = 0.10   # 拒单率 ≤10% 不打折
REJECT_SLOPE = 2.0    # 拒单率每超 1% 系数降 2%(拒单率 30% → 1.0-0.2*2=0.6)
FACTOR_FLOOR = 0.6    # 系数下限
LATENCY_OK = 1.5      # 平均延迟(s)高于此再打 0.9

_cache = {"mtime": 0.0, "data": None}  # mtime 缓存(下单链路不因读 json 变慢)


def _load():
    """读执行质量数据, 带 mtime 缓存(下单链路 0ms 读)。"""
    global _cache
    if not EQ_FILE.exists():
        return None
    try:
        m = EQ_FILE.stat().st_mtime
        if _cache["data"] is None or m != _cache["mtime"]:
            _cache["data"] = json.loads(EQ_FILE.read_text())
            _cache["mtime"] = m
        return _cache["data"]
    except Exception:
        return None


def record(direction_key, code, latency=0.0):
    """记录一次下单结果。code==0 成功, 否则被拒(oddsChange 被拒/其他失败)。"""
    try:
        d = {}
        if EQ_FILE.exists():
            try:
                d = json.loads(EQ_FILE.read_text())
            except Exception:
                pass
        cell = d.get(direction_key, {"success": 0, "rejected": 0, "total": 0,
                                     "latency_sum": 0.0, "latency_n": 0})
        cell["total"] = cell.get("total", 0) + 1
        if code == 0:
            cell["success"] = cell.get("success", 0) + 1
        else:
            cell["rejected"] = cell.get("rejected", 0) + 1
        if latency > 0:
            cell["latency_sum"] = cell.get("latency_sum", 0.0) + latency
            cell["latency_n"] = cell.get("latency_n", 0) + 1
        d[direction_key] = cell
        EQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        EQ_FILE.write_text(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def factor(direction_key):
    """算执行质量系数(0.6~1.0)。样本不足/拒单率低返回 1.0。"""
    try:
        d = _load()
        if not d:
            return 1.0
        cell = d.get(direction_key)
        if not cell:
            return 1.0
        total = cell.get("total", 0)
        if total < MIN_SAMPLE:
            return 1.0
        reject_rate = cell.get("rejected", 0) / total
        f = 1.0
        if reject_rate > REJECT_FLOOR:
            f = max(FACTOR_FLOOR, 1.0 - (reject_rate - REJECT_FLOOR) * REJECT_SLOPE)
        ln = cell.get("latency_n", 0)
        if ln > 0 and cell.get("latency_sum", 0.0) / ln > LATENCY_OK:
            f = max(FACTOR_FLOOR, f * 0.9)
        return f
    except Exception:
        return 1.0
