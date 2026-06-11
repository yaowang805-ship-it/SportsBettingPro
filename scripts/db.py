#!/usr/bin/env python3
"""数据库快捷查询工具。

用法:
    python scripts/db.py                  # 查看所有表及行数
    python scripts/db.py bet_log          # 查看表结构 + 前10行
    python scripts/db.py bet_log 20       # 查看前20行
    python scripts/db.py "SELECT ..."     # 执行 SQL
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "storage" / "sportsbetting.db"


def fmt_rows(rows, headers):
    col_widths = [len(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r):
            col_widths[i] = max(col_widths[i], len(str(v or "")))
    sep = "  ".join("-" * w for w in col_widths)
    header = "  ".join(h.ljust(w) for h, w in zip(headers, col_widths))
    lines = [header, sep]
    for r in rows:
        lines.append("  ".join((str(v or "NULL").ljust(w) for v, w in zip(r, col_widths))))
    return "\n".join(lines)


def list_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print(f"📋 {len(rows)} 张表:\n")
    for (name,) in rows:
        cnt = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
        print(f"  {name:<25} {cnt} 行")


def describe_table(conn, name, limit=10):
    cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
    print(f"📋 {name} ({len(cols)} 列):\n")
    print(f"  {'列名':<20} {'类型':<10} {'非空':<6} {'主键':<5} 默认值")
    print(f"  {'-'*20} {'-'*10} {'-'*6} {'-'*5} {'-'*10}")
    for c in cols:
        print(f"  {c['name']:<20} {c['type']:<10} {'Y' if c['notnull'] else '':<6} {'*' if c['pk'] else '':<5} {c['dflt_value'] or ''}")
    rows = conn.execute(f"SELECT * FROM [{name}] LIMIT {limit}").fetchall()
    if rows:
        headers = [c["name"] for c in cols]
        print(f"\n📄 前 {len(rows)} 行:\n")
        print(fmt_rows([list(r) for r in rows], headers))
    else:
        print("\n📄 (空表)")


def run_sql(conn, sql):
    try:
        cur = conn.execute(sql)
        if sql.strip().upper().startswith("SELECT"):
            rows = cur.fetchall()
            if not rows:
                print("(0 行)")
                return
            desc = [d[0] for d in cur.description]
            print(f"📊 {len(rows)} 行:\n")
            print(fmt_rows([list(r) for r in rows], desc))
        else:
            conn.commit()
            print(f"✅ 影响 {cur.rowcount} 行")
    except Exception as e:
        print(f"❌ {e}")


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    args = sys.argv[1:]

    if not args:
        list_tables(conn)
    elif args[0].strip().upper().startswith("SELECT") or args[0].strip().upper().startswith("PRAGMA"):
        run_sql(conn, args[0])
    else:
        table = args[0]
        limit = int(args[1]) if len(args) > 1 else 10
        describe_table(conn, table, limit)

    conn.close()


if __name__ == "__main__":
    main()
