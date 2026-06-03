"""文件级写锁 — 防止 CSV/JSON 并发写入损坏。

用法:
    with locked_open("/path/to/file.json", "w") as f:
        json.dump(data, f)

    with locked_open("/path/to/file.csv", "a") as f:
        f.write("new row\\n")
"""
import fcntl
import os
from pathlib import Path
from typing import IO


_LOCK_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "locks"
_LOCK_DIR.mkdir(parents=True, exist_ok=True)


def _lock_path(file_path: str) -> Path:
    """每个文件对应一个锁文件。"""
    safe_name = Path(file_path).resolve().name.replace(".", "_")
    return _LOCK_DIR / f"{safe_name}.lock"


class locked_open:
    """带 fcntl.flock 的文件打开上下文管理器。

    阻塞锁：直到获取到锁才返回。锁文件自动清理。
    """

    def __init__(self, file_path: str, mode: str = "r", **kwargs):
        self.file_path = file_path
        self.mode = mode
        self.kwargs = kwargs
        self.lock_fp: IO = None
        self.fp: IO = None

    def __enter__(self):
        lock_path = _lock_path(self.file_path)
        self.lock_fp = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self.lock_fp, fcntl.LOCK_EX)
        # 确保目标目录存在
        Path(self.file_path).parent.mkdir(parents=True, exist_ok=True)
        self.fp = open(self.file_path, self.mode, **self.kwargs)
        return self.fp

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fp:
            self.fp.close()
        if self.lock_fp is not None:
            fcntl.flock(self.lock_fp, fcntl.LOCK_UN)
            os.close(self.lock_fp)
            lpath = _lock_path(self.file_path)
            try:
                lpath.unlink(missing_ok=True)
            except OSError:
                pass
        return False
