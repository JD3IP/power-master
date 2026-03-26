"""In-memory ring buffer for capturing recent log entries."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone


class LogRecord:
    __slots__ = ("timestamp", "level", "logger_name", "message")

    def __init__(self, timestamp: str, level: str, logger_name: str, message: str):
        self.timestamp = timestamp
        self.level = level
        self.logger_name = logger_name
        self.message = message

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger_name,
            "message": self.message,
        }


class RingBufferHandler(logging.Handler):
    """Logging handler that stores the last N records in a ring buffer."""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self._buffer: deque[LogRecord] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = LogRecord(
                timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                level=record.levelname,
                logger_name=record.name,
                message=self.format(record),
            )
            with self._lock:
                self._buffer.append(entry)
        except Exception:
            self.handleError(record)

    def get_records(self, limit: int = 200, level: str | None = None) -> list[dict]:
        """Return recent log records, newest first."""
        with self._lock:
            records = list(self._buffer)
        if level:
            level_upper = level.upper()
            records = [r for r in records if r.level == level_upper]
        records.reverse()
        return [r.to_dict() for r in records[:limit]]


class DbLogHandler(logging.Handler):
    """Logging handler that queues records for periodic async DB writes."""

    def __init__(self, repository: object) -> None:
        super().__init__()
        self._repo = repository
        self._pending: list[LogRecord] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = LogRecord(
                timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                level=record.levelname,
                logger_name=record.name,
                message=self.format(record),
            )
            with self._lock:
                self._pending.append(entry)
        except Exception:
            self.handleError(record)

    async def flush_to_db(self) -> int:
        """Write queued records to the database. Returns count written."""
        with self._lock:
            batch = self._pending[:]
            self._pending.clear()
        if not batch:
            return 0
        for entry in batch:
            await self._repo.store_log(
                recorded_at=entry.timestamp,
                level=entry.level,
                logger_name=entry.logger_name,
                message=entry.message,
            )
        await self._repo.db.commit()
        return len(batch)


# Singleton instance
log_buffer = RingBufferHandler(capacity=1000)
log_buffer.setFormatter(logging.Formatter("%(message)s"))
