"""SQLAlchemy 数据库层 — 支持 SQLite（开发）和 PostgreSQL（生产）。

通过 DATABASE_URL 环境变量切换后端，默认 SQLite。
保持与旧版 `db.*` API 完全向后兼容。

用法:
    from src.storage.database import db
    db.record_bet(home="Lakers", away="Celtics", stake=100, odds=1.91, prob=0.55)
    bets = db.get_recent_bets(limit=20)
"""
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker

from config.settings import DATA_DIR
from config.logging_config import get_logger
from src.storage.models import Base, BetLog, Prediction, ModelAccuracy, \
    CLVData, PerformanceSnapshot, OddsCache

logger = get_logger(__name__)

DB_PATH = DATA_DIR / "sportsbetting.db"
_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _orm_to_dict(obj: Any) -> Dict[str, Any]:
    """ORM 对象 → dict（去掉 SQLAlchemy 内部状态）。"""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _value_for_json(val: Any) -> Any:
    """将 SQLAlchemy 返回值转为 JSON 兼容格式。"""
    if isinstance(val, datetime):
        return val.isoformat()
    return val


def _resolve_db_url(db_path: Optional[str] = None) -> str:
    """解析数据库 URL。"""
    url = db_path or os.environ.get("DATABASE_URL", "")
    if url:
        if not url.startswith("sqlite://") and not url.startswith("postgresql"):
            url = f"sqlite:///{url}"
        return url
    return f"sqlite:///{DB_PATH}"


class SportsDatabase:
    """线程安全的 SQLAlchemy 数据库封装。

    支持 SQLite（默认）和 PostgreSQL（通过 DATABASE_URL 环境变量）。
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_url = _resolve_db_url(db_path)
        self._is_sqlite = "sqlite" in self.db_url
        self._init_engine()
        self._init_schema()

    def _init_engine(self):
        """创建 engine + scoped session。"""
        kwargs = {}
        if self._is_sqlite:
            kwargs["connect_args"] = {"timeout": 5}
        else:
            kwargs["pool_size"] = int(os.environ.get("DB_POOL_SIZE", "5"))
            kwargs["max_overflow"] = int(os.environ.get("DB_MAX_OVERFLOW", "10"))

        self.engine = create_engine(self.db_url, **kwargs)
        self.Session = scoped_session(sessionmaker(bind=self.engine))

    def _init_schema(self):
        """自动创建表（若不存在）。"""
        with _LOCK:
            Base.metadata.create_all(self.engine)
            if self._is_sqlite:
                with self.engine.connect() as conn:
                    conn.execute(text("PRAGMA journal_mode=WAL"))
                    conn.execute(text("PRAGMA busy_timeout=5000"))
                    conn.commit()
            logger.info("  📦 DB: %s (%s)", self.db_url,
                        "PostgreSQL" if not self._is_sqlite else "SQLite")

    # ─── Bet Log ────────────────────────────────────────────

    def record_bet(self, match_key: str = "", home: str = "", away: str = "",
                   sport: str = "football", bet_type: str = "h2h",
                   stake: float = 0, odds: float = 0, prob: float = 0,
                   edge: Optional[float] = None, notes: str = "") -> int:
        match_key = match_key or f"{home} vs {away}"
        with _LOCK, self.Session() as session:
            bet = BetLog(
                match_key=match_key, home_team=home, away_team=away,
                sport=sport, bet_type=bet_type, stake=stake, odds=odds,
                model_prob=prob, edge=edge, notes=notes,
                placed_at=_utcnow(),
            )
            session.add(bet)
            session.commit()
            return bet.id

    def settle_bet(self, bet_id: int, result: str, profit: float):
        with _LOCK, self.Session() as session:
            bet = session.query(BetLog).filter_by(id=bet_id).first()
            if bet:
                bet.result = result
                bet.profit = profit
                bet.settled_at = _utcnow()
                session.commit()

    def get_recent_bets(self, limit: int = 50, sport: str = "") -> List[Dict]:
        with self.Session() as session:
            q = session.query(BetLog)
            if sport:
                q = q.filter_by(sport=sport)
            rows = q.order_by(BetLog.placed_at.desc()).limit(limit).all()
            return [{k: _value_for_json(v) for k, v in _orm_to_dict(r).items()}
                    for r in rows]

    def get_bet_stats(self, sport: str = "") -> Dict:
        with self.Session() as session:
            q = session.query(BetLog)
            if sport:
                q = q.filter_by(sport=sport)
            rows = q.all()
            total = len(rows)
            wins = sum(1 for r in rows if r.result == "win")
            losses = sum(1 for r in rows if r.result == "loss")
            total_profit = sum((r.profit or 0) for r in rows)
            settled = [r for r in rows if r.result is not None]
            avg_odds = (sum(r.odds for r in settled) / len(settled)
                        if settled else 0.0)
            return {
                "total": total,
                "wins": wins,
                "losses": losses,
                "total_profit": total_profit,
                "avg_odds": round(avg_odds, 4),
            }

    # ─── Predictions ─────────────────────────────────────────

    def record_prediction(self, match_key: str, sport: str,
                          home_team: str, away_team: str,
                          model_name: str, home_prob: float = None,
                          away_prob: float = None, draw_prob: float = None,
                          over_prob: float = None, under_prob: float = None,
                          commence_time: str = "") -> int:
        with _LOCK, self.Session() as session:
            pred = Prediction(
                match_key=match_key, sport=sport,
                home_team=home_team, away_team=away_team,
                model_name=model_name, home_prob=home_prob,
                away_prob=away_prob, draw_prob=draw_prob,
                over_prob=over_prob, under_prob=under_prob,
                commence_time=commence_time,
                predicted_at=_utcnow(),
            )
            session.add(pred)
            session.commit()
            return pred.id

    # ─── Model Accuracy ─────────────────────────────────────

    def update_accuracy(self, model_name: str, target: str,
                        correct: bool, prob: float):
        with _LOCK, self.Session() as session:
            existing = session.query(ModelAccuracy).filter_by(
                model_name=model_name, target=target).first()
            if existing:
                existing.total_predictions += 1
                existing.correct += 1 if correct else 0
                existing.accuracy = (existing.correct /
                                     max(existing.total_predictions, 1))
                existing.updated_at = _utcnow()
            else:
                session.add(ModelAccuracy(
                    model_name=model_name, target=target,
                    total_predictions=1,
                    correct=1 if correct else 0,
                    accuracy=1.0 if correct else 0.0,
                    updated_at=_utcnow(),
                ))
            session.commit()

    def get_accuracy(self, model_name: str = "", target: str = "") -> List[Dict]:
        with self.Session() as session:
            q = session.query(ModelAccuracy)
            if model_name:
                q = q.filter_by(model_name=model_name)
            if target:
                q = q.filter_by(target=target)
            rows = q.order_by(ModelAccuracy.updated_at.desc()).all()
            return [{k: _value_for_json(v) for k, v in _orm_to_dict(r).items()}
                    for r in rows]

    # ─── Performance Snapshots ──────────────────────────────

    def record_performance(self, balance: float = 0, equity: float = 0,
                           drawdown_pct: float = 0, roi: float = 0,
                           win_rate: float = 0, total_bets: int = 0,
                           settled_bets: int = 0, notes: str = ""):
        with _LOCK, self.Session() as session:
            session.add(PerformanceSnapshot(
                timestamp=_utcnow(), balance=balance, equity=equity,
                drawdown_pct=drawdown_pct, roi=roi, win_rate=win_rate,
                total_bets=total_bets, settled_bets=settled_bets,
                notes=notes,
            ))
            session.commit()

    def get_performance_history(self, limit: int = 30) -> List[Dict]:
        with self.Session() as session:
            rows = session.query(PerformanceSnapshot).order_by(
                PerformanceSnapshot.timestamp.desc()).limit(limit).all()
            return [{k: _value_for_json(v) for k, v in _orm_to_dict(r).items()}
                    for r in rows]

    # ─── CLV Data ───────────────────────────────────────────

    def record_clv(self, match_key: str, bookmaker: str, market: str,
                   opening: float, closing: float):
        clv = closing - opening
        with _LOCK, self.Session() as session:
            session.add(CLVData(
                match_key=match_key, bookmaker=bookmaker, market=market,
                opening_odds=opening, closing_odds=closing,
                clv=clv, captured_at=_utcnow(),
            ))
            session.commit()

    def get_clv_summary(self, limit: int = 100) -> List[Dict]:
        with self.Session() as session:
            rows = session.query(CLVData).order_by(
                CLVData.captured_at.desc()).limit(limit).all()
            return [{k: _value_for_json(v) for k, v in _orm_to_dict(r).items()}
                    for r in rows]

    # ─── Odds Cache ─────────────────────────────────────────

    def save_odds(self, sport_key: str, games_data: List[Dict]):
        now = _utcnow()
        rows = []
        for g in games_data:
            home = g.get("home_team", "")
            away = g.get("away_team", "")
            for bm in g.get("bookmakers", []):
                markets = {m["key"]: m["outcomes"] for m in bm.get("markets", [])}
                h2h = markets.get("h2h", [])
                totals = markets.get("totals", [])
                h2h_home = next((o["price"] for o in h2h
                                 if o["name"].lower() == home.lower()), None)
                h2h_away = next((o["price"] for o in h2h
                                 if o["name"].lower() == away.lower()), None)
                h2h_draw = next((o["price"] for o in h2h
                                 if o["name"].lower() == "draw"), None)
                over = next((o for o in totals
                             if o.get("name", "").lower() == "over"), None)
                under = next((o for o in totals
                              if o.get("name", "").lower() == "under"), None)
                rows.append(OddsCache(
                    sport_key=sport_key, home_team=home, away_team=away,
                    bookmaker=bm.get("key"),
                    h2h_home=h2h_home, h2h_away=h2h_away, h2h_draw=h2h_draw,
                    total_over=over["price"] if over else None,
                    total_under=under["price"] if under else None,
                    total_point=over.get("point") if over else None,
                    commence_time=g.get("commence_time"),
                    fetched_at=now,
                ))
        if rows:
            with _LOCK, self.Session() as session:
                session.add_all(rows)
                session.commit()

    def get_odds_history(self, sport_key: str = "", home_team: str = "",
                         away_team: str = "", limit: int = 100) -> List[Dict]:
        with self.Session() as session:
            q = session.query(OddsCache)
            if sport_key:
                q = q.filter_by(sport_key=sport_key)
            if home_team:
                q = q.filter_by(home_team=home_team)
            if away_team:
                q = q.filter_by(away_team=away_team)
            rows = q.order_by(OddsCache.fetched_at.desc()).limit(limit).all()
            return [{k: _value_for_json(v) for k, v in _orm_to_dict(r).items()}
                    for r in rows]

    # ─── Maintenance ────────────────────────────────────────

    def vacuum(self):
        """回收磁盘空间（SQLite 专用）。"""
        if self._is_sqlite:
            with self.engine.connect() as conn:
                conn.execute(text("VACUUM"))
                conn.commit()

    def get_db_size(self) -> int:
        """返回数据库文件大小（SQLite 专用）。"""
        if self._is_sqlite:
            db_file = self.db_url.replace("sqlite:///", "")
            return Path(db_file).stat().st_size if Path(db_file).exists() else 0
        # PostgreSQL: 查询数据库大小
        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text("SELECT pg_database_size(current_database())"))
                return result.scalar() or 0
        except Exception:
            return 0

    def close(self):
        self.Session.remove()
        self.engine.dispose()

    def get_db_type(self) -> str:
        """返回当前数据库类型。"""
        return "postgresql" if not self._is_sqlite else "sqlite"


# 全局单例（保持向后兼容）
db = SportsDatabase()
