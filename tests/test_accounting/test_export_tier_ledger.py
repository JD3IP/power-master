"""Tests for export tier ledger with persistence.

Covers:
- Per-tier export accumulation
- Tier cap enforcement and overflow to next tier
- Daily reset at local midnight
- Persistence to kv_store and restoration on restart
- Revenue attribution per tier
- Time-deterministic state (no wall-clock flakiness)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import aiosqlite

from power_master.accounting.export_tier_ledger import (
    ExportTierLedger,
    ExportTierState,
    KV_EXPORT_TIER_LEDGER_KEY,
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


# ZEROHERO Super Export tier structure (Phase 2 scenario):
# Tier 1: first 15 kWh/day @ 10c
# Tier 2: remainder @ 2c (unlimited)
ZEROHERO_TIERS = {
    "premium-15kwh": 15.0,  # First 15 kWh
    "flat-2c": None,  # Remainder (unlimited)
}


class TestExportTierLedger:
    """Test export tier ledger basics and accounting."""

    @pytest.mark.asyncio
    async def test_init_with_no_repo(self) -> None:
        """Ledger can be initialized without a repo."""
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=None,
        )
        assert ledger.get_tier_consumption("premium-15kwh") == 0.0
        assert ledger.get_tier_consumption("flat-2c") == 0.0

    @pytest.mark.asyncio
    async def test_init_with_repo(self, repo: Repository) -> None:
        """Ledger initializes with repo and loads persisted state."""
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger.init_persistence(repo)
        # No prior state; should start fresh
        assert ledger.get_tier_consumption("premium-15kwh") == 0.0

    @pytest.mark.asyncio
    async def test_single_tier_1_export(self, repo: Repository) -> None:
        """Export into tier 1 (capped tier) accumulates correctly."""
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger.init_persistence(repo)

        # Export 5 kWh @ 10c
        result = await ledger.record_export(5000, 10.0)

        assert result["tier_name"] == "premium-15kwh"
        assert result["kwh_in_tier"] == 5.0
        assert result["revenue_cents"] == 50  # 5 kWh * 10c

        assert ledger.get_tier_consumption("premium-15kwh") == 5.0
        assert ledger.get_tier_consumption("flat-2c") == 0.0

    @pytest.mark.asyncio
    async def test_fill_tier_1_then_overflow(self, repo: Repository) -> None:
        """Exports fill tier 1 to cap, then overflow to tier 2."""
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger.init_persistence(repo)

        # Export 10 kWh @ 10c (still in tier 1)
        result1 = await ledger.record_export(10000, 10.0)
        assert result1["tier_name"] == "premium-15kwh"
        assert result1["kwh_in_tier"] == 10.0

        # Export 8 kWh @ 10c (fills tier 1 to 18, but cap is 15)
        # So 5 kWh goes to tier 1, 3 kWh overflows to tier 2
        result2 = await ledger.record_export(8000, 10.0)
        assert result2["tier_name"] == "flat-2c"  # Last kWh went to tier 2
        assert ledger.get_tier_consumption("premium-15kwh") == 15.0
        assert ledger.get_tier_consumption("flat-2c") == 3.0

        # Verify revenue: 15 kWh @ 10c + 3 kWh @ 10c (still exported @ 10c)
        assert result2["revenue_cents"] == 80  # 8 kWh * 10c

    @pytest.mark.asyncio
    async def test_tier_2_unlimited_accumulates(self, repo: Repository) -> None:
        """Tier 2 (unlimited) accepts any amount."""
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger.init_persistence(repo)

        # Fill tier 1
        await ledger.record_export(15000, 10.0)
        assert ledger.get_tier_consumption("premium-15kwh") == 15.0

        # Add 100 kWh to tier 2
        await ledger.record_export(100000, 2.0)
        assert ledger.get_tier_consumption("flat-2c") == 100.0

    @pytest.mark.asyncio
    async def test_zero_export_ignored(self, repo: Repository) -> None:
        """Zero or negative exports are ignored."""
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger.init_persistence(repo)

        result = await ledger.record_export(0, 10.0)
        assert result["tier_name"] is None
        assert result["revenue_cents"] == 0

        result = await ledger.record_export(-5000, 10.0)
        assert result["tier_name"] is None

    @pytest.mark.asyncio
    async def test_persistence_restore_same_day(self, repo: Repository) -> None:
        """Ledger restores state when restarted same day."""
        ledger1 = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger1.init_persistence(repo)

        # Export some energy
        await ledger1.record_export(12000, 10.0)
        assert ledger1.get_tier_consumption("premium-15kwh") == 12.0

        # Create new ledger and restore
        ledger2 = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger2.init_persistence(repo)

        # Should have loaded the persisted state
        assert ledger2.get_tier_consumption("premium-15kwh") == 12.0

    @pytest.mark.asyncio
    async def test_persistence_resets_on_day_boundary(self, repo: Repository) -> None:
        """Ledger resets state when a new local day begins."""
        ledger1 = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger1.init_persistence(repo)

        # Export some energy
        await ledger1.record_export(12000, 10.0)
        assert ledger1.get_tier_consumption("premium-15kwh") == 12.0

        # Manually set the stored date to yesterday to simulate day boundary
        tz = ZoneInfo("Australia/Brisbane")
        today = datetime.now(tz).date().isoformat()
        yesterday = (datetime.now(tz).date() - timedelta(days=1)).isoformat()

        # Fetch the stored state and backdate it
        stored = await repo.kv_get(KV_EXPORT_TIER_LEDGER_KEY)
        assert stored is not None
        stored["local_date_str"] = yesterday
        await repo.kv_set(KV_EXPORT_TIER_LEDGER_KEY, stored)

        # Create new ledger; should detect day boundary and reset
        ledger2 = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger2.init_persistence(repo)

        # State should be reset
        assert ledger2.get_tier_consumption("premium-15kwh") == 0.0
        assert ledger2.get_tier_consumption("flat-2c") == 0.0

    @pytest.mark.asyncio
    async def test_get_all_tier_consumption(self, repo: Repository) -> None:
        """get_all_tier_consumption returns entire ledger."""
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger.init_persistence(repo)

        await ledger.record_export(10000, 10.0)
        await ledger.record_export(8000, 10.0)

        all_consumption = ledger.get_all_tier_consumption()
        assert all_consumption["premium-15kwh"] == 15.0
        assert all_consumption["flat-2c"] == 3.0

    @pytest.mark.asyncio
    async def test_realistic_daily_scenario(self, repo: Repository) -> None:
        """Realistic ZEROHERO scenario: multiple exports throughout day."""
        ledger = ExportTierLedger(
            timezone_name="Australia/Brisbane",
            tier_structure=ZEROHERO_TIERS,
            repo=repo,
        )
        await ledger.init_persistence(repo)

        # Morning: 2 kWh @ 2c (flat rate, pre-evening window)
        # Since tiers are filled in order and tier 1 is premium-15kwh, this goes to premium
        result1 = await ledger.record_export(2000, 2.0)
        assert result1["tier_name"] == "premium-15kwh"
        assert result1["revenue_cents"] == 4

        # Evening starts: 12 kWh @ 10c (premium tier; fills to 14 total in tier 1)
        result2 = await ledger.record_export(12000, 10.0)
        assert result2["tier_name"] == "premium-15kwh"
        assert result2["revenue_cents"] == 120

        # Evening: another 5 kWh @ 10c
        # This fills premium tier (2 + 12 + 5 = 19 > cap of 15)
        # So 1 kWh @ 10c to premium (to reach cap of 15), 4 kWh @ 10c to flat
        result3 = await ledger.record_export(5000, 10.0)
        assert result3["tier_name"] == "flat-2c"
        assert result3["revenue_cents"] == 50  # All 5 kWh exported @ 10c (caller's rate)

        # Verify final state
        assert ledger.get_tier_consumption("premium-15kwh") == 15.0
        assert ledger.get_tier_consumption("flat-2c") == 4.0  # Only overflow from last export


class TestExportTierState:
    """Test ExportTierState serialization."""

    def test_to_dict_serialization(self) -> None:
        """State can be serialized to dict."""
        state = ExportTierState(
            local_date_str="2026-06-15",
            tier_ledger={"premium-15kwh": 12.5, "flat-2c": 3.0},
            timestamp="2026-06-15T18:30:00+10:00",
        )
        d = state.to_dict()
        assert d["local_date_str"] == "2026-06-15"
        assert d["tier_ledger"]["premium-15kwh"] == 12.5
        assert d["tier_ledger"]["flat-2c"] == 3.0

    def test_from_dict_deserialization(self) -> None:
        """State can be deserialized from dict."""
        d = {
            "local_date_str": "2026-06-15",
            "tier_ledger": {"premium-15kwh": 12.5, "flat-2c": 3.0},
            "timestamp": "2026-06-15T18:30:00+10:00",
        }
        state = ExportTierState.from_dict(d)
        assert state.local_date_str == "2026-06-15"
        assert state.tier_ledger["premium-15kwh"] == 12.5
        assert state.tier_ledger["flat-2c"] == 3.0

    def test_from_dict_handles_missing_fields(self) -> None:
        """from_dict gracefully handles missing fields."""
        d = {"local_date_str": "2026-06-15"}
        state = ExportTierState.from_dict(d)
        assert state.local_date_str == "2026-06-15"
        assert state.tier_ledger == {}
