"""Tests for free-window daily cap tracker with persistence.

Covers:
- Cap increment and remaining cap calculation
- Daily reset at local midnight
- Persistence to kv_store and restoration on restart
- Approaching and exhausted state detection
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import aiosqlite

from power_master.accounting.free_window_cap import (
    FreeWindowCapTracker,
    FreeWindowCapState,
    KV_FREE_WINDOW_CAP_KEY,
)
from power_master.db.repository import Repository


@pytest.fixture
async def memory_db():
    """Create an in-memory SQLite database for testing."""
    db = await aiosqlite.connect(":memory:")
    # Create kv_store table
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


class TestFreeWindowCapTracker:
    """Test free-window cap tracker basics."""

    @pytest.mark.asyncio
    async def test_init_with_no_repo(self) -> None:
        """Tracker can be initialized without a repo."""
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=None,
        )
        assert tracker.get_remaining_cap() == 50.0
        assert tracker.get_consumed_today() == 0.0

    @pytest.mark.asyncio
    async def test_init_with_repo(self, repo: Repository) -> None:
        """Tracker initializes with repo and loads persisted state."""
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker.init_persistence(repo)
        # No prior state; should start fresh
        assert tracker.get_remaining_cap() == 50.0

    @pytest.mark.asyncio
    async def test_increment_updates_consumption(self, repo: Repository) -> None:
        """Increment adds to consumed total and persists."""
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        await tracker.increment(10.0)
        assert tracker.get_consumed_today() == 10.0
        assert tracker.get_remaining_cap() == 40.0

        await tracker.increment(15.0)
        assert tracker.get_consumed_today() == 25.0
        assert tracker.get_remaining_cap() == 25.0

    @pytest.mark.asyncio
    async def test_increment_clamps_to_cap(self, repo: Repository) -> None:
        """Increment doesn't exceed cap."""
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        await tracker.increment(40.0)
        await tracker.increment(20.0)  # Would exceed cap
        assert tracker.get_consumed_today() == 50.0  # Clamped to cap
        assert tracker.get_remaining_cap() == 0.0

    @pytest.mark.asyncio
    async def test_is_cap_approaching(self, repo: Repository) -> None:
        """Detects approaching cap state (80%)."""
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # Below threshold
        await tracker.increment(30.0)
        assert not tracker.is_cap_approaching()

        # At threshold (50 * 0.80 = 40)
        await tracker.increment(10.0)
        assert tracker.is_cap_approaching()

    @pytest.mark.asyncio
    async def test_is_cap_exhausted(self, repo: Repository) -> None:
        """Detects fully exhausted cap."""
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        await tracker.increment(40.0)
        assert not tracker.is_cap_exhausted()

        await tracker.increment(10.0)
        assert tracker.is_cap_exhausted()

    @pytest.mark.asyncio
    async def test_persistence_restore_same_day(self, repo: Repository) -> None:
        """Tracker restores state when restarted same day."""
        tracker1 = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker1.init_persistence(repo)
        await tracker1.increment(25.0)

        # Create new tracker and restore
        tracker2 = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker2.init_persistence(repo)

        # Should have loaded the persisted state
        assert tracker2.get_consumed_today() == 25.0
        assert tracker2.get_remaining_cap() == 25.0

    @pytest.mark.asyncio
    async def test_persistence_resets_on_day_boundary(self, repo: Repository) -> None:
        """Tracker resets state when a new local day begins.

        This test uses mocking/monkeypatch to simulate day boundary.
        """
        # Save initial state for 2026-06-15
        tracker1 = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker1.init_persistence(repo)
        await tracker1.increment(25.0)

        # Manually set the stored date to yesterday to simulate day boundary
        tz = ZoneInfo("Australia/Brisbane")
        today = datetime.now(tz).date().isoformat()
        yesterday = (datetime.now(tz).date() - timedelta(days=1)).isoformat()

        # Fetch the stored state and backdate it
        stored = await repo.kv_get(KV_FREE_WINDOW_CAP_KEY)
        assert stored is not None
        stored["local_date_str"] = yesterday
        await repo.kv_set(KV_FREE_WINDOW_CAP_KEY, stored)

        # Create new tracker; should detect day boundary and reset
        tracker2 = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker2.init_persistence(repo)

        # State should be reset
        assert tracker2.get_consumed_today() == 0.0
        assert tracker2.get_remaining_cap() == 50.0

    @pytest.mark.asyncio
    async def test_negative_increment_ignored(self, repo: Repository) -> None:
        """Negative increments are ignored."""
        tracker = FreeWindowCapTracker(
            timezone_name="Australia/Brisbane",
            cap_kwh_per_day=50.0,
            repo=repo,
        )
        await tracker.init_persistence(repo)
        await tracker.increment(10.0)

        # Try negative increment
        await tracker.increment(-5.0)
        assert tracker.get_consumed_today() == 10.0  # Unchanged


class TestFreeWindowCapState:
    """Test FreeWindowCapState serialization."""

    def test_to_dict_serialization(self) -> None:
        """State can be serialized to dict."""
        state = FreeWindowCapState(
            local_date_str="2026-06-15",
            kwh_consumed=25.5,
            timestamp="2026-06-15T10:30:00+10:00",
        )
        d = state.to_dict()
        assert d["local_date_str"] == "2026-06-15"
        assert d["kwh_consumed"] == 25.5
        assert d["timestamp"] == "2026-06-15T10:30:00+10:00"

    def test_from_dict_deserialization(self) -> None:
        """State can be deserialized from dict."""
        d = {
            "local_date_str": "2026-06-15",
            "kwh_consumed": 25.5,
            "timestamp": "2026-06-15T10:30:00+10:00",
        }
        state = FreeWindowCapState.from_dict(d)
        assert state.local_date_str == "2026-06-15"
        assert state.kwh_consumed == 25.5
        assert state.timestamp == "2026-06-15T10:30:00+10:00"

    def test_from_dict_handles_missing_fields(self) -> None:
        """from_dict gracefully handles missing fields."""
        d = {"local_date_str": "2026-06-15"}  # Missing kwh_consumed, timestamp
        state = FreeWindowCapState.from_dict(d)
        assert state.local_date_str == "2026-06-15"
        assert state.kwh_consumed == 0.0
        assert state.timestamp == ""
