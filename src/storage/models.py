"""SQLAlchemy ORM 模型 — 数据库无关的声明式定义。

支持 SQLite（开发）和 PostgreSQL（生产）后端。
后端通过 DATABASE_URL 环境变量切换：
  DATABASE_URL=sqlite:///data/storage/sportsbetting.db  （默认）
  DATABASE_URL=postgresql://user:pass@host:5432/sportsbetting
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Integer, Float, String, DateTime, Index, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─── Bet Log ────────────────────────────────────────────────

class BetLog(Base):
    __tablename__ = "bet_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_key = Column(String(255), nullable=False)
    home_team = Column(String(128), nullable=False)
    away_team = Column(String(128), nullable=False)
    sport = Column(String(64), default="football")
    bet_type = Column(String(64), nullable=False)
    stake = Column(Float, nullable=False)
    odds = Column(Float, nullable=False)
    model_prob = Column(Float, nullable=False)
    edge = Column(Float, nullable=True)
    result = Column(String(16), nullable=True)
    profit = Column(Float, nullable=True)
    placed_at = Column(DateTime, nullable=False, default=_utcnow)
    settled_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_bet_log_placed", "placed_at"),
        Index("idx_bet_log_result", "result"),
    )


# ─── Predictions ────────────────────────────────────────────

class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_key = Column(String(255), nullable=False)
    sport = Column(String(64), nullable=False)
    home_team = Column(String(128), nullable=False)
    away_team = Column(String(128), nullable=False)
    commence_time = Column(String(64), nullable=True)
    model_name = Column(String(64), nullable=False)
    home_prob = Column(Float, nullable=True)
    away_prob = Column(Float, nullable=True)
    draw_prob = Column(Float, nullable=True)
    over_prob = Column(Float, nullable=True)
    under_prob = Column(Float, nullable=True)
    predicted_at = Column(DateTime, nullable=False, default=_utcnow)
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    was_correct = Column(Integer, nullable=True)

    __table_args__ = (
        Index("idx_pred_match", "match_key"),
        Index("idx_pred_model", "model_name"),
    )


# ─── Model Accuracy ─────────────────────────────────────────

class ModelAccuracy(Base):
    __tablename__ = "model_accuracy"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_name = Column(String(64), nullable=False)
    target = Column(String(64), nullable=False)
    total_predictions = Column(Integer, default=0)
    correct = Column(Integer, default=0)
    accuracy = Column(Float, default=0.0)
    brier_score = Column(Float, nullable=True)
    log_loss = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        Index("idx_model_acc", "model_name", "target", unique=True),
    )


# ─── CLV Data ───────────────────────────────────────────────

class CLVData(Base):
    __tablename__ = "clv_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_key = Column(String(255), nullable=False)
    bookmaker = Column(String(64), nullable=True)
    market = Column(String(64), nullable=True)
    opening_odds = Column(Float, nullable=True)
    closing_odds = Column(Float, nullable=True)
    clv = Column(Float, nullable=True)
    captured_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        Index("idx_clv_match", "match_key"),
    )


# ─── Performance Snapshots ──────────────────────────────────

class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=_utcnow)
    balance = Column(Float, nullable=True)
    equity = Column(Float, nullable=True)
    drawdown_pct = Column(Float, nullable=True)
    roi = Column(Float, nullable=True)
    win_rate = Column(Float, nullable=True)
    total_bets = Column(Integer, nullable=True)
    settled_bets = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)


# ─── Odds Cache ─────────────────────────────────────────────

class OddsCache(Base):
    __tablename__ = "odds_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sport_key = Column(String(64), nullable=False)
    home_team = Column(String(128), nullable=False)
    away_team = Column(String(128), nullable=False)
    bookmaker = Column(String(64), nullable=True)
    h2h_home = Column(Float, nullable=True)
    h2h_away = Column(Float, nullable=True)
    h2h_draw = Column(Float, nullable=True)
    total_over = Column(Float, nullable=True)
    total_under = Column(Float, nullable=True)
    total_point = Column(Float, nullable=True)
    spread_home = Column(Float, nullable=True)
    spread_point = Column(Float, nullable=True)
    commence_time = Column(String(64), nullable=True)
    fetched_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        Index("idx_odds_sport", "sport_key", "fetched_at"),
    )
