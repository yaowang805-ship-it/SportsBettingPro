#!/usr/bin/env python3
"""一次性迁移: 把旧 DB(data/storage/storage/sportsbetting.db)的 4 张表
合并到新 DB(data/storage/sportsbetting.db)。

背景: config/database.py 之前 DB_PATH 多拼了一层 "storage",
导致 4 张表写到了嵌套目录的另一个文件。现已修正 DB_PATH, 本脚本把旧库数据搬过来。

用法(先停守护进程, 避免 DB 被锁):
    launchctl bootout gui/$(id -u)/com.sportsbettingpro.daemon 2>/dev/null
    .venv312/bin/python scripts/merge_dbs.py
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sportsbettingpro.daemon.plist
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.database import DB_PATH

OLD_DB = ROOT / "data" / "storage" / "storage" / "sportsbetting.db"
TABLES = ["budget_tracker", "pushed_fingerprints", "push_meta", "push_clv"]


def main():
    if not OLD_DB.exists():
        print(f"旧 DB 不存在: {OLD_DB} (可能已迁移过), 退出")
        return

    # 新 DB(经 config.database.init_db 已在首次连接建好 4 张表)
    from config.database import init_db
    init_db()
    new_conn = sqlite3.connect(str(DB_PATH), timeout=10)
    new_conn.execute("PRAGMA busy_timeout=5000")
    new_conn.execute(f"ATTACH DATABASE ? AS old", (str(OLD_DB),))

    for table in TABLES:
        old_cols = [r[1] for r in new_conn.execute(f"PRAGMA old.table_info({table})")]
        new_cols = [r[1] for r in new_conn.execute(f"PRAGMA table_info({table})")]
        common = [c for c in old_cols if c in new_cols]
        if not common:
            print(f"[跳过] {table}: 无共有列")
            continue
        old_count = new_conn.execute(f"SELECT COUNT(*) FROM old.{table}").fetchone()[0]
        if old_count == 0:
            print(f"[跳过] {table}: 旧表空")
            continue
        cols = ", ".join(common)
        new_conn.execute(f"INSERT OR IGNORE INTO {table} ({cols}) SELECT {cols} FROM old.{table}")
        new_count = new_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"[ok] {table}: 旧 {old_count} 行 → 新 {new_count} 行")

    new_conn.commit()  # 先提交 INSERT, 再 DETACH
    try:
        new_conn.execute("DETACH DATABASE old")
    except sqlite3.OperationalError as e:
        print(f"[warn] DETACH 失败(可忽略): {e}")
    new_conn.close()

    bak = OLD_DB.with_suffix(".db.bak")
    OLD_DB.rename(bak)
    print(f"\n✅ 迁移完成, 旧 DB 已改名: {bak.name}")


if __name__ == "__main__":
    main()
