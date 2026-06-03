"""SQLite 数据库层 — 替代 CSV/JSON 文件存储。

提供 ACID 事务、类型安全、便捷查询，逐步替换 flat file 存储。

用法:
    from src.storage.database import db
    db.record_bet(home="Lakers", away="Celtics", stake=100, odds=1.91, prob=0.55)
    bets = db.get_recent_bets(limit=20)
    accuracy = db.get_model_accuracy("fb_win_result")
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import DATA_DIR
from config.logging_config import get_logger

logger = get_logger(__name__)

DB_PATH = DATA_DIR / "sportsbetting.db"
_LOCK = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SportsDatabase:
    """线程安全的 SQLite 数据库封装。"""

    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_schema(self):
        with _LOCK:
            conn = self._conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bet_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_key       TEXT NOT NULL,
                    home_team       TEXT NOT NULL,
                    away_team       TEXT NOT NULL,
                    sport           TEXT DEFAULT 'football',
                    bet_type        TEXT NOT NULL,
                    stake           REAL NOT NULL,
                    odds            REAL NOT NULL,
                    model_prob      REAL NOT NULL,
                    edge            REAL,
                    result          TEXT,
                    profit          REAL,
                    placed_at       TEXT NOT NULL,
                    settled_at      TEXT,
                    notes           TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_bet_log_placed
                    ON bet_log(placed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_bet_log_result
                    ON bet_log(result);

                CREATE TABLE IF NOT EXISTS predictions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_key       TEXT NOT NULL,
                    sport           TEXT NOT NULL,
                    home_team       TEXT NOT NULL,
                    away_team       TEXT NOT NULL,
                    commence_time   TEXT,
                    model_name      TEXT NOT NULL,
                    home_prob       REAL,
                    away_prob       REAL,
                    draw_prob       REAL,
                    over_prob       REAL,
                    under_prob      REAL,
                    predicted_at    TEXT NOT NULL,
                    home_score      INTEGER,
                    away_score      INTEGER,
                    was_correct     INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_pred_match
                    ON predictions(match_key);
                CREATE INDEX IF NOT EXISTS idx_pred_model
                    ON predictions(model_name);

                CREATE TABLE IF NOT EXISTS model_accuracy (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_name      TEXT NOT NULL,
                    target          TEXT NOT NULL,
                    total_predictions INTEGER DEFAULT 0,
                    correct         INTEGER DEFAULT 0,
                    accuracy        REAL DEFAULT 0.0,
                    brier_score     REAL,
                    log_loss        REAL,
                    updated_at      TEXT NOT NULL,
                    UNIQUE(model_name, target)
                );

                CREATE TABLE IF NOT EXISTS clv_data (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_key       TEXT NOT NULL,
                    bookmaker       TEXT,
                    market          TEXT,
                    opening_odds    REAL,
                    closing_odds    REAL,
                    clv             REAL,
                    captured_at     TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_clv_match
                    ON clv_data(match_key);

                CREATE TABLE IF NOT EXISTS performance_snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT NOT NULL,
                    balance         REAL,
                    equity          REAL,
                    drawdown_pct    REAL,
                    roi             REAL,
                    win_rate        REAL,
                    total_bets      INTEGER,
                    settled_bets    INTEGER,
                    notes           TEXT
                );

                CREATE TABLE IF NOT EXISTS odds_cache (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    sport_key       TEXT NOT NULL,
                    home_team       TEXT NOT NULL,
                    away_team       TEXT NOT NULL,
                    bookmaker       TEXT,
                    h2h_home        REAL,
                    h2h_away        REAL,
                    h2h_draw        REAL,
                    total_over      REAL,
                    total_under     REAL,
                    total_point     REAL,
                    spread_home     REAL,
                    spread_point    REAL,
                    commence_time   TEXT,
                    fetched_at      TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_odds_sport
                    ON odds_cache(sport_key, fetched_at DESC);
            """)
            conn.commit()

    # ─── Bet Log ────────────────────────────────────────────

    def record_bet(self, match_key: str = "", home: str = "", away: str = "",
                   sport: str = "football", bet_type: str = "h2h",
                   stake: float = 0, odds: float = 0, prob: float = 0,
                   edge: Optional[float] = None, notes: str = "") -> int:
        match_key = match_key or f"{home} vs {away}"
        with _LOCK:
            cur = self._conn().execute(
                """INSERT INTO bet_log
                   (match_key, home_team, away_team, sport, bet_type,
                    stake, odds, model_prob, edge, placed_at, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (match_key, home, away, sport, bet_type,
                 stake, odds, prob, edge, _utcnow(), notes))
            self._conn().commit()
            return cur.lastrowid

    def settle_bet(self, bet_id: int, result: str, profit: float):
        with _LOCK:
            self._conn().execute(
                "UPDATE bet_log SET result=?, profit=?, settled_at=? WHERE id=?",
                (result, profit, _utcnow(), bet_id))
            self._conn().commit()

    def get_recent_bets(self, limit: int = 50, sport: str = "") -> List[Dict]:
        query = "SELECT * FROM bet_log"
        params = []
        if sport:
            query += " WHERE sport=?"
            params.append(sport)
        query += " ORDER BY placed_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_bet_stats(self, sport: str = "") -> Dict:
        query_base = "FROM bet_log"
        params = []
        if sport:
            query_base += " WHERE sport=?"
            params.append(sport)
        row = self._conn().execute(
            f"SELECT COUNT(*) as total, "
            f"SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins, "
            f"SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) as losses, "
            f"COALESCE(SUM(profit), 0) as total_profit, "
            f"COALESCE(AVG(CASE WHEN result IS NOT NULL THEN odds END), 0) as avg_odds "
            f"{query_base}", params).fetchone()
        return dict(row) if row else {"total": 0, "wins": 0, "losses": 0, "total_profit": 0, "avg_odds": 0}

    # ─── Model Accuracy ─────────────────────────────────────

    def update_accuracy(self, model_name: str, target: str,
                        correct: bool, prob: float):
        with _LOCK:
            existing = self._conn().execute(
                "SELECT * FROM model_accuracy WHERE model_name=? AND target=?",
                (model_name, target)).fetchone()
            if existing:
                total = existing["total_predictions"] + 1
                corr = existing["correct"] + (1 if correct else 0)
                acc = corr / total if total > 0 else 0.0
                self._conn().execute(
                    "UPDATE model_accuracy SET total_predictions=?, correct=?, "
                    "accuracy=?, updated_at=? WHERE id=?",
                    (total, corr, acc, _utcnow(), existing["id"]))
            else:
                self._conn().execute(
                    "INSERT INTO model_accuracy (model_name, target, "
                    "total_predictions, correct, accuracy, updated_at) "
                    "VALUES (?,?,1,?,?,?)",
                    (model_name, target, 1 if correct else 0,
                     1.0 if correct else 0.0, _utcnow()))
            self._conn().commit()

    def get_accuracy(self, model_name: str = "", target: str = "") -> List[Dict]:
        query = "SELECT * FROM model_accuracy WHERE 1=1"
        params = []
        if model_name:
            query += " AND model_name=?"
            params.append(model_name)
        if target:
            query += " AND target=?"
            params.append(target)
        rows = self._conn().execute(query + " ORDER BY updated_at DESC", params).fetchall()
        return [dict(r) for r in rows]

    # ─── Performance Snapshots ──────────────────────────────

    def record_performance(self, balance: float, equity: float = 0,
                           drawdown_pct: float = 0, roi: float = 0,
                           win_rate: float = 0, total_bets: int = 0,
                           settled_bets: int = 0, notes: str = ""):
        with _LOCK:
            self._conn().execute(
                "INSERT INTO performance_snapshots "
                "(timestamp, balance, equity, drawdown_pct, roi, "
                "win_rate, total_bets, settled_bets, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (_utcnow(), balance, equity, drawdown_pct, roi,
                 win_rate, total_bets, settled_bets, notes))
            self._conn().commit()

    def get_performance_history(self, limit: int = 30) -> List[Dict]:
        rows = self._conn().execute(
            "SELECT * FROM performance_snapshots "
            "ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ─── CLV Data ───────────────────────────────────────────

    def record_clv(self, match_key: str, bookmaker: str, market: str,
                   opening: float, closing: float):
        clv = closing - opening
        with _LOCK:
            self._conn().execute(
                "INSERT INTO clv_data (match_key, bookmaker, market, "
                "opening_odds, closing_odds, clv, captured_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (match_key, bookmaker, market,
                 opening, closing, clv, _utcnow()))
            self._conn().commit()

    def get_clv_summary(self, limit: int = 100) -> List[Dict]:
        rows = self._conn().execute(
            "SELECT * FROM clv_data ORDER BY captured_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ─── Odds Cache ─────────────────────────────────────────

    def save_odds(self, sport_key: str, games_data: List[Dict]):
        """批量保存赔率快照。"""
        now = _utcnow()
        rows = []
        for g in games_data:
            home = g.get("home_team", "")
            away = g.get("away_team", "")
            for bm in g.get("bookmakers", []):
                markets = {m["key"]: m["outcomes"] for m in bm.get("markets", [])}
                h2h = markets.get("h2h", [])
                totals = markets.get("totals", [])
                h2h_home = next((o["price"] for o in h2h if o["name"].lower() == home.lower()), None)
                h2h_away = next((o["price"] for o in h2h if o["name"].lower() == away.lower()), None)
                h2h_draw = next((o["price"] for o in h2h if o["name"].lower() == "draw"), None)
                over = next((o for o in totals if o.get("name", "").lower() == "over"), None)
                under = next((o for o in totals if o.get("name", "").lower() == "under"), None)
                rows.append((
                    sport_key, home, away, bm.get("key"),
                    h2h_home, h2h_away, h2h_draw,
                    over["price"] if over else None,
                    under["price"] if under else None,
                    over.get("point") if over else None,
                    None, None,
                    g.get("commence_time"), now
                ))
        if rows:
            with _LOCK:
                self._conn().executemany(
                    "INSERT INTO odds_cache (sport_key, home_team, away_team, "
                    "bookmaker, h2h_home, h2h_away, h2h_draw, total_over, "
                    "total_under, total_point, spread_home, spread_point, "
                    "commence_time, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
                self._conn().commit()

    def get_odds_history(self, sport_key: str = "", home_team: str = "",
                         away_team: str = "", limit: int = 100) -> List[Dict]:
        query = "SELECT * FROM odds_cache WHERE 1=1"
        params = []
        if sport_key:
            query += " AND sport_key=?"
            params.append(sport_key)
        if home_team:
            query += " AND home_team=?"
            params.append(home_team)
        if away_team:
            query += " AND away_team=?"
            params.append(away_team)
        query += " ORDER BY fetched_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ─── Maintenance ────────────────────────────────────────

    def vacuum(self):
        """回收磁盘空间。"""
        self._conn().execute("VACUUM")

    def get_db_size(self) -> int:
        return Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# 全局单例
db = SportsDatabase()
