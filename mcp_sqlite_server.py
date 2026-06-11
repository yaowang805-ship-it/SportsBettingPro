#!/usr/bin/env python3
"""Minimal MCP server for sportsbetting.db — enables SQL queries via Claude Code.

Protocol: JSON-RPC 2.0 over stdio (MCP standard transport).
"""
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict

DB_PATH = Path(__file__).resolve().parent / "data" / "storage" / "sportsbetting.db"

TOOLS = [
    {
        "name": "query_database",
        "description": "Execute a read-only SQL query against sportsbetting.db",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL SELECT query",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "list_tables",
        "description": "List all tables in sportsbetting.db",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "describe_table",
        "description": "Show schema of a table",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": "Table name"},
            },
            "required": ["table_name"],
        },
    },
]


def _query(sql: str) -> list:
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT") and not stripped.startswith("PRAGMA"):
        return [{"error": "Only SELECT and PRAGMA queries are allowed"}]
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        return [dict(row) for row in cur.fetchall()][:200]
    finally:
        conn.close()


def _list_tables() -> list:
    return _query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")


def _describe_table(table_name: str) -> list:
    return _query(f"PRAGMA table_info({table_name})")


def handle_request(req: Dict[str, Any]) -> Dict[str, Any]:
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sportsbetting-sqlite", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            if tool_name == "query_database":
                result = _query(args["sql"])
            elif tool_name == "list_tables":
                result = _list_tables()
            elif tool_name == "describe_table":
                result = _describe_table(args["table_name"])
            else:
                raise ValueError(f"Unknown tool: {tool_name}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

    if method == "notifications/initialized":
        return None

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            continue


if __name__ == "__main__":
    main()
