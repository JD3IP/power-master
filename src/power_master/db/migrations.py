"""Database schema migrations."""

from __future__ import annotations

import logging

import aiosqlite

from power_master.db.models import SCHEMA_VERSION, TABLES

logger = logging.getLogger(__name__)


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Run pending schema migrations."""
    current = await _get_current_version(db)

    if current == 0:
        logger.info("Creating database schema (version %d)", SCHEMA_VERSION)
        for statement in TABLES:
            await db.execute(statement)
        await db.execute(
            "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
            (SCHEMA_VERSION,),
        )
        await db.commit()
        logger.info("Schema created successfully")
    elif current < SCHEMA_VERSION:
        logger.info("Migrating database from version %d to %d", current, SCHEMA_VERSION)
        await _apply_migrations(db, current, SCHEMA_VERSION)
        await db.execute(
            "UPDATE schema_version SET version = ? WHERE id = 1",
            (SCHEMA_VERSION,),
        )
        await db.commit()
        logger.info("Migration complete")
    else:
        logger.debug("Database schema is up to date (version %d)", current)


async def _get_current_version(db: aiosqlite.Connection) -> int:
    """Get current schema version, returns 0 if table doesn't exist."""
    try:
        async with db.execute("SELECT version FROM schema_version WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except aiosqlite.OperationalError:
        return 0


async def _apply_migrations(
    db: aiosqlite.Connection, from_version: int, to_version: int
) -> None:
    """Apply incremental migrations between versions."""
    if from_version < 2:
        await _migrate_v1_to_v2(db)


async def _migrate_v1_to_v2(db: aiosqlite.Connection) -> None:
    """Add forecast_samples table for per-horizon forecast persistence."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_samples (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_type   TEXT NOT NULL,
            metric          TEXT NOT NULL,
            fetched_at      TEXT NOT NULL,
            horizon_hours   REAL NOT NULL,
            target_time     TEXT NOT NULL,
            predicted_value REAL NOT NULL
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_fcsamples_target "
        "ON forecast_samples(provider_type, metric, target_time)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_fcsamples_fetched "
        "ON forecast_samples(fetched_at)"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fcsamples_dedup "
        "ON forecast_samples(provider_type, metric, fetched_at, horizon_hours)"
    )
    logger.info("Migrated to v2: forecast_samples table created")
