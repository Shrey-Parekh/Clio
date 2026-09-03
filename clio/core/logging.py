"""Structured logging: readable console, JSON lines to a rotating file.

Every decision Clio makes and every tool it calls goes through this - the point
is to be able to debug what happened after the fact, not to guess.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR_NAME = "logs"
_LOG_FILE_NAME = "clio.jsonl"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_CONSOLE_DATE_FORMAT = "%H:%M:%S"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", root: Path | None = None) -> None:
    """Configure the root logger. Call once, at startup."""
    root = root or Path(__file__).resolve().parents[2]
    log_dir = root / _LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(level)

    # Release file handles deterministically rather than leaning on refcount GC
    # to do it. Matters if setup_logging is ever called twice (config reload).
    for existing in logger.handlers[:]:
        existing.close()
        logger.removeHandler(existing)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_CONSOLE_DATE_FORMAT))
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_dir / _LOG_FILE_NAME,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(_JsonFormatter())
    logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
