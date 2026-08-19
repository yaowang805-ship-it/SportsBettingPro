#!/usr/bin/env python3
"""重建 pinnacle_odds_archive.db — 去掉 UNIQUE 约束, 只保留价格变化点。

为什么要重建:
    原表 UNIQUE(matchup_id, market_type, designation, period, points) + INSERT OR IGNORE:
      · 让球/大小球(points 非 NULL) 每条线只留**首见价**(开盘价) → 归档回捞对 hc/ou 完全无效
      · 独赢(points 为 NULL, SQLite 的 UNIQUE 不约束 NULL) 反而每次扫描都插一条,
        即使价格没变 → 实测 3,671,193 行独赢里约 97% 是重复价格

    改成"价格变化才留":
      · hc/ou 首次拥有真实时间序列 → 能取赛前最后价当收盘价
      · 独赢去掉冗余 → 全库从 3.86M 行大幅缩小
    也就是**库变小 + 能力变强**, 不是拿空间换功能。

并发安全:
    流水线在跑, 归档器持续写入。做法是先按 id <= M 的快照建新表, 最后在一个事务里
    补齐 id > M 的增量再换名 —— 换名窗口只有毫秒级, 不丢数据。

用法: nice -n 10 .venv312/bin/python scripts/rebuild_odds_archive.py [--vacuum]
"""
import argparse
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "storage" / "pinnacle_odds_archive.db"

COLS = ("sport, league_id, league_name, matchup_id, home, away, market_type, "
        "designation, period, points, price, fetched_at, match_start")

# 价格变化点 = 与同一 key 上一条(按 fetched_at)价格不同的行。
# PARTITION BY 里 NULL 会归为同一组, 正是我们要的(独赢 points 为 NULL)。
CHANGE_POINTS = f"""
SELECT {COLS} FROM (
    SELECT {COLS}, id,
           LAG(price) OVER (
               PARTITION BY matchup_id, market_type, designation, period, points
               ORDER BY fetched_at, id
           ) AS prev_price
    FROM odds_archive
    WHERE id <= ?
)
WHERE prev_price IS NULL OR prev_price != price
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vacuum", action="store_true", help="重建后 VACUUM 回收磁盘")
    args = ap.parse_args()

    t0 = time.time()
    conn = sqlite3.connect(str(DB), timeout=60, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA synchronous=NORMAL")

    before = conn.execute("SELECT COUNT(*) FROM odds_archive").fetchone()[0]
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM odds_archive").fetchone()[0]
    print(f"原表 {before:,} 行, 快照 max_id={max_id:,}", flush=True)

    conn.execute("DROP TABLE IF EXISTS odds_archive_new")
    # 与原表同构, 但**不带 UNIQUE** —— 这正是要去掉的东西
    conn.execute("""CREATE TABLE odds_archive_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sport TEXT NOT NULL, league_id INTEGER NOT NULL, league_name TEXT NOT NULL,
        matchup_id INTEGER NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
        market_type TEXT NOT NULL, designation TEXT, period INTEGER DEFAULT 0,
        points REAL, price REAL NOT NULL, fetched_at TEXT NOT NULL, match_start TEXT
    )""")

    print("拷贝价格变化点...", flush=True)
    conn.execute(f"INSERT INTO odds_archive_new ({COLS}) {CHANGE_POINTS}", (max_id,))
    copied = conn.execute("SELECT COUNT(*) FROM odds_archive_new").fetchone()[0]
    print(f"  变化点 {copied:,} 行 (压缩 {(1 - copied / max(1, before)) * 100:.1f}%), "
          f"耗时 {time.time() - t0:.0f}s", flush=True)

    # 换名前补齐重建期间归档器新写入的行, 事务内完成, 窗口极短
    print("补增量 + 换名...", flush=True)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"INSERT INTO odds_archive_new ({COLS}) "
                     f"SELECT {COLS} FROM odds_archive WHERE id > ?", (max_id,))
        catchup = conn.execute("SELECT changes()").fetchone()[0]
        conn.execute("DROP TABLE odds_archive")
        conn.execute("ALTER TABLE odds_archive_new RENAME TO odds_archive")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    print(f"  补入重建期间新增 {catchup:,} 行", flush=True)

    # 普通索引(非 UNIQUE): 支撑"按 key 取赛前最后一条"和按场次/日期查询
    print("建索引...", flush=True)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_key ON odds_archive "
                 "(matchup_id, designation, period, points, fetched_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_sport ON odds_archive (sport)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_date ON odds_archive (match_start)")

    after = conn.execute("SELECT COUNT(*) FROM odds_archive").fetchone()[0]
    print(f"\n新表 {after:,} 行 (原 {before:,}, 缩减 {(1 - after / max(1, before)) * 100:.1f}%)", flush=True)

    if args.vacuum:
        print("VACUUM 回收磁盘...", flush=True)
        conn.execute("VACUUM")
    conn.close()
    print(f"完成, 总耗时 {time.time() - t0:.0f}s  文件 {DB.stat().st_size / 1e6:.0f}MB", flush=True)


if __name__ == "__main__":
    main()
