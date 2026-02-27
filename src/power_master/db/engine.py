"""SQLite database engine with WAL mode for concurrent reads."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from power_master.db.migrations import run_migrations

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None

# Tables in dependency order (parents before children) for recovery.
_RECOVERY_TABLES = [
    "schema_version",
    "config_versions",
    "forecast_snapshots",
    "tariff_schedules",
    "billing_cycles",
    "optimisation_plans",
    "plan_slots",
    "inverter_commands",
    "telemetry",
    "accounting_events",
    "scheduled_loads",
    "load_execution_log",
    "system_events",
    "optimisation_cycle_log",
    "historical_data",
    "load_profile_estimates",
    "bom_locations",
    "spike_events",
]


async def _check_integrity(db: aiosqlite.Connection) -> bool:
    """Run PRAGMA integrity_check and return True if the database is healthy."""
    try:
        async with db.execute("PRAGMA integrity_check") as cursor:
            rows = await cursor.fetchall()
        # A healthy DB returns a single row: ("ok",)
        if len(rows) == 1 and str(rows[0][0]).lower() == "ok":
            return True
        problems = [str(r[0]) for r in rows[:10]]
        logger.error("Database integrity check failed: %s", "; ".join(problems))
        return False
    except Exception:
        logger.error("Database integrity check raised an exception", exc_info=True)
        return False


async def _recover_database(corrupt_path: Path, new_path: Path) -> dict[str, int]:
    """Recover readable rows from a corrupt database into a fresh one.

    Creates the new DB with the full schema, then copies each table's
    rows individually — a corrupt page in one table won't block others.

    Returns a dict of {table_name: rows_recovered}.
    """
    recovered: dict[str, int] = {}

    # Create fresh DB with schema
    new_db = await aiosqlite.connect(str(new_path))
    await new_db.execute("PRAGMA journal_mode=WAL")
    await new_db.execute("PRAGMA synchronous=FULL")
    await new_db.execute("PRAGMA foreign_keys=OFF")  # Disable during recovery
    await run_migrations(new_db)

    # Open corrupt DB read-only
    try:
        corrupt_db = await aiosqlite.connect(
            f"file:{corrupt_path}?mode=ro", uri=True,
        )
    except Exception:
        logger.error("Cannot open corrupt database for recovery", exc_info=True)
        await new_db.close()
        return recovered

    for table in _RECOVERY_TABLES:
        try:
            # Get column names from the new (schema-correct) DB
            async with new_db.execute(f"PRAGMA table_info({table})") as cur:
                cols_info = await cur.fetchall()
            if not cols_info:
                continue
            col_names = [c[1] for c in cols_info]
            col_list = ", ".join(col_names)
            placeholders = ", ".join("?" * len(col_names))

            # Read from corrupt DB
            async with corrupt_db.execute(f"SELECT {col_list} FROM {table}") as cur:
                rows = await cur.fetchall()

            if not rows:
                continue

            # Insert into new DB
            await new_db.executemany(
                f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
                rows,
            )
            await new_db.commit()
            recovered[table] = len(rows)
            logger.info("Recovered %d rows from %s", len(rows), table)

        except Exception:
            logger.warning("Could not recover table %s (corrupt pages)", table)

    await corrupt_db.close()
    await new_db.execute("PRAGMA foreign_keys=ON")
    await new_db.close()
    return recovered


async def init_db(db_path: str | Path) -> aiosqlite.Connection:
    """Initialise the database connection with WAL mode and run migrations.

    If the database is corrupted, attempts to recover readable data into
    a fresh database.  The corrupt file is kept as a timestamped backup.
    """
    global _db
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists():
        # Quick integrity check before proceeding
        try:
            test_db = await aiosqlite.connect(str(db_path))
            healthy = await _check_integrity(test_db)
            await test_db.close()
        except Exception:
            healthy = False

        if not healthy:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            backup = db_path.with_suffix(f".corrupt-{stamp}.db")
            logger.warning(
                "Database corruption detected — attempting recovery..."
            )

            # Recover into a temporary new DB
            recovered_path = db_path.with_suffix(".recovered.db")
            recovered = await _recover_database(db_path, recovered_path)

            total_rows = sum(recovered.values())
            if recovered:
                logger.info(
                    "Recovery complete: %d rows across %d tables (%s)",
                    total_rows, len(recovered),
                    ", ".join(f"{t}={n}" for t, n in recovered.items()),
                )
            else:
                logger.warning("No data could be recovered from corrupt database")

            # Move corrupt DB to backup
            for suffix in ("", "-wal", "-shm"):
                src = db_path.parent / (db_path.name + suffix)
                if src.exists():
                    dst = db_path.parent / (backup.name + suffix)
                    shutil.move(str(src), str(dst))

            # Replace with recovered DB
            if recovered_path.exists():
                shutil.move(str(recovered_path), str(db_path))
                # Clean up any recovered WAL/SHM
                for suffix in ("-wal", "-shm"):
                    f = recovered_path.parent / (recovered_path.name + suffix)
                    if f.exists():
                        f.unlink()

    db = await aiosqlite.connect(str(db_path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=FULL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    db.row_factory = aiosqlite.Row

    await run_migrations(db)
    _db = db
    logger.info("Database initialised at %s (WAL mode, synchronous=FULL)", db_path)
    return db


async def get_db() -> aiosqlite.Connection:
    """Get the active database connection."""
    if _db is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _db


async def checkpoint_wal() -> None:
    """Checkpoint the WAL file to keep it from growing unbounded.

    Call this periodically (e.g. every 30 minutes) to reduce corruption
    risk from unclean shutdowns.
    """
    if _db is not None:
        try:
            await _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logger.debug("WAL checkpoint completed")
        except Exception:
            logger.warning("WAL checkpoint failed", exc_info=True)


async def close_db() -> None:
    """Close the database connection."""
    global _db
    if _db is not None:
        try:
            await _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        await _db.close()
        _db = None
        logger.info("Database connection closed")
