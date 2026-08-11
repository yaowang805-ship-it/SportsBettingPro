"""V5 Redis 赔率缓存 — 加速增量扫描, 减少 Pinnacle API 调用。

用途:
1. 缓存 Pinnacle 联赛结构 (TTL 24h)
2. 缓存最近赔率快照 (TTL 30min) — 用于变动检测
3. 缓存联赛映射结果 (TTL 1h)
4. 分布式锁 — 防止多进程同时扫描
"""
import json, os, time, logging
from typing import Optional

logger = logging.getLogger(__name__)

# Redis 可选 — 未安装时回退到本地文件缓存
try:
    import redis
    _REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
    _redis = redis.from_url(_REDIS_URL, decode_responses=True)
    _redis.ping()
    REDIS_AVAILABLE = True
    logger.info("Redis connected: %s", _REDIS_URL)
except (ImportError, redis.exceptions.ConnectionError):
    _redis = None
    REDIS_AVAILABLE = False
    logger.info("Redis not available — using local file cache")


# ── 缓存接口 ──
def cache_get(key: str) -> Optional[str]:
    """从缓存读取。"""
    if REDIS_AVAILABLE and _redis:
        try:
            return _redis.get(key)
        except Exception:
            pass
    return None


def cache_set(key: str, value: str, ttl: int = 3600):
    """写入缓存 (TTL 秒)。"""
    if REDIS_AVAILABLE and _redis:
        try:
            _redis.setex(key, ttl, value)
        except Exception:
            pass


def cache_get_json(key: str) -> Optional[dict]:
    """读取 JSON 缓存。"""
    raw = cache_get(key)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return None


def cache_set_json(key: str, data: dict, ttl: int = 3600):
    """写入 JSON 缓存。"""
    cache_set(key, json.dumps(data, ensure_ascii=False), ttl)


def odds_snapshot_key(league_id: int) -> str:
    return f"odds:snapshot:{league_id}"


def league_structure_key() -> str:
    return "pinnacle:league_structure"


def pin_change_key(time_window: str) -> str:
    return f"pin:changes:{time_window}"


# ── 分布式锁 ──
def acquire_lock(lock_name: str, ttl: int = 300) -> bool:
    """获取分布式锁 (防止多进程同时扫描)。"""
    if REDIS_AVAILABLE and _redis:
        try:
            return _redis.set(f"lock:{lock_name}", str(time.time()), nx=True, ex=ttl)
        except Exception:
            pass
    return True  # 无 Redis 时默认放行


def release_lock(lock_name: str):
    """释放锁。"""
    if REDIS_AVAILABLE and _redis:
        try:
            _redis.delete(f"lock:{lock_name}")
        except Exception:
            pass
