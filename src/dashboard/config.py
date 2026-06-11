"""Dashboard configuration — page settings and data file paths."""
from pathlib import Path
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "storage"
SNAPSHOT_DIR = DATA_DIR / "odds_snapshots"
PROCESSED_DIR = ROOT / "data" / "processed"
MODEL_DIR = ROOT / "models"


@dataclass
class AppConfig:
    page_title: str = "SportsBettingPro 监控面板"
    page_icon: str = "chart_with_upwards_trend"
    layout: str = "wide"
    refresh_interval: int = 60


config = AppConfig()

# Data file paths
PERF_FILE = DATA_DIR / "performance_history.csv"
PRED_LOG_FILE = DATA_DIR / "prediction_log.csv"
BET_HISTORY_FILE = DATA_DIR / "bet_history.csv"
RISK_STATE_FILE = DATA_DIR / "risk_state.json"
SYSTEM_HEALTH_FILE = DATA_DIR / "system_health.json"
CLV_REPORT_FILE = DATA_DIR / "clv_report.json"
NBA_RATINGS_FILE = DATA_DIR / "nba_power_ratings.json"
FB_RATINGS_FILE = DATA_DIR / "fb_power_ratings.json"
RECOMMENDATIONS_FILE = DATA_DIR / "daily_recommendations.json"
SNAPSHOT_FILE = SNAPSHOT_DIR / "last_snapshot.json"
MOVEMENTS_FILE = SNAPSHOT_DIR / "movements.json"
BACKTEST_FILE = DATA_DIR / "model_backtest_summary.json"
CALIBRATION_FILE = DATA_DIR / "calibration_data.csv"

# New / fixed paths
PORTFOLIO_FILE = DATA_DIR / "virtual_portfolio.json"
MODEL_ACCURACY_FILE = DATA_DIR / "model_accuracy_history.csv"
ARBITRAGE_FILE = DATA_DIR / "arbitrage_log.json"
EDGE_ATTRIBUTION_FILE = DATA_DIR / "edge_attribution_report.json"
TEAM_EDGE_FILE = DATA_DIR / "team_edge_tracking.json"
DECAY_REPORT_FILE = DATA_DIR / "model_decay_report.json"
BANKROLL_SIM_FILE = DATA_DIR / "bankroll_simulation.json"
BB_RECS_FILE = DATA_DIR / "daily_bb_recommendations.json"
FB_RECS_FILE = DATA_DIR / "daily_fb_recommendations.json"
ATTRIBUTION_FILE = DATA_DIR / "performance_attribution.json"
