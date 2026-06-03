"""Centralized logging configuration for SportsBettingPro.

Replaces all bare print() calls with structured logging.

Usage:
    from config.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("message")
    logger.warning("warning")
    logger.error("error")
"""
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-32s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def setup_logging(
    log_level: str = "INFO",
    log_to_file: bool = True,
    log_to_console: bool = True,
    log_dir: Optional[Path] = None,
) -> None:
    """Configure root logger once at application startup.

    Args:
        log_level: One of DEBUG, INFO, WARNING, ERROR
        log_to_file: Enable daily rotating file handler (keep 30 days)
        log_to_console: Enable stderr stream handler
        log_dir: Override default log directory
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    if log_to_console and not any(isinstance(h, logging.StreamHandler)
                                   for h in root.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        console.setLevel(logging.INFO)
        root.addHandler(console)

    if log_to_file:
        log_path = (log_dir or LOG_DIR) / "sportsbetting.log"
        handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(log_path),
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        root.addHandler(handler)

    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name.

    Convention: pass __name__ so log records show module path.
    """
    return logging.getLogger(name)
