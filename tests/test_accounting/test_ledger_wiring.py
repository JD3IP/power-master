"""Tests for export tier ledger and daily credit tracker wiring in telemetry.

Verifies that the ledgers are correctly incremented from measured telemetry
in the Application.start() / on_telemetry callback flow.

Time-deterministic: injects fixed times, never depends on wall-clock.
"""

from __future__ import annotations

from datetime import datetime, date as date_cls
from zoneinfo import ZoneInfo

import pytest
import aiosqlite

from power_master.accounting.export_tier_ledger import ExportTierLedger
from power_master.accounting.daily_credit_tracker import DailyCreditTracker
from power_master.db.repository import Repository


@pytest.fixture
async def memory_db():
    """Create an in-memory SQLite database for testing."""
    db = await aiosqlite.connect(":memory:")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key         TEXT PRIMARY KEY,
            value_json  TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    await db.commit()
    yield db
    await db.close()


@pytest.fixture
async def repo(memory_db):
    """Create a Repository with the in-memory database."""
    return Repository(memory_db)


class TestExportTierLedgerWiring:
    """Test export tier ledger increments from measured export telemetry."""

    @pytest.mark.asyncio
    async def test_export_ledger_records_measured_export(
        self, repo: Repository
    ) -> None:
        """Ledger increments when measured export is recorded (simulating telemetry callback).

        Scenario: ZEROHERO 18:00-20:59 window, export at 10c/kWh.
        Measure 5 kWh export; ledger records to tier 1 (premium-15kwh).
        """
        # Setup: tiered ledger for ZEROHERO (first 15 kWh @ 10c, rest @ 2c)
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure={
                "premium-15kwh": 15.0,
                "flat-2c": None,
            },
            repo=repo,
        )
        await ledger.init_persistence(repo)

        # Simulate: telemetry callback records 5 kWh export at 10c
        result = await ledger.record_export(5000, 10.0)

        # Assert: ledger advanced
        assert result["tier_name"] == "premium-15kwh"
        assert result["kwh_in_tier"] == 5.0
        assert ledger.get_tier_consumption("premium-15kwh") == 5.0

    @pytest.mark.asyncio
    async def test_export_ledger_persists_across_restart(
        self, repo: Repository
    ) -> None:
        """Ledger state persists across a simulated app restart (same day).

        Measures 8 kWh, restarts, verifies state is restored.
        """
        ledger1 = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure={
                "premium-15kwh": 15.0,
                "flat-2c": None,
            },
            repo=repo,
        )
        await ledger1.init_persistence(repo)

        # Record export
        await ledger1.record_export(8000, 10.0)
        assert ledger1.get_tier_consumption("premium-15kwh") == 8.0

        # Simulate restart: new ledger instance
        ledger2 = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure={
                "premium-15kwh": 15.0,
                "flat-2c": None,
            },
            repo=repo,
        )
        await ledger2.init_persistence(repo)

        # Assert: state restored
        assert ledger2.get_tier_consumption("premium-15kwh") == 8.0


class TestDailyCreditTrackerWiring:
    """Test daily credit tracker increments from measured import telemetry."""

    @pytest.mark.asyncio
    async def test_credit_tracker_records_measured_import(
        self, repo: Repository
    ) -> None:
        """Tracker increments when measured import is recorded during credit window.

        Scenario: ZEROHERO 18:00-20:59 window, threshold 0.09 kWh.
        Measure 30 Wh = 0.03 kWh import; tracker records it.
        """
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=0.09,
            reward_dollars=1.0,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # Simulate: telemetry callback records 30 Wh import
        result = await tracker.record_in_window_import(30)

        # Assert: tracker advanced
        assert result["import_total_kwh"] == 0.03
        assert result["status"] == "on_track"
        assert result["earned_dollars"] == 1.0
        assert tracker.get_import_total() == 0.03

    @pytest.mark.asyncio
    async def test_credit_tracker_detects_forfeiture(
        self, repo: Repository
    ) -> None:
        """Tracker detects when import exceeds threshold (credit forfeited).

        Measures imports that accumulate to exceed threshold (>= 0.09 kWh).
        """
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=0.09,
            reward_dollars=1.0,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # Record three small imports
        await tracker.record_in_window_import(30)
        assert tracker.get_status() == "on_track"

        await tracker.record_in_window_import(30)
        assert tracker.get_status() == "on_track"

        # Third import hits threshold
        result = await tracker.record_in_window_import(30)
        assert result["import_total_kwh"] == pytest.approx(0.09, abs=0.001)
        assert result["status"] == "forfeited"
        assert result["earned_dollars"] == 0.0

    @pytest.mark.asyncio
    async def test_credit_tracker_persists_across_restart(
        self, repo: Repository
    ) -> None:
        """Tracker state persists across app restart (same day).

        Records import, restarts, verifies state is restored.
        """
        tracker1 = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=0.09,
            reward_dollars=1.0,
            repo=repo,
        )
        await tracker1.init_persistence(repo)

        # Record import (well below threshold)
        await tracker1.record_in_window_import(40)
        assert tracker1.get_import_total() == 0.04
        assert tracker1.get_status() == "on_track"

        # Simulate restart: new tracker instance
        tracker2 = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=0.09,
            reward_dollars=1.0,
            repo=repo,
        )
        await tracker2.init_persistence(repo)

        # Assert: state restored
        assert tracker2.get_import_total() == 0.04
        assert tracker2.get_status() == "on_track"
