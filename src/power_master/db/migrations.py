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
        # Single transaction: if any migration step raises, the version bump
        # is rolled back with the rest so the DB never lands in a partially-
        # migrated state that looks like a completed older version.
        try:
            await db.execute("BEGIN")
            await _apply_migrations(db, current, SCHEMA_VERSION)
            await db.execute(
                "UPDATE schema_version SET version = ? WHERE id = 1",
                (SCHEMA_VERSION,),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
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
    if from_version < 3:
        await _migrate_v2_to_v3(db)
    if from_version < 4:
        await _migrate_v3_to_v4(db)
    if from_version < 5:
        await _migrate_v4_to_v5(db)


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


async def _migrate_v2_to_v3(db: aiosqlite.Connection) -> None:
    """Add notification_log table for persistent notification history."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            emitted_at      TEXT NOT NULL,
            event_name      TEXT NOT NULL,
            severity        TEXT NOT NULL,
            tier            TEXT NOT NULL DEFAULT 'informational',
            title           TEXT NOT NULL,
            message         TEXT NOT NULL,
            action_json     TEXT,
            incident_id     TEXT,
            correlation_id  TEXT,
            channels_sent   TEXT NOT NULL DEFAULT ''
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_notif_log_time ON notification_log(emitted_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_notif_log_incident ON notification_log(incident_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_notif_log_correlation ON notification_log(correlation_id)"
    )
    logger.info("Migrated to v3: notification_log table created")


async def _migrate_v3_to_v4(db: aiosqlite.Connection) -> None:
    """Add command_audit_log table for command execution audit trail."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS command_audit_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            issued_at       TEXT NOT NULL,
            mode            TEXT NOT NULL,
            power_w         INTEGER NOT NULL,
            source          TEXT NOT NULL,
            source_type     TEXT NOT NULL,
            reason          TEXT NOT NULL,
            priority        INTEGER NOT NULL,
            result          TEXT NOT NULL DEFAULT 'pending',
            latency_ms      INTEGER,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_issued ON command_audit_log(issued_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_source_type ON command_audit_log(source_type, issued_at)"
    )
    logger.info("Migrated to v4: command_audit_log table created")


async def _migrate_v4_to_v5(db: aiosqlite.Connection) -> None:
    """Add provider_type column to accounting_events for era segmentation (Amber→TOU cutover)."""
    await db.execute(
        "ALTER TABLE accounting_events ADD COLUMN provider_type TEXT NOT NULL DEFAULT 'amber'"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_provider ON accounting_events(provider_type, started_at)"
    )
    logger.info("Migrated to v5: provider_type column added to accounting_events")
