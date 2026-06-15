"""Tests for provider/era segmentation during tariff cutover (e.g., Amber → TOU).

Unit 7 Phase 1: Verify accounting continuity when switching providers.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from power_master.accounting.events import (
    create_export_event,
    create_import_event,
    create_self_consumption_event,
)
from power_master.accounting.engine import AccountingEngine
from power_master.config.schema import AppConfig


class TestEventProviderTagging:
    """Events are tagged with provider type at creation."""

    def test_import_event_includes_provider_type(self) -> None:
        event = create_import_event(energy_wh=2000, rate_cents=20.0, provider_type="amber")
        assert event.provider_type == "amber"
        assert event.event_type == "grid_import"

    def test_export_event_includes_provider_type(self) -> None:
        event = create_export_event(
            energy_wh=1000, rate_cents=8.0, cost_basis_cents=5, provider_type="tou",
        )
        assert event.provider_type == "tou"
        assert event.event_type == "grid_export"

    def test_self_consumption_event_includes_provider_type(self) -> None:
        event = create_self_consumption_event(
            energy_wh=3000, avoided_rate_cents=25.0, provider_type="tou",
        )
        assert event.provider_type == "tou"
        assert event.event_type == "self_consumption"

    def test_events_default_provider_type_is_amber(self) -> None:
        """For backward compatibility, default provider is 'amber'."""
        import_ev = create_import_event(2000, 20.0)
        export_ev = create_export_event(1000, 8.0)
        self_ev = create_self_consumption_event(3000, 25.0)

        assert import_ev.provider_type == "amber"
        assert export_ev.provider_type == "amber"
        assert self_ev.provider_type == "amber"


class TestAccountingEngineProviderTracking:
    """AccountingEngine tracks and applies the active provider type."""

    def test_engine_initializes_provider_from_config(self) -> None:
        config = AppConfig()
        # Default config has tariff.type = "amber"
        engine = AccountingEngine(config)
        assert engine.provider_type == "amber"

    def test_set_provider_type_changes_active_provider(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config)
        assert engine.provider_type == "amber"

        engine.set_provider_type("tou")
        assert engine.provider_type == "tou"

    @pytest.mark.asyncio
    async def test_recorded_events_use_current_provider_type(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config)

        # Record with Amber (default)
        event1 = await engine.record_grid_import(2000, 20.0)
        assert event1.provider_type == "amber"

        # Switch to TOU
        engine.set_provider_type("tou")

        # Record with TOU
        event2 = await engine.record_grid_import(2000, 15.0)
        assert event2.provider_type == "tou"

        # Both events exist in the engine's event list
        assert len(engine._events) == 2
        assert engine._events[0].provider_type == "amber"
        assert engine._events[1].provider_type == "tou"

    @pytest.mark.asyncio
    async def test_export_event_tagged_with_current_provider(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        # Record with Amber
        event1 = await engine.record_grid_export(1000, 8.0)
        assert event1.provider_type == "amber"

        # Switch to TOU
        engine.set_provider_type("tou")

        # Record with TOU
        event2 = await engine.record_grid_export(1000, 10.0)
        assert event2.provider_type == "tou"

        # Both events recorded
        assert len(engine._events) == 2

    @pytest.mark.asyncio
    async def test_self_consumption_event_tagged_with_current_provider(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config)

        # Record with Amber
        event1 = await engine.record_self_consumption(3000, 25.0)
        assert event1.provider_type == "amber"

        # Switch to TOU
        engine.set_provider_type("tou")

        # Record with TOU
        event2 = await engine.record_self_consumption(3000, 28.0)
        assert event2.provider_type == "tou"

        # Both events recorded
        assert len(engine._events) == 2


class TestCutoverContinuityWithWACB:
    """WACB is a continuous physical quantity and carries across provider switches.

    The provider change does NOT reset WACB — it only changes the cost basis
    going forward. All energy currently stored has one WACB; it doesn't retroactively
    change when rates change.
    """

    @pytest.mark.asyncio
    async def test_wacb_unchanged_by_provider_switch(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        # Charge under Amber at 5c
        engine.record_grid_charge(2000, 5.0)
        wacb_after_amber = engine.wacb_cents

        # Switch to TOU
        engine.set_provider_type("tou")

        # WACB is unchanged by the switch itself
        assert engine.wacb_cents == wacb_after_amber

        # Charge under TOU at 0c (free window)
        engine.record_grid_charge(3000, 0.0)

        # WACB decreases (new charge at cheaper rate)
        assert engine.wacb_cents < wacb_after_amber

    @pytest.mark.asyncio
    async def test_discharge_cost_basis_not_affected_by_provider_change(self) -> None:
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=10.0)

        # Record grid charge under Amber
        engine.record_grid_charge(2000, 5.0)
        wacb_after_charge = engine.wacb_cents

        # Switch to TOU
        engine.set_provider_type("tou")

        # Discharge: cost basis calculated at the time of discharge, using WACB at that moment
        event = await engine.record_grid_export(1000, 10.0)

        # Cost basis is WACB * kWh, not affected by provider change
        # The cost_basis is rounded during record_discharge, so allow for small rounding variation
        expected_cost_basis = round(1.0 * wacb_after_charge)
        assert event.cost_basis_cents == expected_cost_basis or event.cost_basis_cents == expected_cost_basis + 1


class TestCutoverScenario:
    """Simulate a realistic Amber → TOU cutover (Site A scenario)."""

    @pytest.mark.asyncio
    async def test_amber_to_tou_cutover(self) -> None:
        """Scenario: Site A switches from Amber to FOUR4FREE TOU on a specific date.

        - Phase 1 (Amber era): record some events at Amber rates
        - Cutover: switch provider type
        - Phase 2 (TOU era): record events at TOU rates
        - Verify: each era's cost uses correct rates, no double-billing, no gaps
        """
        config = AppConfig()
        engine = AccountingEngine(config, initial_soc=0.5, initial_wacb=20.0)

        # ── Phase 1: Amber era (before cutover) ──
        # Import 5 kWh at 30c (shoulder rate)
        amber_import_1 = await engine.record_grid_import(5000, 30.0)
        assert amber_import_1.cost_cents == 150  # 5 * 30
        assert amber_import_1.provider_type == "amber"

        # Charge battery from Amber import
        engine.record_grid_charge(3000, 30.0)

        # Solar export during midday (0c on both Amber and TOU)
        amber_export_1 = await engine.record_grid_export(2000, 0.0)
        assert amber_export_1.provider_type == "amber"

        # ── Cutover: switch from Amber to TOU ──
        engine.set_provider_type("tou")
        cutover_time = datetime.now(timezone.utc)

        # ── Phase 2: TOU era (after cutover) ──
        # Grid import during free window at 0c (FOUR4FREE)
        tou_import_1 = await engine.record_grid_import(10000, 0.0)
        assert tou_import_1.cost_cents == 0  # 10 * 0
        assert tou_import_1.provider_type == "tou"

        # Charge from free import
        engine.record_grid_charge(10000, 0.0)

        # Export during evening peak (8c on FOUR4FREE FiT)
        tou_export_1 = await engine.record_grid_export(3000, 8.0)
        assert tou_export_1.cost_cents == -24  # Revenue (3 * 8)
        assert tou_export_1.provider_type == "tou"

        # ── Verification ──
        # Both eras represented
        # Note: record_grid_charge() does NOT create events; only import/export/self_consumption do
        all_events = engine._events
        assert len(all_events) == 4  # 2 Amber events (import, export) + 2 TOU events (import, export)
        amber_events = engine.get_events_by_provider("amber")
        tou_events = engine.get_events_by_provider("tou")
        assert len(amber_events) == 2
        assert len(tou_events) == 2

        # Amber costs are correct
        amber_cost = sum(e.cost_cents for e in amber_events)
        # Import 5kWh @ 30c = 150c; export 2kWh @ 0c = 0 (revenue); total = 150
        assert amber_cost == 150

        # TOU costs are correct
        tou_cost = sum(e.cost_cents for e in tou_events)
        # Import 10kWh @ 0c = 0; export 3kWh @ 8c = -24 (revenue); total = -24
        assert tou_cost == -24

        # No double-billing at boundary: billing cycle totals
        cycle = engine.billing.current
        assert cycle is not None
        # Import cost: 150 (Amber) + 0 (TOU) = 150
        assert cycle.total_import_cost_cents == 150
        # Export revenue: 0 (Amber, 0c) + 24 (TOU, 8c) = 24
        assert cycle.total_export_revenue_cents == 24

    @pytest.mark.asyncio
    async def test_amber_history_stays_queryable(self) -> None:
        """Amber history must remain queryable after cutover for before/after comparison."""
        config = AppConfig()
        engine = AccountingEngine(config)

        # Record several Amber events
        for i in range(3):
            await engine.record_grid_import(1000, 20.0)

        # Switch to TOU
        engine.set_provider_type("tou")

        # Record TOU events
        for i in range(2):
            await engine.record_grid_import(1000, 0.0)

        # Query Amber events
        amber_events = engine.get_events_by_provider("amber")
        assert len(amber_events) == 3
        assert all(e.provider_type == "amber" for e in amber_events)
        assert all(e.cost_cents == 20 for e in amber_events)

        # Query TOU events
        tou_events = engine.get_events_by_provider("tou")
        assert len(tou_events) == 2
        assert all(e.provider_type == "tou" for e in tou_events)
        assert all(e.cost_cents == 0 for e in tou_events)

    @pytest.mark.asyncio
    async def test_no_gap_or_overlap_at_cutover_boundary(self) -> None:
        """Verify no double-counting or gaps at the exact cutover instant.

        When switching from Amber to TOU, the boundary is instantaneous:
        - Last Amber event: fully recorded under Amber
        - First TOU event: fully recorded under TOU
        - No slot or energy counted twice or missing
        """
        config = AppConfig()
        engine = AccountingEngine(config)

        # Record a final Amber import
        amber_event = await engine.record_grid_import(5000, 30.0)

        # Immediately switch
        engine.set_provider_type("tou")

        # Record a TOU import immediately after
        tou_event = await engine.record_grid_import(5000, 0.0)

        # Verify costs don't overlap or gap
        total_energy = amber_event.energy_wh + tou_event.energy_wh
        assert total_energy == 10000

        # Verify no cross-contamination: each event uses its provider's rates
        assert amber_event.cost_cents == 150  # Amber rate applied to Amber event
        assert tou_event.cost_cents == 0  # TOU rate applied to TOU event
        assert amber_event.provider_type == "amber"
        assert tou_event.provider_type == "tou"

        # Both events are in the ledger
        assert len(engine._events) == 2

    @pytest.mark.asyncio
    async def test_billing_cycle_not_reset_at_cutover(self) -> None:
        """Billing cycle continues across provider change — no reset."""
        config = AppConfig()
        engine = AccountingEngine(config)

        # Record under Amber
        await engine.record_grid_import(5000, 30.0)
        cycle_before = engine.billing.current
        assert cycle_before is not None
        cost_before = cycle_before.total_import_cost_cents

        # Switch provider (should NOT reset billing cycle)
        engine.set_provider_type("tou")

        # Record under TOU
        await engine.record_grid_import(5000, 0.0)
        cycle_after = engine.billing.current

        # Same cycle object
        assert cycle_after is cycle_before
        # Costs accumulated, not reset
        assert cycle_after.total_import_cost_cents == cost_before + 0  # Amber cost + TOU cost
