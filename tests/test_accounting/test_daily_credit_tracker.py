"""Tests for daily credit tracker with persistence.

Covers:
- In-window import accumulation
- Credit status transitions: on_track -> at_risk -> forfeited
- Credit earned/forfeited accounting
- Daily reset at local midnight
- Persistence to kv_store and restoration on restart
- Time-deterministic state (no wall-clock flakiness)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import aiosqlite

from power_master.accounting.daily_credit_tracker import (
    DailyCreditTracker,
    DailyCreditState,
    KV_DAILY_CREDIT_STATE_KEY,
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


# ZEROHERO evening credit scenario (Phase 2):
# Window: 18:00-20:59 (3 hours)
# Threshold: 0.03 kWh/hour * 3 hours = 0.09 kWh
# Reward: $1.00/day
ZEROHERO_THRESHOLD_KWH = 0.09
ZEROHERO_REWARD_DOLLARS = 1.00


class TestDailyCreditTracker:
    """Test daily credit tracker basics and accounting."""

    @pytest.mark.asyncio
    async def test_init_with_no_repo(self) -> None:
        """Tracker can be initialized without a repo."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=None,
        )
        assert tracker.get_import_total() == 0.0
        assert tracker.get_status() is None
        assert tracker.is_credit_earned()

    @pytest.mark.asyncio
    async def test_init_with_repo(self, repo: Repository) -> None:
        """Tracker initializes with repo and loads persisted state."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker.init_persistence(repo)
        assert tracker.get_import_total() == 0.0

    @pytest.mark.asyncio
    async def test_small_import_stays_on_track(self, repo: Repository) -> None:
        """Small import during window stays on-track for credit."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # 30 Wh = 0.03 kWh (well under threshold of 0.09 kWh)
        result = await tracker.record_in_window_import(30)

        assert result["import_total_kwh"] == 0.03
        assert result["status"] == "on_track"
        assert result["earned_dollars"] == 1.0
        assert tracker.is_credit_earned()

    @pytest.mark.asyncio
    async def test_import_reaches_at_risk_threshold(self, repo: Repository) -> None:
        """Import reaching just above 80% of threshold triggers at-risk status."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # 0.074 kWh > 80% of 0.09 threshold (0.072)
        result = await tracker.record_in_window_import(74)

        assert result["import_total_kwh"] == 0.074
        assert result["status"] == "at_risk"
        assert result["earned_dollars"] == 1.0  # Still earning (not exceeded yet)

    @pytest.mark.asyncio
    async def test_import_exceeds_threshold_forfeits_credit(
        self, repo: Repository
    ) -> None:
        """Import exceeding threshold forfeits credit."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # 0.095 kWh > 0.09 threshold
        result = await tracker.record_in_window_import(95)

        assert result["import_total_kwh"] == 0.095
        assert result["status"] == "forfeited"
        assert result["earned_dollars"] == 0.0
        assert not tracker.is_credit_earned()

    @pytest.mark.asyncio
    async def test_multiple_imports_accumulate(self, repo: Repository) -> None:
        """Multiple import events accumulate toward threshold."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # Three imports of 30 Wh each = 0.09 kWh total
        await tracker.record_in_window_import(30)
        assert tracker.get_import_total() == 0.03

        await tracker.record_in_window_import(30)
        assert tracker.get_import_total() == 0.06

        result = await tracker.record_in_window_import(30)
        assert tracker.get_import_total() == 0.09
        assert result["status"] == "forfeited"  # Exactly at threshold = forfeited
        assert result["earned_dollars"] == 0.0

    @pytest.mark.asyncio
    async def test_zero_import_ignored(self, repo: Repository) -> None:
        """Zero or negative imports are ignored."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        result = await tracker.record_in_window_import(0)
        assert result["import_total_kwh"] == 0.0
        assert result["earned_dollars"] == 1.0

        result = await tracker.record_in_window_import(-10)
        assert result["import_total_kwh"] == 0.0

    @pytest.mark.asyncio
    async def test_persistence_restore_same_day(self, repo: Repository) -> None:
        """Tracker restores state when restarted same day."""
        tracker1 = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker1.init_persistence(repo)

        # Record some import
        await tracker1.record_in_window_import(50)
        assert tracker1.get_import_total() == 0.05
        assert tracker1.get_status() == "on_track"

        # Create new tracker and restore
        tracker2 = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker2.init_persistence(repo)

        # Should have loaded the persisted state
        assert tracker2.get_import_total() == 0.05
        assert tracker2.get_status() == "on_track"

    @pytest.mark.asyncio
    async def test_persistence_resets_on_day_boundary(self, repo: Repository) -> None:
        """Tracker resets state when a new local day begins."""
        tracker1 = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker1.init_persistence(repo)

        # Record some import
        await tracker1.record_in_window_import(50)
        assert tracker1.get_import_total() == 0.05

        # Manually set the stored date to yesterday to simulate day boundary
        tz = ZoneInfo("Australia/Brisbane")
        today = datetime.now(tz).date().isoformat()
        yesterday = (datetime.now(tz).date() - timedelta(days=1)).isoformat()

        # Fetch the stored state and backdate it
        stored = await repo.kv_get(KV_DAILY_CREDIT_STATE_KEY)
        assert stored is not None
        stored["local_date_str"] = yesterday
        await repo.kv_set(KV_DAILY_CREDIT_STATE_KEY, stored)

        # Create new tracker; should detect day boundary and reset
        tracker2 = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker2.init_persistence(repo)

        # State should be reset
        assert tracker2.get_import_total() == 0.0
        assert tracker2.get_status() is None

    @pytest.mark.asyncio
    async def test_status_transition_sequence(self, repo: Repository) -> None:
        """Status transitions correctly: on_track -> at_risk -> forfeited."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # Start: on_track
        result1 = await tracker.record_in_window_import(30)
        assert result1["status"] == "on_track"

        # Approach threshold: at_risk (> 80% of 0.09 = 0.072)
        result2 = await tracker.record_in_window_import(35)  # Total 0.065 kWh; still on_track
        assert result2["status"] == "on_track"

        # Hit at_risk: more than 80%
        result2b = await tracker.record_in_window_import(10)  # Total 0.075 kWh; now at_risk
        assert result2b["status"] == "at_risk"

        # Exceed threshold: forfeited
        result3 = await tracker.record_in_window_import(20)  # Total 0.095 kWh
        assert result3["status"] == "forfeited"

    @pytest.mark.asyncio
    async def test_realistic_daily_scenario(self, repo: Repository) -> None:
        """Realistic ZEROHERO scenario: imports during evening window."""
        tracker = DailyCreditTracker(
            timezone_name="Australia/Brisbane",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            threshold_kwh=ZEROHERO_THRESHOLD_KWH,
            reward_dollars=ZEROHERO_REWARD_DOLLARS,
            repo=repo,
        )
        await tracker.init_persistence(repo)

        # 18:00 - some load from grid
        result1 = await tracker.record_in_window_import(20)
        assert result1["status"] == "on_track"
        assert result1["earned_dollars"] == 1.0

        # 18:30 - more load
        result2 = await tracker.record_in_window_import(25)
        assert result2["status"] == "on_track"
        assert result2["earned_dollars"] == 1.0

        # 19:00 - battery running low, spike of grid import
        result3 = await tracker.record_in_window_import(50)
        total = 0.02 + 0.025 + 0.05  # 0.095 kWh
        assert result3["import_total_kwh"] == pytest.approx(0.095, abs=0.001)
        assert result3["status"] == "forfeited"
        assert result3["earned_dollars"] == 0.0

        # 19:30 - no more imports (but check status via tracker state)
        assert tracker.get_import_total() == pytest.approx(0.095, abs=0.001)
        assert tracker.get_status() == "forfeited"


class TestDailyCreditState:
    """Test DailyCreditState serialization."""

    def test_to_dict_serialization(self) -> None:
        """State can be serialized to dict."""
        state = DailyCreditState(
            local_date_str="2026-06-15",
            credit_name="zerohero-evening",
            window_name="18:00-20:59",
            in_window_import_kwh=0.05,
            threshold_kwh=0.09,
            reward_dollars=1.0,
            status="on_track",
            timestamp="2026-06-15T18:30:00+10:00",
        )
        d = state.to_dict()
        assert d["local_date_str"] == "2026-06-15"
        assert d["credit_name"] == "zerohero-evening"
        assert d["in_window_import_kwh"] == 0.05
        assert d["status"] == "on_track"

    def test_from_dict_deserialization(self) -> None:
        """State can be deserialized from dict."""
        d = {
            "local_date_str": "2026-06-15",
            "credit_name": "zerohero-evening",
            "window_name": "18:00-20:59",
            "in_window_import_kwh": 0.05,
            "threshold_kwh": 0.09,
            "reward_dollars": 1.0,
            "status": "on_track",
            "timestamp": "2026-06-15T18:30:00+10:00",
        }
        state = DailyCreditState.from_dict(d)
        assert state.local_date_str == "2026-06-15"
        assert state.credit_name == "zerohero-evening"
        assert state.in_window_import_kwh == 0.05
        assert state.status == "on_track"

    def test_from_dict_handles_missing_fields(self) -> None:
        """from_dict gracefully handles missing fields."""
        d = {
            "local_date_str": "2026-06-15",
            "credit_name": "zerohero-evening",
            "window_name": "18:00-20:59",
        }
        state = DailyCreditState.from_dict(d)
        assert state.local_date_str == "2026-06-15"
        assert state.in_window_import_kwh == 0.0
        assert state.reward_dollars == 0.0
