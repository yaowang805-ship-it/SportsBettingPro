"""测试文件锁 — locked_open 基本功能。"""
import pytest
import json
import os
from pathlib import Path

from src.storage.file_lock import locked_open


class TestLockedOpen:
    def test_write_and_read(self, tmp_path):
        p = tmp_path / "test.json"
        data = {"key": "value", "num": 42}

        with locked_open(str(p), "w") as f:
            json.dump(data, f)

        with locked_open(str(p), "r") as f:
            loaded = json.load(f)
        assert loaded == data

    def test_append(self, tmp_path):
        p = tmp_path / "test.csv"
        with locked_open(str(p), "w") as f:
            f.write("a,b,c\n")
        with locked_open(str(p), "a") as f:
            f.write("1,2,3\n")

        with locked_open(str(p), "r") as f:
            content = f.read()
        assert content == "a,b,c\n1,2,3\n"

    def test_concurrent_access_no_corruption(self, tmp_path):
        """模拟并发写入 — 确保文件不损坏。"""
        import threading

        p = tmp_path / "concurrent_test.txt"
        errors = []

        def writer(thread_id, n=20):
            try:
                for i in range(n):
                    with locked_open(str(p), "a") as f:
                        f.write(f"thread-{thread_id}-line-{i}\n")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(tid, 30)) for tid in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        with locked_open(str(p), "r") as f:
            lines = f.readlines()
        # Each thread writes 30 lines, 5 threads = 150 lines
        assert len(lines) == 150
        # No partial lines
        for line in lines:
            assert line.endswith("\n")

    def test_non_existent_file(self):
        with locked_open("/tmp/_test_nonexistent_file.txt", "w") as f:
            f.write("hello")
        with locked_open("/tmp/_test_nonexistent_file.txt", "r") as f:
            assert f.read() == "hello"
        Path("/tmp/_test_nonexistent_file.txt").unlink(missing_ok=True)

    def test_write_binary(self, tmp_path):
        p = tmp_path / "test.bin"
        data = b"\x00\x01\x02\xff"
        with locked_open(str(p), "wb") as f:
            f.write(data)
        with locked_open(str(p), "rb") as f:
            assert f.read() == data
