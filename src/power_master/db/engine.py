"""SQLite database engine with WAL mode for concurrent reads."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from power_master.db.migrations import run_migrations

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None


async def init_db(db_path: str | Path) -> aiosqlite.Connection:
    """Initialise the database connection with WAL mode and run migrations."""
    global _db
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(db_path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    db.row_factory = aiosqlite.Row

    await run_migrations(db)
    _db = db
    logger.info("Database initialised at %s (WAL mode)", db_path)
    return db


async def get_db() -> aiosqlite.Connection:
    """Get the active database connection."""
    if _db is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _db


async def close_db() -> None:
    """Close the database connection."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
        logger.info("Database connection closed")
