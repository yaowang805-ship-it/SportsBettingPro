"""拒绝域观察库(2026-10-17 预注册配套): 记录「比价出 edge 但没下单」的信号。

用途: 区分「逆向选择(滞后)」vs「回归均值(噪声)」——只有拒绝域的 CLV 数据能回答。
硬约束(见 PREREGISTRATION.md):
  1. 物理隔离只写, 聚合脚本(dashboard/REAL_ROI_GATE/释放判据/日报)显式 exclude。
  2. 等比例采样: 所有 edge 桶同 SAMPLE_RATE, sample_rate 固化进每条记录(配置变了历史权重不变)。
  3. 观察库数据绝不反馈进 edge 估计器。
"""
import fcntl
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data" / "storage"
REJECT_FILE = DATA / "reject_domain.jsonl"
VOLUME_FILE = DATA / "signal_volume.jsonl"

# 等比例采样率(所有桶相同, 写死, 动态调会破坏跨期可比性)
SAMPLE_RATE = 1.0

# 反事实分析不可用的 reject_reason(人工暂停/系统异常: 不知道那时刻系统能否正常下单)
UNUSABLE_REASONS = {"manual_pause", "system_error"}

# 必填字段(缺一不写, 坏数据比不写更糟)
REQUIRED_FIELDS = (
    "signal_ts", "sport", "sub", "direction", "market_id", "option_type",
    "estimated_edge", "bb_odds", "pin_odds", "fair",
    "bb_snapshot_ts", "pin_snapshot_ts", "bb_snapshot_age", "latency",
    "stake_kelly_raw", "stake_if_placed", "clamp_reason",
    "available_exposure", "reject_reason", "config_hash",
)

# 进程内去重(同一信号重估/重试/幂等会重复进入)
_seen_ids = set()


def edge_cohort(edge):
    """估计 edge → 6 桶编号(等比例采样用)。桶边界覆盖 <2 到 >10 全区间。"""
    e = float(edge) if edge is not None else 0.0
    if e < 2:
        return 0
    if e < 3:
        return 1
    if e < 4:
        return 2
    if e < 6:
        return 3
    if e < 10:
        return 4
    return 5


def _signal_id(sport, sub, market_id, option_type, signal_ts):
    """确定性 id: 同一信号重估时去重, 防重复计数污染桶计数/日均信号量/命中率。"""
    raw = f"{sport}|{sub}|{market_id}|{option_type}|{signal_ts}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def record_reject(rec):
    """记录一条拒绝信号。返回 True=已写 / False=去重跳过。

    rec 必填字段见 REQUIRED_FIELDS; reject_reason 必须是枚举值。
    """
    # 1) 去重: 同一信号不重复记
    sid = _signal_id(rec.get("sport"), rec.get("sub"), rec.get("market_id"),
                     rec.get("option_type"), rec.get("signal_ts"))
    if sid in _seen_ids:
        return False
    _seen_ids.add(sid)
    rec["signal_id"] = sid

    # 2) 采样率 + 桶号固化(等比例, 不事后查配置)
    rec["sample_rate"] = SAMPLE_RATE
    rec["cohort"] = edge_cohort(rec.get("estimated_edge"))

    # 3) schema 校验: 必填字段齐全
    missing = [f for f in REQUIRED_FIELDS if f not in rec]
    if missing:
        rec["_schema_error"] = "missing:" + ",".join(missing)

    # 4) 反事实可用性标记(manual_pause/system_error 不可用于反事实分析)
    rec["is_usable"] = rec.get("reject_reason") not in UNUSABLE_REASONS

    # 5) 落盘(append + fcntl 锁, 防多实例/重启重叠)
    try:
        REJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(REJECT_FILE, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(line)
            f.flush()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True
    except OSError:
        return False


def record_volume(rec):
    """每日信号量侧车: 一行记录当日某格子的 evaluated/accepted/rejected 计数。

    rec: {date, sport, sub, direction, evaluated, accepted, rejected}
    用途: 提供「分母」, 否则被拒率无法解释(信号总量从 200 掉到 50 会被误读为拒绝率变了)。
    """
    try:
        VOLUME_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(VOLUME_FILE, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(line)
            f.flush()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
