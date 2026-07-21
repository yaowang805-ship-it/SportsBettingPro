"""跨进程文件锁 — 防止并发读写 virtual_portfolio.json 造成数据覆盖。"""
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


@contextmanager
def _flock(lock_path: Path):
    """独占文件锁上下文。"""
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def locked_read_write(
    json_path: Path,
    modify_fn: Callable[[dict], Any],
    default: dict | None = None,
    lock_suffix: str = ".lck",
) -> dict:
    """读取 JSON → 调用 modify_fn → 写回，全程持有文件锁。

    Args:
        json_path: JSON 文件路径
        modify_fn: 接收 dict 返回修改后的 dict（或 None 表示不修改）
        default: 文件不存在时的默认值
        lock_suffix: 锁文件后缀

    Returns:
        修改后的 dict
    """
    lock_path = json_path.with_suffix(json_path.suffix + lock_suffix)
    with _flock(lock_path):
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text())
            except Exception:
                data = dict(default) if default else {}
        else:
            data = dict(default) if default else {}

        result = modify_fn(data)

        if result is not None:
            json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        return data
