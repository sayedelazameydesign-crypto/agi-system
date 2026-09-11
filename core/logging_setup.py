"""Structured logging (standard library only).

Two formatters are provided:

* ``text``  -- human friendly, used by the demo and interactive runs
* ``json``  -- one JSON object per line, for log shippers / CI
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    *,
    json_mode: bool = False,
    log_file: Optional[Path | str] = None,
    quiet: bool = False,
) -> logging.Logger:
    """Configure the ``agi`` logger tree and return it."""
    logger = logging.getLogger("agi")
    logger.setLevel(_level(level))
    logger.handlers.clear()
    logger.propagate = False

    if not quiet:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(JsonFormatter() if json_mode else logging.Formatter(DEFAULT_FORMAT))
        logger.addHandler(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    return logger


def event_logger(name: str = "agi.kernel") -> logging.Logger:
    return logging.getLogger(name)


def _level(level: str) -> int:
    return getattr(logging, str(level).upper(), logging.INFO)


__all__ = ["configure_logging", "JsonFormatter", "event_logger"]
