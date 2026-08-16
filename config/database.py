"""SQLite 统一存储 — 关键状态持久化

替代 JSON 文件存储，提供写事务保护和并发安全。
已有表（bet_log / clv_data / predictions 等）保持不动，新增流水线专用表。
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import DATA_DIR

DB_PATH = DATA_DIR / "sportsbetting.db"

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """获取线程本地连接（首次调用自动建表）。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        init_db()  # 首次连接自动建表+迁移
    return _local.conn


def init_db():
    """建表（幂等）。"""
    db = get_conn()
    try:
        db.execute("ALTER TABLE pushed_fingerprints ADD COLUMN ev_pct REAL DEFAULT 0")
        db.commit()
    except Exception:
        pass  # 列已存在

    db.executescript("""
        CREATE TABLE IF NOT EXISTS budget_tracker (
            date        TEXT NOT NULL,
            group_name  TEXT NOT NULL,
            spent       REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (date, group_name)
        );

        CREATE TABLE IF NOT EXISTS pushed_fingerprints (
            fingerprint TEXT PRIMARY KEY,
            ev_pct      REAL DEFAULT 0,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS push_meta (
            meta_key    TEXT PRIMARY KEY,
            meta_value  TEXT
        );

        CREATE TABLE IF NOT EXISTS push_clv (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            sport       TEXT NOT NULL DEFAULT '',
            league      TEXT NOT NULL DEFAULT '',
            home        TEXT NOT NULL DEFAULT '',
            away        TEXT NOT NULL DEFAULT '',
            designation TEXT NOT NULL DEFAULT '',
            sub_market  TEXT NOT NULL DEFAULT '',
            bb_odds     REAL NOT NULL DEFAULT 0,
            pin_odds    REAL NOT NULL DEFAULT 0,
            fair_price  REAL NOT NULL DEFAULT 0,
            ev_pct      REAL NOT NULL DEFAULT 0,
            stake       REAL NOT NULL DEFAULT 0,
            tier        INTEGER NOT NULL DEFAULT 0,
            match_epoch INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_push_clv_sport_league
            ON push_clv(sport, league);
        CREATE INDEX IF NOT EXISTS idx_push_clv_captured
            ON push_clv(captured_at);
    """)
    db.commit()


# ── budget_tracker ──

def get_budget(date: str) -> dict:
    """获取指定日期的预算消耗。"""
    rows = get_conn().execute(
        "SELECT group_name, spent FROM budget_tracker WHERE date = ?", (date,)
    ).fetchall()
    return {r["group_name"]: r["spent"] for r in rows}


def save_budget(spent: dict, date: str):
    """覆盖保存指定日期的预算消耗。"""
    db = get_conn()
    db.execute("DELETE FROM budget_tracker WHERE date = ?", (date,))
    for group, amount in spent.items():
        if amount > 0:
            db.execute(
                "INSERT INTO budget_tracker (date, group_name, spent) VALUES (?, ?, ?)",
                (date, group, amount),
            )
    db.commit()


# ── pushed_fingerprints ──

def load_fingerprints() -> dict:
    """加载所有指纹。V4.3: ev_pct存储为JSON字符串 {ev: float, ts: float}。"""
    import json as _json
    try:
        rows = get_conn().execute(
            "SELECT fingerprint, ev_pct FROM pushed_fingerprints"
        ).fetchall()
        result = {}
        for r in rows:
            val = r["ev_pct"]
            if isinstance(val, str) and val.startswith("{"):
                try: result[r["fingerprint"]] = _json.loads(val)
                except: result[r["fingerprint"]] = {"ev": float(val or 0), "ts": 0}
            elif isinstance(val, (int, float)):
                result[r["fingerprint"]] = {"ev": float(val), "ts": 0}  # 兼容旧int格式
            else:
                result[r["fingerprint"]] = {"ev": 0, "ts": 0}
        return result
    except Exception:
        rows = get_conn().execute("SELECT fingerprint FROM pushed_fingerprints").fetchall()
        return {r["fingerprint"]: {"ev": 0, "ts": 0} for r in rows}


def save_fingerprints(fps: dict):
    """批量覆盖保存指纹。V4.3: ev_pct存JSON字符串 {ev, ts}。"""
    import json as _json
    db = get_conn()
    db.execute("DELETE FROM pushed_fingerprints")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = []
    for fp, val in sorted(fps.items()):
        if isinstance(val, dict):
            ev_str = _json.dumps(val)
        else:
            ev_str = _json.dumps({"ev": float(val), "ts": 0})
        data.append((fp, ev_str, now))
    db.executemany(
        "INSERT INTO pushed_fingerprints (fingerprint, ev_pct, created_at) VALUES (?, ?, ?)",
        data,
    )
    db.commit()


def add_fingerprints(fps: dict):
    """增量添加/更新指纹。V4.3: ev_pct存JSON字符串。"""
    import json as _json
    db = get_conn()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for fp, val in fps.items():
        if isinstance(val, dict):
            ev_str = _json.dumps(val)
        else:
            ev_str = _json.dumps({"ev": float(val), "ts": 0})
        db.execute(
            "INSERT OR REPLACE INTO pushed_fingerprints (fingerprint, ev_pct, created_at) VALUES (?, ?, ?)",
            (fp, ev_str, now),
        )
    db.commit()


# ── push_meta ──

def get_push_meta(key: str, default=None) -> Optional[str]:
    row = get_conn().execute(
        "SELECT meta_value FROM push_meta WHERE meta_key = ?", (key,)
    ).fetchone()
    return row["meta_value"] if row else default


def set_push_meta(key: str, value: str):
    get_conn().execute(
        "INSERT OR REPLACE INTO push_meta (meta_key, meta_value) VALUES (?, ?)",
        (key, value),
    )
    get_conn().commit()


# ── push_clv（CLV 日志） ──

def insert_push_clv(opps: list):
    """批量插入 CLV 记录。"""
    if not opps:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = []
    for o in opps:
        data.append((
            now,
            o.get("sport", ""),
            o.get("league", ""),
            o.get("home_cn", ""),
            o.get("away_cn", ""),
            o.get("designation", ""),
            o.get("_sub_market", o.get("_market", "")),
            o.get("bb_odds", 0),
            o.get("pin_odds", 0),
            o.get("fair_price", 0),
            o.get("ev_pct", 0),
            o.get("_stake", 0),
            o.get("_tier", 0),
            o.get("_pin_epoch", 0) or 0,  # _pin_epoch 可能为 None(无 Pin 开赛时间) → 落 0, 避免 NOT NULL 约束崩
        ))
    get_conn().executemany(
        """INSERT INTO push_clv
           (captured_at, sport, league, home, away, designation, sub_market,
            bb_odds, pin_odds, fair_price, ev_pct, stake, tier, match_epoch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        data,
    )
    get_conn().commit()


# ── CLV 趋势查询 ──

def get_clv_trend(sport: str = "", league: str = "", limit: int = 200) -> list:
    """查询最近 N 条 CLV 记录用于趋势分析。

    CLV 近似为 (bb_odds - fair_price) / fair_price。
    返回 [{sport, league, clv, ev_pct, captured_at}, ...]
    """
    if sport and league:
        rows = get_conn().execute(
            """SELECT sport, league, bb_odds, fair_price, ev_pct, captured_at
               FROM push_clv
               WHERE sport = ? AND league = ?
               ORDER BY id DESC LIMIT ?""",
            (sport, league, limit),
        ).fetchall()
    elif sport:
        rows = get_conn().execute(
            """SELECT sport, league, bb_odds, fair_price, ev_pct, captured_at
               FROM push_clv WHERE sport = ?
               ORDER BY id DESC LIMIT ?""",
            (sport, limit),
        ).fetchall()
    else:
        rows = get_conn().execute(
            """SELECT sport, league, bb_odds, fair_price, ev_pct, captured_at
               FROM push_clv ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    result = []
    for r in rows:
        fp = r["fair_price"]
        clv = (r["bb_odds"] - fp) / fp if fp and fp > 0 else 0
        result.append({
            "sport": r["sport"],
            "league": r["league"],
            "clv": round(clv * 100, 2),
            "ev_pct": r["ev_pct"],
            "captured_at": r["captured_at"],
        })
    return result
