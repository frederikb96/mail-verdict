"""
JSON-lines logging infrastructure for MailVerdict.

Provides structured JSON logging to stdout for container log collection.
All MailVerdict and library loggers emit one JSON object per line.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """
        Format a log record as a single-line JSON string.

        Args:
            record: The log record to format
        """
        record.message = record.getMessage()

        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.message,
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                entry[key] = value

        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)

        if record.stack_info:
            entry["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(entry, default=str)


_LIBRARY_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "sqlalchemy.engine",
    "sqlalchemy.pool",
    "asyncio",
    "fastmcp",
    "httpx",
    "imap_tools",
    "openai",
    "qdrant_client",
)

_NOISY_LOGGERS = (
    "httpcore",
    "hpack",
    "httpx",
)


def setup_logging(level: str) -> None:
    """
    Configure root logger with JSON formatter and redirect library loggers.

    Must be called before any other module logs (typically in __main__.py).

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)

    try:
        import uvicorn.config

        uvicorn.config.LOGGING_CONFIG = {  # type: ignore[assignment]
            "version": 1,
            "disable_existing_loggers": False,
        }
    except ImportError:
        pass

    for name in _LIBRARY_LOGGERS:
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
