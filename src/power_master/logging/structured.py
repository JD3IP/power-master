"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog


def setup_logging(level: str = "INFO", fmt: str = "json", log_file: str = "") -> None:
    """Configure structured logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        fmt: Output format - "json" for production, "console" for development.
        log_file: Optional file path for log output. Empty = stdout only.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if fmt == "console":
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    # Persistent file logging with monthly rotation to /data/logs/
    # This ensures logs survive container restarts and can be read via SSH
    rotating_file_handler = _setup_rotating_file_handler(formatter)
    if rotating_file_handler:
        handlers.append(rotating_file_handler)

    # In-memory ring buffer for dashboard log viewer
    from power_master.dashboard.log_buffer import log_buffer

    log_buffer.setFormatter(formatter)
    handlers.append(log_buffer)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    for handler in handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Suppress noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def _setup_rotating_file_handler(formatter: logging.Formatter) -> logging.Handler | None:
    """Set up a rotating file handler that writes to /data/logs/.

    Uses TimedRotatingFileHandler with 30-day interval (~monthly) and keeps
    6 backup files (~6 months of logs).

    Returns:
        The configured handler, or None if the log directory cannot be created.
    """
    log_dir = Path("/data/logs")

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        # Log to stdout if we can't create the log directory
        import sys
        print(
            f"Warning: Could not create log directory {log_dir}: {e}. "
            f"Continuing with console-only logging.",
            file=sys.stderr,
        )
        return None

    log_file_path = log_dir / "power-master.log"

    try:
        # TimedRotatingFileHandler with when='midnight' and interval=30 rotates
        # every 30 days at midnight. backupCount=6 keeps 6 backup files.
        handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(log_file_path),
            when="midnight",
            interval=30,
            backupCount=6,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        return handler
    except Exception as e:
        # Log to stdout if we can't create the rotating file handler
        import sys
        print(
            f"Warning: Could not set up rotating file handler at {log_file_path}: {e}. "
            f"Continuing with console-only logging.",
            file=sys.stderr,
        )
        return None
